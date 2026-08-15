"""The responder (real architecture, end to end).

For every question:
  1. Gemini maps it to a rule key      (language)
  2. lookup: fine product_rules first, then coarse rules   (deterministic)
  3. compute tax + pgvector citation    (deterministic + retrieval)
  4. Gemini composes the answer         (language, from facts only)
  5. guard: no rule -> "Needs review"   (safety)
"""
import re

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
    itemized/charitable/SALT-cap-addback-tagged figures -- see
    _tagged_amount) out separately from the income figure, same distance-
    based approach as the mixed wage+SE path; if more than one
    unaccounted-for amount remains, the question is ambiguous and this
    defers rather than guessing which one is income. All 5 optional
    figures are additive -- see income_brackets.SALT_TERMS /
    MORTGAGE_INTEREST_ADDBACK_TERMS / MISC_ITEMIZED_TERMS /
    CHARITABLE_TERMS / SALT_CAP_ADDBACK_TERMS. Each of the (up to) 7
    figures has its OWN distinct, non-overlapping anchor phrase, unlike
    FYTC's shared-anchor collision earlier this session, so the same
    exclude-based extraction scales safely."""
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
    others = [a for a, _, _ in _amounts(question) if a not in claimed]
    if len(others) != 1:
        return None
    income_amount = others[0]
    calc = income_brackets.compute_itemized_ca_tax(
        conn, income_amount, itemized_amount, fs, salt_amount=salt_amount,
        mortgage_interest_addback=mortgage_addback, misc_itemized_expenses=misc_expenses,
        charitable_amount=charitable_amount, salt_cap_addback=salt_cap_addback)
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
        f"{surtax_note}{salt_note}{mortgage_note}{misc_note}{charitable_note}{salt_cap_note}{phaseout_note} This assumes your stated itemized-deduction "
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
    loss_amount = _amount_near(question, income_brackets.CAPITAL_LOSS_TERMS)
    if loss_amount is None:
        return None
    others = [a for a, _, _ in _amounts(question) if a != loss_amount]
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


def _income_excess_business_loss_answer(conn, question: str, base: dict):
    """Other income (e.g. wages) + a stated AGGREGATE net business loss --
    see income_brackets.compute_excess_business_loss_ca_tax's docstring for
    the Form 3461 conformity basis (CA's own continuous $313k/$626k
    threshold, not the current federal version). Uses _amount_near/the
    'one other amount' pattern exactly like the capital-loss path."""
    fs = income_brackets.detect_excess_business_loss_signal(question)
    if not fs:
        return None
    loss_amount = _amount_near(question, income_brackets.EXCESS_BUSINESS_LOSS_TERMS)
    if loss_amount is None:
        return None
    others = [a for a, _, _ in _amounts(question) if a != loss_amount]
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
    nol_amount = _amount_near(question, income_brackets.NOL_TERMS)
    if nol_amount is None:
        return None
    others = [a for a, _, _ in _amounts(question) if a != nol_amount]
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


def _amount_near_filtered(question: str, keywords, amounts, window: int = 60):
    """Same nearest-keyword-distance logic as _amount_near, but operating
    on a caller-supplied (already phantom-filtered) amounts list instead
    of calling _amounts() internally -- shared by any feature whose own
    trigger vocabulary contains bare digits that _amounts()'s regex would
    otherwise misparse as a dollar amount (see
    _cannabis_strip_280e_phantom_amounts's "280E" case and
    _qsbs_strip_section_number_phantoms's "Section 1202/1045" case)."""
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
                    best = (amount, dist)
            start = idx + len(kw)
    return best[0] if best else None


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
    expense_amount = _amount_near_filtered(question, income_brackets.CANNABIS_280E_EXPENSE_TERMS, amounts)
    if expense_amount is None:
        return None
    others = [a for a, _, _ in amounts if a != expense_amount]
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
    ira_amount = _amount_near(question, income_brackets.IRA_DEDUCTION_TERMS)
    if ira_amount is None:
        return None
    others = [a for a, _, _ in _amounts(question) if a != ira_amount]
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
    excluded_amount = _amount_near_filtered(question, income_brackets.QSBS_EXCLUDED_AMOUNT_TERMS, amounts)
    if excluded_amount is None:
        return None
    others = [a for a, _, _ in amounts if a != excluded_amount]
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
    hsa_gain_amount = _amount_near(question, income_brackets.HSA_INVESTMENT_GAIN_TERMS)
    if hsa_gain_amount is None:
        return None
    others = [a for a, _, _ in _amounts(question) if a != hsa_gain_amount]
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
    loss_amount = _amount_near(question, income_brackets.CAPITAL_LOSS_CARRYOVER_TERMS)
    if loss_amount is None:
        return None
    others = [a for a, _, _ in _amounts(question) if a != loss_amount]
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
    investment_amount = _amount_near(question, income_credits.INVESTMENT_INCOME_TERMS)
    if investment_amount is None:
        return None
    others = [a for a, _, _ in _amounts(question) if a != investment_amount]
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

    ca_source_amount = _amount_near(question, income_nonresident.CA_SOURCE_AMOUNT_TERMS)
    if ca_source_amount is not None:
        others = [a for a, _, _ in _amounts(question) if a != ca_source_amount]
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

        # Military retirement pay / DoD Survivor Benefit Plan annuity: same
        # early-intercept reasoning as the cross-domain override above, but
        # routed through the FULL income pipeline (not a bare topic lookup)
        # since the real verdict is AGI-dependent (see
        # income_credits.compute_military_retirement_exclusion), not a
        # fixed answer. Found live, via income_item_sweep.py, right after
        # this topic was added: sales' own military-vehicle/federal-area
        # rule cluster ("sales_on_federal_areas", "vehicle_sale_to_
        # servicemember"...) sits close enough in embedding space to ANY
        # "military"-flavored question that "is my military retirement
        # taxable in California" was confidently (and nonsensically)
        # answered as 7.25% SALES TAX on the stated AGI dollar figure,
        # treating income as if it were a purchase price. Sales tries
        # first by default, so without this intercept the income-domain
        # military retirement exclusion would never even be tried.
        if income_credits.detect_military_retirement_signal(question):
            qv_military = _embed(question)
            with income_db.get_conn() as iconn:
                military_result = _answer_income(iconn, question, compose, qv_military)
            if military_result:
                return military_result

        # Cannabis 280E business-expense decoupling: the SAME collision
        # class as the military-retirement guard above, found live via
        # income_item_sweep.py while building this feature. Sales tries
        # first by default, and this project's OWN sales-tax cannabis
        # excise rule (cannabis_retail_adult_use -- "retail sale of
        # cannabis or cannabis products ... for adult (recreational) use")
        # sits close enough to ANY cannabis-flavored question that a
        # question entirely about a LICENSED BUSINESS's net PROFIT and
        # 280E-disallowed expenses (nothing to do with a retail purchase)
        # was confidently answered as sales/excise tax on the stated
        # dollar figure. Without this intercept the income-domain 280E
        # restoration would never even be tried. Checks BOTH the full
        # signal and the missing-filing-status variant (unlike military
        # retirement, this path needs a filing status, so a question
        # missing one must still be intercepted here rather than falling
        # through to sales).
        if (income_brackets.detect_cannabis_280e_signal(question)
                or income_brackets.detect_cannabis_280e_missing_filing_status(question)):
            qv_cannabis = _embed(question)
            with income_db.get_conn() as iconn:
                cannabis_result = _answer_income(iconn, question, compose, qv_cannabis)
            if cannabis_result:
                return cannabis_result

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
           router: str = None, source: str = "live", tax_type: str = None) -> dict:
    """Public entry point: runs _answer(), then logs the outcome to query_log
    (see db.py) for the usage-driven feedback loop -- mining real questions and
    low-confidence answers instead of guessing what to test next. `source`
    tags internal test-script traffic (item_sweep/coverage/smoke_test) so it
    can be told apart from real usage; defaults to 'live'. `tax_type` is the
    optional UI hint (None|"sales"|"income") -- see _answer()'s docstring."""
    result = _answer(question, compose=compose, location=location, router=router,
                      tax_type=tax_type)
    _log_query(question, result, source)
    return result


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Is soda taxable in California?"
    res = answer(q)
    print(f"Q: {q}")
    print(f"-> [{res['category']}] {res['answer_text']}")
