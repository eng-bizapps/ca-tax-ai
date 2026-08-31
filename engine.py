"""The responder (real architecture, end to end).

For every question:
  1. Gemini maps it to a rule key      (language)
  2. lookup: fine product_rules first, then coarse rules   (deterministic)
  3. compute tax + pgvector citation    (deterministic + retrieval)
  4. Gemini composes the answer         (language, from facts only)
  5. guard: no rule -> "Needs review"   (safety)
"""
import json
import re
from datetime import date

import google.generativeai as genai

import cannabis_local
import config
import db
import fees as fee_layer
import entity_tax
import fiduciary_tax
import income_brackets
import income_credits
import income_eligibility
import income_nonresident
import income_db
import district_rates
import local_rates

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))
model = genai.GenerativeModel(config.GEMINI_MODEL)

BASE_RATE = 0.0725                      # CA statewide base; only this gets localized
SPECIAL_RATES = {0.0225, 0.13}         # partial-exemption / additional-tax rules


def _amount(question: str):
    m = re.search(r"\$?\s*([0-9][0-9,]*\.?[0-9]+)", question)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _amounts(question: str):
    """Returns ALL dollar amounts in the question, in order of appearance,
    as (amount, start_char, end_char) -- unlike _amount() (first only).
    Purely additive: every existing single-amount compute path keeps using
    _amount() unchanged. This exists for the mixed-income-source paths that
    genuinely need to distinguish MULTIPLE figures in one question (e.g.
    "$50,000 in wages and $30,000 in self-employment income")."""
    out = []
    for m in re.finditer(r"\$?\s*([0-9][0-9,]*\.?[0-9]+)", question):
        try:
            out.append((float(m.group(1).replace(",", "")), m.start(), m.end()))
        except ValueError:
            continue
    return out


# --- Date parsing, built for the Underpayment Regular Method (FTB 5805
# Worksheet II) -- the first date-handling code anywhere in this
# codebase; everything else here is dollar-amount/keyword-phrase based.
_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")


def _dates(question: str):
    """Mirrors _amounts()'s (value, start, end) shape for calendar dates
    -- MM/DD/YYYY or MM/DD/YY (2-digit year normalized by +2000).
    Invalid calendar dates (e.g. 2/30) are skipped, not raised."""
    out = []
    for m in _DATE_RE.finditer(question):
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            out.append((date(year, month, day), m.start(), m.end()))
        except ValueError:
            continue
    return out


def _mask_dates(question: str, dates) -> str:
    """Overwrites each matched date's character span with 'X' (same
    length -- preserves every OTHER character's offset, so
    _amount_after_filtered_span's keyword-distance arithmetic keeps
    working unmodified on the masked copy). Necessary because
    _amounts()'s digit-sweeping regex (above) has no word-boundary
    guard: verified live that a literal date like "4/15/2025" produces
    PHANTOM amounts 15.0 and 2025.0 without this -- the same collision
    class found 10+ times this session for form/section numbers, just
    triggered by dates instead."""
    chars = list(question)
    for _, start, end in dates:
        for i in range(start, end):
            chars[i] = "X"
    return "".join(chars)


def _pair_amounts_with_dates(amounts, dates, window: int = 25):
    """Forward-only: pairs each amount with the closest date within
    `window` chars AFTER it -- this feature's required phrasing is
    always '$X on/paid DATE'. Each date claimed by at most one amount,
    removed from the candidate pool by position once claimed. Amounts
    with no date within range are simply dropped from the result (the
    caller checks length to detect this)."""
    remaining = list(dates)
    pairs = []
    for amount, a_start, a_end in amounts:
        best = None
        for d, d_start, d_end in remaining:
            if d_start < a_end:
                continue
            dist = d_start - a_end
            if dist <= window and (best is None or dist < best[1]):
                best = ((d, d_start, d_end), dist)
        if best:
            d, d_start, d_end = best[0]
            pairs.append((amount, d))
            remaining = [(dd, ds, de) for dd, ds, de in remaining if (ds, de) != (d_start, d_end)]
    return pairs


def _amount_near(question: str, keywords, window: int = 60):
    """Returns the dollar amount CLOSEST (by character distance) to the
    nearest occurrence of any of `keywords`, or None. Distance-based
    (finds the NEAREST amount to each keyword hit) rather than a fixed
    per-amount radius -- a fixed radius silently mis-tags questions where
    two dollar amounts sit close together in one sentence (e.g. "$50,000
    in wages and $30,000 in self-employment income": a naive fixed window
    around $50,000 also reaches "self-employment" a few words later,
    wrongly matching the WAGE amount to the SE keyword too). Found via
    direct testing -- the same question reordered gave two different tax
    totals before this fix, which should be impossible since the
    underlying facts didn't change."""
    ql = question.lower()
    amounts = _amounts(question)
    if not amounts:
        return None
    best = None
    for kw in keywords:
        start = 0
        while True:
            idx = ql.find(kw, start)
            if idx == -1:
                break
            for amount, a_start, a_end in amounts:
                dist = abs((a_start + a_end) / 2 - idx)
                if dist <= window and (best is None or dist < best[1]):
                    best = (amount, dist)
            start = idx + len(kw)
    return best[0] if best else None


def _catalog(conn):
    """Combined catalog: fine product rules (preferred) + coarse rules (breadth)."""
    items, seen = [], set()
    for pk, label, cond in conn.execute(
            "SELECT product_key, product_label, condition FROM product_rules").fetchall():
        desc = label + (f" -- {cond}" if cond else "")
        items.append((pk, desc))
        seen.add(pk)
    for cat, summ in conn.execute("SELECT category, summary FROM rules").fetchall():
        if cat not in seen:
            items.append((cat, summ or cat))
            seen.add(cat)
    return items


def _extract_key(question: str, items: list) -> str:
    lines = "\n".join(f"- {key} :: {desc}" for key, desc in items)
    prompt = (
        "Map the question to exactly one key from this catalog, or 'unknown' if none fit. "
        "Prefer the MOST SPECIFIC match.\n"
        f"Catalog:\n{lines}\n- unknown :: none of the above fit\n"
        "Reply with ONLY the key, nothing else.\n"
        f"Question: {question}"
    )
    raw = model.generate_content(prompt).text.strip().lower()
    return re.split(r"[\s`\n]+", raw)[0] if raw else "unknown"


ROUTER = "embed"                 # "embed" (local vector similarity) or "gemini"
EMBED_ROUTER_THRESHOLD = 0.27    # cosine distance beyond which we defer to "unknown".
                                 # RECALIBRATED for gemini-embedding-2 (2026-07-08):
                                 # its distance scale is compressed vs embedding-001
                                 # (was 0.35). Evidence: legit in-scope probe max =
                                 # 0.2423, out-of-scope (income/property tax) min =
                                 # 0.2908 -> 0.27 sits in the gap with margin both ways
INFO_THRESHOLD = 0.27            # informational tier: when NO consumer rule matches,
                                 # a stored administrative rule or doc chunk within
                                 # this distance yields a cited "informational"
                                 # answer (never a taxability verdict) instead of a
                                 # blind "needs review" -- calibrated so truly out-of-
                                 # scope questions (income tax, DMV...) still defer
RERANK_K = 8                     # candidates to pull before lexical rerank
RERANK_MARGIN = 0.06             # only rerank candidates within this dist of the best
BRANCH_MARGIN = 0.05             # opposite-verdict rules within this dist of the best,
                                 # used when the router's top match was CONFIDENT
UNCERTAIN_BRANCH_MARGIN = 0.10   # wider margin used when the router itself had to
                                 # fall back to a heuristic tie-break to pick a winner
                                 # (best_ov <= 1) -- i.e. it wasn't sure, so it should
                                 # disclose a plausible opposite-verdict alternate
                                 # reading rather than silently commit to one guess
MAX_BRANCHES = 3                 # become "it depends" branches (at most this many)
ZERO_OV_MARGIN = 0.08            # wider rerank window when the top match shares NO
                                 # distinctive word with the question (vague attractor)

_STOP = set((
    "is are was the a an of to in on for do does i you it my your this that and or "
    "be as at by with from tax taxable sales use california ca subject charge charges "
    "owe owes pay paid apply applies applicable purchase purchased sale sold sell buy "
    "how much what when who where which not no yes item items property california's"
).split())


def _toks(s: str):
    return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 2 and w not in _STOP}


# Curated disambiguation for known generic-vs-specific keyword collisions the pure
# embedder mis-ranks (a generic taxable rule whose text happens to contain the
# specific sibling's words). Each entry: (list of OR-groups that must ALL be hit,
# excluded terms that must NOT appear, preferred product_key). Term-based and
# general -- not tied to any exact question. Runs before embedding routing.
DISAMBIG = [
    ([{"ice"},
      {"ship", "shipping", "shipped", "pack", "packing", "packed", "transport",
       "transporting", "freight"},
      {"food", "foods", "produce", "fruit", "fruits", "vegetable", "vegetables",
       "perishable", "perishables", "seafood", "fish", "meat", "poultry", "dairy",
       "grocery", "groceries", "edible"}],
     set(), "ice_for_food_product_shipment"),
    ([{"water"},
      {"bottled", "still", "noncarbonated", "spring", "purified", "drinking"}],
     {"carbonated", "sparkling", "effervescent", "soda", "seltzer"},
     "bottled_water_noncarbonated"),
    # paper goods are household supplies, not diapers/printed matter
    ([{"toilet", "tissues", "tissue", "napkins"} , {"paper", "tissues", "tissue", "napkins"}],
     {"diaper", "diapers", "menstrual"},
     "household_cleaning_supplies"),
    # software on physical media is taxable, even though the coarse electronic-
    # delivery rule's text mentions "tangible media" as a contrast and pulls it in
    ([{"software"}, {"cd", "disc", "disk", "dvd", "diskette"}],
     {"download", "downloaded", "downloading", "online", "electronically"},
     "prewritten_software_tangible_media"),
    # casual/private-party sales (garage sale, yard sale, "sold by a private
    # individual") are the occasional-sale exemption, not ordinary retail --
    # overrides the strong lexical pull of items like "furniture" toward the
    # general-merchandise default. EXCLUDES vehicles/vessels/aircraft (Reg
    # 1595(c) -- statutorily carved OUT of the occasional-sale exemption even
    # when bought from a private party) and wholesaler/manufacturer sellers
    # (Reg 1595(d) -- a business already selling for resale doesn't become an
    # occasional seller just because one buyer happens to be a private
    # individual) -- found via probe_gen: this rule was over-firing on those
    # carve-out cases and force-routing them to the wrong exempt answer
    # before embedding routing ever got a chance to run.
    ([{"garage", "yard"}, {"sale", "sales"}], set(), "occasional_sale"),
    ([{"private"}, {"individual", "party", "person", "persons"},
      {"sale", "sales", "sold", "selling"}],
     {"vehicle", "vehicles", "car", "cars", "boat", "boats", "vessel", "vessels",
      "aircraft", "airplane", "mobilehome", "wholesaler", "wholesale", "manufacturer"},
     "occasional_sale"),
    # found via query_log mining of real live traffic (2026-07-28), both
    # already flagged as near-misses earlier this session but never fixed:
    # "tax on $50 of groceries" (dist 0.2809, just past EMBED_ROUTER_THRESHOLD
    # 0.27) lost to combination_package_nonfood, a vague-attractor collision
    # -- the correct exempt rule (food_products_general) was only the SECOND
    # candidate. "tax on $100 of clothing in los angeles" (dist 0.2717) landed
    # just past threshold despite general_tangible_personal_property's own
    # embed text already listing clothing/apparel/shoes -- a phrasing-
    # sensitivity gap (item_sweep's fixed "Is X taxable?" template never
    # exercises this "tax on $N of X in <city>" phrasing real users actually
    # use). Both bypass the threshold entirely via the curated tier rather
    # than risk shifting embed text and exposing other latent collisions
    # elsewhere in the catalog (the documented lesson from the fire-
    # extinguisher/office-desk incident earlier this session).
    ([{"groceries", "grocery"}], set(), "food_products_general"),
    ([{"clothing", "clothes", "apparel"}], set(), "general_tangible_personal_property"),
    # found via adversarial hunt (2026-08-08): "I sold my airplane privately"
    # (singular) already correctly routes to aircraft_retail_sale (taxable)
    # via the embedding router alone, no DISAMBIG needed -- but "I sold my
    # airplanES privately" (plural, no other qualifying word) landed on a
    # completely unrelated rule (export delivery to a US government agency,
    # taxable=FALSE) -- a genuine embedding-space miss, not a term-list gap
    # (the existing private-party DISAMBIG entry never even fires here,
    # since it requires an "individual/party/person" word this phrasing
    # doesn't have). Deliberately narrow to the literal adverb "privately"
    # only (not "private") so this can NEVER overlap with "...in a private
    # sale" phrasing, which already correctly finds the MORE SPECIFIC
    # private_party_vessel_or_aircraft_sale rule unaided -- widening to
    # "private" would have shadowed that better, already-working answer
    # with this more generic one. Excludes rental/lease/charter contexts,
    # a different taxable event entirely.
    ([{"airplane", "airplanes", "aircraft"}, {"privately"}],
     {"rent", "rents", "rented", "renting", "rental",
      "lease", "leases", "leased", "leasing",
      "charter", "charters", "chartered", "chartering"},
     "aircraft_retail_sale"),
]


def _disambiguate(question: str):
    q = set(re.findall(r"[a-z]+", question.lower().replace("-", "")))
    for groups, exclude, key in DISAMBIG:
        if exclude & q:
            continue
        if all(g & q for g in groups):
            return key
    return None


def _verify_disambig_hit(conn, question: str, hit: str) -> str:
    """The two 'occasional sale' DISAMBIG entries (garage/yard sale;
    private-party sale) are broader than any exclude list can fully capture --
    a seller already in business (Reg 1595(c)/(d) carve-outs: vehicles,
    vessels, aircraft, wholesalers, manufacturers) doesn't always use one of
    the excluded words (e.g. "I usually sell my products to stores in bulk"
    names none of them). Rather than keep adding narrower exclude words --
    which risks over-fitting to one exact phrasing instead of a real category
    -- do one embedding check before fully trusting an 'occasional_sale' hit:
    if a DIFFERENT, well-grounded candidate (shares specific/discriminating
    vocabulary with the question -- the same signal _find_branches already
    uses) beats it, prefer that instead. Found via probe_gen: the pure
    embedding router already picks the correct rule once its text is
    enriched -- this DISAMBIG entry was the only thing standing in the way."""
    if hit != "occasional_sale":
        return hit
    qv = _embed(question)
    rows = _route_candidates(conn, qv)
    if not rows or rows[0][0] == "occasional_sale" or float(rows[0][2]) > EMBED_ROUTER_THRESHOLD:
        return hit
    top_key, top_text, _d = rows[0]
    if top_key == CATCHALL_KEY:
        # The catch-all is generic BY DESIGN (see CATCHALL_KEY) -- sharing one
        # word with it (e.g. "furniture") means nothing about whether this is
        # an ordinary retail sale or a private/casual one, which is the fact
        # that actually controls occasional-sale eligibility. Trusting it
        # here would silently reintroduce the exact garage-sale-furniture bug
        # this DISAMBIG entry was built to fix. Only a SPECIFIC alternative
        # rule (a real statutory carve-out, e.g. vehicle/vessel/wholesaler)
        # is allowed to override -- never the generic catch-all.
        return hit
    qt_specific = _specific_toks(conn, _toks(question))
    if qt_specific & _specific_toks(conn, _toks(top_text)):
        return top_key          # a specific, well-grounded alternative exists
    return hit


CATCHALL_KEY = "general_tangible_personal_property"   # deliberately generic --
                                                        # demanding lexical grounding
                                                        # from it is backwards (see below)


def _rerank(question: str, rows):
    """rows = [(key, text, dist), ...] sorted ascending by distance.

    Conservative: trust the nearest embedding match UNLESS it shares at most one
    distinctive word with the question (i.e. it likely matched on a generic
    keyword) AND another candidate within RERANK_MARGIN shares at least two more
    distinctive words. This fixes generic-vs-specific collisions ('ice' vs 'ice
    used to ship food products') without overriding confident, well-matched top
    hits (which is what turns correct routes into wrong ones)."""
    best_key, best_text, best_dist = rows[0][0], rows[0][1], float(rows[0][2])
    qt = _toks(question)
    best_ov = len(qt & _toks(best_text))
    if best_ov > 1:
        return best_key
    if best_ov == 0:
        # The catch-all rule is BY DESIGN the fallback for ordinary goods with no
        # specific rule -- its text can never enumerate every possible product, so
        # zero literal overlap is expected, not a sign it's wrong (unlike a real
        # exemption category like "medicines" vaguely attracting unrelated items,
        # which IS a bug). If it's genuinely the closest embedding match, trust it
        # rather than let a coincidentally-lexically-matching but topically
        # unrelated rule (e.g. a repainting SERVICE rule matching on the word
        # "paint" for a "house paint" purchase question) win instead.
        if best_key == CATCHALL_KEY:
            return best_key
        # The top match shares NO distinctive word with the question -- a vague
        # embedding attractor (e.g. 'toothpaste' -> the generic medicines rule).
        # Prefer any lexically grounded candidate in a wider window.
        near = [(k, t, float(d)) for k, t, d in rows
                if float(d) <= best_dist + ZERO_OV_MARGIN]
        cand = max(near, key=lambda c: (len(qt & _toks(c[1])), -c[2]))
        return cand[0] if len(qt & _toks(cand[1])) >= 1 else best_key
    near = [(k, t, float(d)) for k, t, d in rows if float(d) <= best_dist + RERANK_MARGIN]
    cand = max(near, key=lambda c: (len(qt & _toks(c[1])), -c[2]))
    return cand[0] if len(qt & _toks(cand[1])) >= best_ov + 2 else best_key


def _rerank_v2(conn, question: str, rows):
    """SHADOW MODE ONLY -- computed alongside _rerank (see answer()) and
    logged to query_log.rerank_v2_key for comparison, but never used to
    decide a live answer yet. Identical structure and thresholds to _rerank,
    but overlap counting uses _specific_toks (document-frequency-filtered,
    the same signal _find_branches already relies on for branch relevance)
    instead of raw _toks -- so a word that appears in a large fraction of the
    rule catalog (e.g. 'food' at 13.5%) can't count as meaningful overlap on
    its own, the same class of false-confidence bug DF-weighting already
    fixed for branch noise. Only the QUESTION side is filtered (matching
    _find_branches): filtering just one side of a set intersection is
    sufficient, since the overlap can never include a word absent from the
    filtered side."""
    best_key, best_text, best_dist = rows[0][0], rows[0][1], float(rows[0][2])
    qt_specific = _specific_toks(conn, _toks(question))
    best_ov = len(qt_specific & _toks(best_text))
    if best_ov > 1:
        return best_key
    if best_ov == 0:
        if best_key == CATCHALL_KEY:
            return best_key
        near = [(k, t, float(d)) for k, t, d in rows
                if float(d) <= best_dist + ZERO_OV_MARGIN]
        cand = max(near, key=lambda c: (len(qt_specific & _toks(c[1])), -c[2]))
        return cand[0] if len(qt_specific & _toks(cand[1])) >= 1 else best_key
    near = [(k, t, float(d)) for k, t, d in rows if float(d) <= best_dist + RERANK_MARGIN]
    cand = max(near, key=lambda c: (len(qt_specific & _toks(c[1])), -c[2]))
    return cand[0] if len(qt_specific & _toks(cand[1])) >= best_ov + 2 else best_key


def _route_candidates(conn, qv, k: int = RERANK_K):
    # kind='admin' rows (administrative/program rules) are embedded for the
    # informational tier only -- they must NEVER enter consumer verdict routing
    emb = "[" + ",".join(str(float(x)) for x in qv) + "]"
    return conn.execute(
        "SELECT product_key, text, embedding <=> %s::vector AS dist FROM rule_embeddings "
        "WHERE embedding IS NOT NULL AND kind <> 'admin' "
        "ORDER BY dist LIMIT %s", (emb, k)).fetchall()


TIEBREAK_ENABLED = False         # user decision 2026-07-08: keep the routing/truth
                                 # path fully LLM-free. Contested near-ties are handled
                                 # by BRANCHING (disclose both readings) instead of a
                                 # per-query generation call. _llm_tiebreak is kept as
                                 # a documented experiment; flip this to re-enable.
TIEBREAK_MARGIN = 0.03           # candidates this close to the best are a genuine
                                 # near-tie; when they DISAGREE on the verdict, word
                                 # arithmetic can't resolve them (candy-bar vs vending,
                                 # family-boat 0.0001 apart) -> ask the language model


def _llm_tiebreak(conn, question: str, rows, picked: str) -> str:
    """Contested-route resolver: when candidates within TIEBREAK_MARGIN of the
    best disagree on taxable/exempt, one generation call picks the applicable
    rule (real language understanding instead of token counting). The reply is
    validated against the candidate set; any failure falls back to the heuristic
    pick, so behavior can only change on genuinely contested routes."""
    best = float(rows[0][2])
    cands = [(k, t, float(d)) for k, t, d in rows if float(d) <= best + TIEBREAK_MARGIN]
    if len(cands) < 2:
        return picked
    verdicts = {}
    for k, _t, _d in cands:
        r = _lookup(conn, k)
        if r is not None:
            verdicts[k] = bool(r[1])
    if picked not in verdicts or len(set(verdicts.values())) < 2:
        return picked                       # contenders agree -> nothing at stake
    lines = "\n".join(f"- {k} [{'TAXABLE' if verdicts[k] else 'NOT taxable'}]: {t[:220]}"
                      for k, t, _d in cands if k in verdicts)
    prompt = (
        "Pick which ONE rule applies to the question about California sales tax. "
        "If the question does not mention the special context a rule requires "
        "(e.g. vending machine, broker, dealer, prescription), do not pick that "
        "rule -- prefer the rule matching the ordinary reading of the question. "
        "Reply with ONLY the rule key.\n"
        f"RULES:\n{lines}\nQUESTION: {question}"
    )
    try:
        raw = model.generate_content(prompt).text.strip().lower()
        choice = re.split(r"[\s`\n]+", raw)[0].strip(" .:`-")
        if choice in verdicts:
            return choice
    except Exception:
        pass                                # quota/parse failure -> heuristic pick
    return picked


def _extract_key_embed(conn, question: str, threshold: float = None, qv=None) -> str:
    """Route by nearest rule embedding + lexical rerank + contested tie-break.
    Returns the matched key, or 'unknown' when the nearest rule is beyond the
    threshold (the guard for off-topic / out-of-scope questions)."""
    hit = _disambiguate(question)          # curated generic-vs-specific fixes first
    if hit:
        return _verify_disambig_hit(conn, question, hit)
    threshold = EMBED_ROUTER_THRESHOLD if threshold is None else threshold
    qv = qv if qv is not None else _embed(question)
    rows = _route_candidates(conn, qv)
    if not rows or float(rows[0][2]) > threshold:
        return "unknown"
    key = _rerank(question, rows)
    return _llm_tiebreak(conn, question, rows, key) if TIEBREAK_ENABLED else key


def _lookup(conn, key: str):
    """Fine product rule first, then coarse category rule. Returns
    (key, taxable, rate, citation, summary, reg, source_url, measure_fraction)
    or None. measure_fraction is the portion of the price that is the taxable
    base (1.0 = whole price; e.g. 0.33 = cold food through a vending machine);
    coarse rules have no such measure so default to 1.0."""
    r = conn.execute(
        "SELECT product_key, taxable, rate, citation, summary, reg, source_url, "
        "COALESCE(measure_fraction, 1.0) FROM product_rules WHERE product_key=%s",
        (key,)).fetchone()
    if r:
        return r
    return conn.execute(
        "SELECT category, taxable, rate, citation, summary, NULL::text, source_url, "
        "1.0 FROM rules WHERE category=%s", (key,)).fetchone()


def _branch_info(conn, key: str):
    """Label + condition detail for presenting a rule as a branch."""
    r = conn.execute(
        "SELECT product_label, taxable, rate, citation, condition, summary, "
        "COALESCE(measure_fraction, 1.0) FROM product_rules WHERE product_key=%s",
        (key,)).fetchone()
    if not r:
        r = conn.execute(
            "SELECT category, taxable, rate, citation, summary, summary, 1.0 "
            "FROM rules WHERE category=%s", (key,)).fetchone()
    if not r:
        return None
    label, taxable, rate, citation, cond, summ, mfrac = r
    return {"key": key, "label": label, "taxable": bool(taxable),
            "rate": float(rate), "citation": citation,
            "condition": cond or summ or "", "measure_fraction": float(mfrac)}


def _income_branch_info(conn, key: str):
    """Label + condition detail for presenting an income topic as a branch --
    mirrors _branch_info, minus rate/measure_fraction (income topics aren't
    priced items; they're a taxable/not-taxable determination on an income
    TYPE, computed by _income_compute_answer separately, not here)."""
    r = conn.execute(
        "SELECT topic_label, taxable, citation, condition, summary "
        "FROM income_tax_topics WHERE topic_key=%s ORDER BY tax_year DESC LIMIT 1",
        (key,)).fetchone()
    if not r:
        return None
    label, taxable, citation, cond, summ = r
    if taxable is None:
        return None   # not a taxable/exempt topic -- not comparable as a branch
    return {"key": key, "label": label, "taxable": bool(taxable),
            "citation": citation, "condition": cond or summ or ""}


GENERIC_TOKEN_DF = 0.08          # a word appearing in more than this fraction of
                                 # the rule catalog is too common to count as
                                 # meaningful relevance on its own (e.g. "food"
                                 # sits in 13.5% of rules; "auditory"/"wheelchair"
                                 # sit at <1%) -- computed FROM THE CORPUS, not a
                                 # hand-authored word list, so it stays correct
                                 # as rules are added or reworded
GENERIC_TOKEN_MIN_DOCS = 3       # a word must appear in MORE than this many
                                 # documents before the FRACTION alone can flag
                                 # it generic -- protects small corpora (found by
                                 # collision_audit.py against income's 13-topic
                                 # catalog: 8% of 13 is ~1 document, so almost any
                                 # word shared by 2 related topics -- exactly the
                                 # case this check exists to catch -- got wrongly
                                 # excluded as "too common", silently defeating
                                 # branch disclosure for gambling_winnings vs
                                 # california_lottery_winnings). At sales-tax
                                 # scale (520+ rules) this floor never binds --
                                 # 8% of the corpus is already far above 3.
SMALL_CORPUS_DOCS = 100          # below this many documents, DF-based
                                 # specificity is statistically underpowered --
                                 # with too few samples, an ordinary English
                                 # connective (found live: "including", shared
                                 # by two UNRELATED income topics purely because
                                 # both texts happen to use the word, triggered
                                 # a false "branch" between them) can just as
                                 # easily sit at a low document count as a
                                 # genuine content word. Below this size, layer
                                 # in a small universal-English stopword list as
                                 # a second filter -- checked to be a NO-OP at
                                 # sales-tax scale (789 docs): every word below
                                 # was verified to already sit at or above the
                                 # DF-generic cutoff there, so this never
                                 # changes sales-domain matching, only small
                                 # (currently: income) corpora.
_SMALL_CORPUS_EXTRA_STOP = frozenset((
    "including excluding through these even though another only out all any "
    "since whether however rather than same both their they why here there "
    "can have has were also during via one under because before after every "
    "still despite being please makes make back unlike inside later simply "
    "follows related general matches topic run add issued direct window "
    "notes verify answer case itself"
).split())
_TOKEN_DF_CACHE = {}   # keyed by table name -- see _token_doc_freq


def _token_doc_freq(conn, table: str = "rule_embeddings"):
    """(doc-frequency counter, total rows) over every {table}.text, cached per
    process PER TABLE. Lets _find_branches tell a common word ('food', 'sale')
    from a specific/discriminating one ('auditory', 'ornamental') without any
    manually maintained stopword-style list -- it's derived straight from
    what's actually in the current rule catalog. `table` defaults to the
    sales-tax catalog; a future second domain (e.g. income_rule_embeddings)
    gets its OWN independent word-frequency distribution via the per-table
    cache key, rather than silently reusing sales-tax corpus stats -- a word
    common in tax-topic prose ('income', 'return') could otherwise be wrongly
    treated as discriminating (or vice versa) if the two domains shared one
    cache. `table` is always an internal constant, never user input."""
    if table in _TOKEN_DF_CACHE:
        return _TOKEN_DF_CACHE[table]
    import collections
    rows = conn.execute(f"SELECT text FROM {table}").fetchall()
    df = collections.Counter()
    for (text,) in rows:
        for t in _toks(text):
            df[t] += 1
    _TOKEN_DF_CACHE[table] = (df, len(rows))
    return _TOKEN_DF_CACHE[table]


def _specific_toks(conn, tokens, table: str = "rule_embeddings"):
    """Subset of `tokens` that are NOT too common across the rule catalog to
    carry discriminating signal (see GENERIC_TOKEN_DF / GENERIC_TOKEN_MIN_DOCS /
    SMALL_CORPUS_DOCS)."""
    df, n = _token_doc_freq(conn, table)
    if not n:
        return tokens
    cutoff = max(GENERIC_TOKEN_DF * n, GENERIC_TOKEN_MIN_DOCS)
    specific = {t for t in tokens if df.get(t, 0) <= cutoff}
    if n < SMALL_CORPUS_DOCS:
        specific -= _SMALL_CORPUS_EXTRA_STOP
    return specific


def _route_confidence(question: str, rows) -> bool:
    """True when the router's top match is well-grounded (shares >1 distinctive
    word with the question) -- the same condition _rerank uses to trust the
    nearest embedding outright without a heuristic tie-break. False means the
    primary key was picked by fallback logic, i.e. genuine uncertainty."""
    return len(_toks(question) & _toks(rows[0][1])) > 1


def _find_branches(conn, question, rows, primary_key, primary_taxable, margin=BRANCH_MARGIN,
                    table="rule_embeddings", branch_info=None):
    """Opposite-verdict rules close enough to the best match to be plausible
    alternate readings of the question. rows = routing candidates
    [(key, text, dist), ...]. `margin` widens when the router itself was
    uncertain (see _route_confidence) -- an uncertain pick should disclose
    plausible opposite-verdict alternatives more readily than a confident one.
    `table`/`branch_info` let this be reused for the income domain (its own
    embedding table + its own _income_branch_info lookup) without comparing
    against the sales corpus's word-frequency distribution -- see
    _token_doc_freq's per-table cache.

    A branch must be a genuine alternate reading of the SAME aspect of the
    question the primary rule matched on, not just any coincidental word
    overlap -- two conditions, both derived from the data (no hand-listed
    words or per-item exceptions):
      1. it must share a "specific" (not overly common, see GENERIC_TOKEN_DF)
         word with the question -- e.g. "food" alone shouldn't justify a
         branch, since it appears in 13.5% of all rules and matches almost
         any grocery question trivially.
      2. that shared word must be one of the SAME words that grounded the
         PRIMARY pick's own relevance -- otherwise a candidate can ride along
         on a totally unrelated part of the question (e.g. "children's toys"
         is primarily about "toys"; a diaper-exemption rule matching only on
         the unrelated word "children" is not a real alternate reading of
         the same purchase).
    Returns up to MAX_BRANCHES branch dicts."""
    branch_info = branch_info or _branch_info
    best_dist = float(rows[0][2])
    qt = _toks(question)
    qt_specific = _specific_toks(conn, qt, table=table)
    primary_text = next((t for k, t, d in rows if k == primary_key), "")
    primary_ov = qt_specific & _toks(primary_text)
    branches = []
    for k, text, d in rows:
        if k == primary_key or float(d) > best_dist + margin:
            continue
        cand_ov = qt_specific & _toks(text)
        if not cand_ov or (primary_ov and not (cand_ov & primary_ov)):
            continue
        info = branch_info(conn, k)
        if info and info["taxable"] != primary_taxable:
            branches.append(info)
        if len(branches) >= MAX_BRANCHES:
            break
    return branches


def _embed(text: str):
    return genai.embed_content(
        model=config.EMBED_MODEL, content=text,
        output_dimensionality=config.EMBED_DIM,
    )["embedding"]


def _info_lookup(conn, qv, threshold: float = None):
    """Informational tier: when no consumer rule matched, look for the closest
    piece of STORED official content -- an administrative rule (licensing, fee
    programs, filing mechanics: rule_embeddings kind='admin') or a raw doc chunk
    (annotations, industry guides, full reg text). Returns a dict describing the
    best hit within the threshold, or None (-> genuine needs_review). This tier
    NEVER produces a taxability verdict -- only a cited pointer/summary."""
    threshold = INFO_THRESHOLD if threshold is None else threshold
    emb = "[" + ",".join(str(float(x)) for x in qv) + "]"
    best = None
    r = conn.execute(
        "SELECT e.product_key, e.embedding <=> %s::vector AS dist, "
        "p.product_label, p.summary, p.citation, p.source_url "
        "FROM rule_embeddings e JOIN product_rules p ON p.product_key = e.product_key "
        "WHERE e.kind='admin' AND e.embedding IS NOT NULL "
        "ORDER BY dist LIMIT 1", (emb,)).fetchone()
    if r and float(r[1]) <= threshold:
        best = {"source": "admin_rule", "key": r[0], "dist": float(r[1]),
                "title": r[2], "text": r[3], "citation": r[4], "source_url": r[5]}
    # agency='cdtfa' -- doc_chunks.reg is NOT itself agency-qualified, so this
    # filter is required (not cosmetic) once a second agency's content ever
    # gets stored here, or a sales-tax question could surface a citation from
    # a completely different agency's page through this fallback.
    d = conn.execute(
        "SELECT reg, embedding <=> %s::vector AS dist, text, source_url "
        "FROM doc_chunks WHERE embedding IS NOT NULL AND agency='cdtfa' "
        "ORDER BY dist LIMIT 1", (emb,)).fetchone()
    if d and float(d[1]) <= threshold and (best is None or float(d[1]) < best["dist"]):
        best = {"source": "doc", "key": d[0], "dist": float(d[1]),
                "title": d[0], "text": d[2], "citation": None, "source_url": d[3]}
    return best


# ---------------------------------------------------------------------------
# Ring 2 -- FTB income-tax domain (Phase 2: plumbing; Phase 3 adds real content)
#
# Deliberately NOT a union-distance race against the sales-tax router (see the
# Phase 2 plan note: the proven precedent for a second concern layered onto
# answering is fees.py/cannabis_local.py -- a cheap, independently-evaluated
# side channel, not merged into one nearest-neighbor search). Instead
# _answer_income() is tried as a SEQUENTIAL FALLBACK, only after the sales-tax
# path (rule -> fee -> CDTFA informational tier) has already failed to answer
# -- see the guard block in _answer(). This guarantees zero regression risk to
# any currently-passing sales-tax case: they all resolve via `rule` before the
# guard block (and therefore income) is ever reached.
# ---------------------------------------------------------------------------
INCOME_EMBED_ROUTER_THRESHOLD = 0.24   # CALIBRATED 2026-07-28 (income_route_eval.py)
                                        # against all 9 loaded income_tax_topics incl.
                                        # the gambling_winnings/california_lottery_
                                        # winnings same-domain collision pair (opposite
                                        # verdicts) -- resolved correctly at EVERY
                                        # threshold tested, 0 wrong verdicts. In-scope
                                        # probes: 0.140-0.230; out-of-scope: 0.305-0.429.
                                        # 0.24 sits on the safe side of that wide gap.
                                        # Was 0.27 (provisional placeholder, borrowed
                                        # from sales tax's own calibrated value, set
                                        # back when income_rule_embeddings had 0 rows).
INCOME_INFO_THRESHOLD = 0.23           # CALIBRATED 2026-07-20 against the 205 FTB
                                        # doc_chunks embedded in Phase 1 (personal-tax
                                        # + business-tax only). 8 in-scope income
                                        # probes (unemployment/deduction/CalEITC/HOH/
                                        # social-security/bracket/young-child-credit/
                                        # nonresident-credit): distances 0.143-0.239.
                                        # 5 out-of-scope probes (furniture, cannabis,
                                        # property tax, DMV, weather): 0.249-0.422.
                                        # Real gap is thin (0.239 vs 0.249) -- the
                                        # closest collision is "is cannabis taxable"
                                        # matching FTB's cannabis-BUSINESS-income page
                                        # at 0.249, not the sales-tax excise topic.
                                        # Set toward the SAFE (tighter) side of the
                                        # gap rather than the midpoint, matching this
                                        # project's bias for missing a true in-scope
                                        # case (safe defer) over leaking an off-topic
                                        # citation: 0.23 captures 7/8 probes (drops
                                        # only "what is my california tax bracket" at
                                        # 0.2385, which Phase 3's real bracket-table
                                        # content will answer structurally anyway) and
                                        # excludes all 5 out-of-scope probes cleanly.


def _income_route_candidates(conn, qv, k: int = RERANK_K):
    """Mirrors _route_candidates but against the income domain's OWN vector
    space (income_rule_embeddings) -- Phase 3 content. 0 rows as of Phase 2,
    so this is real scaffolding, not yet exercised behavior."""
    emb = "[" + ",".join(str(float(x)) for x in qv) + "]"
    return conn.execute(
        "SELECT topic_key, text, embedding <=> %s::vector AS dist "
        "FROM income_rule_embeddings WHERE embedding IS NOT NULL "
        "ORDER BY dist LIMIT %s", (emb, k)).fetchall()


def _income_lookup(conn, key: str):
    """Mirrors _lookup for the income domain -- Phase 3 scaffold. Returns None
    until income_tax_topics has real rows (0 as of Phase 2); most-recent
    tax_year wins when multiple years exist for the same topic."""
    return conn.execute(
        "SELECT topic_key, taxable, treatment, citation, summary, source_url "
        "FROM income_tax_topics WHERE topic_key=%s ORDER BY tax_year DESC LIMIT 1",
        (key,)).fetchone()


def _income_info_lookup(conn, qv, threshold: float = None):
    """Informational tier for the income domain -- FTB doc_chunks only
    (agency='ftb'). Same philosophy as _info_lookup: never a taxability
    verdict, only a cited pointer; returns None (-> defer) beyond threshold."""
    threshold = INCOME_INFO_THRESHOLD if threshold is None else threshold
    emb = "[" + ",".join(str(float(x)) for x in qv) + "]"
    d = conn.execute(
        "SELECT reg, embedding <=> %s::vector AS dist, text, source_url "
        "FROM doc_chunks WHERE embedding IS NOT NULL AND agency='ftb' "
        "ORDER BY dist LIMIT 1", (emb,)).fetchone()
    if d and float(d[1]) <= threshold:
        return {"source": "doc", "key": d[0], "dist": float(d[1]),
                "title": d[0], "text": d[2], "citation": None, "source_url": d[3]}
    return None


def _income_deduction_answer(conn, question: str, base: dict):
    """Direct answer for a standalone 'what is the standard deduction'
    question -- real structured data (ca_standard_deduction), no LLM call
    needed for something this simple, same precedent as the fee-answered
    path above (a clean f-string, not a generation call)."""
    if not income_brackets.detect_deduction_question(question):
        return None
    fs = income_brackets.detect_filing_status(question)
    statuses = [fs] if fs else ["single", "mfj", "hoh"]  # both distinct tiers if unspecified
    parts, citation, source_url = [], None, None
    for s in statuses:
        d = income_brackets.standard_deduction(conn, s)
        if d:
            parts.append(f"{income_brackets.FILING_STATUS_LABELS[s]}: ${d['amount']:,.0f}")
            citation, source_url = d["citation"], d["source_url"]
    if not parts:
        return None
    result = {**base, "status": "answered", "category": "ca_standard_deduction",
              "citation": citation, "source_url": source_url}
    result["answer_text"] = (
        f"The {income_brackets.DEFAULT_TAX_YEAR} California standard deduction is "
        f"{'; '.join(parts)} ({citation}).")
    return result


def _income_compute_answer(conn, question: str, amount, base: dict):
    """Deterministic wage-earner + standard-deduction bracket computation --
    the SIMPLEST case only (see income_brackets.detect_compute_signal's
    complexity exclude-list); anything more complex (self-employment,
    itemizing, capital gains...) is deliberately left to fall through to the
    informational tier / needs_review rather than guessed at."""
    fs = income_brackets.detect_compute_signal(question)
    if not fs or amount is None:
        return None
    dedu = income_brackets.standard_deduction(conn, fs)
    if not dedu:
        return None
    taxable_income = max(0.0, amount - dedu["amount"])
    calc = income_brackets.compute_ca_tax(conn, taxable_income, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": dedu["amount"], "marginal_rate": calc["marginal_rate"],
              "tax": calc["total_tax"], "citation": calc["citation"],
              "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    income_label, income_caveat = income_brackets.detect_income_description(question)
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in {income_label}, filing status {label}, and the "
        f"standard deduction (${dedu['amount']:,.0f}), your California taxable income is "
        f"about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes {income_label} is your ONLY income source, with no "
        f"other adjustments, credits, or itemized deductions{income_caveat} -- your actual "
        "liability may differ."
    )
    return result


def _income_missing_filing_status_answer(question: str, amount, base: dict):
    """When the question is clearly a tax-computation request (a trigger
    phrase like 'how much tax' + a dollar amount, no complexity
    disqualifiers) but doesn't name a filing status, ask for it explicitly
    instead of a generic 'not covered by current rules' -- filing status
    materially changes which bracket table applies (unlike the wage-only/
    standard-deduction assumptions _income_compute_answer already discloses
    rather than asks about), so it's still not safe to guess, but the user
    should be told exactly what's missing, not left with an unhelpful dead
    end. Found via real usage: "how much tax to pay for income of 100000 in
    california" -- has the trigger + an amount, no filing status -- was
    silently falling all the way through to the generic needs_review text."""
    if amount is None or not income_brackets.detect_compute_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax, I need your filing status: "
        "single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please ask again and "
        "include it (for example, \"...filing single\" or \"...as head of "
        "household\").")
    return result


def _income_exemption_credit_answer(conn, question: str, base: dict):
    """Wage income + a stated exemption-credit question (Form 540 Lines
    7-10) -- see income_brackets.compute_exemption_credit_ca_tax's
    docstring for the credit-vs-deduction mechanic and the phase-out.
    Mirrors _income_compute_answer's plain-wage scope exactly (same
    EXEMPTION_CREDIT_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE), plus one
    optional dependent-count fact.

    Extraction note: a stated dependent count (e.g. "2 dependents") is a
    real, small digit that can appear BEFORE the income figure in some
    phrasings ("with 2 dependents, how much tax do I owe on $80,000...")
    -- unlike the plain compute path's single _amount() (first-match)
    call, this can't safely assume the income figure is whichever number
    appears first. Extracts the dependent count first, removes that
    specific value from the full amounts list, then requires exactly
    one remaining amount for income -- same "N anchors + 1 remainder"
    discipline as the multi-figure paths built earlier this session."""
    fs = income_brackets.detect_exemption_credit_signal(question)
    if not fs:
        return None
    dependent_count = income_brackets.detect_exemption_credit_dependent_count(question)
    amounts = _amounts(question)
    if dependent_count is not None:
        removed = False
        remaining = []
        for a, s, e in amounts:
            if not removed and a == float(dependent_count):
                removed = True
                continue
            remaining.append((a, s, e))
        amounts = remaining
    others = [a for a, _, _ in amounts]
    if len(others) != 1:
        return None
    amount = others[0]
    calc = income_brackets.compute_exemption_credit_ca_tax(
        conn, amount, fs, dependent_count=dependent_count or 0)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000, unaffected by this credit) "
                       f"({calc['surtax_citation']}).")
    exemption = calc["exemption"]
    dep_note = ""
    if exemption["dependent_count"]:
        dep_note = (f" plus ${exemption['dependent_group']:,.2f} for "
                    f"{exemption['dependent_count']} dependent exemption(s)")
    phaseout_note = ""
    if exemption["phaseout_applied"]:
        phaseout_note = (
            f" Your income is above California's exemption-credit phase-out threshold "
            f"(${exemption['threshold']:,.0f} for your filing status), reducing each "
            f"exemption unit by ${exemption['reduction_per_unit']:,.2f} under the Line 32 AGI "
            "Limitation Worksheet.")
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in wage income, filing status {label}"
        f"{', with ' + str(exemption['dependent_count']) + ' dependent(s)' if exemption['dependent_count'] else ''}: "
        f"your California exemption credit is ${exemption['total']:,.2f} "
        f"(${exemption['personal_group']:,.2f} personal exemption credit{dep_note}), "
        f"subtracted directly from your computed tax ({income_brackets.EXEMPTION_CREDIT_CITATION})."
        f"{phaseout_note} Before this credit, your California tax on ${amount:,.2f} of wage "
        f"income (after the standard deduction of ${calc['standard_deduction']:,.0f}) would be "
        f"about ${calc['bracket_tax_before_credit']:,.2f}; after the credit, your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f}.{surtax_note} This does NOT include the Blind or Senior "
        "Exemption Credits (not modeled in this version -- state them explicitly and ask again "
        "if applicable) and assumes wage-only income with no other adjustments -- your actual "
        "liability may differ."
    )
    return result


def _income_exemption_credit_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_exemption_credit_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California exemption credit, I need your filing status: single, "
        "married filing jointly, married filing separately, head of household, or qualifying "
        "surviving spouse. Please ask again and include it.")
    return result


def _use_tax_is_ambiguous(question: str) -> bool:
    """True iff this question is clearly ATTEMPTING a use-tax
    computation but can't safely use the flat AGI lookup -- either an
    explicit over-cap PHRASE (income_brackets.detect_use_tax_over_cap)
    or TWO OR MORE stated dollar figures. Found live via testing: a
    specific item price ("I bought a $2,000 TV") isn't caught by
    phrase-matching alone (no "over $1,000"-style wording present), but
    the lookup table only ever needs ONE figure (AGI) -- a second
    stated amount most likely describes an individual purchase price,
    which this feature can't safely fold into the flat lookup without
    knowing whether it clears FTB's $1,000-per-item cap. Conservative
    by design: two-or-more amounts defers, rather than guessing which
    one is AGI.

    Deliberately NOT true for ZERO amounts -- found live via testing:
    "What is use tax?" (a plain informational question, no computation
    attempted at all) was being swept into this ambiguous-defer path by
    an earlier version that used != 1 instead of >= 2, which incorrectly
    intercepted it with a compute-flavored clarifying message instead of
    letting it fall through to the pre-existing informational answer
    that already covers this topic reasonably."""
    if income_brackets.detect_use_tax_over_cap(question):
        return True
    return len(_amounts(question)) >= 2


def _income_use_tax_answer(question: str, base: dict):
    """Form 540 Line 91 Estimated Use Tax Lookup Table -- see
    income_brackets.compute_estimated_use_tax's docstring for the AGI-
    band mechanic and the $1,000-per-item scope cap. No filing status
    needed (the table is AGI-only); a single stated dollar figure
    (California AGI) is the only input -- see _use_tax_is_ambiguous for
    why a second stated figure routes elsewhere instead of guessing."""
    if not income_brackets.detect_use_tax_signal(question):
        return None
    if _use_tax_is_ambiguous(question):
        return None
    ca_agi = _amount(question)
    if ca_agi is None:
        return None
    use_tax = income_brackets.compute_estimated_use_tax(ca_agi)
    if use_tax is None:
        return None
    result = {**base, "status": "answered", "category": "estimated_use_tax",
              "amount": ca_agi, "tax": use_tax,
              "citation": income_brackets.USE_TAX_CITATION,
              "source_url": income_brackets.USE_TAX_SOURCE_URL}
    result["answer_text"] = (
        f"Assuming ${ca_agi:,.2f} in California Adjusted Gross Income (Form 540 Line 17), your "
        f"estimated use tax under FTB's Estimated Use Tax Lookup Table is ${use_tax:,.2f} "
        f"({income_brackets.USE_TAX_CITATION}). This lookup table only covers individual, "
        "non-business items you purchased for LESS than $1,000 each (from an out-of-state or "
        "online retailer that didn't collect California tax) -- if you bought anything for "
        "$1,000 or more, or anything for business use, that item needs the separate Use Tax "
        "Worksheet (actual price x your district's tax rate) instead, added to this estimate "
        "for anything under $1,000. Report this amount on Form 540, Line 91."
    )
    return result


def _income_use_tax_over_cap_answer(question: str, base: dict):
    """Specific clarifying message for cases _use_tax_is_ambiguous flags
    -- an explicit $1,000+/business-purchase phrase, OR a second stated
    dollar figure this feature can't confidently separate from AGI --
    rather than silently misapplying the flat AGI lookup table."""
    if not income_brackets.detect_use_tax_signal(question):
        return None
    if not _use_tax_is_ambiguous(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "FTB's Estimated Use Tax Lookup Table only covers individual, non-business items "
        "purchased for LESS than $1,000 each, based on your California AGI alone. Your "
        "question mentions more than one dollar figure -- if one of them is the price of a "
        "specific purchase, an item priced at $1,000 or more (or any business purchase) "
        "requires the separate Use Tax Worksheet instead -- the actual purchase price "
        "multiplied by your district's sales/use tax rate (not a flat AGI-based estimate) -- "
        "which this assistant doesn't yet compute. If your purchases are all under $1,000 "
        "each, please ask again stating ONLY your California AGI."
    )
    return result


def _other_state_tax_credit_extract_amounts(question: str):
    """Returns (income_amount, double_taxed_income, other_state_agi,
    other_state_tax_paid) or None if the three anchored figures and the
    single remainder can't be unambiguously extracted -- the "N anchors
    + 1 remainder" pattern (N=3 here, the most anchors used by any
    feature this session) established for the Line 8z EBL-carryover
    build, but using _amount_after_filtered_span (forward-only) instead
    of _amount_near_filtered_span -- see that function's module note for
    why: with 4 dollar figures packed close together, undirected
    nearest-distance can pick a PRECEDING amount over the one the anchor
    phrase actually describes. Anchor phrasing is deliberately state-name-
    AGNOSTIC ("other state AGI", not "Oregon AGI") -- this feature can't
    parse arbitrary US state names, so it requires the taxpayer to
    phrase it generically (disclosed in the missing-info message, not
    silently unsupported)."""
    amounts = _amounts(question)
    match = _amount_after_filtered_span(question, income_brackets.DOUBLE_TAXED_INCOME_TERMS, amounts)
    if match is None:
        return None
    double_taxed_income = match[0]
    remaining = _remove_amount_span(amounts, match)
    match = _amount_after_filtered_span(question, income_brackets.OTHER_STATE_AGI_TERMS, remaining)
    if match is None:
        return None
    other_state_agi = match[0]
    remaining = _remove_amount_span(remaining, match)
    match = _amount_after_filtered_span(question, income_brackets.OTHER_STATE_TAX_PAID_TERMS, remaining)
    if match is None:
        return None
    other_state_tax_paid = match[0]
    remaining = _remove_amount_span(remaining, match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    income_amount = others[0]
    return income_amount, double_taxed_income, other_state_agi, other_state_tax_paid


def _income_other_state_tax_credit_answer(conn, question: str, base: dict):
    """Other State Tax Credit (Schedule S (540)) -- see
    income_brackets.compute_other_state_tax_credit_ca_tax's docstring
    for the two-sided proration/lesser-of mechanic and why "CA tax
    liability" is computed, not asked as a stated fact. The most
    complex extraction built this session: 4 dollar figures (income,
    double-taxed income, other-state AGI, other-state tax paid) plus
    filing status."""
    fs = income_brackets.detect_other_state_tax_credit_signal(question)
    if not fs:
        return None
    extracted = _other_state_tax_credit_extract_amounts(question)
    if extracted is None:
        return None
    income_amount, double_taxed_income, other_state_agi, other_state_tax_paid = extracted
    calc = income_brackets.compute_other_state_tax_credit_ca_tax(
        conn, income_amount, fs, double_taxed_income, other_state_agi, other_state_tax_paid)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000, unaffected by this credit) "
                       f"({calc['surtax_citation']}).")
    credit = calc["credit"]
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in California income (also your CA AGI), filing "
        f"status {label}, ${double_taxed_income:,.2f} of income taxed by both California and "
        f"the other state, a ${other_state_agi:,.2f} other-state AGI, and ${other_state_tax_paid:,.2f} "
        f"in tax paid to the other state: your Other State Tax Credit is ${credit['credit']:,.2f} "
        f"({income_brackets.OTHER_STATE_TAX_CREDIT_CITATION}) -- the LESSER of your CA-side "
        f"proration (${credit['ca_side']:,.2f}, {credit['ca_ratio']*100:.2f}% of your CA tax "
        f"liability) and your other-state-side proration (${credit['other_side']:,.2f}, "
        f"{credit['other_ratio']*100:.2f}% of the tax you paid there). Before this credit, your "
        f"California tax would be about ${calc['bracket_tax_before_credit']:,.2f}; after the "
        f"credit, your estimated {income_brackets.DEFAULT_TAX_YEAR} California income tax is "
        f"about ${calc['total_tax']:,.2f}.{surtax_note} This assumes the SAME double-taxed-"
        "income figure applies to both California's and the other state's share (Schedule S's "
        "own Part I sometimes splits these into two different amounts); does not check whether "
        "the other state already gives ITS OWN residents a credit for CA tax paid (which would "
        "disallow this credit entirely per FTB's anti-double-benefit rule); and doesn't apply "
        "against California AMT (not modeled in this system). Your actual liability may differ."
    )
    return result


def _income_other_state_tax_credit_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_other_state_tax_credit_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Other State Tax Credit, I need your filing status: single, married "
        "filing jointly, married filing separately, head of household, or qualifying surviving "
        "spouse. Please also state your California income, your double-taxed income, the other "
        "state's AGI, and the tax you paid to the other state.")
    return result


def _pte_strip_form_number_phantoms(amounts):
    """Literal "3804" in "form 3804-cr" (this feature's own trigger
    vocabulary) parses as a phantom $3,804.00 dollar amount -- same
    collision class as cannabis 280E/QSBS/Form 2555/Subpart F-GILTI/
    NRA's "1040". Local filter scoped to this feature."""
    return [(a, s, e) for a, s, e in amounts if a != 3804.0]


def _income_pte_credit_answer(conn, question: str, base: dict):
    """PTE Elective Tax Credit (FTB 3804-CR) -- see
    income_brackets.compute_pte_credit_ca_tax's docstring for the
    current-year-absorption-only mechanic and why "CA tax liability" is
    computed, not asked as a stated fact. Extracts the OPTIONAL prior-
    year-carryover anchor FIRST and removes it from the amounts list
    before searching for the REQUIRED K-1 credit anchor -- necessary
    because "PTE credit carryover" contains "PTE credit" as a literal
    substring; by the time the second search runs, the narrowed amounts
    list makes this unambiguous regardless of the anchor-text overlap."""
    fs = income_brackets.detect_pte_credit_signal(question)
    if not fs:
        return None
    amounts = _pte_strip_form_number_phantoms(_amounts(question))
    prior_year_carryover = None
    carryover_match = _amount_near_filtered_span(question, income_brackets.PTE_CREDIT_CARRYOVER_TERMS, amounts)
    if carryover_match is not None:
        prior_year_carryover = carryover_match[0]
        amounts = _remove_amount_span(amounts, carryover_match)
    k1_match = _amount_near_filtered_span(question, income_brackets.PTE_CREDIT_TERMS, amounts)
    if k1_match is None:
        return None
    k1_credit_amount = k1_match[0]
    remaining = _remove_amount_span(amounts, k1_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_pte_credit_ca_tax(
        conn, income_amount, fs, k1_credit_amount, prior_year_carryover=prior_year_carryover or 0.0)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000, unaffected by this credit) "
                       f"({calc['surtax_citation']}).")
    credit = calc["credit"]
    carryover_note = ""
    if prior_year_carryover:
        carryover_note = f" plus your ${prior_year_carryover:,.2f} carryover from a prior year"
    remaining_note = ""
    if credit["remaining_carryover"]:
        remaining_note = (f" The remaining ${credit['remaining_carryover']:,.2f} (your credit "
                          "exceeds your CA tax liability) carries forward for up to 5 years, "
                          "not tracked in this estimate.")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in California income, filing status {label}, and a "
        f"${k1_credit_amount:,.2f} Pass-Through Entity Elective Tax Credit reported on your "
        f"K-1{carryover_note}: your total available credit is ${credit['total_available']:,.2f}, "
        f"of which ${credit['credit_used']:,.2f} is usable this year (capped at your own CA tax "
        f"liability -- this credit is nonrefundable) ({income_brackets.PTE_CREDIT_CITATION})."
        f"{remaining_note} Before this credit, your California tax would be about "
        f"${calc['bracket_tax_before_credit']:,.2f}; after the credit, your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f}.{surtax_note} This assumes the stated K-1 credit figure is "
        "already correct (the 9.3% rate is computed entirely at the entity level, not re-"
        "derived here); does not account for Schedule P's credit-ordering against OTHER "
        "nonrefundable credits you might also claim (e.g. the Other State Tax Credit); and "
        "doesn't model the AMT/TMT interaction (not computed in this system). Your actual "
        "liability may differ."
    )
    return result


def _income_pte_credit_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_pte_credit_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Pass-Through Entity Elective Tax Credit, I need your filing status: "
        "single, married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse. Please also state your California income and your K-1 "
        "PTE credit amount.")
    return result


def _income_late_penalty_reasonable_cause_answer(question: str, base: dict):
    """Dedicated informational redirect for a reasonable-cause/penalty-
    abatement question -- FTB decides this case by case, so this
    assistant deliberately does NOT compute a waived/reduced penalty.
    Checked BEFORE the normal penalty computation, same "specific
    redirect instead of a generic defer" pattern as Roth IRA's."""
    if not income_brackets.detect_late_penalty_reasonable_cause_mention(question):
        return None
    result = {**base, "status": "answered", "category": "late_penalty_reasonable_cause"}
    result["answer_text"] = (
        "California's late-filing and late-payment penalties (R&TC Sections 19131 and 19132) "
        "can be waived if you show the failure was due to reasonable cause and not willful "
        "neglect -- but this is a case-by-case determination FTB makes based on your specific "
        "facts and circumstances (illness, disaster, bad professional advice, etc.), not "
        "something this assistant can compute or predict for you. If you believe you qualify, "
        "you'll need to explain your situation directly to FTB, typically with your return or "
        "in response to a penalty notice. Separately, FTB also offers a one-time Timeliness "
        "Penalty Abatement (R&TC Section 19132.5) for taxpayers who haven't previously used it "
        "and have all other returns/payments current -- also not modeled here. If you want an "
        "estimate of the penalty itself (assuming no abatement), ask again without mentioning "
        "reasonable cause or abatement, stating your unpaid balance and how many months late."
    )
    return result


def _income_late_penalty_answer(question: str, base: dict):
    """Late-filing/late-payment penalties (Form 540 Line 112) -- see
    income_brackets.compute_late_penalties's docstring for the core
    formulas, the required offset, and what's deliberately out of
    scope. No filing status needed. "N months late" is a COUNT, not a
    dollar figure -- extracted and stripped from the amounts list
    before extracting the unpaid-balance dollar figure, same "count vs.
    dollar figure" distinction as the exemption credit's dependent
    count."""
    if not income_brackets.detect_late_penalty_signal(question):
        return None
    months_late = income_brackets.detect_late_penalty_months_late(question)
    if months_late is None:
        return None
    amounts = [(a, s, e) for a, s, e in _amounts(question) if a != months_late]
    others = [a for a, _, _ in amounts]
    if len(others) != 1:
        return None
    unpaid_balance = others[0]
    calc = income_brackets.compute_late_penalties(unpaid_balance, months_late)
    if not calc:
        return None
    result = {**base, "status": "answered", "category": "late_filing_payment_penalty",
              "amount": unpaid_balance, "tax": calc["total_penalty"],
              "citation": income_brackets.LATE_PENALTY_CITATION,
              "source_url": income_brackets.LATE_PENALTY_SOURCE_URL}
    offset_note = ""
    if calc["late_payment_assessed"] < calc["late_payment_computed"]:
        offset_note = (
            f" Your late-payment penalty computes to ${calc['late_payment_computed']:,.2f} "
            f"before the required offset against your late-filing penalty -- since California "
            "reduces the late-payment penalty dollar-for-dollar by the late-filing penalty for "
            f"the same period, only ${calc['late_payment_assessed']:,.2f} of it is actually "
            "assessed on top."
        )
    result["answer_text"] = (
        f"Assuming a ${unpaid_balance:,.2f} unpaid balance and a return filed/paid "
        f"{calc['months_late_int']} month(s) late (any partial month counts as a full month): "
        f"your late-filing penalty is ${calc['late_filing_penalty']:,.2f} (5% per month, capped "
        f"at 25% of the balance).{offset_note} Your total penalty is about "
        f"${calc['total_penalty']:,.2f} ({income_brackets.LATE_PENALTY_CITATION}). This does "
        "NOT include mandatory interest (which compounds daily at a rate that changes every "
        "six months, not modeled here), the separate $135-minimum-penalty rule that can apply "
        "if you filed more than 60 days past California's automatic extended due date "
        "(October 15), or any reasonable-cause/abatement relief you might qualify for -- your "
        "actual amount owed will be higher once interest is added, and could be lower if "
        "relief applies. Assumes you filed and fully paid at the same time."
    )
    return result


def _early_distribution_strip_form_number_phantoms(amounts):
    """Literal "3805" in "form 3805p"/"ftb 3805p" (this feature's own
    trigger vocabulary) parses as a phantom $3,805.00 dollar amount --
    same collision class as cannabis 280E/QSBS/Form 2555/Subpart F-
    GILTI/NRA's "1040"/PTE credit's "3804". Local filter scoped to this
    feature."""
    return [(a, s, e) for a, s, e in amounts if a != 3805.0]


def _income_early_distribution_answer(question: str, base: dict):
    """California additional tax on early retirement distributions (FTB
    3805P Part I) -- see income_brackets.compute_early_distribution_tax's
    docstring for the 2.5%/6% mechanic and why exception-flavored
    questions and non-Part-I account types are deliberately excluded
    from income_brackets.detect_early_distribution_signal rather than
    guessed at here."""
    if not income_brackets.detect_early_distribution_signal(question):
        return None
    q = question.lower()
    is_simple_early = any(t in q for t in income_brackets.EARLY_DISTRIBUTION_SIMPLE_TERMS)
    amounts = _early_distribution_strip_form_number_phantoms(_amounts(question))
    others = [a for a, _, _ in amounts]
    if len(others) != 1:
        return None
    taxable_distribution = others[0]
    calc = income_brackets.compute_early_distribution_tax(taxable_distribution, is_simple_early)
    if not calc:
        return None
    result = {**base, "status": "answered", "category": "early_distribution_tax",
              "amount": taxable_distribution, "tax": calc["tax"],
              "citation": income_brackets.EARLY_DISTRIBUTION_CITATION,
              "source_url": income_brackets.EARLY_DISTRIBUTION_SOURCE_URL}
    rate_note = (f"{calc['rate']*100:g}%" + (" (the SIMPLE-IRA-within-first-2-years rate)"
                 if is_simple_early else " (the standard rate)"))
    result["answer_text"] = (
        f"Assuming ${taxable_distribution:,.2f} as the TAXABLE portion of an early retirement "
        f"distribution (before age 59½, already net of any basis/rollovers), California's "
        f"additional tax is ${calc['tax']:,.2f} -- {rate_note} of the taxable amount "
        f"({income_brackets.EARLY_DISTRIBUTION_CITATION}). This is IN ADDITION TO the separate "
        "federal 10% early-withdrawal penalty and to regular California income tax on the "
        "distribution. This only covers IRA/qualified-plan/annuity distributions with NO "
        "exception claimed -- California's exception list mostly but NOT fully matches the "
        "federal list (two federal exceptions -- phased-retirement annuity payments and "
        "automatic-enrollment permissible withdrawals -- are confirmed NOT recognized for "
        "California), so if any exception might apply to you, ask again mentioning it "
        "specifically and this assistant will flag it instead of guessing. Also doesn't cover "
        "Archer MSA, Medicare Advantage MSA, Coverdell, or ABLE account distributions, which "
        "use different rates entirely."
    )
    return result


def _income_early_distribution_exception_answer(question: str, base: dict):
    """Specific clarifying message when exception-flavored language is
    present -- California's exception list is confirmed to diverge from
    federal's on at least 2 of 25+ codes, so this assistant does not
    guess whether a specific exception applies for California."""
    if not income_brackets.detect_early_distribution_exception_mention(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "California's additional tax on early retirement distributions (FTB 3805P) has its own "
        "exception list, and FTB's own instructions confirm it does NOT fully match the federal "
        "exception list used for the 10% federal penalty -- for example, two federal exceptions "
        "(phased-retirement annuity payments to federal employees, and automatic-enrollment "
        "permissible withdrawals) are federally valid but explicitly NOT recognized for "
        "California. Because a wrong guess here could understate what you owe, this assistant "
        "doesn't compute an exception-based outcome -- please check FTB Form 3805P's current "
        "exception-code list directly, or consult a tax professional, to confirm whether your "
        "specific circumstance qualifies for California."
    )
    return result


def _income_early_distribution_other_account_answer(question: str, base: dict):
    """Specific clarifying message when a non-Part-I account type is
    mentioned (Archer MSA, Medicare Advantage MSA, Coverdell, ABLE,
    HSA) -- these use different rates (12.5%, 50%, or have no CA analog
    at all) this module does not compute."""
    if not income_brackets.detect_early_distribution_other_account_mention(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "The 2.5% additional-tax rate only applies to IRA/qualified-retirement-plan/annuity "
        "distributions (FTB 3805P Part I). Archer MSA non-qualified distributions use a "
        "different 12.5% California rate (R&TC Section 17215), and Medicare Advantage MSA "
        "non-qualified distributions use a 50% rate -- neither is computed by this assistant. "
        "California also does not conform to federal HSA law, so HSA distributions aren't "
        "addressed by this form at all. Please consult FTB Form 3805P's instructions directly "
        "for these account types."
    )
    return result


def _cdc_strip_form_number_phantoms(amounts):
    """Literal "3506" in "form 3506" (this feature's own trigger
    vocabulary) parses as a phantom $3,506.00 dollar amount -- same
    collision class as cannabis 280E/QSBS/Form 2555/Subpart F-GILTI/
    NRA's "1040"/PTE credit's "3804"/early-distribution's "3805". Local
    filter scoped to this feature."""
    return [(a, s, e) for a, s, e in amounts if a != 3506.0]


def _income_cdc_credit_answer(question: str, base: dict):
    """Child and Dependent Care Expenses Credit (FTB 3506) -- see
    income_brackets.compute_cdc_credit's docstring for the equivalence
    conditions the "federal credit x Line 9 percentage" shortcut relies
    on, and why out-of-scope questions are deliberately excluded from
    income_brackets.detect_cdc_credit_signal rather than guessed at
    here."""
    if not income_brackets.detect_cdc_credit_signal(question):
        return None
    amounts = _cdc_strip_form_number_phantoms(_amounts(question))
    credit_match = _amount_near_filtered_span(question, income_brackets.CDC_CREDIT_FEDERAL_CREDIT_TERMS, amounts)
    if credit_match is None:
        return None
    federal_credit_amount = credit_match[0]
    remaining = _remove_amount_span(amounts, credit_match)
    agi_match = _amount_near_filtered_span(question, income_brackets.CDC_CREDIT_FEDERAL_AGI_TERMS, remaining)
    if agi_match is None:
        return None
    federal_agi = agi_match[0]
    calc = income_brackets.compute_cdc_credit(federal_credit_amount, federal_agi)
    if not calc:
        return None
    result = {**base, "status": "answered", "category": "cdc_credit",
              "amount": federal_credit_amount, "credit": None,
              "citation": income_brackets.CDC_CREDIT_CITATION,
              "source_url": income_brackets.CDC_CREDIT_SOURCE_URL}
    if calc["disqualified"]:
        result["answer_text"] = (
            f"With a federal AGI of ${federal_agi:,.2f}, you do NOT qualify for California's "
            "Child and Dependent Care Expenses Credit -- FTB caps eligibility at $100,000 "
            "federal AGI as a hard cutoff, not a gradually-reduced percentage "
            f"({income_brackets.CDC_CREDIT_CITATION})."
        )
        return result
    result["credit"] = calc["credit"]
    result["answer_text"] = (
        f"Assuming a ${federal_credit_amount:,.2f} federal Child and Dependent Care Credit and "
        f"a ${federal_agi:,.2f} federal AGI: your California credit is ${calc['credit']:,.2f} "
        f"({calc['pct']*100:g}% of your federal credit) ({income_brackets.CDC_CREDIT_CITATION}). "
        "This shortcut is only exactly correct for a full-year California resident with ALL "
        "care provided in California and no employer dependent-care benefits received -- FTB "
        "Form 3506 is actually a full parallel worksheet (qualifying expenses x this same "
        "federal-AGI chart, replicating the federal Form 2441 formula, then x this California-"
        "specific percentage), and it never literally reads your federal credit amount as an "
        "input. This credit is nonrefundable with NO carryover -- any amount you can't use this "
        "year is simply lost, not tracked forward. Your actual credit may differ if any care "
        "was provided outside California or your circumstances differ from this common case."
    )
    return result


def _income_cdc_credit_out_of_scope_answer(question: str, base: dict):
    """Specific clarifying message for cases where the federal-credit
    shortcut this module relies on doesn't hold -- nonresident/part-
    year residency, out-of-state care, or employer dependent-care
    benefits, each of which requires FTB 3506's own full worksheet
    (different qualifying-expense/earned-income figures than federal),
    not a guessed percentage-of-federal-credit computation."""
    if not income_brackets.detect_cdc_credit_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "California's Child and Dependent Care Expenses Credit (FTB Form 3506) restricts "
        "qualifying expenses to care provided IN California and, for nonresidents/part-year "
        "residents, CA-source earned income only -- both genuinely different from the federal "
        "credit calculation, and if you received employer dependent-care benefits (W-2 box 10) "
        "there's an additional worksheet involved. This assistant only computes the common "
        "case (full-year CA resident, all care provided in California, no employer benefits) "
        "using a shortcut (federal credit amount x FTB's percentage table) that isn't valid "
        "for your situation. Please consult FTB Form 3506's instructions directly, or a tax "
        "professional, for an accurate figure."
    )
    return result


def _income_adoption_credit_answer(conn, question: str, base: dict):
    """Child Adoption Costs Credit (Form 540 Credit Chart code 197) -- see
    income_brackets.compute_adoption_credit_ca_tax's docstring for the
    cap-at-CA-tax-liability/carryover mechanic. Extracts the qualifying-
    costs anchor first, remainder is treated as income -- same "N anchors
    + 1 remainder" pattern as the PTE credit and Line 8z EBL carryover."""
    fs = income_brackets.detect_adoption_credit_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    costs_match = _amount_near_filtered_span(question, income_brackets.ADOPTION_CREDIT_COST_TERMS, amounts)
    if costs_match is None:
        return None
    qualifying_costs = costs_match[0]
    remaining = _remove_amount_span(amounts, costs_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_adoption_credit_ca_tax(conn, income_amount, fs, qualifying_costs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "adoption_credit",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000, unaffected by this credit) "
                       f"({calc['surtax_citation']}).")
    credit = calc["credit"]
    remaining_note = ""
    if credit["remaining_carryover"]:
        remaining_note = (f" The remaining ${credit['remaining_carryover']:,.2f} (your credit "
                          "exceeds your CA tax liability) carries forward indefinitely until "
                          "used, not tracked in this estimate.")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in California income, filing status {label}, and "
        f"${qualifying_costs:,.2f} in qualifying adoption costs (agency/Department of Social "
        f"Services fees, unreimbursed medical expenses, and family travel expenses) for a child "
        f"adopted from California public agency custody: your available credit is "
        f"${credit['credit_available']:,.2f} (50% of costs, capped at $2,500 per child), of "
        f"which ${credit['credit_used']:,.2f} is usable this year (capped at your own CA tax "
        f"liability -- this credit is nonrefundable) ({income_brackets.ADOPTION_CREDIT_CITATION})."
        f"{remaining_note} Before this credit, your California tax would be about "
        f"${calc['bracket_tax_before_credit']:,.2f}; after the credit, your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f}.{surtax_note} This assumes the child is also a US citizen "
        "or legal resident (the credit's second eligibility gate); doesn't aggregate costs "
        "across a prior unsuccessful adoption attempt, if one applies; doesn't account for a "
        "Schedule CA (540) Line 27 addback if you also itemized these same costs on federal "
        "Schedule A; and doesn't model Schedule P's credit-ordering against other nonrefundable "
        "credits you might also claim. If you adopted more than one child this year, each has "
        "its own separate $2,500 cap -- ask about each child's costs separately. Your actual "
        "liability may differ."
    )
    return result


def _income_adoption_credit_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_adoption_credit_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Child Adoption Costs Credit, I need your filing status: single, "
        "married filing jointly, married filing separately, head of household, or qualifying "
        "surviving spouse. Please also state your California income and your total qualifying "
        "adoption costs.")
    return result


def _income_adoption_credit_out_of_scope_answer(question: str, base: dict):
    """Specific clarifying message when the question itself states an
    eligibility-disqualifying fact (private/international/out-of-state/
    stepparent adoption) -- FTB's own text confirms this credit does NOT
    apply outside CA-public-agency custody, so this is a genuine
    disqualification, not a guessed defer."""
    if not income_brackets.detect_adoption_credit_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "California's Child Adoption Costs Credit only applies to a child who was in the "
        "custody of a California public agency or political subdivision (e.g. adopted through "
        "the county/state foster care system) -- FTB's own instructions state it explicitly "
        "does NOT apply to a child adopted from another country, from another state, or through "
        "a private/independent/stepparent adoption. Based on what you described, this credit "
        "does not apply to your situation."
    )
    return result


def _income_adoption_credit_ambiguous_eligibility_answer(question: str, base: dict):
    """When adoption-credit vocabulary is present but the question states
    neither a public-agency signal nor an out-of-scope term, ask
    specifically about the eligibility gate rather than silently
    assuming it's satisfied -- this credit's restriction to CA-public-
    agency custody is narrow enough that guessing "yes" would risk
    overstating the credit for the more common private-adoption case."""
    if not income_brackets.detect_adoption_credit_ambiguous_eligibility(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "California's Child Adoption Costs Credit only applies to a child who was in the "
        "custody of a California public agency or political subdivision (e.g. adopted through "
        "the county/state foster care system) -- it does NOT apply to private, international, "
        "out-of-state, or stepparent adoptions. Was your child adopted through California's "
        "public foster care/agency system? If so, please also state your total qualifying "
        "adoption costs (agency fees, unreimbursed medical expenses, family travel expenses), "
        "your California income, and your filing status."
    )
    return result


def _catc_strip_form_number_phantoms(amounts):
    """Literal "3592" in "form 3592" (this feature's own trigger
    vocabulary) parses as a phantom $3,592.00 dollar amount -- same
    collision class as cannabis 280E/QSBS/Form 2555/Subpart F-GILTI/
    NRA's "1040"/PTE credit's "3804"/early-distribution's "3805"/CDC
    credit's "3506". Local filter scoped to this feature."""
    return [(a, s, e) for a, s, e in amounts if a != 3592.0]


def _income_catc_credit_answer(conn, question: str, base: dict):
    """College Access Tax Credit (FTB Form 3592) -- see
    income_brackets.compute_catc_credit_ca_tax's docstring for the
    CEFA-certification/allocation-pool caveats and the cap-at-CA-tax-
    liability/6-year-carryover mechanic. Reuses this feature's own
    trigger vocabulary as the contribution-amount anchor, same "anchor
    doubles as trigger" pattern as the PTE credit's K-1 amount."""
    fs = income_brackets.detect_catc_credit_signal(question)
    if not fs:
        return None
    amounts = _catc_strip_form_number_phantoms(_amounts(question))
    contribution_match = _amount_near_filtered_span(question, income_brackets.CATC_TERMS, amounts)
    if contribution_match is None:
        return None
    contribution_amount = contribution_match[0]
    remaining = _remove_amount_span(amounts, contribution_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_catc_credit_ca_tax(conn, income_amount, fs, contribution_amount)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "catc_credit",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000, unaffected by this credit) "
                       f"({calc['surtax_citation']}).")
    credit = calc["credit"]
    remaining_note = ""
    if credit["remaining_carryover"]:
        remaining_note = (f" The remaining ${credit['remaining_carryover']:,.2f} (your credit "
                          "exceeds your CA tax liability) carries forward for up to 6 years, "
                          "not tracked in this estimate.")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in California income, filing status {label}, and a "
        f"${contribution_amount:,.2f} contribution to the College Access Tax Credit Fund: your "
        f"available credit is ${credit['credit_available']:,.2f} (50% of your contribution), of "
        f"which ${credit['credit_used']:,.2f} is usable this year (capped at your own CA tax "
        f"liability -- this credit is nonrefundable) ({income_brackets.CATC_CITATION})."
        f"{remaining_note} Before this credit, your California tax would be about "
        f"${calc['bracket_tax_before_credit']:,.2f}; after the credit, your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f}.{surtax_note} This assumes your FULL contribution was "
        "already reserved and certified by CEFA (the California Educational Facilities "
        "Authority) -- this credit isn't automatic just because you donated; it requires "
        "applying to CEFA, receiving a reservation, contributing that exact amount, and "
        "receiving a certification, and the statewide $500 million/year pool is allocated "
        "first-come-first-served. Doesn't account for a separate $5,000,000 aggregate business-"
        "credit ceiling that applies for 2024-2026 tax years, or a Schedule CA (540) addback if "
        "you also deducted this contribution on federal Schedule A. Your actual liability may "
        "differ, especially if CEFA certified less than your full intended contribution."
    )
    return result


def _income_catc_credit_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_catc_credit_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your College Access Tax Credit, I need your filing status: single, "
        "married filing jointly, married filing separately, head of household, or qualifying "
        "surviving spouse. Please also state your California income and your contribution "
        "amount to the College Access Tax Credit Fund.")
    return result


def _income_isr_penalty_answer(question: str, base: dict):
    """Individual Shared Responsibility Penalty (Form 540 Line 92, FTB
    3853) -- see income_brackets.compute_isr_penalty's docstring for the
    verified 2025 formula and its scope. Household adult/child counts are
    COUNTS, not dollar figures -- stripped from the amounts list before
    extracting household income, same "count vs. dollar figure"
    distinction as the exemption credit's dependent count / late
    penalty's months-late."""
    fs = income_brackets.detect_isr_penalty_signal(question)
    if not fs:
        return None
    n_adults = income_brackets.detect_isr_penalty_household_adults(question)
    if n_adults is None:
        return None
    amounts = [(a, s, e) for a, s, e in _amounts(question) if a != float(n_adults)]
    n_children = income_brackets.detect_isr_penalty_household_children(question) or 0
    if n_children:
        amounts = [(a, s, e) for a, s, e in amounts if a != float(n_children)]
    others = [a for a, _, _ in amounts]
    if len(others) != 1:
        return None
    household_income = others[0]
    calc = income_brackets.compute_isr_penalty(fs, n_adults, n_children, household_income)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "isr_penalty",
              "amount": household_income, "tax": calc["penalty"],
              "citation": income_brackets.ISR_PENALTY_CITATION,
              "source_url": income_brackets.ISR_PENALTY_SOURCE_URL}
    if calc["exempt_below_threshold"]:
        result["answer_text"] = (
            f"With a household of {n_adults} adult(s) and {n_children} child(ren), filing status "
            f"{label}, and ${household_income:,.2f} in household income: you do NOT owe a "
            f"California Individual Shared Responsibility Penalty -- your household income is at "
            f"or below your ${calc['filing_threshold']:,.2f} filing threshold, which zeroes out "
            f"the penalty entirely ({income_brackets.ISR_PENALTY_CITATION}). This assumes you (and "
            "your spouse/RDP, if applicable) were under 65 -- a higher filing threshold applies "
            "at 65+, which could change this result. Your actual liability may differ."
        )
        return result
    result["answer_text"] = (
        f"With a household of {n_adults} adult(s) and {n_children} child(ren) uninsured the "
        f"entire year, filing status {label}, and ${household_income:,.2f} in household income "
        f"(above your ${calc['filing_threshold']:,.2f} filing threshold): your estimated "
        f"California Individual Shared Responsibility Penalty is ${calc['penalty']:,.2f} -- the "
        f"GREATER of a flat ${calc['flat_dollar']:,.2f} ($950/adult + $475/child, capped at "
        f"$2,850) or {income_brackets.ISR_PENALTY_INCOME_RATE*100:g}% of income over your filing "
        f"threshold (${calc['pct_income']:,.2f}), capped at the average statewide bronze-plan "
        f"premium for your household size (${calc['avg_premium_cap']:,.2f}) "
        f"({income_brackets.ISR_PENALTY_CITATION}). This assumes: nobody in your household turned "
        "18 during the year (the per-person rate would otherwise need to change mid-year); you "
        "claimed no coverage exemption (hardship, unaffordability, religious, tribal, "
        "incarceration, short coverage gap, etc.); you (and your spouse/RDP, if applicable) were "
        "under 65 (a higher filing threshold applies at 65+); and your household income doesn't "
        "need to include a dependent's own income or California tax-exempt interest. Your actual "
        "liability may differ."
    )
    return result


def _income_isr_penalty_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_isr_penalty_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California Individual Shared Responsibility Penalty, I need your "
        "filing status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please also state the number of adults and "
        "children in your household, and your household income.")
    return result


def _income_isr_penalty_out_of_scope_answer(question: str, base: dict):
    """Specific clarifying message when exemption/hardship/partial-year/
    65+/mid-year-18th-birthday language is present -- each genuinely
    changes the computation (or requires a case-by-case FTB
    determination via a Marketplace-granted exemption certificate) in a
    way this scoped build does not attempt."""
    if not income_brackets.detect_isr_penalty_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only estimates California's Individual Shared Responsibility Penalty for "
        "the common case: uninsured the ENTIRE tax year, no coverage exemption claimed, nobody in "
        "the household turning 18 during the year, and everyone under 65. Coverage exemptions "
        "(income below filing threshold, unaffordable coverage, hardship, religious conscience, "
        "tribal membership, incarceration, a short coverage gap of 3 months or less, etc.) each "
        "have their own rules -- some require a Marketplace-granted Exemption Certificate Number "
        "that FTB does not compute. Please consult FTB Form 3853's instructions directly, or a "
        "tax professional, for an accurate figure based on your specific situation."
    )
    return result


def _income_isr_penalty_ambiguous_coverage_answer(question: str, base: dict):
    """When ISR-penalty vocabulary is present but the question states
    neither a full-year-uninsured confirmation nor an out-of-scope term,
    ask specifically rather than assuming full-year coverage status
    either way -- guessing "uninsured all year" would risk overstating
    the penalty for someone who actually had partial-year coverage."""
    if not income_brackets.detect_isr_penalty_ambiguous_coverage(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "Were you (and everyone in your household) uninsured for the ENTIRE tax year, with no "
        "health coverage exemption claimed? This assistant only estimates California's Individual "
        "Shared Responsibility Penalty for that specific common case. If so, please also state the "
        "number of adults and children in your household, your household income, and your filing "
        "status."
    )
    return result


def _income_amt_screen_answer(conn, question: str, base: dict):
    """California AMT "screen" (Schedule P (540), Form 540 Line 61) --
    see income_brackets.compute_amt_screen_ca_tax's docstring for why
    AMTI collapses to CA AGI for this narrow population (standard
    deduction, wage-only, zero preference items) and why this is a real
    formula computation, not a hard-coded "always zero" assumption."""
    fs = income_brackets.detect_amt_screen_signal(question)
    if not fs:
        return None
    amount = _amount(question)
    if amount is None:
        return None
    calc = income_brackets.compute_amt_screen_ca_tax(conn, amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "amt_screen",
              "amount": amount, "tax": calc["amt_owed"],
              "citation": income_brackets.AMT_SCREEN_CITATION,
              "source_url": income_brackets.AMT_SCREEN_SOURCE_URL}
    if calc["amt_owed"] <= 0:
        result["answer_text"] = (
            f"Assuming ${amount:,.2f} in California income, filing status {label}, the standard "
            "deduction (not itemizing), and no AMT preference items (no incentive stock options, "
            "passive activity, private activity bond interest, depreciation adjustments, or "
            "similar): you do NOT owe California Alternative Minimum Tax. Your Tentative Minimum "
            f"Tax is ${calc['tmt']:,.2f} (7.0% of ${calc['amti']:,.2f} AMTI minus a "
            f"${calc['exemption']:,.2f} exemption), which is below your regular California tax of "
            f"${calc['regular_tax']:,.2f} -- AMT only applies when TMT EXCEEDS regular tax "
            f"({income_brackets.AMT_SCREEN_CITATION}). For this narrow population (standard "
            "deduction, wage-only income, zero preference items), California's exemption amount "
            "and rate structure mean this is essentially always true. If you itemize deductions, "
            "exercised incentive stock options, have passive activity or depreciation "
            "adjustments, or hold private activity bonds, this simplified check does not apply to "
            "you -- consult Schedule P (540)'s full worksheet or a tax professional."
        )
        return result
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in California income, filing status {label}, the standard "
        "deduction, and no AMT preference items: your Tentative Minimum Tax is "
        f"${calc['tmt']:,.2f} (7.0% of ${calc['amti']:,.2f} AMTI minus a ${calc['exemption']:,.2f} "
        f"exemption), which EXCEEDS your regular California tax of ${calc['regular_tax']:,.2f} -- "
        f"you owe an estimated ${calc['amt_owed']:,.2f} in California Alternative Minimum Tax "
        f"({income_brackets.AMT_SCREEN_CITATION}). This is an unusual result for a standard-"
        "deduction, preference-item-free filer -- double-check your figures, and consult Schedule "
        "P (540) or a tax professional to confirm."
    )
    return result


def _income_amt_screen_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_amt_screen_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To check your California Alternative Minimum Tax exposure, I need your filing status: "
        "single, married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse. Please also state your California income. (This assistant "
        "only handles the common case: standard deduction, wage-only income, no AMT preference "
        "items.)")
    return result


def _income_amt_screen_out_of_scope_answer(question: str, base: dict):
    """Specific clarifying message when itemizing/ISO/passive-activity/
    depreciation/private-activity-bond/NOL language is present -- these
    genuinely require the full ~11-category AMTI build this scoped
    screen does not attempt."""
    if not income_brackets.detect_amt_screen_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "California's Alternative Minimum Tax (Schedule P (540)) requires a full preference-item "
        "computation for anyone who itemizes deductions, exercised incentive stock options, has "
        "passive activity or depreciation adjustments, holds private activity bonds, or has "
        "certain other preference items -- this assistant only handles the much narrower common "
        "case (standard deduction, wage-only income, zero preference items), where AMT is "
        "essentially always $0. Based on what you described, that simplified check doesn't apply "
        "to your situation. Please consult Schedule P (540)'s full worksheet, or a tax "
        "professional, for an accurate figure."
    )
    return result


def _amt_iso_strip_form_number_phantoms(amounts):
    # AMT_SCREEN_TERMS (shared with the base screen) includes "schedule p
    # (540)"/"form 540 line 61" -- bare digit sequences the shared regex
    # would otherwise misparse as dollar amounts if a user's question
    # happens to include that phrasing. Same collision class found 10+
    # times this session, fixed proactively before any live test.
    return [(a, s, e) for a, s, e in amounts if a not in (540.0, 61.0)]


def _income_amt_iso_answer(conn, question: str, base: dict):
    """AMT ISO-exercise addback extension (Schedule P (540) Part I Line
    10) -- see income_brackets.compute_amt_iso_ca_tax's docstring for the
    bargain-element mechanic. One anchor (bargain element) plus income as
    the sole remainder, using the edge-aware _amount_near_anchor_edge
    helper (this feature's phrasing is bidirectional, same family as
    NOL-mixed: "a $150,000 bargain element" and "bargain element of
    $150,000" are both natural)."""
    fs = income_brackets.detect_amt_iso_signal(question)
    if not fs:
        return None
    amounts = _amt_iso_strip_form_number_phantoms(_amounts(question))
    bargain_match = _amount_near_anchor_edge(question, income_brackets.AMT_ISO_BARGAIN_ELEMENT_TERMS, amounts)
    if bargain_match is None:
        return None
    iso_bargain_element = bargain_match[0]
    remaining = _remove_amount_span(amounts, bargain_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_amt_iso_ca_tax(conn, income_amount, iso_bargain_element, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "amt_screen",
              "amount": income_amount, "tax": calc["amt_owed"],
              "citation": income_brackets.AMT_SCREEN_CITATION,
              "source_url": income_brackets.AMT_SCREEN_SOURCE_URL}
    if calc["amt_owed"] <= 0:
        result["answer_text"] = (
            f"Assuming ${income_amount:,.2f} in California income, a ${iso_bargain_element:,.2f} "
            f"incentive stock option (ISO) bargain element (the excess of the stock's fair market "
            f"value at exercise over what you paid), filing status {label}, and the standard "
            f"deduction: your Tentative Minimum Tax is ${calc['tmt']:,.2f} (7.0% of "
            f"${calc['amti']:,.2f} AMTI -- your income plus the ISO bargain element -- minus a "
            f"${calc['exemption']:,.2f} exemption), which is below your regular California tax of "
            f"${calc['regular_tax']:,.2f} -- you do NOT owe California Alternative Minimum Tax "
            f"({income_brackets.AMT_SCREEN_CITATION}). Note the ISO exercise creates no REGULAR "
            "tax income at all -- the bargain element only affects this AMT comparison. This "
            "assumes you exercised and are still holding the stock (no sale this year), one "
            "exercise event, and no other AMT preference items."
        )
        return result
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in California income, a ${iso_bargain_element:,.2f} "
        f"incentive stock option (ISO) bargain element, filing status {label}, and the standard "
        f"deduction: your Tentative Minimum Tax is ${calc['tmt']:,.2f} (7.0% of "
        f"${calc['amti']:,.2f} AMTI minus a ${calc['exemption']:,.2f} exemption), which EXCEEDS "
        f"your regular California tax of ${calc['regular_tax']:,.2f} -- you owe an estimated "
        f"${calc['amt_owed']:,.2f} in California Alternative Minimum Tax "
        f"({income_brackets.AMT_SCREEN_CITATION}). This assumes you exercised and are still "
        "holding the stock (no sale this year), one exercise event, and no other AMT preference "
        "items -- consult Schedule P (540) or a tax professional to confirm."
    )
    return result


def _income_amt_iso_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_amt_iso_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To check your California AMT exposure from an ISO exercise, I need your filing status: "
        "single, married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse. Please also state your California income and your ISO "
        "bargain element (the fair market value at exercise minus what you paid).")
    return result


def _income_amt_iso_same_year_sale_answer(question: str, base: dict):
    """Schedule P (540)'s own carve-out: exercised-and-sold-the-same-year
    means no AMT adjustment at all -- a direct consequence of the source
    text, not a guess. Redirects rather than silently computing either
    way, since assuming the wrong direction risks over- or understating
    AMT owed."""
    if not income_brackets.detect_amt_iso_same_year_sale(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "Since you exercised and sold the ISO stock in the same year, no AMT adjustment applies "
        "at all -- FTB's own instructions for Schedule P (540) confirm the regular-tax and AMT "
        "treatment are identical in that case. If you still want to check your AMT exposure from "
        "other income, ask again without mentioning the ISO sale."
    )
    return result


def _income_kiddie_tax_answer(conn, question: str, base: dict):
    """FTB 3800 kiddie tax on a child's unearned income (Form 540 Line
    31) -- see income_brackets.compute_kiddie_tax_ca_tax's docstring for
    the full worksheet derivation and its disclosed scope (single child,
    standard deduction, no earned income, child's own filing status
    defaults to single). Two anchors (child's unearned income, parent's
    taxable income), no remainder needed since exactly 2 dollar figures
    are expected. Uses forward-only _amount_after_filtered_span -- this
    feature's own natural phrasing is always "ANCHOR is $VALUE" (anchor
    before amount), unlike NOL-mixed's genuinely bidirectional "$Y in X"
    habit. Found live: the edge-aware _amount_near_anchor_edge helper
    (tried first, mirroring NOL-mixed) still picked the WRONG figure
    here -- a short ", " connector before the anchor made an unrelated
    PRECEDING amount from a different clause numerically closer than
    the anchor's own value sitting slightly farther after it. Forward-
    only sidesteps this by construction: it never considers a preceding
    amount at all. Also strips the "3800" phantom (form 3800's own form
    number, a bare 4-digit sequence the shared regex would otherwise
    misparse as a dollar amount -- the same collision class found 10+
    times this session, missed on the first pass here and caught only
    once extraction was tested live)."""
    parent_fs = income_brackets.detect_kiddie_tax_signal(question)
    if not parent_fs:
        return None
    amounts = [(a, s, e) for a, s, e in _amounts(question) if a != 3800.0]
    child_match = _amount_after_filtered_span(question, income_brackets.KIDDIE_TAX_CHILD_INCOME_TERMS, amounts)
    if child_match is None:
        return None
    child_unearned_income = child_match[0]
    remaining = _remove_amount_span(amounts, child_match)
    parent_match = _amount_after_filtered_span(question, income_brackets.KIDDIE_TAX_PARENT_INCOME_TERMS, remaining)
    if parent_match is None:
        return None
    parent_taxable_income = parent_match[0]
    calc = income_brackets.compute_kiddie_tax_ca_tax(conn, child_unearned_income, parent_taxable_income, parent_fs)
    if not calc:
        return None
    parent_label = income_brackets.FILING_STATUS_LABELS[parent_fs]
    result = {**base, "status": "answered", "category": "kiddie_tax",
              "amount": child_unearned_income, "tax": calc["total_tax"],
              "marginal_rate": calc["marginal_rate"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc.get("surtax"):
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services Tax "
                       f"(1% of the child's taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if not calc["kiddie_tax_applies"]:
        result["answer_text"] = (
            f"With ${child_unearned_income:,.2f} in your child's unearned income, the kiddie tax "
            f"(FTB 3800) does NOT apply -- either the unearned income is at or below the "
            f"{income_brackets.DEFAULT_TAX_YEAR} ${income_brackets.KIDDIE_TAX_THRESHOLD:,.0f} "
            "threshold, or your child's own taxable income is $0. Your child's tax is figured the "
            f"normal way: assuming a single filing status and the standard deduction "
            f"(${calc['child_standard_deduction']:,.0f}), taxable income of "
            f"${calc['child_taxable_income']:,.2f}, your child's estimated "
            f"{income_brackets.DEFAULT_TAX_YEAR} California tax is about ${calc['total_tax']:,.2f} "
            f"({calc['citation']}).{surtax_note}"
        )
        return result
    controls_note = (
        "the parent-rate kiddie-tax method controls (it produces a higher amount than your "
        "child's own rate would)" if calc["kiddie_tax_controls"] else
        "your child's own rate actually produces the same or a higher amount than the kiddie-tax "
        "method, so it controls instead (FTB 3800 never produces a WORSE result than your child's "
        "own tax)"
    )
    result["answer_text"] = (
        f"With ${child_unearned_income:,.2f} in your child's unearned income and your (the "
        f"parent's) taxable income of ${parent_taxable_income:,.2f}, filing status {parent_label}: "
        f"${calc['net_unearned_income']:,.2f} of your child's unearned income is net unearned "
        f"income subject to the kiddie tax. Taxed at your marginal rate, that portion contributes "
        f"${calc['tentative_tax_parent_rate']:,.2f}; the rest of your child's taxable income "
        f"(assuming a single filing status and the standard deduction of "
        f"${calc['child_standard_deduction']:,.0f}) is taxed at your child's own rate, adding "
        f"${calc['child_own_rate_tax_on_remaining']:,.2f} -- and {controls_note}. Your child's "
        f"estimated {income_brackets.DEFAULT_TAX_YEAR} California tax is about "
        f"${calc['total_tax']:,.2f} ({calc['citation']}).{surtax_note} This assumes a single "
        "child (no siblings also filing FTB 3800 with the same parent), the standard deduction, "
        "no earned income for your child, and that your child otherwise meets FTB's kiddie-tax "
        "age/support/filing requirements -- your actual liability may differ."
    )
    return result


def _income_kiddie_tax_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_kiddie_tax_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To figure your child's kiddie tax (FTB 3800), I need YOUR (the parent's) filing status: "
        "single, married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse. Please also state your child's unearned income and your "
        "own taxable income.")
    return result


def _income_kiddie_tax_out_of_scope_answer(question: str, base: dict):
    """Specific clarifying message when earned-income/multiple-children/
    itemizing language is present -- this narrow build doesn't attempt
    those branches of Form 3800's worksheet."""
    if not income_brackets.detect_kiddie_tax_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles the common FTB 3800 case: a single child with unearned "
        "income only (no earned income/job), the standard deduction, and no other children also "
        "filing FTB 3800 with the same parent. Based on what you described, that doesn't apply -- "
        "please consult FTB 3800's full worksheet, or a tax professional, for an accurate figure."
    )
    return result


def _amount_after_filtered_span(question: str, keywords, amounts, window: int = 80):
    """Forward-only counterpart to _amount_near_filtered_span -- see that
    function's docstring for why returning (and removing by) a span
    instead of a bare value is necessary. Pair with _remove_amount_span."""
    ql = question.lower()
    if not amounts:
        return None
    best = None
    for kw in keywords:
        start = 0
        while True:
            idx = ql.find(kw, start)
            if idx == -1:
                break
            for amount, a_start, a_end in amounts:
                if a_start < idx:
                    continue
                dist = a_start - idx
                if dist <= window and (best is None or dist < best[1]):
                    best = ((amount, a_start, a_end), dist)
            start = idx + len(kw)
    return best[0] if best else None


def _income_underpayment_answer(conn, question: str, base: dict):
    """Underpayment of Estimated Tax Penalty, SHORT METHOD ONLY (FTB
    5805 Side 2 Part II) -- see income_brackets.compute_underpayment_penalty_ca_tax's
    docstring for the verified 5-step mechanic and its scope. Extracts
    withholding, then prior-year tax, then prior-year AGI as anchors
    (each removed from the amounts list BY POSITION before the next
    search), with the sole remaining figure treated as current-year
    income -- same "N anchors + 1 remainder" pattern as OSTC/PTE/Line
    8z, but using _amount_after_filtered_span (which returns the
    matched tuple's own position) rather than removing "by value" --
    with 4 dollar figures this close together, two DIFFERENT stated
    facts can share the same dollar value (found live: "prior year tax
    was $15,000 ... withholding was $15,000"), and removing by value
    (or by first-list-order occurrence of that value) can strip the
    WRONG occurrence, misattributing the real one to a later anchor.
    Forward-only matching is safe here because this feature's phrasing
    convention is always "X was $Y" (amount follows the anchor)."""
    fs = income_brackets.detect_underpayment_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_after_filtered_span(question, income_brackets.UNDERPAYMENT_WITHHOLDING_TERMS, amounts)
    if match is None:
        return None
    withholding = match[0]
    amounts = _remove_amount_span(amounts, match)
    match = _amount_after_filtered_span(question, income_brackets.UNDERPAYMENT_PRIOR_YEAR_TAX_TERMS, amounts)
    if match is None:
        return None
    prior_year_tax = match[0]
    amounts = _remove_amount_span(amounts, match)
    match = _amount_after_filtered_span(question, income_brackets.UNDERPAYMENT_PRIOR_YEAR_AGI_TERMS, amounts)
    if match is None:
        return None
    prior_year_agi = match[0]
    amounts = _remove_amount_span(amounts, match)
    others = [a for a, _, _ in amounts]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_underpayment_penalty_ca_tax(
        conn, income_amount, fs, prior_year_tax, prior_year_agi, withholding)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    penalty = calc["penalty"]
    result = {**base, "status": "answered", "category": "underpayment_penalty",
              "amount": income_amount, "tax": penalty["penalty"],
              "citation": income_brackets.UNDERPAYMENT_CITATION,
              "source_url": income_brackets.UNDERPAYMENT_SOURCE_URL}
    common_facts = (
        f"With ${income_amount:,.2f} in California income (current-year tax "
        f"${calc['current_year_tax']:,.2f}), filing status {label}, ${calc['prior_year_tax']:,.2f} "
        f"in prior-year CA tax, ${calc['prior_year_agi']:,.2f} in prior-year CA AGI, and "
        f"${calc['withholding']:,.2f} in California withholding"
    )
    if penalty["reason"] == "de_minimis_balance":
        result["answer_text"] = (
            f"{common_facts}: you do NOT owe an Underpayment of Estimated Tax Penalty -- your "
            "balance due (current-year tax minus withholding) is under the $500 ($250 if married "
            f"filing separately) de minimis threshold ({income_brackets.UNDERPAYMENT_CITATION})."
        )
        return result
    if penalty["reason"] == "zero_prior_year_liability":
        result["answer_text"] = (
            f"{common_facts}: you do NOT owe an Underpayment of Estimated Tax Penalty -- you had "
            f"no California tax liability last year ({income_brackets.UNDERPAYMENT_CITATION})."
        )
        return result
    if penalty["reason"] == "safe_harbor_met":
        result["answer_text"] = (
            f"{common_facts}: you do NOT owe an Underpayment of Estimated Tax Penalty -- your "
            f"withholding meets the required annual payment safe harbor (${penalty['required_annual_payment']:,.2f}, "
            "the LESSER of 90% of your current-year tax or 100%/110% of your prior-year tax) "
            f"({income_brackets.UNDERPAYMENT_CITATION})."
        )
        return result
    result["answer_text"] = (
        f"{common_facts}: your estimated Underpayment of Estimated Tax Penalty is "
        f"${penalty['penalty']:,.2f} -- your withholding falls ${penalty['underpayment']:,.2f} "
        f"short of the required annual payment (${penalty['required_annual_payment']:,.2f}, the "
        "LESSER of 90% of your current-year tax or 100%/110% of your prior-year tax), computed "
        f"using FTB's Short Method (the underpayment x .05028767, {income_brackets.DEFAULT_TAX_YEAR}'s "
        f"blended annual rate) ({income_brackets.UNDERPAYMENT_CITATION}). This assumes you paid "
        "your balance with your return (not early -- an early payment could reduce this slightly); "
        "you made NO estimated tax payments this year (only withholding); and you don't qualify "
        "for the Farmer/Fisherman exception. Your actual liability may differ, especially if you "
        "made any estimated payments (the eligibility for this simplified method depends on "
        "whether those were made exactly on the required due dates)."
    )
    return result


def _income_underpayment_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_underpayment_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Underpayment of Estimated Tax Penalty, I need your filing status: "
        "single, married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse. Please also state your California income, your prior-year "
        "CA tax, your prior-year CA AGI, and your California withholding. (This assistant only "
        "handles the common case: no estimated tax payments made, only withholding.)")
    return result


def _income_underpayment_out_of_scope_answer(question: str, base: dict):
    """Specific clarifying message for the Farmer/Fisherman exception or
    the annualized-income installment method -- both need an entirely
    different form/worksheet, not either method built here. NARROWED
    2026-08-28: plain estimated-tax-payment mentions now route to the
    Regular Method feature below instead of landing here."""
    if not income_brackets.detect_underpayment_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "Farmers and fishermen use a separate form (FTB 5805F) with different rules entirely, "
        "and the annualized income installment method needs its own full worksheet (Form 5805 "
        "Part III) this assistant doesn't attempt. Please consult FTB Form 5805's instructions "
        "directly, or a tax professional, for an accurate figure."
    )
    return result


def _underpayment_regular_extract_common_facts(masked_question: str, amounts):
    """Shared extraction for the 4 non-payment facts (withholding,
    prior-year tax, prior-year AGI, current-year income), same order and
    anchor-removal discipline as the Short Method's own
    _income_underpayment_answer. Returns (withholding, prior_year_tax,
    prior_year_agi, current_year_income, remaining_amounts) or None."""
    match = _amount_after_filtered_span(masked_question, income_brackets.UNDERPAYMENT_WITHHOLDING_TERMS, amounts)
    if match is None:
        return None
    withholding = match[0]
    amounts = _remove_amount_span(amounts, match)
    match = _amount_after_filtered_span(masked_question, income_brackets.UNDERPAYMENT_PRIOR_YEAR_TAX_TERMS, amounts)
    if match is None:
        return None
    prior_year_tax = match[0]
    amounts = _remove_amount_span(amounts, match)
    match = _amount_after_filtered_span(masked_question, income_brackets.UNDERPAYMENT_PRIOR_YEAR_AGI_TERMS, amounts)
    if match is None:
        return None
    prior_year_agi = match[0]
    amounts = _remove_amount_span(amounts, match)
    match = _amount_after_filtered_span(masked_question, income_brackets.UNDERPAYMENT_CURRENT_INCOME_TERMS, amounts)
    if match is None:
        return None
    current_year_income = match[0]
    amounts = _remove_amount_span(amounts, match)
    return withholding, prior_year_tax, prior_year_agi, current_year_income, amounts


def _income_underpayment_regular_answer(conn, question: str, base: dict):
    """Underpayment of Estimated Tax Penalty, REGULAR METHOD (FTB 5805
    Worksheet II) -- see income_brackets.compute_underpayment_penalty_
    regular's module note for the full verified mechanic. Extracts the
    4 common facts (same anchors as the Short Method) plus a flat list
    of estimated-payment (amount, date) pairs, buckets them by actual
    payment date into the FTB due-date windows, then runs the Worksheet
    II arithmetic. Dates are masked out of the question BEFORE dollar-
    amount extraction runs -- see _mask_dates's docstring for why this
    is required, not optional, for this specific feature."""
    fs = income_brackets.detect_underpayment_regular_method_signal(question)
    if not fs:
        return None
    dates = _dates(question)
    if not dates:
        return None
    masked = _mask_dates(question, dates)
    amounts = _amounts(masked)
    common = _underpayment_regular_extract_common_facts(masked, amounts)
    if common is None:
        return None
    withholding, prior_year_tax, prior_year_agi, current_year_income, remaining = common
    if not remaining:
        return None
    pairs = _pair_amounts_with_dates(remaining, dates)
    if len(pairs) != len(remaining):
        return None
    buckets = income_brackets.bucket_regular_method_payments(pairs)
    if buckets is None:
        return None
    calc = income_brackets.compute_underpayment_penalty_regular_ca_tax(
        conn, current_year_income, fs, prior_year_tax, prior_year_agi, withholding, buckets)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    penalty = calc["penalty"]
    result = {**base, "status": "answered", "category": "underpayment_penalty_regular",
              "amount": current_year_income, "tax": penalty["penalty"],
              "citation": income_brackets.UNDERPAYMENT_REGULAR_CITATION,
              "source_url": income_brackets.UNDERPAYMENT_SOURCE_URL}
    common_facts = (
        f"With ${current_year_income:,.2f} in California income (current-year tax "
        f"${calc['current_year_tax']:,.2f}), filing status {label}, ${calc['prior_year_tax']:,.2f} "
        f"in prior-year CA tax, ${calc['prior_year_agi']:,.2f} in prior-year CA AGI, "
        f"${calc['withholding']:,.2f} in California withholding, and ${sum(buckets):,.2f} in "
        f"estimated payments (Q1 ${buckets[0]:,.2f}, Q2 ${buckets[1]:,.2f}, Q3 ${buckets[2]:,.2f}, "
        f"Q4 ${buckets[3]:,.2f}, bucketed by the date each was actually paid)"
    )
    if penalty["reason"] == "de_minimis_balance":
        result["answer_text"] = (
            f"{common_facts}: you do NOT owe an Underpayment of Estimated Tax Penalty -- your "
            "balance due (current-year tax minus withholding) is under the $500 ($250 if married "
            f"filing separately) de minimis threshold ({income_brackets.UNDERPAYMENT_REGULAR_CITATION})."
        )
        return result
    if penalty["reason"] == "zero_prior_year_liability":
        result["answer_text"] = (
            f"{common_facts}: you do NOT owe an Underpayment of Estimated Tax Penalty -- you had "
            f"no California tax liability last year ({income_brackets.UNDERPAYMENT_REGULAR_CITATION})."
        )
        return result
    if penalty["reason"] == "safe_harbor_met":
        result["answer_text"] = (
            f"{common_facts}: you do NOT owe an Underpayment of Estimated Tax Penalty -- your "
            f"withholding alone meets the required annual payment safe harbor "
            f"(${penalty['required_annual_payment']:,.2f}, the LESSER of 90% of your current-year "
            f"tax or 100%/110% of your prior-year tax) ({income_brackets.UNDERPAYMENT_REGULAR_CITATION})."
        )
        return result
    if penalty["reason"] == "no_underpayment":
        result["answer_text"] = (
            f"{common_facts}: you do NOT owe an Underpayment of Estimated Tax Penalty -- your "
            "quarterly payments (plus withholding) covered each required installment on time "
            f"({income_brackets.UNDERPAYMENT_REGULAR_CITATION})."
        )
        return result
    col_lines = "; ".join(
        f"Q{i+1}: ${c['underpayment']:,.2f} underpaid, ${c['penalty']:,.2f} penalty"
        for i, c in enumerate(penalty["columns"]) if c["underpayment"] > 0
    )
    result["answer_text"] = (
        f"{common_facts}: your estimated Underpayment of Estimated Tax Penalty is "
        f"${penalty['penalty']:,.2f}, computed using FTB's Regular Method (Worksheet II) against "
        f"a required annual payment of ${penalty['required_annual_payment']:,.2f} (the LESSER of "
        f"90% of your current-year tax or 100%/110% of your prior-year tax). Per-quarter "
        f"breakdown: {col_lines} ({income_brackets.UNDERPAYMENT_REGULAR_CITATION}). This assumes "
        "calendar-year filing, withholding spread evenly across all 4 quarters (not an actual-"
        "date election), and that any underpaid quarter is resolved on the DUE DATE of the next "
        "quarter whose payment covers it (a conservative assumption -- if that later payment "
        "actually arrived earlier, your real penalty may be slightly lower). Doesn't model the "
        "annualized income installment method. Your actual liability may differ."
    )
    return result


def _income_underpayment_regular_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_underpayment_regular_method_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Underpayment of Estimated Tax Penalty using FTB's Regular Method, I "
        "need your filing status: single, married filing jointly, married filing separately, "
        "head of household, or qualifying surviving spouse -- plus the facts described in the "
        "payment template (ask again and I'll show it)."
    )
    return result


def _income_underpayment_regular_template_answer(question: str, base: dict):
    """Fallback: the Regular Method's own vocabulary is recognized (and
    a filing status is stated) but full extraction failed -- no dates
    found, a fact missing, or a payment couldn't be paired with a date.
    Teaches the required template rather than guessing at what's
    missing, same "give the user a template to fill in" pattern that
    unblocked the kiddie-tax build."""
    if not income_brackets.detect_underpayment_regular_method_signal(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Underpayment of Estimated Tax Penalty using FTB's Regular Method, "
        "please restate your question with: your filing status; your California income ('my "
        "income is $X'); your prior-year CA tax ('my prior year tax was $X'); your prior-year CA "
        "AGI ('my prior year AGI was $X'); your California withholding ('my withholding was $X'); "
        "and EACH estimated payment you made, one at a time, as '$AMOUNT on MM/DD/YYYY' -- for "
        "example: 'I paid $1,000 on 5/1/2025 and $2,000 on 9/20/2025.' List every payment with "
        "its own exact date. If you made no estimated payments beyond withholding, ask without "
        "mentioning any estimated payments and I'll use the simpler Short Method instead."
    )
    return result


def _income_self_employment_answer(conn, question: str, amount, base: dict):
    """Sole-proprietor Schedule C self-employment tax computation -- see
    income_brackets.compute_self_employment_ca_tax's docstring for the R&TC
    17072(a)/IRC 62 conformity basis. The SIMPLEST self-employment case only
    (income_brackets.SE_COMPLEXITY_EXCLUDE): one sole proprietorship, no
    other income mixed in, no itemizing -- everything more complex still
    defers, same discipline as _income_compute_answer. `amount` is treated
    as net profit (revenue minus business expenses), not gross revenue or
    federal AGI -- deliberately, since computing from net profit directly
    sidesteps California's QBI non-conformity entirely (see the compute
    function's docstring) rather than requiring an addback."""
    fs = income_brackets.detect_self_employment_signal(question)
    if not fs or amount is None:
        return None
    calc = income_brackets.compute_self_employment_ca_tax(conn, amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "self_employment_income_tax",
              "amount": amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in net self-employment profit (after business "
        f"expenses), filing status {label}, and the standard deduction "
        f"(${calc['standard_deduction']:,.0f}): your self-employment tax is about "
        f"${calc['se_tax']:,.2f}, half of which (${calc['half_se_deduction']:,.2f}) is "
        f"deductible when computing California adjusted gross income "
        f"({income_brackets.SE_CITATION}). Your California taxable income is about "
        f"${calc['taxable_income']:,.2f}, your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes self-employment income is your ONLY income source, "
        "with no other adjustments, credits, or itemized deductions -- your actual "
        "liability may differ."
    )
    return result


def _income_self_employment_missing_filing_status_answer(question: str, amount, base: dict):
    """Mirrors _income_missing_filing_status_answer for the self-employment
    path -- same reasoning: filing status changes which bracket table
    applies, so it's still not safe to guess."""
    if amount is None or not income_brackets.detect_self_employment_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California self-employment income tax, I need your "
        "filing status: single, married filing jointly, married filing "
        "separately, head of household, or qualifying surviving spouse. "
        "Please ask again and include it (for example, \"...filing single\" "
        "or \"...as head of household\").")
    return result


def _income_mixed_wage_se_answer(conn, question: str, base: dict):
    """First multi-amount compute path: 'wages AND self-employment income'
    in one question. Uses _amount_near (not the single-shot _amount) to
    pull out the wage-tagged and SE-tagged dollar figures SEPARATELY --
    see income_brackets.detect_mixed_wage_se_signal's docstring for why
    this can never collide with either single-source path."""
    fs = income_brackets.detect_mixed_wage_se_signal(question)
    if not fs:
        return None
    wage_amount = _amount_near(question, income_brackets.WAGE_CONTEXT_TERMS)
    se_amount = _amount_near(question, income_brackets.SE_TRIGGERS)
    if wage_amount is None or se_amount is None:
        return None
    calc = income_brackets.compute_mixed_wage_se_ca_tax(conn, wage_amount, se_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "self_employment_income_tax",
              "amount": wage_amount + se_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${wage_amount:,.2f} in wage income, ${se_amount:,.2f} in net "
        f"self-employment profit (after business expenses), filing status {label}, "
        f"and the standard deduction (${calc['standard_deduction']:,.0f}): your "
        f"self-employment tax is about ${calc['se_tax']:,.2f} (only on the "
        f"self-employment portion), half of which (${calc['half_se_deduction']:,.2f}) "
        f"is deductible when computing California adjusted gross income "
        f"({income_brackets.SE_CITATION}). Your California taxable income is about "
        f"${calc['taxable_income']:,.2f}, your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes wages and self-employment income are your ONLY "
        "income sources, with no other adjustments, credits, or itemized deductions "
        "-- your actual liability may differ."
    )
    return result


def _income_mixed_wage_se_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_missing_filing_status_answer for the mixed
    wages+self-employment path."""
    if not income_brackets.detect_mixed_wage_se_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax on wages plus self-employment "
        "income, I need your filing status: single, married filing jointly, "
        "married filing separately, head of household, or qualifying surviving "
        "spouse. Please ask again and include it (for example, \"...filing "
        "single\" or \"...as head of household\").")
    return result


def _tagged_amount(question: str, terms, claimed: set):
    """_amount_near, skipped if it collides with a figure another anchor
    phrase already claimed in this same question (same number matched two
    different tags -- ambiguous, so don't double-count it)."""
    amt = _amount_near(question, terms)
    return None if amt in claimed else amt


def _income_itemized_answer(conn, question: str, base: dict):
    """Wage income + a stated itemized-deduction total -- see
    income_brackets.compute_itemized_ca_tax's docstring for the Line 29/30
    conformity basis (greater-of comparison, AGI-limitation PHASE-OUT
    worksheet, MFS exclusion). Uses _amount_near (not _amount) to pull the
    itemized-tagged figure (and, optionally, SALT/mortgage-addback/misc-
    itemized/charitable/SALT-cap-addback/casualty-loss-tagged figures --
    see _tagged_amount) out separately from the income figure, same
    distance-based approach as the mixed wage+SE path; if more than one
    unaccounted-for amount remains, the question is ambiguous and this
    defers rather than guessing which one is income. All 6 optional
    figures are additive -- see income_brackets.SALT_TERMS /
    MORTGAGE_INTEREST_ADDBACK_TERMS / MISC_ITEMIZED_TERMS /
    CHARITABLE_TERMS / SALT_CAP_ADDBACK_TERMS / CASUALTY_LOSS_TERMS. Each
    of the (up to) 8 figures has its OWN distinct, non-overlapping anchor
    phrase, unlike FYTC's shared-anchor collision earlier this session,
    so the same exclude-based extraction scales safely."""
    fs = income_brackets.detect_itemized_signal(question)
    if not fs:
        return None
    itemized_amount = _amount_near(question, income_brackets.ITEMIZED_TERMS)
    if itemized_amount is None:
        return None
    claimed = {itemized_amount}
    salt_amount = _tagged_amount(question, income_brackets.SALT_TERMS, claimed)
    if salt_amount is not None:
        claimed.add(salt_amount)
    mortgage_addback = _tagged_amount(question, income_brackets.MORTGAGE_INTEREST_ADDBACK_TERMS, claimed)
    if mortgage_addback is not None:
        claimed.add(mortgage_addback)
    misc_expenses = _tagged_amount(question, income_brackets.MISC_ITEMIZED_TERMS, claimed)
    if misc_expenses is not None:
        claimed.add(misc_expenses)
    charitable_amount = _tagged_amount(question, income_brackets.CHARITABLE_TERMS, claimed)
    if charitable_amount is not None:
        claimed.add(charitable_amount)
    salt_cap_addback = _tagged_amount(question, income_brackets.SALT_CAP_ADDBACK_TERMS, claimed)
    if salt_cap_addback is not None:
        claimed.add(salt_cap_addback)
    casualty_loss_amount = _tagged_amount(question, income_brackets.CASUALTY_LOSS_TERMS, claimed)
    if casualty_loss_amount is not None:
        claimed.add(casualty_loss_amount)
    others = [a for a, _, _ in _amounts(question) if a not in claimed]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_itemized_ca_tax(
        conn, income_amount, itemized_amount, fs, salt_amount=salt_amount,
        mortgage_interest_addback=mortgage_addback, misc_itemized_expenses=misc_expenses,
        charitable_amount=charitable_amount, salt_cap_addback=salt_cap_addback,
        casualty_loss_amount=casualty_loss_amount)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    salt_note = ""
    if salt_amount is not None:
        salt_note = (f" California does not allow a deduction for state/local income tax, SDI, "
                     f"or general sales tax, so your stated ${salt_amount:,.2f} was subtracted "
                     f"from your itemized total, leaving ${calc['ca_itemized_amount']:,.2f} "
                     f"before the standard-vs-itemized comparison (Schedule CA (540) Line 5a).")
    mortgage_note = ""
    if mortgage_addback is not None:
        mortgage_note = (
            f" California allows mortgage interest deductions federal law disallowed -- "
            f"either because your acquisition debt is between the federal $750,000/$375,000-MFS "
            f"cap and California's higher $1,000,000/$500,000-MFS cap, or because it's home "
            f"equity indebtedness interest federal law suspended -- so your stated "
            f"${mortgage_addback:,.2f} in disallowed interest was added BACK to your itemized "
            f"total (Schedule CA (540) Line 8).")
    misc_note = ""
    if misc_expenses is not None:
        floor = income_amount * income_brackets.MISC_ITEMIZED_FLOOR_RATE
        misc_note = (
            f" California reinstates the miscellaneous itemized deduction category "
            f"(unreimbursed employee expenses, tax preparation fees, and similar) that federal "
            f"law suspended, subject to the same 2%-of-AGI floor that applied before the federal "
            f"suspension: of your stated ${misc_expenses:,.2f}, ${floor:,.2f} (2% of your AGI) "
            f"is not deductible, leaving ${calc['misc_reinstated']:,.2f} added to your itemized "
            f"total (Schedule CA (540) Lines 19-22).")
    charitable_note = ""
    if charitable_amount is not None:
        cap = income_amount * income_brackets.CHARITABLE_AGI_CAP_RATE
        if calc["charitable_disallowed"] > 0:
            charitable_note = (
                f" California caps the charitable contribution deduction at 50% of AGI "
                f"(${cap:,.2f} here), lower than federal's own limit -- of your stated "
                f"${charitable_amount:,.2f} in charitable contributions, "
                f"${calc['charitable_disallowed']:,.2f} exceeds California's cap and was "
                f"subtracted from your itemized total (Schedule CA (540) Lines 11-12).")
        else:
            charitable_note = (
                f" Your stated ${charitable_amount:,.2f} in charitable contributions is under "
                f"California's 50%-of-AGI cap (${cap:,.2f} here), so no adjustment was needed.")
    salt_cap_note = ""
    if salt_cap_addback is not None:
        salt_cap_note = (
            f" The federal deduction for state and local tax (income tax plus property tax "
            f"combined) is capped at $40,000 ($20,000 if married filing separately); California "
            f"does not conform to that cap, so your stated ${salt_cap_addback:,.2f} that was cut "
            f"off by the federal limit was added BACK to your itemized total (Schedule CA (540) "
            f"Line 5e).")
    casualty_note = ""
    if casualty_loss_amount is not None:
        floor = income_amount * income_brackets.CASUALTY_LOSS_AGI_FLOOR_RATE
        if calc["casualty_deductible"] > 0:
            casualty_note = (
                f" Federal law only allows a personal casualty/theft loss deduction for losses "
                f"in a federally declared disaster area; California does not conform and allows "
                f"the deduction regardless. Of your stated ${casualty_loss_amount:,.2f} loss "
                f"(already net of the federal $100-per-event floor and any insurance "
                f"reimbursement), ${floor:,.2f} (10% of your AGI) is not deductible, leaving "
                f"${calc['casualty_deductible']:,.2f} added to your itemized total (Schedule CA "
                f"(540) Line 15).")
        else:
            casualty_note = (
                f" Your stated ${casualty_loss_amount:,.2f} casualty/theft loss does not exceed "
                f"the 10%-of-AGI floor (${floor:,.2f} here), so no deduction applies.")
    phaseout_note = ""
    if calc["phaseout"]:
        phaseout_note = (
            f" Because your income exceeds California's itemized-deduction limitation "
            f"threshold (${calc['phaseout']['threshold']:,.0f} for your filing status), your "
            f"itemized deductions were reduced by ${calc['phaseout']['reduction']:,.2f} under "
            f"the Schedule CA (540) Line 29 worksheet (the smaller of 80% of your itemized "
            f"total or 6% of income over the threshold), leaving "
            f"${calc['ca_itemized_amount']:,.2f}. This assumes your full itemized total is "
            f"subject to the reduction -- medical expenses, investment interest, casualty "
            f"losses, and gambling losses are excluded from the reduction and would make your "
            f"actual reduction slightly smaller (a lower tax) if you have any of those.")
    if calc["used_itemized"]:
        dedu_note = (f"your itemized deductions (${calc['ca_itemized_amount']:,.2f} after any "
                     f"California adjustments), which are larger than the "
                     f"{income_brackets.DEFAULT_TAX_YEAR} standard deduction "
                     f"(${calc['standard_deduction']:,.0f})")
    else:
        dedu_note = (f"the standard deduction (${calc['standard_deduction']:,.0f}), since your "
                     f"itemized deductions (${calc['ca_itemized_amount']:,.2f} after any "
                     "California adjustments) are smaller and California uses whichever is larger")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in gross wage income (also your California AGI, "
        f"with no other adjustments), filing status {label}, and {dedu_note}: your California "
        f"taxable income is about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket "
        f"is {calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note}{salt_note}{mortgage_note}{misc_note}{charitable_note}{salt_cap_note}"
        f"{casualty_note}{phaseout_note} This assumes your stated itemized-deduction "
        "total otherwise already reflects California's rules -- your actual liability may differ."
    )
    return result


def _income_itemized_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_missing_filing_status_answer for the itemized-
    deduction path."""
    if not income_brackets.detect_itemized_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with itemized deductions, I need your "
        "filing status: single, married filing jointly, head of household, or qualifying "
        "surviving spouse. (Married/RDP filing separately has an additional rule this "
        "assistant doesn't yet handle -- see below.) Please ask again and include your "
        "filing status.")
    return result


def _income_itemized_mfs_answer(question: str, base: dict):
    """California requires both spouses/RDPs to itemize (or both to take
    the standard deduction) when filing separately -- if one spouse
    itemizes, the OTHER must too, even if their own itemized total is
    smaller than the standard deduction. This assistant has no way to know
    the other spouse's choice, so it defers with a specific explanation
    rather than silently assuming the greater-of rule that applies to
    every other filing status."""
    if not income_brackets.detect_itemized_mfs_unsupported(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "California has a special rule for married/RDP filing separately: if either "
        "spouse itemizes deductions, both spouses must itemize -- even if one spouse's "
        "itemized total is smaller than the standard deduction. Because this depends on "
        "your spouse's/RDP's own return, this assistant doesn't estimate itemized-"
        "deduction tax for married filing separately. Please consult the FTB Schedule CA "
        "(540) instructions or a tax professional."
    )
    return result


def _income_capital_loss_answer(conn, question: str, base: dict):
    """Income + a stated CURRENT-YEAR capital loss -- see
    income_brackets.compute_capital_loss_ca_tax's docstring for the Line 9
    conformity basis ($3,000/$1,500-MFS annual offset limit, same as
    federal IRC Section 1211). Uses _amount_near/the 'one other amount'
    pattern exactly like the itemized-deduction path; deliberately does
    NOT attempt a prior-year carryover (Schedule D (540) Line 6) -- only a
    single current-year loss figure."""
    fs = income_brackets.detect_capital_loss_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.CAPITAL_LOSS_TERMS, amounts)
    if match is None:
        return None
    loss_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_capital_loss_ca_tax(conn, income_amount, loss_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["carryover"]:
        loss_note = (f"${calc['deductible_loss']:,.2f} of your ${loss_amount:,.2f} capital loss "
                     f"(the annual limit for {label}), with the remaining "
                     f"${calc['carryover']:,.2f} carrying forward to next year's California "
                     "return (not reflected in this estimate)")
    else:
        loss_note = f"your full ${loss_amount:,.2f} capital loss (under the annual limit)"
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in gross income (also your California AGI before the "
        f"loss offset, with no other adjustments), filing status {label}, and deducting "
        f"{loss_note} ({income_brackets.CAPITAL_LOSS_CITATION}), plus the standard deduction "
        f"(${calc['standard_deduction']:,.0f}): your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated loss is a CURRENT-YEAR loss with no capital "
        "loss carryover from a prior year -- your actual liability may differ."
    )
    return result


def _income_capital_loss_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_missing_filing_status_answer for the capital-loss
    path."""
    if not income_brackets.detect_capital_loss_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a capital loss, I need your filing "
        "status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse (the annual loss-offset limit is smaller "
        "for married filing separately). Please ask again and include your filing status.")
    return result


def _ebl_carryover_extract_amounts(question: str):
    """Returns (other_income, business_result, carryover_balance,
    is_loss_year) or None if any of the three required dollar figures
    can't be unambiguously extracted. Two proximity anchors (carryover
    balance, then this year's business result via the income/loss-
    specific term set) plus a single leftover amount for other_income --
    the same "N anchors + 1 remainder" pattern as QSBS/excess-business-
    loss's "one other amount" extraction, generalized to three figures
    instead of two."""
    amounts = _amounts(question)
    carryover_match = _amount_near_filtered_span(question, income_brackets.EBL_CARRYOVER_TERMS, amounts)
    if carryover_match is None:
        return None
    carryover_balance = carryover_match[0]
    remaining = _remove_amount_span(amounts, carryover_match)
    is_loss_year = income_brackets.detect_ebl_carryover_is_loss_year(question)
    business_terms = (income_brackets.EBL_CARRYOVER_LOSS_TERMS if is_loss_year
                       else income_brackets.EBL_CARRYOVER_INCOME_TERMS)
    business_match = _amount_near_filtered_span(question, business_terms, remaining)
    if business_match is None:
        return None
    business_result = business_match[0]
    remaining = _remove_amount_span(remaining, business_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    return other_income, business_result, carryover_balance, is_loss_year


def _extract_ebl_carryover_facts_llm(question: str):
    """Income Coverage Blueprint, Phase 2a PILOT -- structured LLM-based
    extraction for the excess-business-loss-carryover feature, intended
    to replace the hand-rolled 2-anchor regex/proximity extraction
    (_ebl_carryover_extract_amounts) with one general typed extractor,
    per the blueprint's Phase 2a scope. Computed in SHADOW MODE only
    (see _income_ebl_carryover_answer below) -- same "compute both,
    compare, let the proven approach keep deciding the live answer"
    precedent already established by _rerank_v2 in the sales domain, not
    a new risk-tolerance decision invented for this pilot.

    Returns (other_income, business_result, carryover_balance,
    is_loss_year) or None on any extraction/parse failure. A failure
    here must never affect the live answer -- the caller only logs a
    mismatch, it never falls back to or overrides the regex result."""
    prompt = (
        "Extract dollar figures from this California income-tax question "
        "about an excess-business-loss carryover from a prior year "
        "(Schedule CA (540) Line 8z). Reply with ONLY a JSON object (no "
        "markdown fences, no other text) with these exact keys:\n"
        '  "other_income": the taxpayer\'s OTHER income (e.g. wages), as a plain number\n'
        '  "business_result": this year\'s business income OR loss amount, as a plain number\n'
        '  "is_loss_year": true if business_result is described as a LOSS this year, false if it is income/profit\n'
        '  "carryover_balance": the stated prior-year excess business loss carryover amount, as a plain number\n'
        "Use null for any key you cannot determine from the question.\n"
        f"QUESTION: {question}"
    )
    try:
        raw = model.generate_content(prompt).text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        other_income = float(data["other_income"])
        business_result = float(data["business_result"])
        carryover_balance = float(data["carryover_balance"])
        is_loss_year = bool(data["is_loss_year"])
    except Exception:
        return None
    return other_income, business_result, carryover_balance, is_loss_year


def _income_ebl_carryover_answer(conn, question: str, base: dict):
    """Other income + this year's business result + a stated prior-year
    excess-business-loss carryover balance -- see
    income_brackets.compute_ebl_carryover_ca_tax's docstring for the
    Schedule CA Line 8z conformity basis and the two modeled cases.
    Checked BEFORE _income_excess_business_loss_answer in the dispatcher
    -- "excess business loss carryover" contains "excess business loss"
    as a substring, so without this ordering the EXISTING Line 8p
    detector would swallow carryover-phrased questions first (same "move
    the check earlier" fix as K-1 capital gain/real-estate-professional's
    dispatcher placement).

    Phase 2a's LLM extractor (_extract_ebl_carryover_facts_llm) is
    PROMOTED TO LIVE as of 2026-08-23, but ONLY as a fallback -- regex/
    proximity extraction (_ebl_carryover_extract_amounts) remains
    PRIMARY and decides the answer whenever it succeeds, unchanged from
    before. The LLM is called ONLY when regex returns None entirely,
    per explicit user sign-off scoped to exactly this mode (not "LLM
    primary" or "LLM only," both considered and declined). This can
    only ADD coverage, never override a regex answer that already
    exists. Promotion was backed by a same-day adversarial test (see
    the income-coverage-blueprint-progress.md memory note): regex fails
    outright (returns None) on reordered-fact phrasing and on anchor-
    phrase-to-value disconnects that a fixed keyword-anchor approach
    can't generalize past -- both hand-verified cases where the LLM
    extracted every fact correctly. `result["extraction_method"]`
    records which path answered, and the LLM-fallback case gets an
    explicit disclosure appended to answer_text, since this is the
    first place in the codebase a probabilistic step decides a live
    financial computation's INPUTS rather than just its wording."""
    fs = income_brackets.detect_ebl_carryover_signal(question)
    if not fs:
        return None
    extracted = _ebl_carryover_extract_amounts(question)
    used_llm_fallback = False
    if extracted is None:
        extracted = _extract_ebl_carryover_facts_llm(question)
        if extracted is None:
            return None
        used_llm_fallback = True
    other_income, business_result, carryover_balance, is_loss_year = extracted

    calc = income_brackets.compute_ebl_carryover_ca_tax(
        conn, other_income, business_result, carryover_balance, is_loss_year, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"],
              "extraction_method": "llm_fallback" if used_llm_fallback else "regex"}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if is_loss_year:
        if calc["new_excess_business_loss"]:
            carryover_note = (
                f"your ${business_result:,.2f} business loss this year combines with your "
                f"${carryover_balance:,.2f} prior-year carryover for a ${calc['combined_loss']:,.2f} "
                f"total, of which ${calc['allowed_loss']:,.2f} is deductible this year (the "
                f"{income_brackets.DEFAULT_TAX_YEAR} excess business loss threshold for {label} is "
                f"${calc['threshold']:,.0f}), with the remaining ${calc['new_excess_business_loss']:,.2f} "
                "carrying forward again as a NEW excess business loss carryover (not reflected in "
                "this estimate)")
        else:
            carryover_note = (
                f"your ${business_result:,.2f} business loss this year combines with your "
                f"${carryover_balance:,.2f} prior-year carryover for a ${calc['combined_loss']:,.2f} "
                f"total, fully deductible this year (under the ${calc['threshold']:,.0f} excess "
                f"business loss threshold for {label}, so the limitation does not apply)")
    else:
        carryover_note = (
            f"your ${business_result:,.2f} in business income this year fully absorbs your "
            f"${carryover_balance:,.2f} prior-year excess business loss carryover, deducted in "
            "full and uncapped (California treats this carryover as separate from an NOL "
            "carryover, so no threshold applies to simply using up an existing carryover)")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (e.g. wages), filing status {label}, and "
        f"{carryover_note} ({income_brackets.EBL_CARRYOVER_CITATION}): your California AGI is "
        f"about ${calc['agi']:,.2f}. After the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated figures are the correct aggregate business "
        "result and carryover balance (not independently re-derived from Form 3461 components) "
        "-- your actual liability may differ."
    )
    if used_llm_fallback:
        result["answer_text"] += (
            " (Your figures were interpreted using AI-assisted extraction, since the wording "
            "didn't match this assistant's standard pattern-matching -- please double-check "
            "that the amounts above match what you intended.)"
        )
    return result


def _income_ebl_carryover_partial_answer(question: str, base: dict):
    """Specific clarifying message for the ONE case Line 8z's own text
    doesn't spell out -- this year's business income is positive but
    LESS than the stated carryover balance (partial absorption). FTB
    defers this to an unverified Form 3461 PDF worksheet (see the module
    note on income_brackets.compute_ebl_carryover_ca_tax) -- rather than
    guess at that arithmetic, this gets its own message instead of a
    silent wrong number or a generic defer."""
    fs = income_brackets.detect_ebl_carryover_signal(question)
    if not fs:
        return None
    extracted = _ebl_carryover_extract_amounts(question)
    if extracted is None:
        return None
    other_income, business_result, carryover_balance, is_loss_year = extracted
    if is_loss_year or business_result >= carryover_balance:
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        f"Your ${business_result:,.2f} in business income this year isn't enough to fully "
        f"offset your ${carryover_balance:,.2f} prior-year excess business loss carryover. "
        "FTB's Schedule CA (540) instructions refer this partial-absorption case to Form FTB "
        "3461's own worksheet (lines 14b-17), which this assistant hasn't independently "
        "verified -- rather than guess at that computation, please consult a tax professional "
        "or FTB Form 3461's instructions directly for this specific case."
    )
    return result


def _income_ebl_carryover_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_ebl_carryover_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with an excess business loss carryover from a "
        "prior year, I need your filing status: single, married filing jointly, married filing "
        "separately, head of household, or qualifying surviving spouse.")
    return result


def _income_excess_business_loss_answer(conn, question: str, base: dict):
    """Other income (e.g. wages) + a stated AGGREGATE net business loss --
    see income_brackets.compute_excess_business_loss_ca_tax's docstring for
    the Form 3461 conformity basis (CA's own continuous $313k/$626k
    threshold, not the current federal version). Uses _amount_near/the
    'one other amount' pattern exactly like the capital-loss path."""
    fs = income_brackets.detect_excess_business_loss_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.EXCESS_BUSINESS_LOSS_TERMS, amounts)
    if match is None:
        return None
    loss_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_excess_business_loss_ca_tax(conn, income_amount, loss_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["excess_business_loss"]:
        loss_note = (f"${calc['allowed_loss']:,.2f} of your ${loss_amount:,.2f} business loss "
                     f"(the {income_brackets.DEFAULT_TAX_YEAR} excess business loss threshold for "
                     f"{label} is ${calc['threshold']:,.0f}), with the remaining "
                     f"${calc['excess_business_loss']:,.2f} carrying forward as an excess "
                     "business loss carryover to next year's California return (not reflected "
                     "in this estimate)")
    else:
        loss_note = (f"your full ${loss_amount:,.2f} business loss (under the "
                     f"${calc['threshold']:,.0f} excess business loss threshold for {label}, so "
                     "the limitation does not apply)")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in other income (also your California AGI before the "
        f"loss offset, with no other adjustments), filing status {label}, and deducting "
        f"{loss_note} ({income_brackets.EXCESS_BUSINESS_LOSS_CITATION}), plus the standard "
        f"deduction (${calc['standard_deduction']:,.0f}): your California taxable income is "
        f"about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated business loss is the correct AGGREGATE net "
        "figure across all your trades/businesses (Schedule C, rental/K-1, farm, etc. combined) "
        "-- your actual liability may differ."
    )
    return result


def _income_excess_business_loss_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_capital_loss_missing_filing_status_answer for the
    excess-business-loss path."""
    if not income_brackets.detect_excess_business_loss_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a business loss, I need your filing "
        "status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse (the excess business loss threshold is "
        "higher for married filing jointly / qualifying surviving spouse). Please ask again "
        "and include your filing status.")
    return result


def _income_nol_answer(conn, question: str, base: dict):
    """Business income + a stated NOL carryover deduction -- see
    income_brackets.compute_nol_ca_tax's docstring for the Form 3805V
    suspension-test basis (CA's own 2024-2026 suspension when net business
    income AND modified AGI are both >=$1,000,000, collapsed to a single
    stated business-income figure under this path's sole-income-source
    assumption). Uses _amount_near/the 'one other amount' pattern exactly
    like the excess-business-loss path."""
    fs = income_brackets.detect_nol_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.NOL_TERMS, amounts)
    if match is None:
        return None
    nol_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    business_income = others[0]
    calc = income_brackets.compute_nol_ca_tax(conn, business_income, nol_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": business_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["suspended"]:
        nol_note = (f"your NOL carryover deduction is SUSPENDED this year because your "
                    f"business income (${business_income:,.2f}) is at or above the "
                    f"{income_brackets.DEFAULT_TAX_YEAR} $1,000,000 suspension threshold -- "
                    f"none of your ${nol_amount:,.2f} carryover is deductible this year, and "
                    "the full amount carries forward (with an extended carryforward period) "
                    "to a later year")
    elif calc["remaining_carryover"]:
        nol_note = (f"${calc['nol_deduction']:,.2f} of your ${nol_amount:,.2f} NOL carryover "
                    "is deductible this year (capped at your Modified Taxable Income of "
                    f"${calc['mti']:,.2f}, not a percentage -- California has no 80%-of-income "
                    "cap like current federal law), with the remaining "
                    f"${calc['remaining_carryover']:,.2f} continuing to carry forward")
    else:
        nol_note = (f"your full ${nol_amount:,.2f} NOL carryover is deductible this year "
                    "(the suspension does not apply, and it's within your Modified Taxable "
                    "Income)")
    result["answer_text"] = (
        f"Assuming ${business_income:,.2f} in business income (treated as your ONLY income "
        f"and both your net business income and modified AGI for the suspension test), filing "
        f"status {label}: {nol_note} ({income_brackets.NOL_CITATION}). After the standard "
        f"deduction (${calc['standard_deduction']:,.0f}), your California taxable income is "
        f"about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes an ordinary business NOL (not a disaster-loss carryover, "
        "which is exempt from suspension regardless of income) and that you have no other "
        "income or adjustments beyond the stated business income -- your actual liability may "
        "differ."
    )
    return result


def _income_nol_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_excess_business_loss_missing_filing_status_answer
    for the NOL-suspension path."""
    if not income_brackets.detect_nol_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with an NOL carryover deduction, I need your "
        "filing status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please ask again and include your filing "
        "status.")
    return result


def _income_nol_wages_answer(conn, question: str, base: dict):
    """NOL carryover for a WAGE-ONLY filer with NO current-year business
    income (Schedule CA (540) Line 8a "wages/other income" population) --
    see income_brackets.compute_nol_wages_ca_tax's docstring for why
    suspension is structurally impossible for this population (net
    business income = $0 can never satisfy the suspension test's
    ">=$1,000,000" business-income leg). Uses the position-safe
    _amount_after_filtered_span/_remove_amount_span pattern from the
    start, not the older value-filtering _amount_near used by the
    sibling compute_nol_ca_tax path."""
    fs = income_brackets.detect_nol_wages_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_after_filtered_span(question, income_brackets.NOL_TERMS, amounts)
    if match is None:
        return None
    nol_amount = match[0]
    remaining = _remove_amount_span(amounts, match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    wages = others[0]
    calc = income_brackets.compute_nol_wages_ca_tax(conn, wages, nol_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": wages, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["remaining_carryover"]:
        nol_note = (f"${calc['nol_deduction']:,.2f} of your ${nol_amount:,.2f} NOL carryover is "
                    f"deductible this year (capped at your Modified Taxable Income of "
                    f"${calc['mti']:,.2f}, not a percentage), with the remaining "
                    f"${calc['remaining_carryover']:,.2f} continuing to carry forward")
    else:
        nol_note = (f"your full ${nol_amount:,.2f} NOL carryover is deductible this year "
                    "(within your Modified Taxable Income)")
    result["answer_text"] = (
        f"Assuming ${wages:,.2f} in wages (treated as your ONLY income and your modified AGI "
        f"for the suspension test), filing status {label}, with no current-year business "
        f"income: {nol_note} ({income_brackets.NOL_CITATION}). Because your net business "
        "income this year is $0, California's 2024-2026 NOL suspension rule (which requires "
        "BOTH net business income AND modified AGI to be at least $1,000,000) can never apply "
        "to you, regardless of your wage level -- your carryover is never suspended. After the "
        f"standard deduction (${calc['standard_deduction']:,.0f}), your California taxable "
        f"income is about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes an ordinary business NOL (not a disaster-loss carryover) "
        "and that you have no other income or adjustments beyond your stated wages -- your "
        "actual liability may differ."
    )
    return result


def _income_nol_wages_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_nol_wages_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with an NOL carryover deduction from a closed "
        "business, I need your filing status: single, married filing jointly, married filing "
        "separately, head of household, or qualifying surviving spouse. Please also state your "
        "wages and your NOL carryover amount.")
    return result


def _income_nol_wages_ambiguous_answer(question: str, base: dict):
    """When NOL vocabulary is present but neither a closed-business
    confirmation nor an ongoing-business signal is stated, ask
    specifically -- whether the business has closed changes whether the
    $1,000,000 suspension test can even apply, so this can't be
    guessed either way."""
    if not income_brackets.detect_nol_wages_ambiguous(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "Do you have any current-year business income, or has that business closed (leaving "
        "you with wages and just the NOL carryover)? This changes whether California's "
        "$1,000,000 NOL suspension test can apply to you. If your business has closed, please "
        "also state your wages, your NOL carryover amount, and your filing status."
    )
    return result


def _amount_near_anchor_edge(question: str, keywords, amounts, window: int = 25):
    """Anchor-boundary-aware amount lookup -- built for the NOL-mixed
    feature after _amount_near_filtered_span picked the WRONG amount
    live: that function measures distance from the anchor's START to
    the amount's MIDPOINT, which for a long anchor phrase ("business
    income") can make an unrelated PRECEDING amount from a different
    clause look numerically closer than the anchor's own value sitting
    right after it ("my business income is $500,000" -- the preceding
    "$100,000, my " connector was shorter than "business income" itself,
    so the wrong figure won under the shared function's metric). This
    measures from the anchor's NEAREST EDGE instead (its end, for a
    following amount; its start, for a preceding one) with a tight
    window -- generically useful (not NOL-specific) for any feature
    whose natural phrasing has the anchor and its value separated by
    only a few connector words ("is"/"of"/"in") in either direction;
    reused by the kiddie-tax feature below for the same reason."""
    ql = question.lower()
    if not amounts:
        return None
    best = None
    for kw in keywords:
        start = 0
        while True:
            idx = ql.find(kw, start)
            if idx == -1:
                break
            end = idx + len(kw)
            for amount, a_start, a_end in amounts:
                if a_start >= end:
                    dist = a_start - end
                elif a_end <= idx:
                    dist = idx - a_end
                else:
                    continue
                if dist <= window and (best is None or dist < best[1]):
                    best = ((amount, a_start, a_end), dist)
            start = idx + len(kw)
    return best[0] if best else None


def _nol_mixed_strip_form_number_phantoms(amounts):
    # "1099" (a common ongoing-business trigger term, e.g. "1099 income")
    # is a bare 4-digit sequence that _amounts()'s regex parses as a
    # phantom dollar amount -- the same collision class found 10+ times
    # this session (280E/QSBS/2555/951-8992/1040/3804/3805/3506/3592/
    # 4797-1231-1245-1250/3885), fixed proactively here before any live
    # test rather than waiting to discover it as a bug.
    return [(a, s, e) for a, s, e in amounts if a != 1099.0]


def _income_nol_mixed_answer(conn, question: str, base: dict):
    """NOL carryover for a filer with BOTH wages/other income AND
    current-year business income -- see
    income_brackets.compute_nol_mixed_ca_tax's docstring. Two anchors
    (business income, NOL carryover) plus wages as the sole remainder,
    same 'N anchors + 1 remainder' pattern as the basis-difference
    features -- but unlike that family (whose phrasing is always
    "X is $Y", anchor before amount), this feature's own family
    (mirroring the sibling compute_nol_ca_tax's established convention)
    is naturally phrased either "$Y in business income" (amount before
    anchor) or "business income is $Y" (anchor before amount) depending
    on sentence order. Uses the boundary-aware
    _amount_near_anchor_edge/_remove_amount_span pattern -- see
    that helper's docstring for why the shared _amount_near_filtered_
    span's start-to-midpoint metric picked the WRONG amount on a
    reordered-facts test (a long anchor phrase can end up numerically
    closer to an unrelated preceding value than to its own)."""
    fs = income_brackets.detect_nol_mixed_signal(question)
    if not fs:
        return None
    amounts = _nol_mixed_strip_form_number_phantoms(_amounts(question))
    nol_match = _amount_near_anchor_edge(question, income_brackets.NOL_TERMS, amounts)
    if nol_match is None:
        return None
    nol_amount = nol_match[0]
    remaining = _remove_amount_span(amounts, nol_match)
    biz_match = _amount_near_anchor_edge(question, income_brackets.NOL_MIXED_BUSINESS_INCOME_TERMS, remaining)
    if biz_match is None:
        return None
    business_income = biz_match[0]
    remaining = _remove_amount_span(remaining, biz_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    wages = others[0]
    calc = income_brackets.compute_nol_mixed_ca_tax(conn, wages, business_income, nol_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": wages, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["suspended"]:
        nol_note = (f"your NOL carryover deduction is SUSPENDED this year because both your "
                    f"business income (${business_income:,.2f}) AND your modified AGI "
                    f"(${calc['modified_agi']:,.2f}, wages plus business income) are at or "
                    f"above the {income_brackets.DEFAULT_TAX_YEAR} $1,000,000 suspension "
                    f"threshold -- none of your ${nol_amount:,.2f} carryover is deductible this "
                    "year, and the full amount carries forward (with an extended carryforward "
                    "period) to a later year")
    elif calc["remaining_carryover"]:
        nol_note = (f"${calc['nol_deduction']:,.2f} of your ${nol_amount:,.2f} NOL carryover "
                    "is deductible this year (capped at your Modified Taxable Income of "
                    f"${calc['mti']:,.2f}, not a percentage -- California has no 80%-of-income "
                    "cap like current federal law), with the remaining "
                    f"${calc['remaining_carryover']:,.2f} continuing to carry forward")
    else:
        nol_note = (f"your full ${nol_amount:,.2f} NOL carryover is deductible this year "
                    "(the suspension does not apply, and it's within your Modified Taxable "
                    "Income)")
    result["answer_text"] = (
        f"With ${wages:,.2f} in wages/other income and ${business_income:,.2f} in current-year "
        f"business income (modified AGI of ${calc['modified_agi']:,.2f} for the suspension "
        f"test), filing status {label}: {nol_note} ({income_brackets.NOL_CITATION}). After the "
        f"standard deduction (${calc['standard_deduction']:,.0f}), your California taxable "
        f"income is about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes an ordinary business NOL (not a disaster-loss carryover, "
        "which is exempt from suspension regardless of income) and that you have no other "
        "income or adjustments beyond your stated wages and business income -- your actual "
        "liability may differ."
    )
    return result


def _income_nol_mixed_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_nol_mixed_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with an NOL carryover deduction alongside "
        "wages and current-year business income, I need your filing status: single, married "
        "filing jointly, married filing separately, head of household, or qualifying surviving "
        "spouse. Please also state your wages/other income and your business income "
        "separately.")
    return result


def _income_disaster_loss_carryover_answer(conn, question: str, base: dict):
    """Other income + a stated California disaster loss carryover
    deduction -- see income_brackets.compute_disaster_loss_carryover_ca_
    tax's docstring for the Schedule CA Line 9b1 basis (a flat "copy this
    cell from your own 3805V" pass-through, no suspension test unlike
    NOL carryover). Uses _amount_near/the 'one other amount' pattern
    exactly like the NOL/excess-business-loss paths."""
    fs = income_brackets.detect_disaster_loss_carryover_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.DISASTER_LOSS_CARRYOVER_TERMS, amounts)
    if match is None:
        return None
    carryover_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_disaster_loss_carryover_ca_tax(conn, income_amount, carryover_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["remaining_carryover"]:
        carryover_note = (
            f"${calc['deduction']:,.2f} of your ${carryover_amount:,.2f} disaster loss "
            "carryover is deductible this year (capped at your Modified Taxable Income of "
            f"${calc['mti']:,.2f}, not a percentage -- and unlike an ordinary NOL carryover, "
            "this deduction is NEVER suspended regardless of your income), with the remaining "
            f"${calc['remaining_carryover']:,.2f} continuing to carry forward")
    else:
        carryover_note = (
            f"your full ${carryover_amount:,.2f} disaster loss carryover is deductible this "
            "year (within your Modified Taxable Income, and never subject to the NOL "
            "suspension rule)")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in other income (e.g. wages), treated as your California "
        f"AGI before this deduction, filing status {label}: {carryover_note} "
        f"({income_brackets.DISASTER_LOSS_CARRYOVER_CITATION}). After the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated figure is the correct disaster loss carryover "
        "amount from your own Form FTB 3805V, Part III, line 2, column (f) (not independently "
        "re-derived from the original casualty-loss facts) and no other adjustments -- your "
        "actual liability may differ."
    )
    return result


def _income_disaster_loss_carryover_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_disaster_loss_carryover_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a disaster loss carryover deduction, I "
        "need your filing status: single, married filing jointly, married filing separately, "
        "head of household, or qualifying surviving spouse.")
    return result


def _income_generic_basis_diff_answer(conn, question: str, base: dict):
    """Generic CA/federal capital-gain basis difference (Schedule CA
    (540) Line 7a) -- see income_brackets.compute_generic_basis_diff_ca_tax's
    docstring for the "trust the stated federal AND California gain
    figures directly" pattern this reuses. Extracts federal gain and CA
    gain as two anchors, remainder is other income -- uses
    _amount_after_filtered_span (forward-only), not the undirected
    variant, since this feature's phrasing is always "X is $Y" (found
    live: with "other income is $80,000" stated right before "federal
    capital gain is $50,000", the undirected nearest-distance version
    picked the PRECEDING $80,000 over the $50,000 the anchor actually
    describes -- the same collision class as OSTC/Underpayment)."""
    fs = income_brackets.detect_generic_basis_diff_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    fed_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_FEDERAL_GAIN_TERMS, amounts)
    if fed_match is None:
        return None
    federal_gain = fed_match[0]
    remaining = _remove_amount_span(amounts, fed_match)
    ca_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_CA_GAIN_TERMS, remaining)
    if ca_match is None:
        return None
    ca_gain = ca_match[0]
    remaining = _remove_amount_span(remaining, ca_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_generic_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    adj_direction = "an addition of" if calc["adjustment"] > 0 else ("a subtraction of" if calc["adjustment"] < 0 else "no change from")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (not including this gain), a "
        f"${federal_gain:,.2f} federal capital gain, and a ${ca_gain:,.2f} California capital "
        f"gain (using your California basis), filing status {label}: your Schedule CA (540) "
        f"Line 7a adjustment is {adj_direction} ${abs(calc['adjustment']):,.2f} "
        f"({income_brackets.GENERIC_BASIS_DIFF_CITATION}). Your California AGI is about "
        f"${calc['agi']:,.2f}. After the standard deduction (${calc['standard_deduction']:,.0f}), "
        f"your California taxable income is about ${calc['taxable_income']:,.2f}. Your marginal "
        f"CA tax bracket is {calc['marginal_rate']*100:g}%, and your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated California capital gain figure is the correct, "
        "already-computed CA-basis result (not independently re-derived from your acquisition/"
        "depreciation history) -- your actual liability may differ."
    )
    return result


def _income_generic_basis_diff_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_generic_basis_diff_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a basis difference on a capital gain, I "
        "need your filing status: single, married filing jointly, married filing separately, "
        "head of household, or qualifying surviving spouse. Please also state your other "
        "income, your federal capital gain, and your California capital gain.")
    return result


def _income_generic_basis_diff_out_of_scope_answer(question: str, base: dict):
    """Specific clarifying message when QSBS/K-1/home-sale/installment-
    sale language is present -- each has its own genuinely different
    mechanic, not just a basis question this simplified path can
    answer."""
    if not income_brackets.detect_generic_basis_diff_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles a generic California/federal capital-gain basis "
        "difference for the common case -- Qualified Small Business Stock (Section 1202/1045), "
        "K-1 capital gains, home/residence sales (Section 121 exclusion), and installment sales "
        "(FTB 3805E) each have their own distinct mechanic beyond a simple basis difference. "
        "Please ask about the specific situation directly, or consult a tax professional."
    )
    return result


def _income_installment_sale_basis_diff_answer(conn, question: str, base: dict):
    """Installment sale gain (FTB 3805E) with a CA/federal basis
    difference -- see income_brackets.compute_installment_sale_basis_diff_ca_tax's
    docstring for why this reuses the generic basis-difference math
    unchanged. Same extraction shape/anchor terms and same forward-only
    span pattern as the generic basis-difference feature."""
    fs = income_brackets.detect_installment_sale_basis_diff_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    fed_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_FEDERAL_GAIN_TERMS, amounts)
    if fed_match is None:
        return None
    federal_gain = fed_match[0]
    remaining = _remove_amount_span(amounts, fed_match)
    ca_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_CA_GAIN_TERMS, remaining)
    if ca_match is None:
        return None
    ca_gain = ca_match[0]
    remaining = _remove_amount_span(remaining, ca_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_installment_sale_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    adj_direction = "an addition of" if calc["adjustment"] > 0 else ("a subtraction of" if calc["adjustment"] < 0 else "no change from")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (not including this gain), a "
        f"${federal_gain:,.2f} federal installment sale gain recognized this year, and a "
        f"${ca_gain:,.2f} California installment sale gain recognized this year (using your "
        f"California basis), filing status {label}: your Schedule CA (540) Line 7a adjustment "
        f"is {adj_direction} ${abs(calc['adjustment']):,.2f} ({income_brackets.INSTALLMENT_SALE_BASIS_DIFF_CITATION}). "
        f"Your California AGI is about ${calc['agi']:,.2f}. After the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated federal and California recognized-gain figures "
        "for THIS YEAR are correct (not independently re-derived from your sale price/basis/"
        "gross-profit-ratio/payments-received) and covers only the current year's recognized "
        "gain, not your full remaining installment schedule -- your actual liability may differ."
    )
    return result


def _income_installment_sale_basis_diff_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_installment_sale_basis_diff_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with an installment sale basis difference, I "
        "need your filing status: single, married filing jointly, married filing separately, "
        "head of household, or qualifying surviving spouse. Please also state your other "
        "income, your federal recognized gain this year, and your California recognized gain "
        "this year.")
    return result


def _income_installment_sale_basis_diff_out_of_scope_answer(question: str, base: dict):
    if not income_brackets.detect_installment_sale_basis_diff_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles installment sale gain with a California/federal basis "
        "difference for the common case -- Qualified Small Business Stock (Section 1202/1045), "
        "K-1 capital gains, and home/residence sales (Section 121 exclusion) each have their "
        "own distinct mechanic. Please ask about the specific situation directly, or consult a "
        "tax professional."
    )
    return result


def _income_home_sale_basis_diff_answer(conn, question: str, base: dict):
    """Gain on personal residence sale where CA/federal depreciation
    diverged -- see income_brackets.compute_home_sale_basis_diff_ca_tax's
    docstring for why this reuses the generic basis-difference math
    unchanged, and for the "already net of Section 121 exclusion"
    assumption. Same extraction shape as the other two basis-difference
    features."""
    fs = income_brackets.detect_home_sale_basis_diff_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    fed_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_FEDERAL_GAIN_TERMS, amounts)
    if fed_match is None:
        return None
    federal_gain = fed_match[0]
    remaining = _remove_amount_span(amounts, fed_match)
    ca_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_CA_GAIN_TERMS, remaining)
    if ca_match is None:
        return None
    ca_gain = ca_match[0]
    remaining = _remove_amount_span(remaining, ca_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_home_sale_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    adj_direction = "an addition of" if calc["adjustment"] > 0 else ("a subtraction of" if calc["adjustment"] < 0 else "no change from")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (not including this gain), a "
        f"${federal_gain:,.2f} federal gain on your home sale, and a ${ca_gain:,.2f} California "
        f"gain on your home sale (both already net of any Section 121 exclusion, using your "
        f"California depreciation basis), filing status {label}: your Schedule CA (540) Line "
        f"7a adjustment is {adj_direction} ${abs(calc['adjustment']):,.2f} "
        f"({income_brackets.HOME_SALE_BASIS_DIFF_CITATION}). Your California AGI is about "
        f"${calc['agi']:,.2f}. After the standard deduction (${calc['standard_deduction']:,.0f}), "
        f"your California taxable income is about ${calc['taxable_income']:,.2f}. Your marginal "
        f"CA tax bracket is {calc['marginal_rate']*100:g}%, and your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated federal and California gain figures already "
        "correctly reflect any Section 121 exclusion and your diverged depreciation basis (not "
        "independently re-derived) -- your actual liability may differ."
    )
    return result


def _income_home_sale_basis_diff_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_home_sale_basis_diff_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a home-sale depreciation basis "
        "difference, I need your filing status: single, married filing jointly, married "
        "filing separately, head of household, or qualifying surviving spouse. Please also "
        "state your other income, your federal gain on the sale, and your California gain on "
        "the sale.")
    return result


def _income_home_sale_basis_diff_out_of_scope_answer(question: str, base: dict):
    if not income_brackets.detect_home_sale_basis_diff_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles a home-sale depreciation basis difference for the common "
        "case -- Qualified Small Business Stock (Section 1202/1045), K-1 capital gains, and "
        "installment sales (FTB 3805E) each have their own distinct mechanic. Please ask about "
        "the specific situation directly, or consult a tax professional."
    )
    return result


def _schedule_d1_strip_form_number_phantoms(amounts):
    """Literal "4797" in "form 4797", or "1231"/"1245"/"1250" in "Section
    1231"/"1245 recapture"/"1250 recapture" (this feature's own trigger
    vocabulary) parse as phantom dollar amounts -- same collision class
    as cannabis 280E/QSBS/Form 2555/CDC credit's "3506"/etc. Local
    filter scoped to this feature."""
    phantoms = {4797.0, 1231.0, 1245.0, 1250.0}
    return [(a, s, e) for a, s, e in amounts if a not in phantoms]


def _income_schedule_d1_basis_diff_answer(conn, question: str, base: dict):
    """Schedule D-1/Form 4797 ordinary business-property GAIN with a CA/
    federal basis difference -- see income_brackets.compute_schedule_d1_basis_diff_ca_tax's
    docstring for why this reuses the generic basis-difference math
    unchanged. GAINS only; loss-flavored language defers (see
    detect_schedule_d1_basis_diff_out_of_scope)."""
    fs = income_brackets.detect_schedule_d1_basis_diff_signal(question)
    if not fs:
        return None
    amounts = _schedule_d1_strip_form_number_phantoms(_amounts(question))
    fed_match = _amount_after_filtered_span(question, income_brackets.SCHEDULE_D1_BASIS_DIFF_FEDERAL_GAIN_TERMS, amounts)
    if fed_match is None:
        return None
    federal_gain = fed_match[0]
    remaining = _remove_amount_span(amounts, fed_match)
    ca_match = _amount_after_filtered_span(question, income_brackets.SCHEDULE_D1_BASIS_DIFF_CA_GAIN_TERMS, remaining)
    if ca_match is None:
        return None
    ca_gain = ca_match[0]
    remaining = _remove_amount_span(remaining, ca_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_schedule_d1_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    adj_direction = "an addition of" if calc["adjustment"] > 0 else ("a subtraction of" if calc["adjustment"] < 0 else "no change from")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (not including this gain), a "
        f"${federal_gain:,.2f} federal Schedule D-1/Form 4797 business-property gain, and a "
        f"${ca_gain:,.2f} California business-property gain (using your California basis), "
        f"filing status {label}: your Schedule CA (540) Line 4 adjustment is {adj_direction} "
        f"${abs(calc['adjustment']):,.2f} ({income_brackets.SCHEDULE_D1_BASIS_DIFF_CITATION}). "
        f"Your California AGI is about ${calc['agi']:,.2f}. After the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated federal and California gain figures are the "
        "correct, already-computed Section 1231/1245/1250 results (not independently re-"
        "derived from your acquisition/depreciation history) -- your actual liability may "
        "differ."
    )
    return result


def _income_schedule_d1_basis_diff_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_schedule_d1_basis_diff_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a Schedule D-1/Form 4797 basis "
        "difference, I need your filing status: single, married filing jointly, married "
        "filing separately, head of household, or qualifying surviving spouse. Please also "
        "state your other income, your federal gain, and your California gain.")
    return result


def _income_schedule_d1_basis_diff_out_of_scope_answer(question: str, base: dict):
    if not income_brackets.detect_schedule_d1_basis_diff_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles Schedule D-1/Form 4797 business-property GAINS with a "
        "California/federal basis difference. A net LOSS on business-property sales isn't "
        "subject to the capital-loss annual limit and has its own distinct mechanic this "
        "assistant doesn't compute. Please consult a tax professional for an accurate figure."
    )
    return result


def _rental_depreciation_strip_form_number_phantoms(amounts):
    """Literal "3885" in "FTB 3885A"/"form 3885A" (this feature's own
    trigger vocabulary) parses as a phantom dollar amount -- same
    collision class as 9+ prior collisions this session. Local filter
    scoped to this feature, added proactively before testing."""
    return [(a, s, e) for a, s, e in amounts if a != 3885.0]


def _income_rental_depreciation_basis_diff_answer(conn, question: str, base: dict):
    """Rental/royalty depreciation basis difference, ordinary (non-real-
    estate-professional) case -- see income_brackets.compute_rental_depreciation_basis_diff_ca_tax's
    docstring for why this reuses the generic basis-difference math
    unchanged. INCOME (gains) only; loss/real-estate-professional
    language defers (see detect_rental_depreciation_basis_diff_out_of_scope)."""
    fs = income_brackets.detect_rental_depreciation_basis_diff_signal(question)
    if not fs:
        return None
    amounts = _rental_depreciation_strip_form_number_phantoms(_amounts(question))
    fed_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_FEDERAL_GAIN_TERMS, amounts)
    if fed_match is None:
        return None
    federal_gain = fed_match[0]
    remaining = _remove_amount_span(amounts, fed_match)
    ca_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_CA_GAIN_TERMS, remaining)
    if ca_match is None:
        return None
    ca_gain = ca_match[0]
    remaining = _remove_amount_span(remaining, ca_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_rental_depreciation_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    adj_direction = "an addition of" if calc["adjustment"] > 0 else ("a subtraction of" if calc["adjustment"] < 0 else "no change from")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (not including this rental/royalty "
        f"income), ${federal_gain:,.2f} in federal net rental/royalty income, and "
        f"${ca_gain:,.2f} in California net rental/royalty income (using your California "
        f"depreciation basis), filing status {label}: your Schedule CA (540) Line 5 adjustment "
        f"is {adj_direction} ${abs(calc['adjustment']):,.2f} "
        f"({income_brackets.RENTAL_DEPRECIATION_BASIS_DIFF_CITATION}). Your California AGI is "
        f"about ${calc['agi']:,.2f}. After the standard deduction (${calc['standard_deduction']:,.0f}), "
        f"your California taxable income is about ${calc['taxable_income']:,.2f}. Your marginal "
        f"CA tax bracket is {calc['marginal_rate']*100:g}%, and your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated federal and California figures already "
        "correctly reflect each jurisdiction's own (otherwise-identical) passive-activity-loss "
        "limitation, and that this is NET INCOME, not a loss -- your actual liability may "
        "differ."
    )
    return result


def _income_rental_depreciation_basis_diff_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_rental_depreciation_basis_diff_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a rental/royalty depreciation basis "
        "difference, I need your filing status: single, married filing jointly, married "
        "filing separately, head of household, or qualifying surviving spouse. Please also "
        "state your other income, your federal net rental/royalty income, and your California "
        "net rental/royalty income.")
    return result


def _income_rental_depreciation_basis_diff_out_of_scope_answer(question: str, base: dict):
    if not income_brackets.detect_rental_depreciation_basis_diff_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles rental/royalty depreciation basis differences for a "
        "PROFITABLE (net income) ordinary activity. A net LOSS needs either the real estate "
        "professional allowance or the passive-activity-loss limitation, and real estate "
        "professional status has its own dedicated calculation -- please ask about your "
        "specific situation directly, or consult a tax professional."
    )
    return result


def _income_farm_depreciation_basis_diff_answer(conn, question: str, base: dict):
    """Farm income (Schedule F) depreciation basis difference -- see
    income_brackets.compute_farm_depreciation_basis_diff_ca_tax's
    docstring for why this reuses the generic basis-difference math
    unchanged. INCOME (gains) only; loss language defers."""
    fs = income_brackets.detect_farm_depreciation_basis_diff_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    fed_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_FEDERAL_GAIN_TERMS, amounts)
    if fed_match is None:
        return None
    federal_gain = fed_match[0]
    remaining = _remove_amount_span(amounts, fed_match)
    ca_match = _amount_after_filtered_span(question, income_brackets.GENERIC_BASIS_DIFF_CA_GAIN_TERMS, remaining)
    if ca_match is None:
        return None
    ca_gain = ca_match[0]
    remaining = _remove_amount_span(remaining, ca_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_farm_depreciation_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    adj_direction = "an addition of" if calc["adjustment"] > 0 else ("a subtraction of" if calc["adjustment"] < 0 else "no change from")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (not including this farm income), "
        f"${federal_gain:,.2f} in federal net farm income, and ${ca_gain:,.2f} in California "
        f"net farm income (using your California depreciation basis), filing status {label}: "
        f"your Schedule CA (540) Line 6 adjustment is {adj_direction} "
        f"${abs(calc['adjustment']):,.2f} ({income_brackets.FARM_DEPRECIATION_BASIS_DIFF_CITATION}). "
        f"Your California AGI is about ${calc['agi']:,.2f}. After the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated federal and California figures are the "
        "correct, already-computed net farm income results, and that this is NET INCOME, not "
        "a loss -- your actual liability may differ."
    )
    return result


def _income_farm_depreciation_basis_diff_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_farm_depreciation_basis_diff_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a farm income depreciation basis "
        "difference, I need your filing status: single, married filing jointly, married "
        "filing separately, head of household, or qualifying surviving spouse. Please also "
        "state your other income, your federal net farm income, and your California net farm "
        "income.")
    return result


def _income_farm_depreciation_basis_diff_out_of_scope_answer(question: str, base: dict):
    if not income_brackets.detect_farm_depreciation_basis_diff_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles farm income depreciation basis differences for a "
        "PROFITABLE (net income) activity. A net farm LOSS has its own distinct mechanic this "
        "assistant doesn't compute. Please consult a tax professional for an accurate figure."
    )
    return result


def _income_ira_distribution_basis_diff_answer(conn, question: str, base: dict):
    """IRA distribution basis/timing difference -- see
    income_brackets.compute_ira_distribution_basis_diff_ca_tax's
    docstring for why this reuses the generic basis-difference math
    unchanged. Roth/early-distribution language defers."""
    fs = income_brackets.detect_ira_distribution_basis_diff_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    fed_match = _amount_after_filtered_span(question, income_brackets.IRA_DISTRIBUTION_BASIS_DIFF_FEDERAL_TERMS, amounts)
    if fed_match is None:
        return None
    federal_distribution = fed_match[0]
    remaining = _remove_amount_span(amounts, fed_match)
    ca_match = _amount_after_filtered_span(question, income_brackets.IRA_DISTRIBUTION_BASIS_DIFF_CA_TERMS, remaining)
    if ca_match is None:
        return None
    ca_distribution = ca_match[0]
    remaining = _remove_amount_span(remaining, ca_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_ira_distribution_basis_diff_ca_tax(conn, other_income, federal_distribution, ca_distribution, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    adj_direction = "an addition of" if calc["adjustment"] > 0 else ("a subtraction of" if calc["adjustment"] < 0 else "no change from")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (not including this distribution), a "
        f"${federal_distribution:,.2f} federal taxable IRA distribution, and a "
        f"${ca_distribution:,.2f} California taxable IRA distribution (using your California "
        f"contribution basis), filing status {label}: your Schedule CA (540) Line 4a/4b "
        f"adjustment is {adj_direction} ${abs(calc['adjustment']):,.2f} "
        f"({income_brackets.IRA_DISTRIBUTION_BASIS_DIFF_CITATION}). Your California AGI is "
        f"about ${calc['agi']:,.2f}. After the standard deduction (${calc['standard_deduction']:,.0f}), "
        f"your California taxable income is about ${calc['taxable_income']:,.2f}. Your marginal "
        f"CA tax bracket is {calc['marginal_rate']*100:g}%, and your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated federal and California taxable-distribution "
        "figures are correct (typically derived via FTB Publication 1005's own worksheet, not "
        "independently re-derived here) -- your actual liability may differ."
    )
    return result


def _income_ira_distribution_basis_diff_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_ira_distribution_basis_diff_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with an IRA distribution basis difference, I "
        "need your filing status: single, married filing jointly, married filing separately, "
        "head of household, or qualifying surviving spouse. Please also state your other "
        "income, your federal taxable distribution, and your California taxable distribution.")
    return result


def _income_ira_distribution_basis_diff_out_of_scope_answer(question: str, base: dict):
    if not income_brackets.detect_ira_distribution_basis_diff_out_of_scope(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "This assistant only handles a basic IRA distribution basis difference. Roth IRA "
        "conversions and early-distribution additional tax each have their own distinct "
        "mechanic -- please ask about the specific situation directly, or consult a tax "
        "professional."
    )
    return result


def _cannabis_strip_280e_phantom_amounts(question: str, amounts):
    """_amounts()'s shared regex (an optional dollar sign, then digits)
    has no letter-suffix guard -- by design, so it still cleanly matches
    plain figures like "50000" with no dollar sign -- but that means
    literal "280E" in
    question text (this feature's OWN natural vocabulary, since it's the
    IRC section this whole feature is about) gets parsed as a phantom
    $280.00 amount, which then wins the nearest-amount search against
    itself (distance ~0), corrupting both the expense-amount extraction
    and the 'exactly one other amount' check. Filters out any amount
    matching exactly 280.0 immediately followed by 'e'/'E' with no
    decimal/digit continuation in the ORIGINAL question text -- narrow
    enough that a genuine "$280" business expense (space before the next
    word, or an explicit ".00") is never caught by this filter. Scoped
    locally to this one feature rather than changing the shared
    _amounts()/_amount_near() regex that 20+ other compute paths and
    200+ passing regression cases already depend on unchanged."""
    out = []
    for amount, start, end in amounts:
        if amount == 280.0 and question[end:end + 1].lower() == "e":
            continue
        out.append((amount, start, end))
    return out


def _amount_near_filtered_span(question: str, keywords, amounts, window: int = 60):
    """Nearest-keyword-distance amount lookup, operating on a caller-
    supplied (already phantom-filtered) amounts list instead of calling
    _amounts() internally -- shared by any feature whose own trigger
    vocabulary contains bare digits that _amounts()'s regex would
    otherwise misparse as a dollar amount (see
    _cannabis_strip_280e_phantom_amounts's "280E" case and
    _qsbs_strip_section_number_phantoms's "Section 1202/1045" case).
    Returns the full (amount, start, end) tuple, not just the amount --
    lets the caller remove EXACTLY the matched tuple from a shared
    amounts list by its own position, not by value. Removing "by value"
    (a plain `a != matched_value` filter -- the ORIGINAL pattern used by
    every "N anchors + 1 remainder" multi-figure feature until this was
    found) silently breaks whenever two DIFFERENT stated facts happen to
    share the same dollar figure: found live in the Underpayment-of-
    Estimated-Tax-Penalty build ("prior year tax was $15,000 ...
    withholding was $15,000" in one question) -- filtering by value
    either strips BOTH occurrences (if using `!=`) or, with a naive
    "remove the first list-order occurrence" attempt, can strip the
    WRONG one when the anchor actually matched a LATER-positioned
    duplicate. Every multi-figure feature in this codebase was migrated
    to this span-returning family + _remove_amount_span after that
    discovery."""
    ql = question.lower()
    if not amounts:
        return None
    best = None
    for kw in keywords:
        start = 0
        while True:
            idx = ql.find(kw, start)
            if idx == -1:
                break
            for amount, a_start, a_end in amounts:
                dist = abs((a_start + a_end) / 2 - idx)
                if dist <= window and (best is None or dist < best[1]):
                    best = ((amount, a_start, a_end), dist)
            start = idx + len(kw)
    return best[0] if best else None


def _remove_amount_span(amounts, span):
    """Removes the tuple matching `span` (an (amount, start, end) triple,
    or None -- a no-op) from `amounts` BY POSITION, not by value -- see
    _amount_near_filtered_span's docstring for why value-based removal
    is unsafe whenever two different stated facts share a dollar figure."""
    if span is None:
        return amounts
    _, s, e = span
    return [(a, a_s, a_e) for a, a_s, a_e in amounts if (a_s, a_e) != (s, e)]


def _income_cannabis_280e_answer(conn, question: str, base: dict):
    """Licensed commercial cannabis business net profit + a stated 280E-
    disallowed-expense restoration -- see income_brackets.
    compute_self_employment_ca_tax's cannabis_280e_expenses docstring for
    the R&TC 17209 conformity basis (reuses the self-employment compute
    path, not a parallel one). Uses the 'one other amount' pattern exactly
    like the excess-business-loss/NOL paths, but via the phantom-filtered
    amount helpers above instead of _amounts()/_amount_near() directly."""
    fs = income_brackets.detect_cannabis_280e_signal(question)
    if not fs:
        return None
    amounts = _cannabis_strip_280e_phantom_amounts(question, _amounts(question))
    expense_match = _amount_near_filtered_span(question, income_brackets.CANNABIS_280E_EXPENSE_TERMS, amounts)
    if expense_match is None:
        return None
    expense_amount = expense_match[0]
    remaining = _remove_amount_span(amounts, expense_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    net_profit = others[0]
    calc = income_brackets.compute_self_employment_ca_tax(
        conn, net_profit, fs, cannabis_280e_expenses=expense_amount)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "self_employment_income_tax",
              "amount": net_profit, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${net_profit:,.2f} in federal Schedule C net profit from a LICENSED "
        f"commercial cannabis business (MAUCRSA/DCC), filing status {label}: federal self-"
        f"employment tax is ${calc['se_tax']:,.2f} (${calc['half_se_deduction']:,.2f} "
        f"deductible). California does not conform to federal IRC Section 280E for licensed "
        f"cannabis businesses, so your ${expense_amount:,.2f} in federally-disallowed ordinary "
        f"business expenses is restored as a deduction against California income "
        f"({income_brackets.CANNABIS_280E_CITATION}), giving California AGI of "
        f"${calc['agi']:,.2f}. After the standard deduction (${calc['standard_deduction']:,.0f}), "
        f"your California taxable income is about ${calc['taxable_income']:,.2f}. Your marginal "
        f"CA tax bracket is {calc['marginal_rate']*100:g}%, and your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your business is genuinely MAUCRSA/DCC-licensed (an "
        "unlicensed cannabis business gets no restoration -- federal 280E would fully apply "
        "for California too) and that self-employment income is your only income -- your "
        "actual liability may differ."
    )
    return result


def _income_cannabis_280e_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_self_employment_missing_filing_status_answer for
    the cannabis-280E path."""
    if not income_brackets.detect_cannabis_280e_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax for a licensed cannabis business, I need your "
        "filing status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please ask again and include your filing "
        "status.")
    return result


def _income_cannabis_280e_ambiguous_amount_answer(question: str, base: dict):
    """Catches the remaining gap between _income_cannabis_280e_answer and
    _income_cannabis_280e_missing_filing_status_answer: a filing status
    IS present (so the missing-fs message above doesn't fire) but the
    280E-disallowed-expense figure couldn't be extracted as a SEPARATE
    amount from net profit (e.g. only one dollar figure stated at all) --
    _income_cannabis_280e_answer already tried and returned None by the
    time this runs. Without this, the question falls through the income
    dispatcher entirely and back to sales-tax routing, which (per the
    cross-domain collision this feature exposed -- see _answer()'s
    cannabis-280E intercept) confidently misreads "$500,000 net profit
    from a cannabis business" as a $500,000 RETAIL PURCHASE of cannabis
    product and computes excise/sales tax on it -- exactly the
    "confidently wrong" failure this project exists to prevent."""
    if not income_brackets.detect_cannabis_280e_signal(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax for a licensed cannabis business under R&TC "
        "Section 17209, I need the dollar amount of ordinary business expenses disallowed "
        "federally under IRC Section 280E that you're restoring as a California deduction, "
        "stated separately from your net profit (for example, \"$500,000 net profit with "
        "$150,000 in disallowed 280E expenses\"). Please ask again and include both figures.")
    return result


def _income_roth_ira_answer(question: str, base: dict):
    """Dedicated informational redirect for a Roth IRA deduction question
    -- Roth contributions are NEVER deductible (federal or CA). Checked
    BEFORE _income_ira_deduction_answer in the dispatcher: "roth ira" and
    "roth" both fail IRA_DEDUCTION_TERMS's own roth exclusion, but a
    phrasing like "can I deduct my roth ira contribution" also contains
    "ira contribution" as a substring, so this needs to intercept first
    rather than rely on the other path simply not firing."""
    if not income_brackets.detect_roth_ira_mention(question):
        return None
    result = {**base, "status": "answered", "category": "roth_ira_not_deductible"}
    result["answer_text"] = (
        "Contributions to a Roth IRA are never tax-deductible, for either federal or "
        "California purposes -- Roth contributions are made with after-tax dollars, and the "
        "tax benefit is tax-free qualified withdrawals in retirement instead of an upfront "
        "deduction. If you meant a TRADITIONAL IRA deduction instead, ask again specifying "
        "that."
    )
    return result


def _income_ira_deduction_answer(conn, question: str, base: dict):
    """Income + a stated traditional-IRA deduction -- see
    income_brackets.compute_ira_deduction_ca_tax's docstring for the
    Schedule CA Line 20 conformity basis (SB 711 conformity-date change,
    TY2025 no-adjustment finding). Uses _amount_near/the 'one other
    amount' pattern exactly like the capital-loss/excess-business-loss
    paths."""
    fs = income_brackets.detect_ira_deduction_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.IRA_DEDUCTION_TERMS, amounts)
    if match is None:
        return None
    ira_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_ira_deduction_ca_tax(conn, income_amount, ira_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in income (treated as your California AGI before the "
        f"IRA deduction, with no other adjustments), filing status {label}, and your "
        f"${ira_amount:,.2f} traditional IRA deduction: for {income_brackets.DEFAULT_TAX_YEAR}, "
        f"California allows this deduction UNCHANGED from your federal amount "
        f"({income_brackets.IRA_DEDUCTION_CITATION}) -- the two previously-live CA/federal "
        "divergence triggers for this line (the pre-SECURE-Act age-70½ limit, the CAA "
        "2023 catch-up-contribution indexing) no longer apply under California's 2025 IRC "
        f"conformity-date update. Your California AGI is about ${calc['agi']:,.2f}; after the "
        f"standard deduction (${calc['standard_deduction']:,.0f}), your California taxable "
        f"income is about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes your stated IRA deduction is already a valid federal "
        "deduction (this doesn't re-derive the contribution limit or employer-plan income "
        "phase-out from your age/coverage facts) and that your CA and federal earned income "
        "match (a self-employment/worker-classification mismatch is the one confirmed "
        "remaining case where California and federal amounts could still differ) -- your "
        "actual liability may differ."
    )
    return result


def _income_ira_deduction_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_capital_loss_missing_filing_status_answer for the
    IRA-deduction path."""
    if not income_brackets.detect_ira_deduction_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a traditional IRA deduction, I need your "
        "filing status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please ask again and include your filing "
        "status.")
    return result


def _qsbs_strip_section_number_phantoms(amounts):
    """_amounts()'s shared regex (an optional dollar sign, then digits)
    has no context awareness, so literal "1202" or "1045" in question
    text -- Sections 1202/1045 are this feature's OWN natural vocabulary,
    the IRC sections that govern QSBS -- parse as phantom dollar amounts.
    Same collision class as cannabis 280E's phantom $280 parse (see
    _cannabis_strip_280e_phantom_amounts), fixed the same way: a local
    filter scoped to this one feature rather than touching the shared
    _amounts()/_amount_near() that 20+ other paths depend on. Filters out
    amounts exactly matching 1202.0 or 1045.0 -- a taxpayer stating an
    actual dollar figure of precisely $1,202 or $1,045 for a QSBS
    exclusion is vanishingly unlikely next to the certainty of these
    numbers appearing as bare statute references in any QSBS question."""
    return [(a, s, e) for a, s, e in amounts if a not in (1202.0, 1045.0)]


def _income_qsbs_answer(conn, question: str, base: dict):
    """QSBS (Qualified Small Business Stock, IRC Sections 1202/1045) gain
    -- see income_brackets.compute_qsbs_ca_tax's docstring for the R&TC
    18152.5 full-non-conformity basis. Uses the 'one other amount'
    pattern exactly like the excess-business-loss/NOL/IRA-deduction
    paths, but via the phantom-filtered amounts above instead of
    _amounts()/_amount_near() directly."""
    fs = income_brackets.detect_qsbs_signal(question)
    if not fs:
        return None
    amounts = _qsbs_strip_section_number_phantoms(_amounts(question))
    excluded_match = _amount_near_filtered_span(question, income_brackets.QSBS_EXCLUDED_AMOUNT_TERMS, amounts)
    if excluded_match is None:
        return None
    excluded_amount = excluded_match[0]
    remaining = _remove_amount_span(amounts, excluded_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    federal_taxable_gain = others[0]
    calc = income_brackets.compute_qsbs_ca_tax(conn, federal_taxable_gain, excluded_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": federal_taxable_gain, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${federal_taxable_gain:,.2f} in federal taxable gain from your Qualified "
        f"Small Business Stock (QSBS) sale, with ${excluded_amount:,.2f} excluded or deferred "
        f"federally under IRC Section 1202/1045, filing status {label}: California does NOT "
        f"conform to the federal QSBS exclusion/deferral ({income_brackets.QSBS_CITATION}) -- "
        f"the full ${excluded_amount:,.2f} is added back for California, so your "
        f"California-taxable gain is ${calc['ca_gain']:,.2f}. After the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes the QSBS gain is your only income (no other adjustments) "
        "-- your actual liability may differ."
    )
    return result


def _income_qsbs_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_excess_business_loss_missing_filing_status_answer
    for the QSBS path."""
    if not income_brackets.detect_qsbs_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax on a QSBS sale, I need your filing status: "
        "single, married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse. Please ask again and include your filing status.")
    return result


def _income_hsa_investment_gain_answer(conn, question: str, base: dict):
    """Other income (e.g. wages) + a stated realized gain from selling
    investments held inside an HSA -- see income_brackets.
    compute_hsa_investment_gain_ca_tax's docstring for the Schedule CA
    Line 7a conformity basis (CA doesn't recognize HSA tax-shelter status
    at all, so the gain is CA-taxable with no federal counterpart to
    reconcile against). Uses _amount_near/the 'one other amount' pattern
    exactly like the excess-business-loss/NOL/IRA-deduction paths."""
    fs = income_brackets.detect_hsa_investment_gain_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.HSA_INVESTMENT_GAIN_TERMS, amounts)
    if match is None:
        return None
    hsa_gain_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_hsa_investment_gain_ca_tax(conn, income_amount, hsa_gain_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in other income (e.g. wages), filing status {label}, "
        f"and a ${hsa_gain_amount:,.2f} realized gain from selling investments held inside "
        f"your HSA: California does not recognize HSAs as tax-advantaged "
        f"({income_brackets.HSA_INVESTMENT_GAIN_CITATION}), so this gain is fully taxable for "
        "California THIS YEAR, with no federal counterpart -- federally it stays invisible "
        f"inside the HSA. Your California AGI is about ${calc['agi']:,.2f}; after the standard "
        f"deduction (${calc['standard_deduction']:,.0f}), your California taxable income is "
        f"about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes no other adjustments -- your actual liability may differ."
    )
    return result


def _income_hsa_investment_gain_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_excess_business_loss_missing_filing_status_answer
    for the HSA-investment-gain path."""
    if not income_brackets.detect_hsa_investment_gain_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a gain from investments sold inside your "
        "HSA, I need your filing status: single, married filing jointly, married filing "
        "separately, head of household, or qualifying surviving spouse. Please ask again and "
        "include your filing status.")
    return result


def _income_capital_loss_carryover_answer(conn, question: str, base: dict):
    """Capital loss CARRYOVER from a prior year -- see income_brackets.
    CAPITAL_LOSS_CARRYOVER_TERMS's module note for why this needs its own
    detection: the underlying math is IDENTICAL to
    compute_capital_loss_ca_tax (same annual limit), but that path's own
    disclosure text wrongly claims a current-year-loss assumption when
    the question explicitly says otherwise. Checked BEFORE the generic
    capital-loss path (mirrors K-1 capital gain's ordering fix)."""
    fs = income_brackets.detect_capital_loss_carryover_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.CAPITAL_LOSS_CARRYOVER_TERMS, amounts)
    if match is None:
        return None
    loss_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_capital_loss_ca_tax(conn, income_amount, loss_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": income_amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["carryover"]:
        loss_note = (f"${calc['deductible_loss']:,.2f} of your ${loss_amount:,.2f} capital loss "
                     f"carryover (the annual limit for {label}), with the remaining "
                     f"${calc['carryover']:,.2f} continuing to carry forward to next year's "
                     "California return (not reflected in this estimate)")
    else:
        loss_note = f"your full ${loss_amount:,.2f} capital loss carryover (under the annual limit)"
    result["answer_text"] = (
        f"Assuming ${income_amount:,.2f} in gross income (also your California AGI before the "
        f"loss offset, with no other adjustments), filing status {label}, and deducting "
        f"{loss_note} ({income_brackets.CAPITAL_LOSS_CARRYOVER_CITATION}), plus the standard "
        f"deduction (${calc['standard_deduction']:,.0f}): your California taxable income is "
        f"about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes you were a California resident for ALL prior years that "
        "generated this carryover -- if you were a nonresident or part-year resident in any of "
        "those years, FTB requires recalculating the carryover as if you'd been a CA resident "
        "throughout, which this estimate does not do -- your actual liability may differ."
    )
    return result


def _income_capital_loss_carryover_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_capital_loss_missing_filing_status_answer for the
    carryover-specific path."""
    if not income_brackets.detect_capital_loss_carryover_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a capital loss carryover, I need your "
        "filing status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please ask again and include your filing "
        "status.")
    return result


def _income_fringe_benefit_answer(conn, question: str, base: dict):
    """Self-employed net profit + a stated employer fringe-benefit
    restoration (entertainment, employee parking/transit, on-premises
    meals) -- see income_brackets.compute_self_employment_ca_tax's
    fringe_benefit_restoration docstring for the TCJA/IRC 274 non-
    conformity basis (reuses the self-employment compute path, same
    shape as cannabis 280E). Uses _amount_near/the 'one other amount'
    pattern exactly like the cannabis-280E path."""
    fs = income_brackets.detect_fringe_benefit_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.FRINGE_BENEFIT_TERMS, amounts)
    if match is None:
        return None
    restoration_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    net_profit = others[0]
    calc = income_brackets.compute_self_employment_ca_tax(
        conn, net_profit, fs, fringe_benefit_restoration=restoration_amount)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "self_employment_income_tax",
              "amount": net_profit, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${net_profit:,.2f} in federal Schedule C net profit, filing status {label}: "
        f"federal self-employment tax is ${calc['se_tax']:,.2f} (${calc['half_se_deduction']:,.2f} "
        f"deductible). California does not conform to the federal TCJA limitation on employer "
        f"fringe-benefit expense deductions (entertainment, employee parking/transit, on-"
        f"premises meals) ({income_brackets.FRINGE_BENEFIT_CITATION}), so your "
        f"${restoration_amount:,.2f} in federally-disallowed fringe-benefit expenses is restored "
        f"as a deduction against California income, giving California AGI of "
        f"${calc['agi']:,.2f}. After the standard deduction (${calc['standard_deduction']:,.0f}), "
        f"your California taxable income is about ${calc['taxable_income']:,.2f}. Your marginal "
        f"CA tax bracket is {calc['marginal_rate']*100:g}%, and your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California income tax is about "
        f"${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes you are yourself an EMPLOYER (these are benefits paid to "
        "your employees, not yourself) and that self-employment income is your only income -- "
        "your actual liability may differ."
    )
    return result


def _income_fringe_benefit_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_cannabis_280e_missing_filing_status_answer for the
    fringe-benefit-restoration path."""
    if not income_brackets.detect_fringe_benefit_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with an employer fringe-benefit expense "
        "restoration, I need your filing status: single, married filing jointly, married "
        "filing separately, head of household, or qualifying surviving spouse. Please ask "
        "again and include your filing status.")
    return result


def _income_real_estate_pro_answer(conn, question: str, base: dict):
    """CA non-conformity to IRC 469(c)(7) (real estate professional
    exception) -- see income_brackets.compute_real_estate_pro_ca_tax's
    docstring for the FTB 3801 conformity basis (CA reuses the standard
    $25,000/$100k-$150k MAGI phase-out formula, since it just refuses the
    recharacterization that would have exempted the taxpayer from that
    formula federally). Uses _amount_near/the 'one other amount' pattern
    exactly like the excess-business-loss/NOL paths."""
    fs = income_brackets.detect_real_estate_pro_signal(question)
    if not fs:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_brackets.REAL_ESTATE_PRO_LOSS_TERMS, amounts)
    if match is None:
        return None
    rental_loss = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    other_income = others[0]
    q = question.lower()
    lived_apart = any(t in q for t in income_brackets.MFS_LIVED_APART_TERMS)
    calc = income_brackets.compute_real_estate_pro_ca_tax(conn, other_income, rental_loss, fs, lived_apart)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if calc["disallowed"]:
        loss_note = (f"${calc['ca_allowed_loss']:,.2f} of your ${rental_loss:,.2f} rental loss "
                     f"(California's active-participation allowance at your income level is "
                     f"${calc['ca_allowance']:,.2f}), with the remaining "
                     f"${calc['disallowed']:,.2f} added back for California even though it's "
                     "fully deductible on your federal return")
    else:
        loss_note = (f"your full ${rental_loss:,.2f} rental loss (under California's "
                     f"${calc['ca_allowance']:,.2f} active-participation allowance at your "
                     "income level)")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (also your Modified AGI for the "
        f"phase-out test, with no other adjustments), filing status {label}, and that you "
        f"qualify federally as a real estate professional (so your rental loss is fully "
        f"deductible, nonpassive, on your federal return): California does NOT conform to the "
        f"real estate professional exception ({income_brackets.REAL_ESTATE_PRO_CITATION}) -- "
        f"for California, this activity stays passive, subject to the same $25,000 active-"
        "participation allowance (with its $100,000-$150,000 MAGI phase-out) that applies to "
        f"any other passive rental loss. Deducting {loss_note}, plus the standard deduction "
        f"(${calc['standard_deduction']:,.0f}): your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes this rental activity is your only passive activity (no "
        "netting against other passive income/losses) and no prior-year suspended-loss "
        "carryover -- your actual liability may differ."
    )
    return result


def _income_real_estate_pro_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_excess_business_loss_missing_filing_status_answer
    for the real-estate-professional path."""
    if not income_brackets.detect_real_estate_pro_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax on a real estate professional's rental loss, "
        "I need your filing status: single, married filing jointly, married filing separately, "
        "head of household, or qualifying surviving spouse. Please ask again and include your "
        "filing status.")
    return result


def _income_real_estate_pro_missing_mfs_status_answer(question: str, base: dict):
    """Specific clarifying message for the one MFS-only missing fact --
    lived apart vs. lived together changes the allowance from $12,500
    down to $0, a material difference this path won't guess at."""
    if not income_brackets.detect_real_estate_pro_missing_mfs_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "For married filing separately, I need one more fact: did you live apart from your "
        "spouse for the ENTIRE year? If so, California allows a reduced $12,500 active-"
        "participation allowance (phased out between $50,000-$75,000 MAGI); if you lived "
        "together at any point during the year, California allows NO active-participation "
        "allowance at all for this activity. Please ask again and specify.")
    return result


def _foreign_earned_income_strip_form_number_phantoms(amounts):
    """_amounts()'s shared regex has no context awareness, so literal
    "2555" in question text -- Form 2555 is this feature's own natural
    vocabulary -- parses as a phantom $2,555.00 dollar amount. Same
    collision class as cannabis 280E's "280E" and QSBS's "1202"/"1045"
    (see _qsbs_strip_section_number_phantoms), fixed the same way: a
    local filter scoped to this feature rather than touching the shared
    _amounts()/_amount_near() that 20+ other paths depend on."""
    return [(a, s, e) for a, s, e in amounts if a != 2555.0]


def _income_foreign_earned_income_answer(conn, question: str, base: dict):
    """Other income (e.g. wages) + a stated Form 2555 foreign earned
    income/housing EXCLUSION amount (Line 8d) and/or a stated foreign
    housing DEDUCTION amount (Line 24j) -- see income_brackets.
    compute_foreign_earned_income_ca_tax's docstring for the conformity
    basis of both (California doesn't conform to IRC 911 at all for
    this resident-population form -- flat, unconditional addback either
    way). Extracts the housing-DEDUCTION anchor FIRST and removes it
    from the amounts list before searching for the exclusion anchor --
    necessary because "form 2555" (a bare FOREIGN_EARNED_INCOME_TERMS
    trigger) is also a substring of several FOREIGN_HOUSING_DEDUCTION_
    TERMS phrases (e.g. "form 2555 housing deduction"), so without this
    ordering a single stated housing-deduction figure could get
    double-counted as BOTH an exclusion and a deduction. Falls back
    correctly to the original single-anchor behavior when only one of
    the two is mentioned (housing_deduction_amount/excluded_amount
    defaults to 0.0 when its anchor isn't found).

    REMOVING the housing anchor from the amounts list is not sufficient
    on its own to prevent double-counting -- found live via testing
    "...a $30,000 form 2555 housing deduction single": after the
    $30,000 is correctly claimed by the housing search, the ONLY
    remaining amount ($80,000, the wage figure) can still fall within
    _amount_near_filtered_span's proximity window of the bare "form 2555"
    substring even though it's semantically unrelated (the window is a
    fixed character radius, not a phrase-boundary check). Fixed by only
    attempting the exclusion-amount search when the question contains
    an EXCLUSION-SPECIFIC term (not just the bare "form 2555" shared
    with the housing phrase), or bare "form 2555" appears WITHOUT any
    housing wording at all -- i.e. "form 2555" alone never triggers a
    second, spurious search once a housing anchor has already claimed
    the question's one housing-flavored mention of that form number."""
    fs = income_brackets.detect_foreign_earned_income_signal(question)
    if not fs:
        return None
    amounts = _foreign_earned_income_strip_form_number_phantoms(_amounts(question))
    q = question.lower()
    has_housing_terms = any(t in q for t in income_brackets.FOREIGN_HOUSING_DEDUCTION_TERMS)
    housing_deduction_amount = 0.0
    if has_housing_terms:
        match = _amount_near_filtered_span(question, income_brackets.FOREIGN_HOUSING_DEDUCTION_TERMS, amounts)
        if match is not None:
            housing_deduction_amount = match[0]
            amounts = _remove_amount_span(amounts, match)
    excluded_amount = 0.0
    exclusion_specific_terms = income_brackets.FOREIGN_EARNED_INCOME_TERMS - {"form 2555"}
    has_exclusion_specific_terms = any(t in q for t in exclusion_specific_terms)
    has_bare_form_2555 = "form 2555" in q
    if has_exclusion_specific_terms or (has_bare_form_2555 and not has_housing_terms):
        match = _amount_near_filtered_span(question, income_brackets.FOREIGN_EARNED_INCOME_TERMS, amounts)
        if match is not None:
            excluded_amount = match[0]
            amounts = _remove_amount_span(amounts, match)
    if excluded_amount <= 0 and housing_deduction_amount <= 0:
        return None
    others = [a for a, _, _ in amounts]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_foreign_earned_income_ca_tax(
        conn, other_income, excluded_amount, fs, housing_deduction_amount=housing_deduction_amount)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if excluded_amount > 0 and housing_deduction_amount > 0:
        addback_note = (
            f"${excluded_amount:,.2f} excluded from federal income under Form 2555's foreign "
            f"earned income and housing exclusion ({income_brackets.FOREIGN_EARNED_INCOME_CITATION}), "
            f"plus a ${housing_deduction_amount:,.2f} foreign housing deduction also claimed "
            f"federally under Form 2555 ({income_brackets.FOREIGN_HOUSING_DEDUCTION_CITATION})"
        )
    elif housing_deduction_amount > 0:
        addback_note = (
            f"a ${housing_deduction_amount:,.2f} foreign housing deduction claimed federally "
            f"under Form 2555 ({income_brackets.FOREIGN_HOUSING_DEDUCTION_CITATION})"
        )
    else:
        addback_note = (
            f"${excluded_amount:,.2f} excluded from federal income under Form 2555 (foreign "
            f"earned income and housing exclusion) ({income_brackets.FOREIGN_EARNED_INCOME_CITATION})"
        )
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (e.g. wages), filing status {label}, "
        f"and {addback_note}: California does NOT conform to either federal mechanism -- as a "
        "California resident, the full amount(s) are added back and taxed. Your California AGI "
        f"is about ${calc['agi']:,.2f}; after the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes you are filing as a California RESIDENT (part-year/"
        "nonresident apportionment works differently) and no other adjustments -- your actual "
        "liability may differ."
    )
    return result


def _income_foreign_earned_income_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_hsa_investment_gain_missing_filing_status_answer
    for the foreign-earned-income path."""
    if not income_brackets.detect_foreign_earned_income_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a Form 2555 foreign earned income "
        "exclusion and/or foreign housing deduction, I need your filing status: single, "
        "married filing jointly, married filing separately, head of household, or qualifying "
        "surviving spouse. Please ask again and include your filing status.")
    return result


def _cfc_951_strip_section_number_phantoms(amounts):
    """Literal "951" in "951(a)"/"951a" (both IRC section references --
    this feature's own trigger vocabulary, for Subpart F and GILTI
    respectively) parses as a phantom $951.00 dollar amount via
    _amounts()'s shared regex (same collision class as cannabis "280E",
    QSBS "1202"/"1045", Form 2555's "2555"). Local filter shared by both
    CFC-inclusion paths since they collide on the same numeric
    coincidence."""
    return [(a, s, e) for a, s, e in amounts if a != 951.0]


def _gilti_strip_form_number_phantoms(amounts):
    """Literal "8992" in "Form 8992" (GILTI's own trigger vocabulary --
    the federal GILTI computation form) parses as a phantom $8,992.00
    dollar amount -- same collision class, scoped to GILTI only since
    Subpart F doesn't reference this form."""
    return [(a, s, e) for a, s, e in amounts if a != 8992.0]


def _income_subpart_f_answer(conn, question: str, base: dict):
    """Other income (e.g. wages) + a stated federal Subpart F (IRC
    951(a)) inclusion amount -- see
    income_brackets.compute_subpart_f_ca_tax's docstring for the Schedule
    CA Line 8n conformity basis (California doesn't conform to IRC
    951(a); CA taxes CFC earnings only on actual distribution, so the
    federal inclusion is fully subtracted back out). Phantom-filtered for
    literal "951" the same way QSBS/cannabis/Form 2555 are."""
    fs = income_brackets.detect_subpart_f_signal(question)
    if not fs:
        return None
    amounts = _cfc_951_strip_section_number_phantoms(_amounts(question))
    inclusion_match = _amount_near_filtered_span(question, income_brackets.SUBPART_F_TERMS, amounts)
    if inclusion_match is None:
        return None
    inclusion_amount = inclusion_match[0]
    remaining = _remove_amount_span(amounts, inclusion_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_subpart_f_ca_tax(conn, other_income, inclusion_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (e.g. wages), filing status {label}, "
        f"and a ${inclusion_amount:,.2f} federal Subpart F (IRC Section 951(a)) inclusion as a "
        "U.S. shareholder of a controlled foreign corporation: California does NOT conform to "
        f"this federal inclusion ({income_brackets.SUBPART_F_CITATION}) -- CA taxes CFC "
        "earnings only when actually distributed, so the full inclusion amount is subtracted "
        f"back out. Your federal AGI (about ${calc['federal_agi']:,.2f}) included this amount, "
        f"but your California AGI is about ${calc['agi']:,.2f}; after the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes you are a California RESIDENT (part-year/nonresident "
        "apportionment works differently), that your stated inclusion amount is already your "
        "correct federal Section 951(a) inclusion (this doesn't independently verify your CFC-"
        "shareholder status or ownership percentage), and no other adjustments -- your actual "
        "liability may differ."
    )
    return result


def _income_subpart_f_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_foreign_earned_income_missing_filing_status_answer
    for the Subpart F path."""
    if not income_brackets.detect_subpart_f_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a federal Subpart F (IRC Section 951(a)) "
        "income inclusion, I need your filing status: single, married filing jointly, married "
        "filing separately, head of household, or qualifying surviving spouse. Please ask again "
        "and include your filing status.")
    return result


def _income_gilti_answer(conn, question: str, base: dict):
    """Other income (e.g. wages) + a stated federal GILTI (IRC 951A)
    inclusion amount -- see income_brackets.compute_gilti_ca_tax's
    docstring for the Schedule CA Line 8o conformity basis. Same
    mechanic as _income_subpart_f_answer (CFC inclusion fully subtracted
    back out), different IRC section/vintage. Phantom-filtered for both
    literal "951" and literal "8992" (Form 8992)."""
    fs = income_brackets.detect_gilti_signal(question)
    if not fs:
        return None
    amounts = _gilti_strip_form_number_phantoms(_cfc_951_strip_section_number_phantoms(_amounts(question)))
    inclusion_match = _amount_near_filtered_span(question, income_brackets.GILTI_TERMS, amounts)
    if inclusion_match is None:
        return None
    inclusion_amount = inclusion_match[0]
    remaining = _remove_amount_span(amounts, inclusion_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_gilti_ca_tax(conn, other_income, inclusion_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other income (e.g. wages), filing status {label}, "
        f"and a ${inclusion_amount:,.2f} federal GILTI (IRC Section 951A) inclusion as a U.S. "
        "shareholder of a controlled foreign corporation: California does NOT conform to this "
        f"federal inclusion ({income_brackets.GILTI_CITATION}) -- CA taxes CFC earnings only "
        "when actually distributed, so the full inclusion amount is subtracted back out. Your "
        f"federal AGI (about ${calc['federal_agi']:,.2f}) included this amount, but your "
        f"California AGI is about ${calc['agi']:,.2f}; after the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes you are a California RESIDENT (part-year/nonresident "
        "apportionment works differently), that your stated inclusion amount is already your "
        "correct federal GILTI amount with no IRC Section 962 election in place (this doesn't "
        "independently verify your CFC-shareholder status or ownership percentage), and no "
        "other adjustments -- your actual liability may differ."
    )
    return result


def _income_gilti_missing_filing_status_answer(question: str, base: dict):
    """Mirrors _income_subpart_f_missing_filing_status_answer for the
    GILTI path."""
    if not income_brackets.detect_gilti_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with a federal GILTI (IRC Section 951A) "
        "inclusion, I need your filing status: single, married filing jointly, married filing "
        "separately, head of household, or qualifying surviving spouse. Please ask again and "
        "include your filing status.")
    return result


def _nra_strip_1040nr_phantoms(amounts):
    """Literal "1040" in "1040-nr"/"1040nr" (this feature's own trigger
    vocabulary -- the federal nonresident-alien return form number)
    parses as a phantom $1,040.00 dollar amount via _amounts()'s shared
    regex (same collision class as cannabis "280E", QSBS "1202"/"1045",
    Form 2555's "2555", Subpart F/GILTI's "951"/"8992"). Local filter
    scoped to this feature."""
    return [(a, s, e) for a, s, e in amounts if a != 1040.0]


def _income_nra_foreign_income_answer(conn, question: str, base: dict):
    """Other income + a stated foreign-source income or loss figure for
    a CA-resident federal-nonresident-alien filer -- see
    income_brackets.compute_nra_foreign_income_ca_tax's docstring for the
    Schedule CA Line 8z worldwide-income-true-up conformity basis.
    Phantom-filtered for literal "1040" the same way QSBS/cannabis/Form
    2555/Subpart F/GILTI are."""
    fs = income_brackets.detect_nra_foreign_income_signal(question)
    if not fs:
        return None
    is_loss = income_brackets.detect_nra_foreign_income_is_loss(question)
    amounts = _nra_strip_1040nr_phantoms(_amounts(question))
    terms = (income_brackets.NRA_FOREIGN_LOSS_AMOUNT_TERMS if is_loss
             else income_brackets.NRA_FOREIGN_INCOME_AMOUNT_TERMS)
    foreign_match = _amount_near_filtered_span(question, terms, amounts)
    if foreign_match is None:
        return None
    foreign_amount = foreign_match[0]
    remaining = _remove_amount_span(amounts, foreign_match)
    others = [a for a, _, _ in remaining]
    if len(others) != 1:
        return None
    other_income = others[0]
    calc = income_brackets.compute_nra_foreign_income_ca_tax(
        conn, other_income, foreign_amount, is_loss, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "ca_income_tax_bracket",
              "amount": other_income, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    if is_loss:
        adj_note = (f"a ${foreign_amount:,.2f} loss from foreign sources, which California "
                    "SUBTRACTS to reflect your worldwide income")
    else:
        adj_note = (f"${foreign_amount:,.2f} in foreign-source income not reported on your "
                    "federal Form 1040-NR (which generally covers only U.S.-source/effectively-"
                    "connected income), which California ADDS BACK to reflect your worldwide "
                    "income")
    result["answer_text"] = (
        f"Assuming ${other_income:,.2f} in other U.S.-source income already reported on your "
        f"federal return (e.g. wages), filing status {label}, and {adj_note} "
        f"({income_brackets.NRA_FOREIGN_INCOME_CITATION}) -- since California taxes full-year "
        "residents on WORLDWIDE income regardless of federal nonresident-alien status: your "
        f"California AGI is about ${calc['agi']:,.2f}. After the standard deduction "
        f"(${calc['standard_deduction']:,.0f}), your California taxable income is about "
        f"${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes you are a full-year California RESIDENT (this line doesn't "
        "apply the same way on a nonresident/part-year Form 540NR return) and no other "
        "adjustments -- your actual liability may differ."
    )
    return result


def _income_nra_foreign_income_missing_filing_status_answer(question: str, base: dict):
    if not income_brackets.detect_nra_foreign_income_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California income tax with foreign-source income or losses as a "
        "federal nonresident alien, I need your filing status: single, married filing jointly, "
        "married filing separately, head of household, or qualifying surviving spouse.")
    return result


def _income_caleitc_investment_answer(conn, question: str, base: dict):
    """CalEITC + a stated investment-income figure -- see
    income_credits.compute_caleitc_with_investment_income's docstring for
    the FTB 3514 Step 2 conformity basis ($4,814 2025 investment-income
    disqualification limit -- NOT the same as the federal EITC's ~$11,950
    limit, verified against the actual current CA form rather than assumed
    by analogy). Uses _amount_near/the 'one other amount' pattern exactly
    like the itemized-deduction and capital-loss paths."""
    children = income_credits.detect_caleitc_investment_signal(question)
    if children is None:
        return None
    amounts = _amounts(question)
    match = _amount_near_filtered_span(question, income_credits.INVESTMENT_INCOME_TERMS, amounts)
    if match is None:
        return None
    investment_amount = match[0]
    others = [a for a, _, _ in _remove_amount_span(amounts, match)]
    if len(others) != 1:
        return None
    earned_income = others[0]
    calc = income_credits.compute_caleitc_with_investment_income(conn, earned_income, investment_amount, children)
    if not calc:
        return None
    child_label = "no qualifying children" if children == 0 else (
        "1 qualifying child" if children == 1 else f"{children} qualifying children"
        + (" (3 or more)" if children == 3 else ""))
    limit = income_credits.CALEITC_INVESTMENT_INCOME_LIMIT
    if calc["disqualified"]:
        result = {**base, "status": "answered", "category": "caleitc",
                  "amount": earned_income, "tax": 0.0, "citation": calc["citation"],
                  "source_url": calc["source_url"]}
        result["answer_text"] = (
            f"Because your stated investment income (${investment_amount:,.2f}) is more than "
            f"the {income_brackets.DEFAULT_TAX_YEAR} CalEITC investment-income limit "
            f"(${limit:,.0f}), you do not qualify for CalEITC this year -- regardless of your "
            f"earned income or number of qualifying children ({calc['citation']})."
        )
        return result
    result = {**base, "status": "answered", "category": "caleitc",
              "amount": earned_income, "tax": calc["credit"], "citation": calc["citation"],
              "source_url": calc["source_url"]}
    result["answer_text"] = (
        f"Assuming ${earned_income:,.2f} in California earned income with {child_label}, and "
        f"${investment_amount:,.2f} in investment income (under the "
        f"{income_brackets.DEFAULT_TAX_YEAR} CalEITC investment-income limit of ${limit:,.0f}, "
        "so it does not disqualify you), and that your federal AGI equals your earned income "
        f"(no other adjustments), your estimated California Earned Income Tax Credit (CalEITC) "
        f"is ${calc['credit']:,.2f} ({calc['citation']}). This is an estimate only -- your "
        "actual credit depends on filing a complete return."
    )
    return result


def _income_caleitc_investment_missing_children_answer(question: str, base: dict):
    """Mirrors _income_missing_children_answer for the investment-income-
    aware CalEITC path."""
    if not income_credits.detect_caleitc_investment_missing_children(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your CalEITC, I need to know your number of qualifying children "
        "(0, 1, 2, or 3 or more). Please ask again and include it (for example, "
        "\"...with 2 qualifying children\" or \"...with no children\").")
    return result


def _income_caleitc_answer(conn, question: str, amount, base: dict):
    """CalEITC lookup against the verified 658-row 2025 table (see
    load_income_content.py's docstring) -- the SIMPLEST case only (see
    income_credits.CREDIT_COMPLEXITY_EXCLUDE): assumes wage-only earned
    income with federal AGI equal to CA earned income, skipping Form 3514
    Part II's AGI-vs-earned-income reconciliation."""
    children = income_credits.detect_caleitc_signal(question)
    if children is None or amount is None:
        return None
    hit = income_credits.lookup_eitc_table(conn, amount, children)
    if not hit:
        return None
    child_label = "no qualifying children" if children == 0 else (
        "1 qualifying child" if children == 1 else f"{children} qualifying children"
        + (" (3 or more)" if children == 3 else ""))
    result = {**base, "status": "answered", "category": "caleitc",
              "amount": amount, "tax": hit["credit"], "citation": hit["citation"],
              "source_url": hit["source_url"]}
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in California earned income with {child_label}, and "
        f"that your federal AGI equals your earned income (no investment income or other "
        f"adjustments), your estimated California Earned Income Tax Credit (CalEITC) is "
        f"${hit['credit']:,.2f} ({hit['citation']}). This is an estimate only -- your actual "
        "credit depends on filing a complete return."
    )
    return result


def _income_missing_children_answer(question: str, amount, base: dict):
    """Mirrors _income_missing_filing_status_answer: a clearly CalEITC-shaped
    question missing only the number of qualifying children gets a specific,
    actionable clarifying message instead of a generic dead end."""
    if amount is None or not income_credits.detect_caleitc_missing_children(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your CalEITC, I need to know your number of qualifying children "
        "(0, 1, 2, or 3 or more). Please ask again and include it (for example, "
        "\"...with 2 qualifying children\" or \"...with no children\").")
    return result


def _income_ycta_answer(conn, question: str, amount, base: dict):
    """Young Child Tax Credit -- see income_credits.compute_ycta for the
    exact FTB-form arithmetic. Unlike CalEITC, this doesn't need a
    children-count from the question (eligibility already requires a
    qualifying child under 6, which asking this question implies)."""
    if amount is None or not income_credits.detect_ycta_signal(question):
        return None
    hit = income_credits.compute_ycta(conn, amount)
    if not hit:
        return None
    result = {**base, "status": "answered", "category": "young_child_tax_credit",
              "amount": amount, "tax": hit["credit"], "citation": hit["citation"],
              "source_url": hit["source_url"]}
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in California earned income and a qualifying child under "
        f"age 6, your estimated Young Child Tax Credit (YCTC) is ${hit['credit']:,.2f} "
        f"({hit['citation']}). This is an estimate only -- your actual credit depends on "
        "filing a complete return and meeting all YCTC eligibility requirements."
    )
    return result


def _fytc_income_amount(question: str):
    """FYTC questions naturally state TWO or THREE numbers close together
    (age, foster-care age, income -- "I am 20... foster care at age 15...
    made $50,000"). Found via testing: _amount_near's nearest-keyword-
    DISTANCE approach picked the WRONG number here -- it measures from a
    keyword's START position, which systematically under-counts the
    distance to whichever number comes right after the keyword's own
    length, and with three numbers clustered this close together that
    bias was enough to flip the winner (a stated age of 15 beat the
    actual $50,000 income figure for the keyword "made"). FIX: the same
    "exclude the known OTHER numbers, trust what's left" pattern already
    used for itemized deductions/capital losses -- explicitly exclude the
    stated age and the foster-care-age number (both independently
    detected elsewhere for eligibility), then require EXACTLY ONE
    remaining amount. More numbers than that -> ambiguous, defer."""
    known = set()
    age = income_credits.fytc_stated_age(question)
    if age is not None:
        known.add(float(age))
    fc_age = income_credits.fytc_foster_care_age_number(question)
    if fc_age is not None:
        known.add(float(fc_age))
    others = [a for a, _, _ in _amounts(question) if a not in known]
    return others[0] if len(others) == 1 else None


def _income_fytc_answer(conn, question: str, base: dict):
    """Foster Youth Tax Credit -- see income_credits.compute_fytc's
    docstring for the two-gate design (CalEITC-eligibility verified via
    the real table lookup, THEN the FYTC-specific phase-out arithmetic).
    Needs a children count too (not because FYTC's amount depends on it,
    but because verifying the CalEITC gate does)."""
    children = income_credits.detect_fytc_signal(question)
    if children is None:
        return None
    amount = _fytc_income_amount(question)
    if amount is None:
        return None
    hit = income_credits.compute_fytc(conn, amount, children)
    if not hit:
        return None
    if not hit["eligible_for_caleitc"]:
        result = {**base, "status": "answered", "category": "foster_youth_tax_credit",
                  "amount": amount, "tax": 0.0}
        result["answer_text"] = (
            f"Based on ${amount:,.2f} in California earned income and the children you "
            "stated, you would not qualify for CalEITC -- and the Foster Youth Tax Credit "
            "requires being allowed the CalEITC first, so you would not qualify for FYTC "
            "either (2025 FTB 3514 Booklet, Step 10)."
        )
        return result
    child_label = "no qualifying children" if children == 0 else (
        "1 qualifying child" if children == 1 else f"{children} qualifying children"
        + (" (3 or more)" if children == 3 else ""))
    result = {**base, "status": "answered", "category": "foster_youth_tax_credit",
              "amount": amount, "tax": hit["credit"], "citation": hit["citation"],
              "source_url": hit["source_url"]}
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in California earned income with {child_label}, that you "
        "were 18 to 25 at year end, and that you were in foster care at age 13 or older and "
        "placed through the California foster care system, your estimated Foster Youth Tax "
        f"Credit (FYTC) is ${hit['credit']:,.2f} ({hit['citation']}). This is an estimate "
        "only -- your actual credit depends on filing a complete return and your foster "
        "youth status being verified by the California Department of Social Services."
    )
    return result


def _income_fytc_age_disqualified_answer(question: str, base: dict):
    """A clean, definitive 'no' when foster-care-at-13+ already checks out
    but the stated age is explicitly outside 18-25 -- not a generic defer,
    same distinction income_eligibility draws for HOH's age test."""
    if not income_credits.detect_fytc_age_disqualified(question):
        return None
    amount = _fytc_income_amount(question)
    if amount is None:
        return None
    result = {**base, "status": "answered", "category": "foster_youth_tax_credit",
              "amount": amount, "tax": 0.0}
    result["answer_text"] = (
        "Based on what you stated, you do not qualify for the Foster Youth Tax Credit -- "
        "FYTC requires being 18 to 25 years old at the end of the tax year (2025 FTB 3514 "
        "Booklet, Step 10)."
    )
    return result


def _income_fytc_checklist_incomplete_answer(question: str, base: dict):
    """Specific checklist instead of a generic defer -- same pattern as
    every other missing-fact clarifying message in this project."""
    if not income_credits.detect_fytc_checklist_incomplete(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Foster Youth Tax Credit (FYTC), I need ALL of the following "
        "stated in one question: (1) your California earned income and number of "
        "qualifying children (so I can verify you're allowed the CalEITC first -- FYTC "
        "requires it), (2) your age (must be 18 to 25 at year end), and (3) that you were "
        "in foster care at age 13 or older, placed through the California foster care "
        "system. Example: \"what is my FYTC if I am 20, was in foster care at age 15, and "
        "made $9,975 with no children?\""
    )
    return result


def _senior_hoh_income_amount(question: str):
    """Excludes the stated age before isolating the income figure --
    same 'exclude known non-target numbers' pattern as FYTC/itemized/
    capital-loss, applied preemptively here rather than rediscovered
    through testing (a two-number question -- age + income -- is exactly
    the shape that pattern already handles cleanly)."""
    known = set()
    age = income_credits.senior_hoh_stated_age(question)
    if age is not None:
        known.add(float(age))
    others = [a for a, _, _ in _amounts(question) if a not in known]
    return others[0] if len(others) == 1 else None


def _income_senior_hoh_answer(question: str, base: dict):
    """Senior Head of Household Credit -- see
    income_credits.compute_senior_hoh_credit's docstring for the formula,
    the separate-eligibility-ceiling wrinkle, and why AGI (not taxable
    income) is the figure this trusts directly -- the 540 instructions
    state the $98,652 ceiling is AGI-based specifically, and deriving
    taxable income from AGI would need a current filing status that's
    genuinely ambiguous for someone whose qualifying person just died."""
    if not income_credits.detect_senior_hoh_signal(question):
        return None
    agi = _senior_hoh_income_amount(question)
    if agi is None:
        return None
    hit = income_credits.compute_senior_hoh_credit(agi)
    if not hit:
        return None
    result = {**base, "status": "answered", "category": "senior_hoh_credit",
              "amount": agi, "tax": hit["credit"], "citation": hit["citation"],
              "source_url": hit["source_url"]}
    if not hit["eligible_income"]:
        result["answer_text"] = (
            f"Based on ${agi:,.2f} in California AGI -- at or above the "
            f"${income_credits.SENIOR_HOH_INCOME_CEILING:,.0f} ceiling -- you do not qualify "
            f"for the Senior Head of Household Credit ({hit['citation']})."
        )
    else:
        result["answer_text"] = (
            f"Assuming ${agi:,.2f} in California AGI, and that you were 65 or older at year "
            "end, qualified as Head of Household for at least 1 of the past 2 years, and "
            "your qualifying person died within the past 2 years, your estimated Senior "
            f"Head of Household Credit is ${hit['credit']:,.2f} (2% of taxable income, "
            f"capped at ${income_credits.SENIOR_HOH_MAX_CREDIT:,.0f}) ({hit['citation']}). "
            "This uses your AGI as an approximation for taxable income, which can only "
            "OVERSTATE the credit slightly (never understate it) -- your actual credit "
            "depends on filing a complete return."
        )
    return result


def _income_senior_hoh_age_disqualified_answer(question: str, base: dict):
    """Clean 'no' when the other facts check out but age is explicitly
    under 65 -- not a generic defer."""
    if not income_credits.detect_senior_hoh_age_disqualified(question):
        return None
    result = {**base, "status": "answered", "category": "senior_hoh_credit", "tax": 0.0}
    result["answer_text"] = (
        "Based on what you stated, you do not qualify for the Senior Head of Household "
        f"Credit -- it requires being 65 or older at year end ({income_credits.SENIOR_HOH_CITATION})."
    )
    return result


def _income_senior_hoh_checklist_incomplete_answer(question: str, base: dict):
    if not income_credits.detect_senior_hoh_checklist_incomplete(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Senior Head of Household Credit, I need ALL of the following "
        "stated in one question: (1) that you were 65 or older at year end, (2) that you "
        "qualified as Head of Household for at least 1 of the past 2 years, (3) that your "
        "qualifying person died within the past 2 years, and (4) your California AGI. "
        "Example: \"what is my senior head of household credit if I am 67, qualified for "
        "head of household last year, my qualifying person died this year, and my AGI is "
        "$40,000?\""
    )
    return result


def _quantity_excluded_amount(question: str):
    """Returns the single dollar amount left after excluding any figure
    immediately followed by 'month(s)'/'day(s)' -- a duration quantity
    (e.g. "the last 6 months", "146 days") is never the target tax-
    liability figure. Preemptive use of the FYTC lesson (a non-dollar
    number can still collide with the real one) for Joint
    Custody/Dependent Parent's simpler 1-2-number questions."""
    ql = question.lower()
    kept = []
    for amount, start, end in _amounts(question):
        after = ql[end:end + 10].strip()
        if after.startswith("month") or after.startswith("day"):
            continue
        kept.append(amount)
    return kept[0] if len(kept) == 1 else None


def _joint_custody_tax_amount(question: str):
    """Also excludes the stated residency-day count specifically (on top
    of the generic month/day-quantity exclusion above), since Joint
    Custody's day count is checked as a NUMBER, not just skipped as a
    quantity phrase."""
    known = set()
    days = income_credits.joint_custody_stated_days(question)
    if days is not None:
        known.add(float(days))
    ql = question.lower()
    kept = []
    for amount, start, end in _amounts(question):
        if amount in known:
            continue
        after = ql[end:end + 10].strip()
        if after.startswith("month") or after.startswith("day"):
            continue
        kept.append(amount)
    return kept[0] if len(kept) == 1 else None


def _income_joint_custody_answer(question: str, base: dict):
    """Joint Custody Head of Household Credit -- see
    income_credits.compute_shared_hoh_parent_credit's docstring for why
    this trusts a STATED tax-liability figure directly (Form 540 line 35
    is the taxpayer's computed CA tax before special credits, a figure
    this project's bracket engine doesn't derive since it doesn't model
    CA's exemption-credit mechanic)."""
    if not income_credits.detect_joint_custody_signal(question):
        return None
    tax_liability = _joint_custody_tax_amount(question)
    if tax_liability is None:
        return None
    hit = income_credits.compute_shared_hoh_parent_credit(
        tax_liability, income_credits.JOINT_CUSTODY_CITATION, income_credits.JOINT_CUSTODY_SOURCE_URL)
    if not hit:
        return None
    result = {**base, "status": "answered", "category": "joint_custody_hoh_credit",
              "amount": tax_liability, "tax": hit["credit"], "citation": hit["citation"],
              "source_url": hit["source_url"]}
    result["answer_text"] = (
        f"Assuming ${tax_liability:,.2f} in California tax liability (Form 540 line 35, "
        "your computed tax before special credits), and that you have joint custody of "
        "your child/stepchild/grandchild under a custody agreement, pay more than half "
        "their expenses, were unmarried (or married but lived apart from your spouse all "
        "year), and your home was your child's main home for between 146 and 219 days, "
        "your estimated Joint Custody Head of Household Credit is "
        f"${hit['credit']:,.2f} (30% of tax liability, capped at "
        f"${income_credits.JOINT_CUSTODY_MAX_CREDIT:,.0f}) ({hit['citation']}). This is an "
        "estimate only -- your actual credit depends on filing a complete return, and you "
        "cannot claim this together with the Dependent Parent Credit."
    )
    return result


def _income_joint_custody_residency_disqualified_answer(question: str, base: dict):
    """Clean 'no' when every other fact checks out but the stated day
    count is explicitly outside 146-219 -- not a generic defer."""
    if not income_credits.detect_joint_custody_residency_disqualified(question):
        return None
    result = {**base, "status": "answered", "category": "joint_custody_hoh_credit", "tax": 0.0}
    result["answer_text"] = (
        "Based on what you stated, you do not qualify for the Joint Custody Head of "
        "Household Credit -- your home must have been your child's main home for at least "
        "146 but not more than 219 days of the year "
        f"({income_credits.JOINT_CUSTODY_CITATION}). (If the child lived with you MORE than "
        "half the year, you may qualify for Head of Household filing status instead -- ask "
        "about that separately.)"
    )
    return result


def _income_joint_custody_checklist_incomplete_answer(question: str, base: dict):
    if not income_credits.detect_joint_custody_checklist_incomplete(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Joint Custody Head of Household Credit, I need ALL of the "
        "following stated in one question: (1) that you have joint custody of your child/"
        "stepchild/grandchild under a custody agreement, (2) that you pay more than half "
        "their (or the household's) expenses, (3) that you were unmarried, or married but "
        "lived apart from your spouse the whole year, (4) the number of days (146-219) "
        "your home was the child's main home, and (5) your California tax liability. "
        "Example: \"what is my joint custody head of household credit if I have joint "
        "custody of my daughter, pay more than half her expenses, was unmarried, she lived "
        "with me 180 days, and my tax liability is $2,000?\""
    )
    return result


def _income_dependent_parent_answer(question: str, base: dict):
    """Credit for Dependent Parent -- shares Joint Custody HOH's exact
    amount formula (30% of tax liability, capped at $610), different
    eligibility (must file MFS, spouse apart 6+ months, support a parent
    -- not a child)."""
    if not income_credits.detect_dependent_parent_signal(question):
        return None
    tax_liability = _quantity_excluded_amount(question)
    if tax_liability is None:
        return None
    hit = income_credits.compute_shared_hoh_parent_credit(
        tax_liability, income_credits.DEPENDENT_PARENT_CITATION, income_credits.DEPENDENT_PARENT_SOURCE_URL)
    if not hit:
        return None
    result = {**base, "status": "answered", "category": "dependent_parent_credit",
              "amount": tax_liability, "tax": hit["credit"], "citation": hit["citation"],
              "source_url": hit["source_url"]}
    result["answer_text"] = (
        f"Assuming ${tax_liability:,.2f} in California tax liability (Form 540 line 35, "
        "your computed tax before special credits), and that you were married/RDP filing "
        "separately, your spouse was not a member of your household during the last 6 "
        "months of the year, and you furnished more than half the household expenses for "
        "your dependent mother's or father's home, your estimated Credit for Dependent "
        f"Parent is ${hit['credit']:,.2f} (30% of tax liability, capped at "
        f"${income_credits.JOINT_CUSTODY_MAX_CREDIT:,.0f}) ({hit['citation']}). This is an "
        "estimate only -- your actual credit depends on filing a complete return, and you "
        "cannot claim this together with the Joint Custody Head of Household Credit."
    )
    return result


def _income_dependent_parent_checklist_incomplete_answer(question: str, base: dict):
    if not income_credits.detect_dependent_parent_checklist_incomplete(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your Credit for Dependent Parent, I need ALL of the following stated "
        "in one question: (1) that you were married/RDP filing separately, (2) that your "
        "spouse was not a member of your household during the last 6 months of the year, "
        "(3) that you furnished more than half the household expenses for your dependent "
        "mother's or father's home, and (4) your California tax liability. Example: \"what "
        "is my dependent parent credit if I am married filing separately, my spouse was "
        "not a member of my household for the last six months, I paid more than half my "
        "mother's household expenses, and my tax liability is $2,000?\""
    )
    return result


def _income_renters_credit_answer(conn, question: str, amount, base: dict):
    """Nonrefundable Renter's Credit -- a flat $60/$120 amount by
    filing-status tier with a hard income ceiling (not a gradual phase-out
    like YCTC). Needs filing status like the bracket/deduction paths;
    explicitly discloses the eligibility facts it CANNOT verify from a
    general question (paid rent >=half the year, not a dependent, no
    property tax exemption) rather than silently assuming them."""
    if amount is None or not income_credits.detect_renters_credit_signal(question):
        return None
    fs = income_brackets.detect_filing_status(question)
    if not fs:
        return None
    hit = income_credits.compute_renters_credit(conn, amount, fs)
    if not hit:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "renters_credit",
              "amount": amount, "tax": hit["credit"], "citation": hit["citation"],
              "source_url": hit["source_url"]}
    result["answer_text"] = (
        f"Assuming your California income is ${amount:,.2f}, filing status {label}, and that "
        f"you paid rent in California for at least half the year, were not claimed as a "
        f"dependent, and did not receive a property tax exemption, your California "
        f"Nonrefundable Renter's Credit is ${hit['credit']:,.2f} ({hit['citation']})."
    )
    return result


def _income_military_retirement_answer(conn, question: str, amount, base: dict):
    """Military retirement pay / DoD Survivor Benefit Plan exclusion (NEW
    for TY2025-2029, Schedule CA (540) Tier 1 expansion) -- an AGI
    eligibility CLIFF, not a gradual phase-out (see
    income_credits.compute_military_retirement_exclusion). Answers the
    eligibility question from AGI + filing status alone; deliberately does
    NOT try to also extract a second "amount of retirement pay received"
    figure -- see income_credits.py's module comment for why (the same
    two-clustered-numbers extraction risk that caused real bugs building
    FYTC)."""
    if amount is None or not income_credits.detect_military_retirement_signal(question):
        return None
    fs = income_brackets.detect_filing_status(question)
    if not fs:
        return None
    hit = income_credits.compute_military_retirement_exclusion(amount, fs)
    if not hit:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "military_retirement_exclusion",
              "taxable": not hit["eligible"], "amount": amount,
              "citation": hit["citation"], "source_url": hit["source_url"]}
    if hit["eligible"]:
        result["answer_text"] = (
            f"With federal AGI of ${amount:,.2f} (filing status {label}), which is at or "
            f"below the ${hit['ceiling']:,.0f} limit, you may exclude up to "
            f"${hit['cap_per_type']:,.0f} EACH of military retirement pay and DoD Survivor "
            f"Benefit Plan annuity payments from California income for tax years 2025-2029 "
            f"({hit['citation']})."
        )
    else:
        result["answer_text"] = (
            f"With federal AGI of ${amount:,.2f} (filing status {label}), which EXCEEDS the "
            f"${hit['ceiling']:,.0f} limit for this exclusion, none of your military "
            f"retirement pay or DoD Survivor Benefit Plan annuity is excluded -- it remains "
            f"fully taxable for California, same as federal ({hit['citation']})."
        )
    return result


def _income_military_retirement_missing_fs_answer(question: str, amount, base: dict):
    """Mirrors _income_renters_credit_missing_fs_answer."""
    if amount is None or not income_credits.detect_military_retirement_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To determine whether your military retirement pay or DoD Survivor Benefit Plan "
        "payments are excluded from California income, I need your filing status: single, "
        "married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse -- the AGI limit depends on it. Please ask again and "
        "include it.")
    return result


def _income_nonresident_answer(conn, question: str, base: dict):
    """Ring 3, Phases 1-3 -- full-year CA nonresident OR part-year CA
    resident, wage-only. Tries the numeric partial-split path FIRST (a
    stated CA-source dollar figure via _amount_near/income_nonresident.
    CA_SOURCE_AMOUNT_TERMS, same distinct-anchor exclude-based extraction
    as _income_itemized_answer's tagged figures); for full-year
    nonresidents only, if no such figure is present this falls back to
    the Phase 1 phrase-based ALL/NONE path
    (income_nonresident.detect_ca_source_fraction), which resolves to
    ca_source_amount == total_wages or == 0 before calling the same
    compute function. Part-year residents (Phase 3) have no ALL/NONE
    shortcut -- see income_nonresident.py's module docstring for why --
    so they always need the stated numeric figure. Both populations call
    the EXACT SAME compute_nonresident_wage_tax (confirmed via FTB Pub
    1100 research: identical formula, only what the stated CA-source
    figure MEANS differs); only the detection and answer wording branch
    on `is_part_year`."""
    if income_nonresident.detect_nonresident_signal(question):
        is_part_year = False
    elif income_nonresident.detect_part_year_signal(question):
        is_part_year = True
    else:
        return None
    fs = income_brackets.detect_filing_status(question)
    if not fs:
        return None

    amounts = _amounts(question)
    ca_source_match = _amount_near_filtered_span(question, income_nonresident.CA_SOURCE_AMOUNT_TERMS, amounts)
    if ca_source_match is not None:
        ca_source_amount = ca_source_match[0]
        others = [a for a, _, _ in _remove_amount_span(amounts, ca_source_match)]
        if len(others) != 1:
            return None
        total_wages = others[0]
    elif is_part_year:
        return None
    else:
        fraction = income_nonresident.detect_ca_source_fraction(question)
        if fraction is None:
            return None
        total_wages = _amount(question)
        if total_wages is None:
            return None
        ca_source_amount = total_wages * fraction

    calc = income_nonresident.compute_nonresident_wage_tax(conn, total_wages, ca_source_amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    category = "part_year_resident_wage_tax" if is_part_year else "nonresident_wage_tax"
    result = {**base, "status": "answered", "category": category,
              "amount": total_wages, "taxable_income": calc["ca_taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["ca_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}

    if is_part_year:
        surtax_note = ""
        if calc["surtax"]:
            surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                           f"Tax on your TOTAL income (1% of total taxable income over "
                           f"$1,000,000) ({calc['citation']}).")
        result["answer_text"] = (
            f"Assuming ${calc['ca_source_amount']:,.2f} of your ${total_wages:,.2f} in total "
            f"wage income for the year counts as California-source under the part-year "
            f"resident rule (all wages earned while you were a California resident, plus any "
            f"wages earned while a nonresident that were for work physically performed in "
            f"California), you had no other income, filing status {label}: California computes "
            f"your tax using an EFFECTIVE RATE (the tax on your total income at California's "
            f"regular brackets, divided by that total income -- here, ${calc['tax_on_total']:,.2f} "
            f"/ ${calc['total_taxable_income']:,.2f} = {calc['effective_rate']*100:.2f}%), then "
            f"applies it to your California-source income after prorating your standard "
            f"deduction by the same California-source share (${calc['prorated_deduction']:,.2f} "
            f"of ${calc['standard_deduction']:,.2f}). Your estimated California tax is "
            f"${calc['ca_tax']:,.2f} ({calc['citation']}).{surtax_note} This assumes wage "
            "income only, with no other income from any source -- your actual liability may "
            "differ."
        )
    elif calc["ca_source_amount"] == 0.0:
        result["answer_text"] = (
            f"Assuming none of your ${total_wages:,.2f} in wage income was earned working "
            f"physically in California, and you were a nonresident of California for the "
            f"entire year: you likely owe no California tax on this income, since California "
            f"only taxes nonresidents on California-source income ({calc['citation']}). This "
            "assumes wage income only, with no other California-source income (rental, "
            "business, or investment) -- your actual liability may differ."
        )
    elif calc["ca_source_amount"] == total_wages:
        surtax_note = ""
        if calc["surtax"]:
            surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                           f"Tax (1% of total taxable income over $1,000,000, prorated the same "
                           f"way as the rest of your tax) ({calc['citation']}).")
        result["answer_text"] = (
            f"Assuming all ${total_wages:,.2f} of your wage income was earned working "
            f"physically in California, you had no other income, and you were a nonresident "
            f"of California for the entire year, filing status {label}: California computes "
            f"your tax using an EFFECTIVE RATE (the tax on your total income at California's "
            f"regular brackets, divided by that total income), then applies it to your "
            f"California-source income. Since 100% of your income is California-source here, "
            f"this works out to the SAME ${calc['ca_tax']:,.2f} a California resident with "
            f"identical income would owe ({calc['citation']}).{surtax_note} This assumes wage "
            "income only, with no other income from any source -- your actual liability may "
            "differ, especially if you have income from outside California too (a common "
            "case this assistant doesn't yet handle -- state a nonresident question with "
            "SOME income earned outside California and it will correctly defer rather than "
            "guess)."
        )
    else:
        surtax_note = ""
        if calc["surtax"]:
            surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                           f"Tax on your TOTAL income (1% of total taxable income over "
                           f"$1,000,000) ({calc['citation']}).")
        result["answer_text"] = (
            f"Assuming ${calc['ca_source_amount']:,.2f} of your ${total_wages:,.2f} in total "
            f"wage income was earned working physically in California ({calc['ca_source_fraction']*100:.1f}%), "
            f"you had no other income, and you were a nonresident of California for the "
            f"entire year, filing status {label}: California computes your tax using an "
            f"EFFECTIVE RATE (the tax on your total income at California's regular brackets, "
            f"divided by that total income -- here, ${calc['tax_on_total']:,.2f} / "
            f"${calc['total_taxable_income']:,.2f} = {calc['effective_rate']*100:.2f}%), then "
            f"applies it to your California-source income after prorating your standard "
            f"deduction by the same California-source share (${calc['prorated_deduction']:,.2f} "
            f"of ${calc['standard_deduction']:,.2f}). Your estimated California tax is "
            f"${calc['ca_tax']:,.2f} ({calc['citation']}).{surtax_note} This assumes wage "
            "income only, with no other income from any source -- your actual liability may "
            "differ."
        )
    return result


def _income_nonresident_missing_source_answer(question: str, base: dict):
    """Nonresident or part-year-resident signal present but the CA-source
    amount couldn't be determined -- mirrors the missing-filing-status
    pattern used throughout this project. Gated on at least one dollar
    amount being present at all -- without any figure, the more pressing
    problem is a missing income amount, not a missing CA-source
    specification, so this falls through to a different/generic
    needs_review instead."""
    if not _amounts(question):
        return None
    if income_nonresident.detect_nonresident_missing_source(question):
        result = {**base, "status": "needs_review"}
        result["answer_text"] = (
            "To estimate your California nonresident tax, I need to know how much of your wage "
            "income was earned working physically in California. You can either state a specific "
            "dollar figure (for example, \"$80,000 in wages, $30,000 of which was earned working "
            "in California\"), or say \"I worked entirely in California\" or \"I did not work in "
            "California at all\" if that applies.")
        return result
    if income_nonresident.detect_part_year_missing_source(question):
        result = {**base, "status": "needs_review"}
        result["answer_text"] = (
            "To estimate your California part-year resident tax, I need your total "
            "California-source income for the year under the part-year rule: ALL wages earned "
            "while you were a California resident (regardless of where the work was performed), "
            "PLUS any wages earned while you were a nonresident that were for work physically "
            "performed in California. Please state your total wage income for the year and this "
            "combined California-source figure (for example, \"$90,000 in wages for the year, "
            "$60,000 of which was California-source\").")
        return result
    return None


def _income_nonresident_fallback_answer(question: str, base: dict):
    """Catch-all for any nonresident- OR part-year-resident-signaled
    question that reaches this point without being answered by either
    function above -- e.g. a stated CA-source split that doesn't make
    sense (exceeds total wages), or a missing filing status. Without this,
    the question would fall through to the generic wage-only RESIDENT
    bracket path below and silently compute tax as if the person were a
    full-year California resident -- a confidently wrong answer for
    someone who explicitly said they're a nonresident or part-year
    resident. Deliberately generic wording since this covers several
    distinct underlying causes (invalid split, missing filing status) that
    _income_nonresident_missing_source_answer's more specific message
    doesn't cover."""
    if not (income_nonresident.detect_nonresident_signal(question)
            or income_nonresident.detect_part_year_signal(question)):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "I can see you're asking about California nonresident or part-year resident tax, but "
        "I'm missing something needed to compute it correctly -- your filing status, and your "
        "total wage income along with a California-source dollar amount that doesn't exceed it. "
        "Please restate your question with these details.")
    return result


def _income_k1_grantor_trust_answer(question: str, base: dict):
    """A K-1 question mentioning a GRANTOR trust specifically must be
    redirected, not computed: FTB's optional simplified reporting for
    grantor trusts means the income is taxed DIRECTLY to the grantor on
    the grantor's own personal return, not via a real K-1 -- see
    income_brackets.py's K-1 section docstring. Checked FIRST, before any
    other K-1 logic, since computing a K-1-shaped answer here would be
    based on a form the taxpayer likely never actually receives."""
    if not income_brackets.detect_grantor_trust_mention(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "For a GRANTOR trust, California follows the federal simplified reporting rule: the "
        "trust's income is taxed DIRECTLY to the grantor on the grantor's own personal tax "
        "return -- there isn't a real Schedule K-1 to report in the way this question is "
        "phrased. If you're the grantor, please ask about this income as your own personal "
        "income (for example, state the dollar amount and your filing status directly, the "
        "same way you would for wages) rather than as K-1 income."
    )
    return result


def _income_k1_answer(conn, question: str, base: dict):
    """Business entities Phase B (extended to trust/estate K-1s, same
    session) -- K-1 pass-through income to the INDIVIDUAL beneficiary/
    owner's personal return. See income_brackets.py's K-1 section
    docstring for the verified Schedule CA (540) Line 5 basis and
    disclosed non-modeled adjustments (business AND trust/estate). K-1-
    only scope: assumes the K-1 amount is the taxpayer's ONLY income.
    `amount` is extracted here (not shared with the later generic
    `amount = _amount(question)` line) since this must run BEFORE
    entity_tax's checks below -- a question like "K-1 from my S-corp"
    would otherwise risk being routed to entity_tax's ENTITY-level answer
    instead (defended twice: this function runs first, AND entity_tax.py's
    own K1_EXCLUDE_TERMS refuses to fire on K-1 language regardless of
    ordering). Grantor-trust mentions are intercepted separately, before
    this function is even reached -- see _income_k1_grantor_trust_answer."""
    fs = income_brackets.detect_k1_signal(question)
    if not fs:
        return None
    amount = _amount(question)
    if amount is None:
        return None
    calc = income_brackets.compute_k1_ca_tax(conn, amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "k1_pass_through_income_tax",
              "amount": amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['citation']}).")
    if income_brackets.detect_trust_estate_k1(question):
        adjustment_note = (
            "It does not account for California-specific adjustments that commonly apply "
            "(such as depreciation/basis differences), nor for basis, at-risk, or "
            "passive-activity-loss limitations. Make sure the figure you stated EXCLUDES any "
            "tax-exempt interest shown separately on your K-1 -- that portion is not taxable "
            "and should not be included."
        )
    else:
        adjustment_note = (
            "It does not account for California-specific adjustments that commonly apply "
            "(such as adding back the entity's own California tax, or depreciation/basis "
            "differences), nor for basis, at-risk, or passive-activity-loss limitations, nor "
            "for California's separate elective Pass-Through Entity tax credit if your entity "
            "elected into it."
        )
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in K-1 pass-through income is your ONLY income, filing "
        f"status {label}, and the standard deduction (${calc['standard_deduction']:,.0f}): "
        f"your California tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This uses your STATED K-1 amount as-is -- {adjustment_note} This also "
        f"assumes no other income (wages, self-employment, etc.) -- your actual liability may "
        f"differ."
    )
    return result


def _income_k1_missing_fs_answer(question: str, base: dict):
    """Mirrors _income_missing_filing_status_answer for the K-1-only path."""
    if not income_brackets.detect_k1_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California tax on K-1 pass-through income, I need your filing "
        "status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please ask again and include it (for "
        "example, \"...filing single\" or \"...as head of household\").")
    return result


def _income_k1_fallback_answer(question: str, base: dict):
    """Catch-all for any K-1-signaled question not answered by either
    function above (missing dollar amount, or a K1_COMPLEXITY_EXCLUDE term
    present alongside K-1 language, e.g. mixed wage+K-1 income). Without
    this, a K-1 question mentioning an entity type ("K-1 from my S-corp")
    could fall through toward entity_tax's ENTITY-level answer below (or,
    if that path also declines, the generic wage-only bracket path) and
    risk a confidently wrong answer about the wrong taxpayer -- the entity
    itself, not the individual -- the same bug class as nonresident tax
    Phase 2's fallback fix."""
    q = question.lower()
    if not any(t in q for t in income_brackets.K1_TRIGGERS):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "I can see you're asking about K-1 pass-through income, but I'm missing something "
        "needed to compute your PERSONAL California tax on it -- your filing status and your "
        "stated K-1 income amount. I can currently only handle K-1 income as your ONLY income "
        "source; if you also have wage or self-employment income, please ask about the K-1 "
        "amount on its own, or check back for updates.")
    return result


def _income_k1_capital_gain_answer(conn, question: str, base: dict):
    """K-1 pass-through CAPITAL GAIN (Schedule CA Line 7a / Schedule D
    Line 2) -- see income_brackets.K1_CAPITAL_GAIN_TERMS's module note
    for why this needs its OWN trigger rather than widening K1_TRIGGERS
    (K1_COMPLEXITY_EXCLUDE deliberately excludes "capital gain" from the
    ordinary K-1 income path). Reuses compute_k1_ca_tax's math unchanged
    (same K-1-only, sole-income scope) but overrides the citation to the
    correct Line 2/Schedule D source rather than Line 5/Schedule CA."""
    fs = income_brackets.detect_k1_capital_gain_signal(question)
    if not fs:
        return None
    amount = _amount(question)
    if amount is None:
        return None
    calc = income_brackets.compute_k1_ca_tax(conn, amount, fs)
    if not calc:
        return None
    label = income_brackets.FILING_STATUS_LABELS[fs]
    result = {**base, "status": "answered", "category": "k1_pass_through_capital_gain_tax",
              "amount": amount, "taxable_income": calc["taxable_income"],
              "standard_deduction": calc["standard_deduction"],
              "marginal_rate": calc["marginal_rate"], "tax": calc["total_tax"],
              "citation": income_brackets.K1_CAPITAL_GAIN_CITATION,
              "source_url": income_brackets.K1_CAPITAL_GAIN_SOURCE_URL}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = (f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services "
                       f"Tax (1% of taxable income over $1,000,000) ({calc['surtax_citation']}).")
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in K-1 pass-through capital gain (from a partnership, "
        f"fiduciary, S corporation, or LLC's California Schedule K-1) is your ONLY income, "
        f"filing status {label}, and the standard deduction (${calc['standard_deduction']:,.0f}): "
        f"California taxes capital gains as ordinary income with no special rate "
        f"({income_brackets.K1_CAPITAL_GAIN_CITATION}), so your estimated "
        f"{income_brackets.DEFAULT_TAX_YEAR} California tax is about ${calc['total_tax']:,.2f}."
        f"{surtax_note} This uses your STATED California K-1 capital gain amount as-is -- it "
        "does not account for basis, at-risk, or passive-activity-loss limitations, and "
        "assumes no other income (wages, self-employment, etc.) -- your actual liability may "
        "differ."
    )
    return result


def _income_k1_capital_gain_missing_fs_answer(question: str, base: dict):
    """Mirrors _income_k1_missing_fs_answer for the K-1-capital-gain path."""
    if not income_brackets.detect_k1_capital_gain_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California tax on K-1 pass-through capital gain, I need your filing "
        "status: single, married filing jointly, married filing separately, head of "
        "household, or qualifying surviving spouse. Please ask again and include it.")
    return result


def _entity_tax_answer(conn, question: str, base: dict):
    """Ring 3, business entities Phase A -- entity-level California annual/
    minimum tax (S-corps, LLCs, partnerships). Genuinely different from
    every other path in this domain: it taxes the ENTITY, not an
    individual's personal return (that's Phase B, K-1 pass-through, not
    yet built). See entity_tax.py's module docstring for the verified
    per-entity-type formula. Ambiguous bare "partnership" phrasing is
    handled by _entity_tax_ambiguous_type_answer instead -- this function
    only proceeds once a SPECIFIC entity type is known."""
    entity_type, is_ambiguous = entity_tax.detect_entity_type(question)
    if is_ambiguous or entity_type is None:
        return None
    if not entity_tax.detect_entity_compute_signal(question):
        return None

    ca_income = None
    if entity_type in entity_tax.INCOME_REQUIRED_TYPES:
        ca_income = _amount(question)
        if ca_income is None:
            return None

    is_first_year = entity_tax.detect_first_year(question)
    calc = entity_tax.compute_entity_tax(conn, entity_type, ca_income, is_first_year)
    if not calc:
        return None

    label = entity_tax.ENTITY_TYPE_LABELS[entity_type]
    result = {**base, "status": "answered", "category": "entity_annual_tax",
              "amount": ca_income, "tax": calc["total_tax"],
              "citation": calc["citation"], "source_url": calc["source_url"]}

    parts = []
    if calc["annual_tax_waived"]:
        parts.append(f"the $800 minimum annual tax is WAIVED (this is your {label}'s first "
                      f"taxable year, and California permanently waives the $800 floor for "
                      f"entities formed or qualified on or after January 1, 2020)")
    elif calc["annual_tax"] > 0:
        parts.append(f"an $800 minimum annual tax")
    else:
        parts.append("no annual tax (general partnerships do not owe California's $800 "
                      "minimum annual tax)")
    if calc["income_tax"] > 0:
        rate_pct = calc["income_tax_rate"] * 100
        parts.append(f"${calc['income_tax']:,.2f} in entity-level income tax "
                      f"({rate_pct:.1f}% of ${ca_income:,.2f} in net California income)")
    if calc["fee_amount"] > 0:
        parts.append(f"an LLC fee of ${calc['fee_amount']:,.2f} (based on ${ca_income:,.2f} "
                      f"in total California income) ({calc['fee_citation']})")
    breakdown = "; plus ".join(parts) if len(parts) > 1 else parts[0]

    result["answer_text"] = (
        f"Assuming your {label} is a single-state California entity with no other complicating "
        f"factors: it owes {breakdown}, for a total of ${calc['total_tax']:,.2f} "
        f"(Form {calc['form_number']}) ({calc['citation']}). This covers only the entity's own "
        f"California franchise/annual tax -- it does NOT cover how this income is taxed to you "
        f"personally as a shareholder, partner, or member (a separate calculation from your "
        f"Schedule K-1). This also assumes no multi-state apportionment and no combined/unitary "
        f"group filing -- your actual liability may differ if either applies."
    )
    return result


def _entity_tax_missing_income_answer(question: str, base: dict):
    """Entity type is known and needs an income figure (LLC fee tier or
    S-corp's 1.5%/3.5% income tax), but none was stated."""
    entity_type, is_ambiguous = entity_tax.detect_entity_type(question)
    if is_ambiguous or entity_type is None:
        return None
    if entity_type not in entity_tax.INCOME_REQUIRED_TYPES:
        return None
    if not entity_tax.detect_entity_compute_signal(question):
        return None
    if _amount(question) is not None:
        return None
    label = entity_tax.ENTITY_TYPE_LABELS[entity_type]
    income_label = ("net California income" if entity_type.startswith("s_corp") or entity_type.startswith("c_corp")
                     else "total California income")
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        f"To estimate your {label}'s California tax, I need its {income_label} for the year. "
        f"Please restate your question with that figure (for example, "
        f"\"how much tax does my {label} owe on $300,000 in {income_label}\").")
    return result


def _entity_tax_ambiguous_type_answer(question: str, base: dict):
    """Bare "partnership" with no general/limited/liability qualifier --
    general partnerships owe $0, LPs/LLPs owe $800, so this must not be
    guessed."""
    entity_type, is_ambiguous = entity_tax.detect_entity_type(question)
    if not is_ambiguous:
        return None
    if not entity_tax.detect_entity_compute_signal(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "California taxes partnerships differently depending on the specific type: a GENERAL "
        "partnership owes no California annual tax at all, while a LIMITED partnership (LP) or "
        "limited liability partnership (LLP) owes an $800 minimum annual tax. Please restate "
        "your question specifying which type of partnership you have.")
    return result


def _fiduciary_tax_grantor_redirect_answer(question: str, base: dict):
    """A fiduciary-tax question mentioning a GRANTOR trust must be
    redirected, not computed -- FTB's optional simplified reporting means
    grantor trust income is taxed DIRECTLY to the grantor on the grantor's
    own personal return, so a grantor trust never owes THIS fiduciary-
    level tax at all. Reuses income_brackets.GRANTOR_TRUST_TERMS, the same
    constant trust/estate Phase B's K-1 grantor-trust redirect uses.
    Checked FIRST, before any other fiduciary-tax logic."""
    q = question.lower()
    if not any(t in q for t in income_brackets.GRANTOR_TRUST_TERMS):
        return None
    if not fiduciary_tax.detect_fiduciary_compute_signal(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "For a GRANTOR trust, California follows the federal simplified reporting rule: the "
        "trust's income is taxed DIRECTLY to the grantor on the grantor's own personal tax "
        "return -- the trust itself does not pay this fiduciary-level tax. If you're the "
        "grantor, please ask about this income as your own personal income instead (state the "
        "dollar amount and your filing status directly, the same way you would for wages)."
    )
    return result


def _fiduciary_tax_answer(conn, question: str, base: dict):
    """Ring 3, trust/estate Phase A -- fiduciary-level tax on RETAINED
    (undistributed) trust/estate income. See fiduciary_tax.py's module
    docstring for the verified Form 541 mechanic (reuses
    income_brackets.compute_ca_tax's bracket step unchanged, subtracts a
    small exemption credit). Two shapes: a full-distribution question
    (answered directly, $0 fiduciary tax, no residency/amount needed) or a
    retained-income question (needs both the CA-residency bail-out
    assertion AND a stated dollar amount)."""
    fiduciary_type = fiduciary_tax.detect_fiduciary_type(question)
    if not fiduciary_type:
        return None
    if not fiduciary_tax.detect_fiduciary_compute_signal(question):
        return None
    label = fiduciary_tax.FIDUCIARY_TYPE_LABELS[fiduciary_type]

    if fiduciary_tax.detect_full_distribution(question):
        result = {**base, "status": "answered", "category": "fiduciary_tax", "tax": 0.0}
        result["answer_text"] = (
            f"If your {label} distributed ALL of its income to beneficiaries, it owes NO "
            f"California fiduciary income tax itself -- California allows a distribution "
            f"deduction equal to the amount distributed (up to distributable net income), "
            f"which offsets the {label}'s own taxable income entirely. Each beneficiary "
            f"instead reports and pays tax on their own share via Schedule K-1 (541) -- ask "
            f"about that as K-1 pass-through income on the beneficiary's personal return."
        )
        return result

    if not fiduciary_tax.detect_ca_resident_entity_assertion(question):
        return None

    amount = _amount(question)
    if amount is None:
        return None
    calc = fiduciary_tax.compute_fiduciary_tax(conn, amount, fiduciary_type)
    if not calc:
        return None

    result = {**base, "status": "answered", "category": "fiduciary_tax",
              "amount": amount, "tax": calc["total_tax"], "marginal_rate": calc["marginal_rate"],
              "citation": calc["citation"], "source_url": calc["source_url"]}
    surtax_note = ""
    if calc["surtax"]:
        surtax_note = f" This includes a ${calc['surtax']:,.2f} Behavioral Health Services Tax."
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} of retained (undistributed) income and that your {label} "
        f"qualifies for California's residency bail-out (no Schedule G apportionment needed): "
        f"California computes fiduciary tax using the SAME bracket schedule as an individual "
        f"filer (tax before credit: ${calc['tax_before_credit']:,.2f}), then subtracts a "
        f"${calc['exemption_credit']:,.2f} exemption credit ({calc['citation']}), for a total "
        f"California tax of ${calc['total_tax']:,.2f}.{surtax_note} This assumes the retained "
        f"amount is genuinely NOT distributed to beneficiaries (distributed income is instead "
        f"taxed to beneficiaries via K-1, not here), and doesn't account for the full "
        f"Distributable Net Income computation (tax-exempt income, capital gains allocated to "
        f"corpus) -- a reasonable estimate for the simple case, not a guarantee."
    )
    return result


def _fiduciary_tax_missing_residency_answer(question: str, base: dict):
    """Fiduciary type + compute signal present, not a full-distribution
    question, but no CA-residency bail-out condition was stated."""
    fiduciary_type = fiduciary_tax.detect_fiduciary_type(question)
    if not fiduciary_type:
        return None
    if not fiduciary_tax.detect_fiduciary_compute_signal(question):
        return None
    if fiduciary_tax.detect_full_distribution(question):
        return None
    if fiduciary_tax.detect_ca_resident_entity_assertion(question):
        return None
    label = fiduciary_tax.FIDUCIARY_TYPE_LABELS[fiduciary_type]
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        f"California's rules for whether a {label}'s income is fully taxable here (versus "
        f"needing apportionment) depend on residency. I can compute this only for the "
        f"straightforward case where ALL trustees are California residents, OR all "
        f"non-contingent beneficiaries are California residents, OR all of the {label}'s "
        f"income is California-source. Please restate your question confirming one of these "
        f"applies (for example, \"all trustees are California residents\")."
    )
    return result


def _fiduciary_tax_missing_amount_answer(question: str, base: dict):
    """Fiduciary type + compute signal + residency assertion all present,
    not a full-distribution question, but no retained-income figure was
    stated."""
    fiduciary_type = fiduciary_tax.detect_fiduciary_type(question)
    if not fiduciary_type:
        return None
    if not fiduciary_tax.detect_fiduciary_compute_signal(question):
        return None
    if fiduciary_tax.detect_full_distribution(question):
        return None
    if not fiduciary_tax.detect_ca_resident_entity_assertion(question):
        return None
    if _amount(question) is not None:
        return None
    label = fiduciary_tax.FIDUCIARY_TYPE_LABELS[fiduciary_type]
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        f"To estimate your {label}'s California fiduciary tax, I need the amount of RETAINED "
        f"(undistributed) income for the year. Please restate your question with that figure."
    )
    return result


def _fiduciary_tax_fallback_answer(question: str, base: dict):
    """Catch-all for any fiduciary-type-and-compute-signaled question not
    answered by any function above (e.g. an invalid amount). Mirrors the
    same defensive-fallback discipline as nonresident tax Phase 2 and the
    K-1/entity-tax collision fix -- though here the risk is lower since
    "trust"/"estate" are already excluded from the generic wage-only
    bracket path's own COMPLEXITY_EXCLUDE, so this is belt-and-suspenders
    for a clearer message, not the last line of defense against a wrong
    answer."""
    fiduciary_type = fiduciary_tax.detect_fiduciary_type(question)
    if not fiduciary_type:
        return None
    if not fiduciary_tax.detect_fiduciary_compute_signal(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "I can see you're asking about California fiduciary tax on trust or estate income, but "
        "I'm missing something needed to compute it correctly -- either confirmation that all "
        "of the income was distributed to beneficiaries, or a stated retained-income amount "
        "plus confirmation that the trust/estate qualifies as fully California-taxable (see "
        "the residency conditions above). Please restate your question with these details."
    )
    return result


def _income_renters_credit_missing_fs_answer(question: str, amount, base: dict):
    """Mirrors _income_missing_filing_status_answer / _income_missing_children_answer."""
    if amount is None or not income_credits.detect_renters_credit_missing_filing_status(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To estimate your California Renter's Credit, I need your filing status: single, "
        "married filing jointly, married filing separately, head of household, or "
        "qualifying surviving spouse -- the credit amount depends on it. Please ask again "
        "and include it.")
    return result


def _income_hoh_determination_answer(question: str, base: dict):
    """A REAL Head of Household eligibility determination -- see
    income_eligibility's module docstring for the FTB Form 3532 conformity
    basis and the narrow v1 scope (unmarried the entire year, taxpayer's
    own child, child lived with taxpayer >half the year, taxpayer paid
    >half the home costs, simple age/full-time-student test). Distinct
    from the existing head_of_household_eligibility INFORMATIONAL topic
    (which only explains the criteria) -- this gives an actual yes/no."""
    verdict = income_eligibility.detect_hoh_determination(question)
    if verdict is None:
        return None
    result = {**base, "status": "answered", "category": "head_of_household_determination",
              "taxable": verdict, "citation": income_eligibility.HOH_CITATION,
              "source_url": income_eligibility.HOH_SOURCE_URL}
    if verdict:
        result["answer_text"] = (
            "Based on what you stated -- unmarried the entire year, your child lived with "
            "you more than half the year, you paid more than half the cost of keeping up "
            "your home, and your child meets the age/student test -- you qualify for "
            "California Head of Household filing status. This assumes the support, joint-"
            f"return, and citizenship requirements are also met ({income_eligibility.HOH_CITATION}). "
            "You must also attach Form FTB 3532 to your return."
        )
    else:
        result["answer_text"] = (
            "Based on what you stated -- married for the entire year -- you do not qualify "
            "for California Head of Household filing status. (If you were married but lived "
            "apart from your spouse for the last 6 months of the year, you may still be "
            f"\"considered unmarried\" for HOH purposes -- see {income_eligibility.HOH_CITATION} "
            "for that separate test, which this assistant doesn't evaluate.)"
        )
    return result


def _income_hoh_checklist_incomplete_answer(question: str, base: dict):
    """When the question is clearly asking for an HOH determination but
    doesn't state enough facts to reach one, give the specific checklist
    instead of a generic defer -- same pattern as every other
    missing-fact clarifying message in this module."""
    if not income_eligibility.detect_hoh_checklist_incomplete(question):
        return None
    result = {**base, "status": "needs_review"}
    result["answer_text"] = (
        "To determine if you qualify for California Head of Household filing status, "
        "I need ALL of the following stated in one question: (1) that you were unmarried "
        "the entire year, (2) that you paid more than half the cost of keeping up your "
        "home, (3) that your own child (birth, step, adopted, or foster) lived with you "
        "more than half the year, and (4) your child's age (and whether they were a "
        "full-time student, if age 19-23). Example: \"I was unmarried all year, paid more "
        "than half the cost of keeping up my home, and my 10-year-old son lived with me "
        "all year -- do I qualify for head of household?\" (This assistant only handles "
        "this simplest case -- married-but-separated taxpayers and qualifying RELATIVES, "
        f"rather than your own child, need FTB's fuller test: {income_eligibility.HOH_CITATION}.)"
    )
    return result


def _income_topic_by_key(conn, compose: bool, topic_key: str, base: dict, branches=None):
    """Build a structured income_tax_topics verdict for a KNOWN topic_key --
    shared by the embedding-routed path (_income_topic_answer, which may pass
    disclosed opposite-verdict `branches`) and the cross-domain override
    (_cross_domain_income_override), which already knows exactly which topic
    applies and skips routing/branch-finding entirely."""
    topic = _income_lookup(conn, topic_key)
    if not topic:
        return None
    t_key, t_taxable, _t_treatment, t_citation, t_summary, t_source_url = topic
    branches = branches or []
    result = {**base, "status": "conditional" if branches else "answered", "category": t_key,
              "taxable": bool(t_taxable) if t_taxable is not None else None,
              "citation": t_citation, "source_url": t_source_url, "branches": branches}
    if not compose:
        return result
    verdict = ("not taxable in California" if t_taxable is False
               else "taxable in California" if t_taxable else "treatment varies")
    text = (f"{t_summary} ({t_citation})" if t_summary else f"This is {verdict} ({t_citation}).")
    if branches:
        alts = "; ".join(
            f"if {b['condition']}, it may instead be "
            f"{'taxable' if b['taxable'] else 'not taxable'} ({b['citation']})"
            for b in branches)
        text += f" NOTE -- this depends on the specifics: {alts}."
    result["answer_text"] = text
    return result


def _income_topic_answer(conn, question: str, compose: bool, rows, base: dict):
    """Structured topic verdict (income_tax_topics, via income_rule_embeddings
    routing -- the Phase 2 scaffold, now real). Unlike the informational
    tier, this states an actual taxable/not-taxable fact with its own
    citation, not just a paraphrased pointer.

    Also checks for opposite-verdict BRANCHES near the primary pick (mirrors
    the sales-tax _find_branches call in _answer()) -- added after
    collision_audit.py --domain=income flagged gambling_winnings (taxable)
    sitting only 0.008 from california_lottery_winnings (exempt), and live
    testing confirmed it was a REAL bug: "I won money from the california
    lottery, is that taxable" was answered a confident (and wrong) TAXABLE,
    with no disclosure of the CA-specific lottery exclusion, because this
    path never called _find_branches at all until now."""
    if not rows or float(rows[0][2]) > INCOME_EMBED_ROUTER_THRESHOLD:
        return None
    primary_key = rows[0][0]
    primary = _income_lookup(conn, primary_key)
    branches = []
    if primary and primary[1] is not None:
        margin = BRANCH_MARGIN if _route_confidence(question, rows) else UNCERTAIN_BRANCH_MARGIN
        branches = _find_branches(conn, question, rows, primary_key, bool(primary[1]),
                                   margin=margin, table="income_rule_embeddings",
                                   branch_info=_income_branch_info)
    return _income_topic_by_key(conn, compose, primary_key, base, branches=branches)


# Curated CROSS-DOMAIN overrides -- same shape/philosophy as DISAMBIG, but
# redirecting to the INCOME domain instead of resolving within sales tax.
# Found by direct testing, not theorized: "do I have to pay tax on a gift I
# received" confidently matched BOTH domains (sales 0.24 -> promotional_gifts,
# CDTFA Reg 1670, the GIVER's use-tax liability on property given away --
# income 0.23 -> gifts_and_inheritance, correctly nontaxable). Under the
# normal sequential fallback (sales tried first, see the module note above
# _answer_income) sales would win and hand back a CONFIDENTLY WRONG verdict
# on what is actually an income-tax question -- exactly the "confident in
# both domains" case the Phase 2 plan review flagged as a real risk, now
# concretely observed. Each entry: (OR-group A, OR-group B, income topic_key)
# -- both groups must hit, deliberately narrow/high-precision (the asker's
# own words must put them on the RECEIVING end; "I gave a gift" must NOT
# fire this, since that genuinely is the sales-tax giver question).
CROSS_DOMAIN_INCOME_OVERRIDE = [
    ({"gift", "gifts", "inheritance", "inherit", "inherited"},
     {"received", "receive", "receiving", "got", "get"},
     "gifts_and_inheritance"),
    # Found live via income_item_sweep.py right after the Schedule CA Tier 1
    # expansion added educator_expenses: sales' INFORMATIONAL tier (not even
    # a rule match -- doc_chunks) latched onto an entirely unrelated CDTFA
    # "Cooking Class Providers" industry guide (dist=0.265, under threshold)
    # purely on "classroom"/"instruction"-flavored vocabulary overlap,
    # shadowing the income-domain educator expense deduction question
    # before it was ever tried. group_b requires a "deduct*" word
    # specifically (not just "tax") since "classroom supplies... sales tax"
    # is a genuinely different, legitimate SALES question (is the PURCHASE
    # taxable) -- "deduct" is a verb that essentially never appears in that
    # framing, so it safely discriminates the income-tax reading.
    ({"classroom", "educator", "educators", "teacher", "teachers"},
     {"deduct", "deducting", "deductible", "deduction", "deductions"},
     "educator_expenses"),
]

# Additional receiver signal found by the cross-domain sweep (2026-07-28):
# "my grandmother GAVE ME money as a gift" is just as clearly receiver-
# phrased as "I received a gift", but shares none of group_b's words --
# it was routing to sales/promotional_gifts (WRONG, confidently). Bag-of-
# words alone can't safely add "gave" to group_b (that would also fire on
# the genuinely giver-phrased "I gave a gift"); the real signal is ADJACENCY
# -- gave/give/given immediately followed by a first-person object
# (me/us) -- so this needs a phrase regex, not another bare word.
_RECEIVER_GAVE_PATTERN = re.compile(r"\b(?:gave|give|given)\b(?:\s+\w+){0,2}\s+(?:me|us)\b")


def _cross_domain_income_override(question: str):
    ql = question.lower()
    q = set(re.findall(r"[a-z]+", ql))
    for group_a, group_b, topic_key in CROSS_DOMAIN_INCOME_OVERRIDE:
        if (group_a & q) and ((group_b & q) or _RECEIVER_GAVE_PATTERN.search(ql)):
            return topic_key
    return None


def _answer_income(conn, question: str, compose: bool, qv):
    """FTB income-tax domain fallback. Only called from _answer()'s guard
    block, after the sales-tax path has already failed to answer -- see the
    module-level note above. Tries, in order: standard-deduction lookup ->
    bracket/tax computation (simplest wage-earner case only) -> CalEITC
    table lookup -> Young Child Tax Credit -> structured topic verdict ->
    informational citation. Returns None (caller falls through to
    needs_review) if nothing in the income domain matches."""
    base = {
        "category": None, "taxable": None, "rate": None, "amount": None,
        "tax": None, "citation": None, "source_url": None, "location": None,
        "rate_basis": None, "branches": [], "fees": [], "info": None,
        "city_cannabis_tax": None, "route_dist": None, "rerank_v2_key": None,
        "domain": "income",
    }
    dedu_result = _income_deduction_answer(conn, question, base)
    if dedu_result:
        return dedu_result

    # Senior HOH / Joint Custody HOH / Dependent Parent checked HERE,
    # BEFORE the generic wage-only bracket path below -- found via
    # testing: these 3 credits' own NAMES contain "head of household"
    # (which income_brackets.detect_filing_status reads as a genuine
    # filing-status statement) and their natural phrasing ("my tax
    # liability is $X") happens to contain "tax liability", one of
    # income_brackets.COMPUTE_TRIGGERS. Together, a complete, valid
    # question for one of these 3 credits was being silently hijacked by
    # the generic bracket-compute system before ever reaching this
    # credit-specific logic -- once misdetected as HOH filing status
    # with a stray dollar amount as "gross wage income", it either
    # computed a bogus $0 bracket answer or (worse) fired the generic
    # "please state your filing status" defer despite one having been
    # given, just not in bracket-path vocabulary. None of the OTHER
    # credits below share "head of household" in their name or need a
    # "tax liability" figure, so they aren't exposed to this same
    # collision and didn't need to move.
    senior_hoh_result = _income_senior_hoh_answer(question, base)
    if senior_hoh_result:
        return senior_hoh_result

    senior_hoh_age_disqualified_result = _income_senior_hoh_age_disqualified_answer(question, base)
    if senior_hoh_age_disqualified_result:
        return senior_hoh_age_disqualified_result

    senior_hoh_incomplete_result = _income_senior_hoh_checklist_incomplete_answer(question, base)
    if senior_hoh_incomplete_result:
        return senior_hoh_incomplete_result

    joint_custody_result = _income_joint_custody_answer(question, base)
    if joint_custody_result:
        return joint_custody_result

    joint_custody_disqualified_result = _income_joint_custody_residency_disqualified_answer(question, base)
    if joint_custody_disqualified_result:
        return joint_custody_disqualified_result

    joint_custody_incomplete_result = _income_joint_custody_checklist_incomplete_answer(question, base)
    if joint_custody_incomplete_result:
        return joint_custody_incomplete_result

    dependent_parent_result = _income_dependent_parent_answer(question, base)
    if dependent_parent_result:
        return dependent_parent_result

    dependent_parent_incomplete_result = _income_dependent_parent_checklist_incomplete_answer(question, base)
    if dependent_parent_incomplete_result:
        return dependent_parent_incomplete_result

    # Military retirement exclusion checked HERE too, same reason as the 3
    # credits above: natural phrasing ("how much tax do I owe on my
    # military retirement, AGI is $130,000 filing single") can satisfy the
    # generic wage-only bracket path's own trigger conditions (COMPUTE_
    # TRIGGERS + a detected filing status + a dollar amount) before ever
    # reaching this credit-specific logic if placed later.
    military_amount = _amount(question)
    military_retirement_result = _income_military_retirement_answer(conn, question, military_amount, base)
    if military_retirement_result:
        return military_retirement_result

    military_retirement_missing_fs_result = _income_military_retirement_missing_fs_answer(
        question, military_amount, base)
    if military_retirement_missing_fs_result:
        return military_retirement_missing_fs_result

    # Nonresident/part-year-resident tax (Ring 3, Phases 1-3) checked HERE too, same reason as
    # military retirement above: "how much tax do I owe on my wages,
    # filing single" phrasing is exactly what the generic wage-only
    # bracket path also triggers on, and this credit-specific logic must
    # get first look.
    nonresident_result = _income_nonresident_answer(conn, question, base)
    if nonresident_result:
        return nonresident_result

    nonresident_missing_source_result = _income_nonresident_missing_source_answer(
        question, base)
    if nonresident_missing_source_result:
        return nonresident_missing_source_result

    nonresident_fallback_result = _income_nonresident_fallback_answer(question, base)
    if nonresident_fallback_result:
        return nonresident_fallback_result

    # K-1 pass-through income (business entities Phase B) checked BEFORE
    # entity-level tax below -- a question mentioning both an entity type
    # ("K-1 from my S-corp") and K-1 language must be answered as the
    # INDIVIDUAL's personal tax on the pass-through income, not the
    # ENTITY's own tax, so this needs first look (also defended inside
    # entity_tax.py itself via K1_EXCLUDE_TERMS, in case ordering ever
    # changes).
    k1_grantor_trust_result = _income_k1_grantor_trust_answer(question, base)
    if k1_grantor_trust_result:
        return k1_grantor_trust_result

    k1_result = _income_k1_answer(conn, question, base)
    if k1_result:
        return k1_result

    k1_missing_fs_result = _income_k1_missing_fs_answer(question, base)
    if k1_missing_fs_result:
        return k1_missing_fs_result

    k1_capital_gain_result = _income_k1_capital_gain_answer(conn, question, base)
    if k1_capital_gain_result:
        return k1_capital_gain_result

    k1_capital_gain_missing_fs_result = _income_k1_capital_gain_missing_fs_answer(question, base)
    if k1_capital_gain_missing_fs_result:
        return k1_capital_gain_missing_fs_result

    k1_fallback_result = _income_k1_fallback_answer(question, base)
    if k1_fallback_result:
        return k1_fallback_result

    # Entity-level business tax (Ring 3, business entities Phase A) checked
    # HERE too, same defensive-early reasoning as the paths above --
    # entity-tax vocabulary (LLC/S-corp/partnership) is already excluded
    # from the generic wage-only path's COMPLEXITY_EXCLUDE, but this
    # dedicated path needs first look to actually ANSWER rather than just
    # correctly avoid answering.
    entity_result = _entity_tax_answer(conn, question, base)
    if entity_result:
        return entity_result

    entity_missing_income_result = _entity_tax_missing_income_answer(question, base)
    if entity_missing_income_result:
        return entity_missing_income_result

    entity_ambiguous_result = _entity_tax_ambiguous_type_answer(question, base)
    if entity_ambiguous_result:
        return entity_ambiguous_result

    foreign_earned_income_result = _income_foreign_earned_income_answer(conn, question, base)
    if foreign_earned_income_result:
        return foreign_earned_income_result

    missing_foreign_earned_income_fs_result = _income_foreign_earned_income_missing_filing_status_answer(question, base)
    if missing_foreign_earned_income_fs_result:
        return missing_foreign_earned_income_fs_result

    subpart_f_result = _income_subpart_f_answer(conn, question, base)
    if subpart_f_result:
        return subpart_f_result

    missing_subpart_f_fs_result = _income_subpart_f_missing_filing_status_answer(question, base)
    if missing_subpart_f_fs_result:
        return missing_subpart_f_fs_result

    gilti_result = _income_gilti_answer(conn, question, base)
    if gilti_result:
        return gilti_result

    missing_gilti_fs_result = _income_gilti_missing_filing_status_answer(question, base)
    if missing_gilti_fs_result:
        return missing_gilti_fs_result

    nra_foreign_income_result = _income_nra_foreign_income_answer(conn, question, base)
    if nra_foreign_income_result:
        return nra_foreign_income_result

    missing_nra_foreign_income_fs_result = _income_nra_foreign_income_missing_filing_status_answer(question, base)
    if missing_nra_foreign_income_fs_result:
        return missing_nra_foreign_income_fs_result

    # Real-estate-professional (IRC 469(c)(7) non-conformity) checked
    # HERE, BEFORE fiduciary trust/estate tax below -- found live via
    # testing: fiduciary_tax.detect_fiduciary_type deliberately matches
    # bare "estate" as a substring (its own docstring calls this an
    # "accepted... low-risk" tradeoff, reasoning that a false match could
    # only pick the wrong small exemption credit on an ACTUAL fiduciary
    # question) -- but "real ESTATE professional" also contains "estate",
    # and without this ordering, a real-estate-professional question was
    # swallowed by the fiduciary fallback's generic trustee/beneficiary-
    # residency defer instead of this path's own dedicated answer. Same
    # "move the check earlier" fix as K-1 capital gain's dispatcher
    # placement, not a change to fiduciary_tax's own accepted tradeoff.
    real_estate_pro_result = _income_real_estate_pro_answer(conn, question, base)
    if real_estate_pro_result:
        return real_estate_pro_result

    missing_re_pro_fs_result = _income_real_estate_pro_missing_filing_status_answer(question, base)
    if missing_re_pro_fs_result:
        return missing_re_pro_fs_result

    missing_re_pro_mfs_status_result = _income_real_estate_pro_missing_mfs_status_answer(question, base)
    if missing_re_pro_mfs_status_result:
        return missing_re_pro_mfs_status_result

    # Fiduciary-level trust/estate tax (Ring 3, trust/estate Phase A)
    # checked HERE, after entity_tax -- "trust"/"estate" vocabulary
    # doesn't collide with entity_tax's own type checks (S-corp/LLC/
    # partnership), and this path defends against the K-1 path itself
    # (fiduciary_tax.detect_fiduciary_compute_signal refuses to fire on
    # K-1 language), so ordering relative to the K-1 checks above doesn't
    # matter for correctness, only for which specific message a
    # mixed-signal question gets.
    fiduciary_grantor_result = _fiduciary_tax_grantor_redirect_answer(question, base)
    if fiduciary_grantor_result:
        return fiduciary_grantor_result

    fiduciary_result = _fiduciary_tax_answer(conn, question, base)
    if fiduciary_result:
        return fiduciary_result

    fiduciary_missing_residency_result = _fiduciary_tax_missing_residency_answer(question, base)
    if fiduciary_missing_residency_result:
        return fiduciary_missing_residency_result

    fiduciary_missing_amount_result = _fiduciary_tax_missing_amount_answer(question, base)
    if fiduciary_missing_amount_result:
        return fiduciary_missing_amount_result

    fiduciary_fallback_result = _fiduciary_tax_fallback_answer(question, base)
    if fiduciary_fallback_result:
        return fiduciary_fallback_result

    exemption_credit_result = _income_exemption_credit_answer(conn, question, base)
    if exemption_credit_result:
        return exemption_credit_result

    missing_exemption_credit_fs_result = _income_exemption_credit_missing_filing_status_answer(question, base)
    if missing_exemption_credit_fs_result:
        return missing_exemption_credit_fs_result

    use_tax_result = _income_use_tax_answer(question, base)
    if use_tax_result:
        return use_tax_result

    use_tax_over_cap_result = _income_use_tax_over_cap_answer(question, base)
    if use_tax_over_cap_result:
        return use_tax_over_cap_result

    other_state_tax_credit_result = _income_other_state_tax_credit_answer(conn, question, base)
    if other_state_tax_credit_result:
        return other_state_tax_credit_result

    missing_other_state_tax_credit_fs_result = _income_other_state_tax_credit_missing_filing_status_answer(question, base)
    if missing_other_state_tax_credit_fs_result:
        return missing_other_state_tax_credit_fs_result

    pte_credit_result = _income_pte_credit_answer(conn, question, base)
    if pte_credit_result:
        return pte_credit_result

    missing_pte_credit_fs_result = _income_pte_credit_missing_filing_status_answer(question, base)
    if missing_pte_credit_fs_result:
        return missing_pte_credit_fs_result

    late_penalty_reasonable_cause_result = _income_late_penalty_reasonable_cause_answer(question, base)
    if late_penalty_reasonable_cause_result:
        return late_penalty_reasonable_cause_result

    late_penalty_result = _income_late_penalty_answer(question, base)
    if late_penalty_result:
        return late_penalty_result

    early_distribution_result = _income_early_distribution_answer(question, base)
    if early_distribution_result:
        return early_distribution_result

    early_distribution_exception_result = _income_early_distribution_exception_answer(question, base)
    if early_distribution_exception_result:
        return early_distribution_exception_result

    early_distribution_other_account_result = _income_early_distribution_other_account_answer(question, base)
    if early_distribution_other_account_result:
        return early_distribution_other_account_result

    cdc_credit_result = _income_cdc_credit_answer(question, base)
    if cdc_credit_result:
        return cdc_credit_result

    cdc_credit_out_of_scope_result = _income_cdc_credit_out_of_scope_answer(question, base)
    if cdc_credit_out_of_scope_result:
        return cdc_credit_out_of_scope_result

    adoption_credit_result = _income_adoption_credit_answer(conn, question, base)
    if adoption_credit_result:
        return adoption_credit_result

    missing_adoption_credit_fs_result = _income_adoption_credit_missing_filing_status_answer(question, base)
    if missing_adoption_credit_fs_result:
        return missing_adoption_credit_fs_result

    adoption_credit_out_of_scope_result = _income_adoption_credit_out_of_scope_answer(question, base)
    if adoption_credit_out_of_scope_result:
        return adoption_credit_out_of_scope_result

    adoption_credit_ambiguous_result = _income_adoption_credit_ambiguous_eligibility_answer(question, base)
    if adoption_credit_ambiguous_result:
        return adoption_credit_ambiguous_result

    catc_credit_result = _income_catc_credit_answer(conn, question, base)
    if catc_credit_result:
        return catc_credit_result

    missing_catc_credit_fs_result = _income_catc_credit_missing_filing_status_answer(question, base)
    if missing_catc_credit_fs_result:
        return missing_catc_credit_fs_result

    isr_penalty_result = _income_isr_penalty_answer(question, base)
    if isr_penalty_result:
        return isr_penalty_result

    missing_isr_penalty_fs_result = _income_isr_penalty_missing_filing_status_answer(question, base)
    if missing_isr_penalty_fs_result:
        return missing_isr_penalty_fs_result

    isr_penalty_out_of_scope_result = _income_isr_penalty_out_of_scope_answer(question, base)
    if isr_penalty_out_of_scope_result:
        return isr_penalty_out_of_scope_result

    isr_penalty_ambiguous_result = _income_isr_penalty_ambiguous_coverage_answer(question, base)
    if isr_penalty_ambiguous_result:
        return isr_penalty_ambiguous_result

    # AMT ISO extension checked BEFORE the base AMT screen below -- an ISO-
    # exercise question satisfies both this feature's own trigger AND the
    # base screen's own out-of-scope exclusion (which defers to this
    # feature), same "more specific before generic" ordering discipline
    # used throughout this codebase (basis-difference family, NOL-mixed).
    amt_iso_same_year_sale_result = _income_amt_iso_same_year_sale_answer(question, base)
    if amt_iso_same_year_sale_result:
        return amt_iso_same_year_sale_result

    amt_iso_result = _income_amt_iso_answer(conn, question, base)
    if amt_iso_result:
        return amt_iso_result

    missing_amt_iso_fs_result = _income_amt_iso_missing_filing_status_answer(question, base)
    if missing_amt_iso_fs_result:
        return missing_amt_iso_fs_result

    amt_screen_result = _income_amt_screen_answer(conn, question, base)
    if amt_screen_result:
        return amt_screen_result

    missing_amt_screen_fs_result = _income_amt_screen_missing_filing_status_answer(question, base)
    if missing_amt_screen_fs_result:
        return missing_amt_screen_fs_result

    amt_screen_out_of_scope_result = _income_amt_screen_out_of_scope_answer(question, base)
    if amt_screen_out_of_scope_result:
        return amt_screen_out_of_scope_result

    kiddie_tax_result = _income_kiddie_tax_answer(conn, question, base)
    if kiddie_tax_result:
        return kiddie_tax_result

    missing_kiddie_tax_fs_result = _income_kiddie_tax_missing_filing_status_answer(question, base)
    if missing_kiddie_tax_fs_result:
        return missing_kiddie_tax_fs_result

    kiddie_tax_out_of_scope_result = _income_kiddie_tax_out_of_scope_answer(question, base)
    if kiddie_tax_out_of_scope_result:
        return kiddie_tax_out_of_scope_result

    underpayment_result = _income_underpayment_answer(conn, question, base)
    if underpayment_result:
        return underpayment_result

    missing_underpayment_fs_result = _income_underpayment_missing_filing_status_answer(question, base)
    if missing_underpayment_fs_result:
        return missing_underpayment_fs_result

    # Underpayment REGULAR method (late/partial estimated payments) --
    # mutually exclusive with the short method above by construction
    # (the short method's own signal excludes on estimated-payment
    # vocabulary; this one requires it), so order relative to it doesn't
    # matter for correctness -- positioned here, before the now-narrowed
    # out-of-scope check, so a question this feature CAN'T fully extract
    # still gets its own template-teaching message instead of the
    # generic farmer/fisherman redirect below.
    underpayment_regular_result = _income_underpayment_regular_answer(conn, question, base)
    if underpayment_regular_result:
        return underpayment_regular_result

    missing_underpayment_regular_fs_result = _income_underpayment_regular_missing_filing_status_answer(question, base)
    if missing_underpayment_regular_fs_result:
        return missing_underpayment_regular_fs_result

    underpayment_regular_template_result = _income_underpayment_regular_template_answer(question, base)
    if underpayment_regular_template_result:
        return underpayment_regular_template_result

    underpayment_out_of_scope_result = _income_underpayment_out_of_scope_answer(question, base)
    if underpayment_out_of_scope_result:
        return underpayment_out_of_scope_result

    amount = _amount(question)
    compute_result = _income_compute_answer(conn, question, amount, base)
    if compute_result:
        return compute_result

    missing_fs_result = _income_missing_filing_status_answer(question, amount, base)
    if missing_fs_result:
        return missing_fs_result

    se_result = _income_self_employment_answer(conn, question, amount, base)
    if se_result:
        return se_result

    missing_se_fs_result = _income_self_employment_missing_filing_status_answer(question, amount, base)
    if missing_se_fs_result:
        return missing_se_fs_result

    mixed_result = _income_mixed_wage_se_answer(conn, question, base)
    if mixed_result:
        return mixed_result

    missing_mixed_fs_result = _income_mixed_wage_se_missing_filing_status_answer(question, base)
    if missing_mixed_fs_result:
        return missing_mixed_fs_result

    itemized_result = _income_itemized_answer(conn, question, base)
    if itemized_result:
        return itemized_result

    missing_itemized_fs_result = _income_itemized_missing_filing_status_answer(question, base)
    if missing_itemized_fs_result:
        return missing_itemized_fs_result

    itemized_mfs_result = _income_itemized_mfs_answer(question, base)
    if itemized_mfs_result:
        return itemized_mfs_result

    capital_loss_carryover_result = _income_capital_loss_carryover_answer(conn, question, base)
    if capital_loss_carryover_result:
        return capital_loss_carryover_result

    missing_capital_loss_carryover_fs_result = _income_capital_loss_carryover_missing_filing_status_answer(question, base)
    if missing_capital_loss_carryover_fs_result:
        return missing_capital_loss_carryover_fs_result


    capital_loss_result = _income_capital_loss_answer(conn, question, base)
    if capital_loss_result:
        return capital_loss_result

    missing_capital_loss_fs_result = _income_capital_loss_missing_filing_status_answer(question, base)
    if missing_capital_loss_fs_result:
        return missing_capital_loss_fs_result

    # Excess business loss CARRYOVER (Line 8z) checked HERE, BEFORE the
    # plain Line 8p excess-business-loss check below -- "excess business
    # loss carryover" contains "excess business loss" as a substring, so
    # without this ordering the plain Line 8p detector would swallow
    # carryover-phrased questions first (same "move the check earlier"
    # fix as K-1 capital gain/real-estate-professional's placement).
    ebl_carryover_result = _income_ebl_carryover_answer(conn, question, base)
    if ebl_carryover_result:
        return ebl_carryover_result

    ebl_carryover_partial_result = _income_ebl_carryover_partial_answer(question, base)
    if ebl_carryover_partial_result:
        return ebl_carryover_partial_result

    missing_ebl_carryover_fs_result = _income_ebl_carryover_missing_filing_status_answer(question, base)
    if missing_ebl_carryover_fs_result:
        return missing_ebl_carryover_fs_result

    excess_business_loss_result = _income_excess_business_loss_answer(conn, question, base)
    if excess_business_loss_result:
        return excess_business_loss_result

    missing_ebl_fs_result = _income_excess_business_loss_missing_filing_status_answer(question, base)
    if missing_ebl_fs_result:
        return missing_ebl_fs_result

    nol_result = _income_nol_answer(conn, question, base)
    if nol_result:
        return nol_result

    missing_nol_fs_result = _income_nol_missing_filing_status_answer(question, base)
    if missing_nol_fs_result:
        return missing_nol_fs_result

    nol_wages_result = _income_nol_wages_answer(conn, question, base)
    if nol_wages_result:
        return nol_wages_result

    missing_nol_wages_fs_result = _income_nol_wages_missing_filing_status_answer(question, base)
    if missing_nol_wages_fs_result:
        return missing_nol_wages_fs_result

    # Mixed-source NOL checked BEFORE nol_wages_ambiguous below -- both
    # require an ongoing-business signal to differ from the closed-
    # business case, so they're mutually exclusive by construction, but
    # this ordering keeps the more specific "here's a computed answer"
    # path ahead of the generic clarifying-question fallback regardless.
    nol_mixed_result = _income_nol_mixed_answer(conn, question, base)
    if nol_mixed_result:
        return nol_mixed_result

    missing_nol_mixed_fs_result = _income_nol_mixed_missing_filing_status_answer(question, base)
    if missing_nol_mixed_fs_result:
        return missing_nol_mixed_fs_result

    nol_wages_ambiguous_result = _income_nol_wages_ambiguous_answer(question, base)
    if nol_wages_ambiguous_result:
        return nol_wages_ambiguous_result

    disaster_loss_carryover_result = _income_disaster_loss_carryover_answer(conn, question, base)
    if disaster_loss_carryover_result:
        return disaster_loss_carryover_result

    missing_disaster_loss_carryover_fs_result = _income_disaster_loss_carryover_missing_filing_status_answer(question, base)
    if missing_disaster_loss_carryover_fs_result:
        return missing_disaster_loss_carryover_fs_result

    # Installment sale basis difference checked BEFORE the generic basis-
    # difference checks below -- found live: a natural phrasing like
    # "installment sale with a basis difference" contains BOTH "basis
    # difference" (a GENERIC_BASIS_DIFF_TERMS trigger) AND "installment
    # sale" (one of GENERIC_BASIS_DIFF_OUT_OF_SCOPE_TERMS's own terms),
    # so without this ordering the GENERIC feature's own out-of-scope
    # redirect would claim the question first, before the MORE SPECIFIC
    # installment-sale feature ever got a chance to answer it (same "move
    # the more specific check earlier" fix as K-1 capital gain/EBL
    # carryover's dispatcher placement).
    installment_sale_basis_diff_result = _income_installment_sale_basis_diff_answer(conn, question, base)
    if installment_sale_basis_diff_result:
        return installment_sale_basis_diff_result

    missing_installment_sale_basis_diff_fs_result = _income_installment_sale_basis_diff_missing_filing_status_answer(question, base)
    if missing_installment_sale_basis_diff_fs_result:
        return missing_installment_sale_basis_diff_fs_result

    installment_sale_basis_diff_out_of_scope_result = _income_installment_sale_basis_diff_out_of_scope_answer(question, base)
    if installment_sale_basis_diff_out_of_scope_result:
        return installment_sale_basis_diff_out_of_scope_result

    # Home-sale depreciation basis difference checked BEFORE the generic
    # basis-difference checks below too, same reasoning as installment
    # sale's own ordering fix -- "home sale"/"personal residence" is one
    # of GENERIC_BASIS_DIFF_OUT_OF_SCOPE_TERMS's own exclusion terms.
    home_sale_basis_diff_result = _income_home_sale_basis_diff_answer(conn, question, base)
    if home_sale_basis_diff_result:
        return home_sale_basis_diff_result

    missing_home_sale_basis_diff_fs_result = _income_home_sale_basis_diff_missing_filing_status_answer(question, base)
    if missing_home_sale_basis_diff_fs_result:
        return missing_home_sale_basis_diff_fs_result

    home_sale_basis_diff_out_of_scope_result = _income_home_sale_basis_diff_out_of_scope_answer(question, base)
    if home_sale_basis_diff_out_of_scope_result:
        return home_sale_basis_diff_out_of_scope_result

    # Schedule D-1/Form 4797 basis difference checked BEFORE the generic
    # basis-difference checks below too -- proactively applying the same
    # ordering lesson from installment sale/home sale, since the generic
    # feature's own out-of-scope guard doesn't know about Schedule D-1/
    # Form 4797 vocabulary and could otherwise claim the question first.
    schedule_d1_basis_diff_result = _income_schedule_d1_basis_diff_answer(conn, question, base)
    if schedule_d1_basis_diff_result:
        return schedule_d1_basis_diff_result

    missing_schedule_d1_basis_diff_fs_result = _income_schedule_d1_basis_diff_missing_filing_status_answer(question, base)
    if missing_schedule_d1_basis_diff_fs_result:
        return missing_schedule_d1_basis_diff_fs_result

    schedule_d1_basis_diff_out_of_scope_result = _income_schedule_d1_basis_diff_out_of_scope_answer(question, base)
    if schedule_d1_basis_diff_out_of_scope_result:
        return schedule_d1_basis_diff_out_of_scope_result

    # Rental/royalty depreciation basis difference checked BEFORE the
    # generic basis-difference checks below too, same proactive ordering
    # as installment sale/home sale/Schedule D-1.
    rental_depreciation_basis_diff_result = _income_rental_depreciation_basis_diff_answer(conn, question, base)
    if rental_depreciation_basis_diff_result:
        return rental_depreciation_basis_diff_result

    missing_rental_depreciation_basis_diff_fs_result = _income_rental_depreciation_basis_diff_missing_filing_status_answer(question, base)
    if missing_rental_depreciation_basis_diff_fs_result:
        return missing_rental_depreciation_basis_diff_fs_result

    rental_depreciation_basis_diff_out_of_scope_result = _income_rental_depreciation_basis_diff_out_of_scope_answer(question, base)
    if rental_depreciation_basis_diff_out_of_scope_result:
        return rental_depreciation_basis_diff_out_of_scope_result

    # Farm depreciation basis difference checked BEFORE the generic
    # basis-difference checks below too, same proactive ordering as the
    # rest of this family.
    farm_depreciation_basis_diff_result = _income_farm_depreciation_basis_diff_answer(conn, question, base)
    if farm_depreciation_basis_diff_result:
        return farm_depreciation_basis_diff_result

    missing_farm_depreciation_basis_diff_fs_result = _income_farm_depreciation_basis_diff_missing_filing_status_answer(question, base)
    if missing_farm_depreciation_basis_diff_fs_result:
        return missing_farm_depreciation_basis_diff_fs_result

    farm_depreciation_basis_diff_out_of_scope_result = _income_farm_depreciation_basis_diff_out_of_scope_answer(question, base)
    if farm_depreciation_basis_diff_out_of_scope_result:
        return farm_depreciation_basis_diff_out_of_scope_result

    # IRA distribution basis difference checked BEFORE the generic
    # basis-difference checks below too, same proactive ordering as the
    # rest of this family.
    ira_distribution_basis_diff_result = _income_ira_distribution_basis_diff_answer(conn, question, base)
    if ira_distribution_basis_diff_result:
        return ira_distribution_basis_diff_result

    missing_ira_distribution_basis_diff_fs_result = _income_ira_distribution_basis_diff_missing_filing_status_answer(question, base)
    if missing_ira_distribution_basis_diff_fs_result:
        return missing_ira_distribution_basis_diff_fs_result

    ira_distribution_basis_diff_out_of_scope_result = _income_ira_distribution_basis_diff_out_of_scope_answer(question, base)
    if ira_distribution_basis_diff_out_of_scope_result:
        return ira_distribution_basis_diff_out_of_scope_result

    generic_basis_diff_result = _income_generic_basis_diff_answer(conn, question, base)
    if generic_basis_diff_result:
        return generic_basis_diff_result

    missing_generic_basis_diff_fs_result = _income_generic_basis_diff_missing_filing_status_answer(question, base)
    if missing_generic_basis_diff_fs_result:
        return missing_generic_basis_diff_fs_result

    generic_basis_diff_out_of_scope_result = _income_generic_basis_diff_out_of_scope_answer(question, base)
    if generic_basis_diff_out_of_scope_result:
        return generic_basis_diff_out_of_scope_result

    cannabis_280e_result = _income_cannabis_280e_answer(conn, question, base)
    if cannabis_280e_result:
        return cannabis_280e_result

    missing_cannabis_280e_fs_result = _income_cannabis_280e_missing_filing_status_answer(question, base)
    if missing_cannabis_280e_fs_result:
        return missing_cannabis_280e_fs_result

    ambiguous_cannabis_280e_result = _income_cannabis_280e_ambiguous_amount_answer(question, base)
    if ambiguous_cannabis_280e_result:
        return ambiguous_cannabis_280e_result

    roth_ira_result = _income_roth_ira_answer(question, base)
    if roth_ira_result:
        return roth_ira_result

    ira_deduction_result = _income_ira_deduction_answer(conn, question, base)
    if ira_deduction_result:
        return ira_deduction_result

    missing_ira_fs_result = _income_ira_deduction_missing_filing_status_answer(question, base)
    if missing_ira_fs_result:
        return missing_ira_fs_result

    qsbs_result = _income_qsbs_answer(conn, question, base)
    if qsbs_result:
        return qsbs_result

    missing_qsbs_fs_result = _income_qsbs_missing_filing_status_answer(question, base)
    if missing_qsbs_fs_result:
        return missing_qsbs_fs_result

    hsa_gain_result = _income_hsa_investment_gain_answer(conn, question, base)
    if hsa_gain_result:
        return hsa_gain_result

    missing_hsa_gain_fs_result = _income_hsa_investment_gain_missing_filing_status_answer(question, base)
    if missing_hsa_gain_fs_result:
        return missing_hsa_gain_fs_result

    fringe_benefit_result = _income_fringe_benefit_answer(conn, question, base)
    if fringe_benefit_result:
        return fringe_benefit_result

    missing_fringe_benefit_fs_result = _income_fringe_benefit_missing_filing_status_answer(question, base)
    if missing_fringe_benefit_fs_result:
        return missing_fringe_benefit_fs_result

    caleitc_investment_result = _income_caleitc_investment_answer(conn, question, base)
    if caleitc_investment_result:
        return caleitc_investment_result

    missing_caleitc_investment_children_result = _income_caleitc_investment_missing_children_answer(question, base)
    if missing_caleitc_investment_children_result:
        return missing_caleitc_investment_children_result

    caleitc_result = _income_caleitc_answer(conn, question, amount, base)
    if caleitc_result:
        return caleitc_result

    missing_children_result = _income_missing_children_answer(question, amount, base)
    if missing_children_result:
        return missing_children_result

    ycta_result = _income_ycta_answer(conn, question, amount, base)
    if ycta_result:
        return ycta_result

    fytc_result = _income_fytc_answer(conn, question, base)
    if fytc_result:
        return fytc_result

    fytc_age_disqualified_result = _income_fytc_age_disqualified_answer(question, base)
    if fytc_age_disqualified_result:
        return fytc_age_disqualified_result

    fytc_incomplete_result = _income_fytc_checklist_incomplete_answer(question, base)
    if fytc_incomplete_result:
        return fytc_incomplete_result

    renters_result = _income_renters_credit_answer(conn, question, amount, base)
    if renters_result:
        return renters_result

    missing_renters_fs_result = _income_renters_credit_missing_fs_answer(question, amount, base)
    if missing_renters_fs_result:
        return missing_renters_fs_result

    hoh_result = _income_hoh_determination_answer(question, base)
    if hoh_result:
        return hoh_result

    hoh_incomplete_result = _income_hoh_checklist_incomplete_answer(question, base)
    if hoh_incomplete_result:
        return hoh_incomplete_result

    rows = _income_route_candidates(conn, qv)
    topic_result = _income_topic_answer(conn, question, compose, rows, base)
    if topic_result:
        return topic_result

    info = _income_info_lookup(conn, qv)
    if not info:
        return None
    result = {**base, "status": "informational", "info": info,
              "citation": info["citation"], "source_url": info["source_url"]}
    if not compose:
        return result
    cite = info["citation"] or info["source_url"] or "official FTB source"
    prompt = (
        "The user's question is about California PERSONAL INCOME TAX (Franchise "
        "Tax Board topics: filing status, deductions, credits, which income types "
        "are taxable, residency) -- NOT sales/use tax on a purchase. Using ONLY "
        "the source text below, answer in 2-3 sentences and cite the source. If "
        "the source states specific numbers (deduction amounts, credit amounts, "
        "rates), include them; do not state any fact that is not in the source.\n"
        f"SOURCE ({cite}): {info['text'][:1200]}\n"
        f"QUESTION: {question}"
    )
    text = model.generate_content(prompt).text.strip()
    result["answer_text"] = (
        f"{text}\n(Informational -- based on {cite}; not tax advice specific to "
        "your situation.)")
    return result


def _effective_rate(conn, taxable, base_rate, question, location):
    """Pick the rate to apply. Only standard-rate (7.25%) taxable items get
    localized; partial/special-rate items are left alone; exempt stays 0.
    Returns (eff_rate, rate_basis, loc_label).

    Tries ADDRESS-LEVEL precision first (district_rates.py, a live CDTFA
    API call) when the question contains a full street address -- only
    then, since it's the one case city/county granularity (local_rates.py)
    can't resolve on its own (a sub-city special tax district). Falls
    through to the existing city/county path on ANY failure: no address
    detected, the API call fails/times out, or the geocode confidence
    isn't high enough to trust -- district_rates.lookup_by_address already
    encodes all of that as a plain None, so this function doesn't need its
    own special-casing beyond "if it returned something, use it; if not,
    fall back exactly as before this feature existed." A near-boundary
    AMBIGUOUS result (two different rates plausible) is used but flagged
    in rate_basis rather than silently picked, matching this project's
    disclosure-over-silent-guessing pattern elsewhere (HOH/credit branches,
    conditional sales verdicts)."""
    if not taxable:
        return base_rate, "exempt", None
    if base_rate in SPECIAL_RATES:
        loc = location or local_rates.detect(conn, question)
        loc_info = local_rates.resolve(conn, loc) if loc else None
        return base_rate, "special/partial rate; local district tax not auto-applied", \
            (loc_info["label"] if loc_info else None)

    addr = district_rates.detect_address(question)
    if addr:
        street, city, zip_code = addr
        addr_result = district_rates.lookup_by_address(street, city, zip_code)
        if addr_result:
            if addr_result["ambiguous"]:
                alt = addr_result["alternates"][0]
                basis = (
                    f"address-level rate for {addr_result['formatted_address']} "
                    f"({addr_result['jurisdiction']}) -- NOTE: this address is near a tax-"
                    f"rate-area boundary; a nearby area (\"{alt['jurisdiction']}\") has a "
                    f"different rate of {alt['rate'] * 100:.3f}%, so confirm the exact rate "
                    f"with CDTFA if precision matters ({addr_result['citation']})"
                )
            else:
                basis = (
                    f"address-level rate for {addr_result['formatted_address']} "
                    f"({addr_result['jurisdiction']}, {addr_result['citation']})"
                )
            return addr_result["rate"], basis, addr_result["formatted_address"]
        # address detected but the API couldn't confidently resolve it --
        # fall back to the CITY already parsed out of the address, not
        # local_rates.detect(question) below: its "at <city>" regex greedily
        # grabs the STREET portion instead of the city when both follow
        # "at" in the same question ("at 123 Main St, Sacramento" ->
        # captures "123 Main St"), which would otherwise drop a perfectly
        # resolvable city rate all the way down to the statewide base.
        loc_info = local_rates.resolve(conn, city)
        if loc_info:
            return loc_info["rate"], \
                f"combined rate for {loc_info['label']} (as of {loc_info['as_of']})", \
                loc_info["label"]
        # city itself doesn't resolve either (e.g. a non-CA city) -- fall
        # through to city/county below, same as if no address was given.

    loc = location or local_rates.detect(conn, question)
    loc_info = local_rates.resolve(conn, loc) if loc else None
    if loc_info:
        return loc_info["rate"], \
            f"combined rate for {loc_info['label']} (as of {loc_info['as_of']})", \
            loc_info["label"]
    return base_rate, "statewide base 7.25% (give a city/county for the local rate)", None


# --- Income Coverage Blueprint, Phase 2b: generalized domain-routing
# intercept -- replaces the growing list of hand-written per-feature
# early-intercept guards (military retirement, cannabis 280E, foreign-
# earned-income, each added only AFTER a real sales-domain misrouting
# bug was found live) with ONE mechanism covering every registered
# income-feature signal at once, including future ones.
#
# THE BUG CLASS THIS FIXES: sales tries first by default (see _answer's
# own docstring); the sales domain's embedding-based router occasionally
# sits close enough to an income-flavored question's embedding to
# confidently (and wrongly) answer it as sales/excise tax on the stated
# dollar figure. This was found FOUR separate times this session --
# military retirement pay vs. sales' federal-areas/vehicle-sale cluster,
# cannabis 280E vs. sales' cannabis excise rule, and foreign-earned-
# income/housing-deduction vs. a general "tangible personal property"
# fallback (the last one for only ONE capitalization of an otherwise-
# identical question) -- each discovered only by adversarial testing,
# never by code review, and each patched with its own hand-written guard
# after the fact. That pattern doesn't scale: every new income feature
# is a fresh, undiscoverable-by-inspection roll of the embedding-
# collision dice until it happens to get tested against the right
# phrasing.
#
# THE FIX: any question that a registered income-feature's OWN
# deterministic signal detector recognizes gets routed through the full
# income pipeline FIRST, unconditionally -- sales never gets a look at
# it, regardless of embedding distance. This is strictly more permissive
# than the 3 guards it replaces (which only covered 3 features); it
# covers all ~40 registered detectors at once, including every feature
# built this session, and any future one gets the SAME protection simply
# by being added to the CHECKS list below -- no bug report required
# first.
#
# WHY THIS IS SAFE (not just convenient): every function in CHECKS
# already requires its OWN feature's specific trigger vocabulary AND
# (for the "_signal" variants) a genuine filing-status statement before
# returning truthy -- these are the SAME conservative detectors already
# proven, across 304 income_item_sweep.py cases, not to misfire on
# ordinary wage/sales-adjacent phrasing. Forcing income-first when one of
# them fires does not lower that bar; it only guarantees sales doesn't
# get an undeserved first crack at a question these detectors already
# recognize as unambiguously income-shaped.
#
# Each check is called defensively: a bad or unexpectedly-erroring
# detector for ONE feature must never take down the intercept for every
# OTHER feature, so exceptions are swallowed per-check, not propagated.
_INCOME_SIGNAL_CHECKS = (
    # income_brackets.py -- core compute paths and their missing-filing-
    # status counterparts (a signal fires on a COMPLETE question; the
    # missing-filing-status variant fires on the same vocabulary minus
    # the filing status, so both must be checked to protect a question
    # that's recognizably this feature but incomplete).
    income_brackets.detect_compute_signal,
    income_brackets.detect_compute_missing_filing_status,
    income_brackets.detect_exemption_credit_signal,
    income_brackets.detect_exemption_credit_missing_filing_status,
    income_brackets.detect_use_tax_signal,
    income_brackets.detect_other_state_tax_credit_signal,
    income_brackets.detect_other_state_tax_credit_missing_filing_status,
    income_brackets.detect_pte_credit_signal,
    income_brackets.detect_pte_credit_missing_filing_status,
    income_brackets.detect_late_penalty_signal,
    income_brackets.detect_late_penalty_reasonable_cause_mention,
    income_brackets.detect_early_distribution_signal,
    income_brackets.detect_early_distribution_exception_mention,
    income_brackets.detect_early_distribution_other_account_mention,
    income_brackets.detect_cdc_credit_signal,
    income_brackets.detect_cdc_credit_out_of_scope,
    income_brackets.detect_adoption_credit_signal,
    income_brackets.detect_adoption_credit_missing_filing_status,
    income_brackets.detect_adoption_credit_out_of_scope,
    income_brackets.detect_adoption_credit_ambiguous_eligibility,
    income_brackets.detect_catc_credit_signal,
    income_brackets.detect_catc_credit_missing_filing_status,
    income_brackets.detect_isr_penalty_signal,
    income_brackets.detect_isr_penalty_missing_filing_status,
    income_brackets.detect_isr_penalty_out_of_scope,
    income_brackets.detect_isr_penalty_ambiguous_coverage,
    income_brackets.detect_amt_screen_signal,
    income_brackets.detect_amt_screen_missing_filing_status,
    income_brackets.detect_amt_screen_out_of_scope,
    income_brackets.detect_amt_iso_signal,
    income_brackets.detect_amt_iso_missing_filing_status,
    income_brackets.detect_amt_iso_same_year_sale,
    income_brackets.detect_kiddie_tax_signal,
    income_brackets.detect_kiddie_tax_missing_filing_status,
    income_brackets.detect_kiddie_tax_out_of_scope,
    income_brackets.detect_underpayment_signal,
    income_brackets.detect_underpayment_missing_filing_status,
    income_brackets.detect_underpayment_out_of_scope,
    income_brackets.detect_underpayment_regular_method_signal,
    income_brackets.detect_underpayment_regular_method_missing_filing_status,
    income_brackets.detect_self_employment_signal,
    income_brackets.detect_self_employment_missing_filing_status,
    income_brackets.detect_mixed_wage_se_signal,
    income_brackets.detect_mixed_wage_se_missing_filing_status,
    income_brackets.detect_k1_signal,
    income_brackets.detect_k1_missing_filing_status,
    income_brackets.detect_grantor_trust_mention,
    income_brackets.detect_trust_estate_k1,
    income_brackets.detect_itemized_signal,
    income_brackets.detect_itemized_missing_filing_status,
    income_brackets.detect_itemized_mfs_unsupported,
    income_brackets.detect_capital_loss_signal,
    income_brackets.detect_capital_loss_missing_filing_status,
    income_brackets.detect_capital_loss_carryover_signal,
    income_brackets.detect_capital_loss_carryover_missing_filing_status,
    income_brackets.detect_excess_business_loss_signal,
    income_brackets.detect_excess_business_loss_missing_filing_status,
    income_brackets.detect_ebl_carryover_signal,
    income_brackets.detect_ebl_carryover_missing_filing_status,
    income_brackets.detect_nol_signal,
    income_brackets.detect_nol_missing_filing_status,
    income_brackets.detect_nol_wages_signal,
    income_brackets.detect_nol_wages_missing_filing_status,
    income_brackets.detect_nol_mixed_signal,
    income_brackets.detect_nol_mixed_missing_filing_status,
    income_brackets.detect_nol_wages_ambiguous,
    income_brackets.detect_disaster_loss_carryover_signal,
    income_brackets.detect_disaster_loss_carryover_missing_filing_status,
    income_brackets.detect_generic_basis_diff_signal,
    income_brackets.detect_generic_basis_diff_missing_filing_status,
    income_brackets.detect_generic_basis_diff_out_of_scope,
    income_brackets.detect_installment_sale_basis_diff_signal,
    income_brackets.detect_installment_sale_basis_diff_missing_filing_status,
    income_brackets.detect_installment_sale_basis_diff_out_of_scope,
    income_brackets.detect_home_sale_basis_diff_signal,
    income_brackets.detect_home_sale_basis_diff_missing_filing_status,
    income_brackets.detect_home_sale_basis_diff_out_of_scope,
    income_brackets.detect_schedule_d1_basis_diff_signal,
    income_brackets.detect_schedule_d1_basis_diff_missing_filing_status,
    income_brackets.detect_schedule_d1_basis_diff_out_of_scope,
    income_brackets.detect_rental_depreciation_basis_diff_signal,
    income_brackets.detect_rental_depreciation_basis_diff_missing_filing_status,
    income_brackets.detect_rental_depreciation_basis_diff_out_of_scope,
    income_brackets.detect_farm_depreciation_basis_diff_signal,
    income_brackets.detect_farm_depreciation_basis_diff_missing_filing_status,
    income_brackets.detect_farm_depreciation_basis_diff_out_of_scope,
    income_brackets.detect_ira_distribution_basis_diff_signal,
    income_brackets.detect_ira_distribution_basis_diff_missing_filing_status,
    income_brackets.detect_ira_distribution_basis_diff_out_of_scope,
    income_brackets.detect_cannabis_280e_signal,
    income_brackets.detect_cannabis_280e_missing_filing_status,
    income_brackets.detect_ira_deduction_signal,
    income_brackets.detect_ira_deduction_missing_filing_status,
    income_brackets.detect_roth_ira_mention,
    income_brackets.detect_qsbs_signal,
    income_brackets.detect_qsbs_missing_filing_status,
    income_brackets.detect_hsa_investment_gain_signal,
    income_brackets.detect_hsa_investment_gain_missing_filing_status,
    income_brackets.detect_k1_capital_gain_signal,
    income_brackets.detect_k1_capital_gain_missing_filing_status,
    income_brackets.detect_fringe_benefit_signal,
    income_brackets.detect_fringe_benefit_missing_filing_status,
    income_brackets.detect_real_estate_pro_signal,
    income_brackets.detect_real_estate_pro_missing_filing_status,
    income_brackets.detect_foreign_earned_income_signal,
    income_brackets.detect_foreign_earned_income_missing_filing_status,
    income_brackets.detect_subpart_f_signal,
    income_brackets.detect_subpart_f_missing_filing_status,
    income_brackets.detect_gilti_signal,
    income_brackets.detect_gilti_missing_filing_status,
    income_brackets.detect_nra_foreign_income_signal,
    income_brackets.detect_nra_foreign_income_missing_filing_status,
    income_brackets.detect_deduction_question,
    # income_credits.py
    income_credits.detect_caleitc_signal,
    income_credits.detect_ycta_signal,
    income_credits.detect_fytc_signal,
    income_credits.detect_renters_credit_signal,
    income_credits.detect_renters_credit_missing_filing_status,
    income_credits.detect_senior_hoh_signal,
    income_credits.detect_joint_custody_signal,
    income_credits.detect_dependent_parent_signal,
    income_credits.detect_military_retirement_signal,
    income_credits.detect_military_retirement_missing_filing_status,
    # entity_tax.py / fiduciary_tax.py -- top-level compute signals only
    # (detect_entity_type/detect_fiduciary_type are sub-helpers for WHICH
    # entity/fiduciary type, not "does this domain apply" checks).
    entity_tax.detect_entity_compute_signal,
    fiduciary_tax.detect_fiduciary_compute_signal,
    # income_nonresident.py
    income_nonresident.detect_nonresident_signal,
    income_nonresident.detect_part_year_signal,
)


def _income_has_any_signal(question: str) -> bool:
    """True iff ANY registered income-feature detector recognizes this
    question -- see the module note above _INCOME_SIGNAL_CHECKS for why
    this replaces the old per-feature early-intercept guard pattern."""
    for check in _INCOME_SIGNAL_CHECKS:
        try:
            if check(question):
                return True
        except Exception:
            continue
    return False


def _answer(question: str, compose: bool = True, location: str = None,
            router: str = None, tax_type: str = None) -> dict:
    """`tax_type` (None|"sales"|"income") is a HINT, not a hard gate --
    "income" tries the income domain first but still falls back to sales if
    income can't answer; None/"sales" behave identically to each other
    (sales first, income as the existing fallback/override). A wrong manual
    pick degrades gracefully instead of failing hard. See the Ring 2
    database-split plan (design decision #4) for the full rationale."""
    router = router or ROUTER
    with db.get_conn() as conn:
        # cross-domain override -- checked BEFORE sales routing so a known
        # collision (see CROSS_DOMAIN_INCOME_OVERRIDE) can never be shadowed
        # by a confident-but-wrong sales-tax match; sales never gets a look.
        cross_topic = _cross_domain_income_override(question)
        if cross_topic:
            base = {
                "category": None, "taxable": None, "rate": None, "amount": None,
                "tax": None, "citation": None, "source_url": None, "location": None,
                "rate_basis": None, "branches": [], "fees": [], "info": None,
                "city_cannabis_tax": None, "route_dist": None, "rerank_v2_key": None,
                "domain": "income",
            }
            with income_db.get_conn() as iconn:
                cross_result = _income_topic_by_key(iconn, compose, cross_topic, base)
            if cross_result:
                return cross_result

        # Generalized income-signal intercept (Income Coverage Blueprint,
        # Phase 2b) -- see _income_has_any_signal's module note above for
        # the full rationale. This ONE check replaces what used to be
        # three separate hand-written guards here (military retirement,
        # cannabis 280E, foreign-earned-income/housing-deduction), each
        # added only after its own live sales-misrouting bug was found.
        # Those three signals are now just three of the ~40 entries in
        # _INCOME_SIGNAL_CHECKS -- removing their bespoke guards here is
        # not a scope reduction, it's replacing three special cases with
        # the general mechanism that already subsumes them, PLUS every
        # other registered income feature that never got its own
        # incident-driven guard at all.
        if _income_has_any_signal(question):
            qv_income_signal = _embed(question)
            with income_db.get_conn() as iconn:
                income_signal_result = _answer_income(iconn, question, compose, qv_income_signal)
            if income_signal_result:
                return income_signal_result

        branches, qv = [], None
        route_dist = None
        rerank_v2_key = None      # shadow mode -- see _rerank_v2
        income_tried = False

        if tax_type == "income":
            # user-hinted: try income FIRST, still fall back to sales below
            # if income can't answer. _answer_income requires a real qv (it
            # embeds unconditionally), so compute it now and reuse it in the
            # sales router below instead of embedding a second time.
            qv = _embed(question)
            with income_db.get_conn() as iconn:
                income_first = _answer_income(iconn, question, compose, qv)
            income_tried = True
            if income_first:
                return income_first

        if router == "embed":
            hit = _disambiguate(question)
            if hit:
                key = _verify_disambig_hit(conn, question, hit)  # curated match, self-verified
            else:
                qv = qv if qv is not None else _embed(question)
                rows = _route_candidates(conn, qv)
                if rows:
                    route_dist = float(rows[0][2])
                if not rows or route_dist > EMBED_ROUTER_THRESHOLD:
                    key = "unknown"
                else:
                    key = _rerank(question, rows)
                    rerank_v2_key = _rerank_v2(conn, question, rows)  # shadow, unused for routing
                    if TIEBREAK_ENABLED:
                        key = _llm_tiebreak(conn, question, rows, key)
        else:
            key = _extract_key(question, _catalog(conn))
        amount = _amount(question)
        rule = _lookup(conn, key) if key and key != "unknown" else None
        fees = fee_layer.applicable(question, amount)   # CDTFA fees that stack on sales tax

        # city-level Cannabis Business Tax (a LOCAL ordinance, not CDTFA -- see
        # cannabis_local.py; only surfaces for cities we've actually researched)
        loc_str = location or local_rates.detect(conn, question)
        loc_info = local_rates.resolve(conn, loc_str) if loc_str else None
        city_cannabis = cannabis_local.applicable(
            conn, question, loc_info["jurisdiction"] if loc_info else None, amount)

        # --- guard: nothing matched -> fee, then informational, then defer ---
        if not rule:
            base = {
                "category": None, "taxable": None, "rate": None, "amount": amount,
                "tax": None, "citation": None, "source_url": None, "location": None,
                "rate_basis": None, "branches": [], "fees": fees, "info": None,
                "city_cannabis_tax": city_cannabis, "route_dist": route_dist,
                "rerank_v2_key": rerank_v2_key, "domain": "sales",
            }
            if fees:
                # question is about a fee we DO cover, even without a sales-tax rule
                if not compose:
                    return {**base, "status": "answered"}
                fl = "; ".join(f"{f['name']}: {f['detail']} ({f['citation']})" for f in fees)
                return {**base, "status": "answered",
                        "answer_text": f"California charges the following fee(s): {fl}. "
                                       f"These are separate from sales tax. (as of {fees[0]['as_of']})"}
            # informational tier: administrative/program topics we have on file
            # (licensing, registration, distributor fees, filing...) get a cited
            # pointer instead of a blind deferral. NEVER a taxability verdict.
            qv_for_info = qv if qv is not None else _embed(question)
            info = _info_lookup(conn, qv_for_info)
            if info:
                result = {**base, "status": "informational", "info": info,
                          "citation": info["citation"], "source_url": info["source_url"]}
                if not compose:
                    return result
                cite = info["citation"] or info["source_url"] or "official CDTFA source"
                prompt = (
                    "The user's question is about a CDTFA administrative or program "
                    "topic (licensing, fees on businesses, filing, registration...), "
                    "not about whether a consumer purchase is taxable. Using ONLY the "
                    "source text below, answer in 2-3 sentences, cite the source, and "
                    "do NOT state any sales-tax rate or taxable/exempt verdict.\n"
                    f"SOURCE ({cite}): {info['text'][:1200]}\n"
                    f"QUESTION: {question}"
                )
                text = model.generate_content(prompt).text.strip()
                result["answer_text"] = (
                    f"{text}\n(Informational -- based on {cite}; "
                    "not a taxability determination.)")
                return result
            # income domain (FTB): tried only after sales tax has fully failed
            # to answer -- see the module note above _answer_income. Skipped
            # if tax_type="income" already tried it above with identical
            # inputs (same question/qv) -- a second call would just repeat
            # the same deterministic result for nothing.
            income_result = None
            if not income_tried:
                with income_db.get_conn() as iconn:
                    income_result = _answer_income(iconn, question, compose, qv_for_info)
            if income_result:
                return income_result
            return {**base, "status": "needs_review",
                    "answer_text": "Needs review - not covered by current rules."}

        cat, taxable, base_rate, citation, summary, reg, rule_url, measure_fraction = rule
        base_rate = float(base_rate)
        measure_fraction = float(measure_fraction)   # taxable base = fraction x price

        # --- branching: opposite-verdict rules nearly as close as the primary ---
        # widen the search when the router's own pick was uncertain (had to fall
        # back to a heuristic tie-break) -- disclose more readily instead of
        # silently committing to a low-confidence guess
        if qv is not None:
            margin = BRANCH_MARGIN if _route_confidence(question, rows) else UNCERTAIN_BRANCH_MARGIN
            branches = _find_branches(conn, question, rows, cat, bool(taxable), margin=margin)

        # --- localize the rate (only for standard-rate taxable items) ---
        eff_rate, rate_basis, loc_label = _effective_rate(
            conn, taxable, base_rate, question, location)

        # tax is charged on the MEASURE (fraction x price), not always the whole
        # price -- e.g. cold food through a vending machine is taxed on 33%
        tax = round(amount * eff_rate * measure_fraction, 2) \
            if (amount is not None and taxable) else \
            (0.0 if amount is not None else None)

        # per-branch effective rate/tax (same location context as the primary)
        for b in branches:
            b_rate, _basis, _lbl = _effective_rate(conn, b["taxable"], b["rate"],
                                                   question, location)
            b_frac = b.get("measure_fraction", 1.0)
            b["rate"] = b_rate
            b["tax"] = round(amount * b_rate * b_frac, 2) \
                if (amount is not None and b["taxable"]) \
                else (0.0 if amount is not None else None)

        result = {
            "status": "conditional" if branches else "answered",
            "category": cat, "taxable": taxable, "rate": eff_rate,
            "base_rate": base_rate, "measure_fraction": measure_fraction,
            "amount": amount, "tax": tax, "citation": citation,
            # source link is the rule's OWN reg, so it always matches the citation
            "location": loc_label, "rate_basis": rate_basis, "source_url": rule_url,
            "branches": branches, "fees": fees, "info": None, "answer_text": None,
            "city_cannabis_tax": city_cannabis, "route_dist": route_dist,
            "rerank_v2_key": rerank_v2_key, "domain": "sales",
        }
        if not compose:  # grading only needs the verdict -> save the extra calls
            return result

        # supporting passage from the SAME reg as the citation (not an unrelated one)
        hits = db.vector_search(conn, qv if qv is not None else _embed(question),
                                k=1, reg=reg)
        source_text = hits[0][1] if hits else ""
        if not result["source_url"]:
            result["source_url"] = hits[0][2] if hits else None

    verdict = "taxable" if taxable else "not taxable"
    # a reduced measure means tax is charged on only part of the price
    measure_note = ""
    if taxable and measure_fraction != 1.0:
        pct = measure_fraction * 100
        measure_note = (
            f"IMPORTANT: tax applies to only {pct:g}% of the price (a partial/"
            f"special measure), not the full price -- so the ${eff_rate*100:g}% rate "
            f"is charged on {pct:g}% of the amount. State this measure explicitly. ")
    facts = (
        f"Most likely case: {verdict}. Rate: {eff_rate * 100:g}% ({rate_basis}). "
        f"Citation: {citation}. " + measure_note
        + (f"Computed tax on ${amount:,.2f} = ${tax:,.2f} "
           + (f"(= {eff_rate*100:g}% of {measure_fraction*100:g}% of ${amount:,.2f}). "
              if measure_fraction != 1.0 else ". ")
           if tax is not None else "")
        + f"Rule: {summary}. "
    )
    if branches:
        cases = [f"CASE 1 [{'TAXABLE' if taxable else 'NOT taxable'}] ({citation}): {summary[:200]}"]
        for i, b in enumerate(branches, start=2):
            cases.append(f"CASE {i} [{'TAXABLE' if b['taxable'] else 'NOT taxable'}] "
                         f"({b['citation']}): {b['label']} -- {b['condition'][:180]}")
        facts += "It depends on the situation. " + " ".join(cases) + " "
        n_cases = 1 + len(branches)
        instruction = (
            f"The answer DEPENDS on the situation. There are {n_cases} cases and your "
            f"answer must contain exactly {n_cases} sentences, one per case, in order: "
            "start the first with 'Generally,' (the most likely reading) and the others "
            "with 'However,' or 'But'. Never write the words 'CASE n'. Each case's "
            "taxable/not-taxable outcome and its condition must be kept EXACTLY as given "
            "(never invert, merge, or skip any case), and each sentence must end with its "
            "citation in parentheses."
        )
    else:
        instruction = "Answer in 2-3 sentences. Always include the citation."
    if fees:
        fl = "; ".join(f"{f['name']} -- {f['detail']} ({f['citation']})" for f in fees)
        facts += (f"IN ADDITION TO SALES TAX, these CDTFA fees apply and must each be "
                  f"stated as a separate line: {fl}. ")
        instruction += (" Then add one sentence per CDTFA fee above, stating it applies "
                        "IN ADDITION TO sales tax with its exact amount and citation; do "
                        "not fold the fee into the tax or restate the fee amounts.")
    if city_cannabis:
        if city_cannabis["status"] == "verified" and city_cannabis["applies"] \
                and city_cannabis["rate"] is None:
            # verified but genuinely TIERED (e.g. Oakland: a progressive annual-
            # revenue tax on the retailer, not a flat per-transaction rate) --
            # we have real, sourced data but no single number is honest to state
            facts += (
                f"The retailer in {city_cannabis['jurisdiction'].title()} is also subject to a "
                f"local Cannabis Business Tax, but it is a TIERED tax on the retailer's ANNUAL "
                f"gross receipts (not a flat percentage of this purchase): {city_cannabis['rate_note']} "
                f"({city_cannabis['citation']}). ")
            instruction += (
                " Then add one sentence noting the city also imposes its own tiered Cannabis "
                "Business Tax on the retailer's annual revenue (not a flat rate on this purchase, "
                "and not an itemized charge to the customer) -- give the approximate typical rate "
                "range from the facts and its citation, and note the exact rate depends on the "
                "retailer's revenue tier.")
        elif city_cannabis["status"] == "verified" and city_cannabis["applies"]:
            cc = city_cannabis
            amt = f" (approximately ${cc['cost']:,.2f} on this purchase)" if cc.get("cost") is not None else ""
            facts += (
                f"The retailer in {cc['jurisdiction'].title()} is also subject to a "
                f"{cc['rate']*100:g}% local Cannabis Business Tax{amt} ({cc['citation']}). "
                "This is a tax on the RETAILER's gross receipts, not a separate line item "
                "charged to the customer -- it is typically built into the shelf price. ")
            instruction += (
                " Then add one sentence noting the local Cannabis Business Tax: state it is "
                "a tax on the retailer (not an itemized charge to the customer) that is "
                "usually reflected in the shelf price, with its rate and citation.")
        elif city_cannabis["status"] == "verified" and not city_cannabis["applies"]:
            facts += (
                f"{city_cannabis['jurisdiction'].title()}'s local Cannabis Business Tax is "
                f"NOT currently in effect ({city_cannabis['rate_note']}). ")
            instruction += (" Then add one sentence noting the city has a cannabis business "
                            "tax on the books but it is not currently in effect, with why.")
        elif city_cannabis["status"] == "unresolved":
            facts += (
                f"{city_cannabis['jurisdiction'].title()} likely also imposes its own local "
                f"Cannabis Business Tax on retailers, but the current rate is NOT independently "
                f"verified by this system ({city_cannabis['rate_note']}). ")
            instruction += (" Then add one sentence flagging that the city likely also imposes "
                            "its own cannabis business tax on retailers, but the exact current "
                            "rate is unconfirmed -- advise checking with the retailer or city.")
    facts += f"Supporting source text: {source_text[:1000]}"
    compose_prompt = (
        f"Answer the user's California sales-tax question using ONLY the facts below. "
        f"{instruction} Do not add outside knowledge.\n"
        f"FACTS: {facts}\nQUESTION: {question}"
    )
    text = model.generate_content(compose_prompt).text.strip()
    if citation not in text:
        text = f"{text} ({citation})"
    result["answer_text"] = text
    return result


def _log_query(question: str, result: dict, source: str) -> None:
    """Best-effort usage log -- must never break a real answer, so any failure
    (including a stale connection) is swallowed. See query_log in db.py."""
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO query_log (question, status, product_key, route_dist, "
                "branched, source, rerank_v2_key, domain) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (question, result.get("status"), result.get("category"),
                 result.get("route_dist"), bool(result.get("branches")), source,
                 result.get("rerank_v2_key"), result.get("domain", "sales")),
            )
    except Exception:
        pass


def answer(question: str, compose: bool = True, location: str = None,
           router: str = None, source: str = "live", tax_type: str = None,
           remembered_filing_status: str = None) -> dict:
    """Public entry point: runs _answer(), then logs the outcome to query_log
    (see db.py) for the usage-driven feedback loop -- mining real questions and
    low-confidence answers instead of guessing what to test next. `source`
    tags internal test-script traffic (item_sweep/coverage/smoke_test) so it
    can be told apart from real usage; defaults to 'live'. `tax_type` is the
    optional UI hint (None|"sales"|"income") -- see _answer()'s docstring.

    `remembered_filing_status` (None|"single"|"mfs"|"mfj"|"hoh"|"qss") is an
    OPTIONAL session-memory hint (see app.py): a filing status the user
    stated earlier in the same chat session, not necessarily in THIS
    question. Never trusted blindly -- pass 1 always runs on the question
    exactly as asked, identical to today's behavior with no new kwarg. Only
    if pass 1 comes back needing a filing status SPECIFICALLY (an income-
    domain needs_review whose own answer_text says so -- the literal
    convention every _income_*missing_filing_status*/_missing_fs_answer
    function in this file already uses) do we retry ONCE with the
    remembered status appended as plain trailing text, using
    income_brackets.FILING_STATUS_LABELS -- text detect_filing_status
    itself already recognizes, verified to round-trip for all 5 keys.
    Scoped this narrowly on purpose: unconditionally splicing filing-status
    text into EVERY question risks feeding it into sales-tax routing for
    questions never about income tax -- verified live that COMPUTE_TRIGGERS'
    bare "how much tax" phrase can hijack a genuinely sales-phrased question
    ("how much tax do I owe on a $500 couch") into a confidently WRONG
    income-bracket computation once a filing status is present in the text.
    Gating the retry on pass 1's OWN result (income domain + needs_review +
    mentions filing status) keeps this feature from ever running on a
    question that didn't already, on its own, ask for one -- though this is
    a mitigation, not a full fix, since that exact couch phrasing already
    independently triggers a filing-status defer on pass 1 today; the root
    cause (COMPUTE_TRIGGERS' breadth) is a separate, pre-existing
    engine limitation, not something this memory feature is responsible
    for closing."""
    detected_filing_status = income_brackets.detect_filing_status(question)
    detected_filing_status_label = (
        income_brackets.FILING_STATUS_LABELS[detected_filing_status]
        if detected_filing_status else None)

    result = _answer(question, compose=compose, location=location, router=router,
                      tax_type=tax_type)

    used_remembered_filing_status = False
    remembered_filing_status_label = None
    if (detected_filing_status is None
            and remembered_filing_status in income_brackets.FILING_STATUS_LABELS
            and result.get("domain") == "income"
            and result.get("status") == "needs_review"
            and "filing status" in (result.get("answer_text") or "").lower()):
        remembered_filing_status_label = income_brackets.FILING_STATUS_LABELS[remembered_filing_status]
        augmented_question = f"{question}, {remembered_filing_status_label}"
        retry_result = _answer(augmented_question, compose=compose, location=location,
                                router=router, tax_type=tax_type)
        if retry_result:
            result = retry_result
            used_remembered_filing_status = True
        else:
            remembered_filing_status_label = None

    result["detected_filing_status"] = detected_filing_status
    result["detected_filing_status_label"] = detected_filing_status_label
    result["used_remembered_filing_status"] = used_remembered_filing_status
    result["remembered_filing_status_label"] = remembered_filing_status_label

    _log_query(question, result, source)
    return result


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Is soda taxable in California?"
    res = answer(q)
    print(f"Q: {q}")
    print(f"-> [{res['category']}] {res['answer_text']}")
