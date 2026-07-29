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
import income_brackets
import income_credits
import income_db
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


GENERIC_TOKEN_DF = 0.08          # a word appearing in more than this fraction of
                                 # the rule catalog is too common to count as
                                 # meaningful relevance on its own (e.g. "food"
                                 # sits in 13.5% of rules; "auditory"/"wheelchair"
                                 # sit at <1%) -- computed FROM THE CORPUS, not a
                                 # hand-authored word list, so it stays correct
                                 # as rules are added or reworded
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
    carry discriminating signal (see GENERIC_TOKEN_DF)."""
    df, n = _token_doc_freq(conn, table)
    if not n:
        return tokens
    return {t for t in tokens if df.get(t, 0) / n <= GENERIC_TOKEN_DF}


def _route_confidence(question: str, rows) -> bool:
    """True when the router's top match is well-grounded (shares >1 distinctive
    word with the question) -- the same condition _rerank uses to trust the
    nearest embedding outright without a heuristic tie-break. False means the
    primary key was picked by fallback logic, i.e. genuine uncertainty."""
    return len(_toks(question) & _toks(rows[0][1])) > 1


def _find_branches(conn, question, rows, primary_key, primary_taxable, margin=BRANCH_MARGIN):
    """Opposite-verdict rules close enough to the best match to be plausible
    alternate readings of the question. rows = routing candidates
    [(key, text, dist), ...]. `margin` widens when the router itself was
    uncertain (see _route_confidence) -- an uncertain pick should disclose
    plausible opposite-verdict alternatives more readily than a confident one.

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
    best_dist = float(rows[0][2])
    qt = _toks(question)
    qt_specific = _specific_toks(conn, qt)
    primary_text = next((t for k, t, d in rows if k == primary_key), "")
    primary_ov = qt_specific & _toks(primary_text)
    branches = []
    for k, text, d in rows:
        if k == primary_key or float(d) > best_dist + margin:
            continue
        cand_ov = qt_specific & _toks(text)
        if not cand_ov or (primary_ov and not (cand_ov & primary_ov)):
            continue
        info = _branch_info(conn, k)
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
    result["answer_text"] = (
        f"Assuming ${amount:,.2f} in gross wage income, filing status {label}, and the "
        f"standard deduction (${dedu['amount']:,.0f}), your California taxable income is "
        f"about ${calc['taxable_income']:,.2f}. Your marginal CA tax bracket is "
        f"{calc['marginal_rate']*100:g}%, and your estimated {income_brackets.DEFAULT_TAX_YEAR} "
        f"California income tax is about ${calc['total_tax']:,.2f} ({calc['citation']})."
        f"{surtax_note} This assumes wage income only, with no other adjustments, credits, "
        "or itemized deductions -- your actual liability may differ."
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


def _income_topic_by_key(conn, compose: bool, topic_key: str, base: dict):
    """Build a structured income_tax_topics verdict for a KNOWN topic_key --
    shared by the embedding-routed path (_income_topic_answer) and the
    cross-domain override (_cross_domain_income_override), which already
    knows exactly which topic applies and skips routing entirely."""
    topic = _income_lookup(conn, topic_key)
    if not topic:
        return None
    t_key, t_taxable, _t_treatment, t_citation, t_summary, t_source_url = topic
    result = {**base, "status": "answered", "category": t_key,
              "taxable": bool(t_taxable) if t_taxable is not None else None,
              "citation": t_citation, "source_url": t_source_url}
    if not compose:
        return result
    verdict = ("not taxable in California" if t_taxable is False
               else "taxable in California" if t_taxable else "treatment varies")
    result["answer_text"] = (
        f"{t_summary} ({t_citation})" if t_summary else f"This is {verdict} ({t_citation}).")
    return result


def _income_topic_answer(conn, question: str, compose: bool, rows, base: dict):
    """Structured topic verdict (income_tax_topics, via income_rule_embeddings
    routing -- the Phase 2 scaffold, now real). Unlike the informational
    tier, this states an actual taxable/not-taxable fact with its own
    citation, not just a paraphrased pointer."""
    if not rows or float(rows[0][2]) > INCOME_EMBED_ROUTER_THRESHOLD:
        return None
    return _income_topic_by_key(conn, compose, rows[0][0], base)


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

    amount = _amount(question)
    compute_result = _income_compute_answer(conn, question, amount, base)
    if compute_result:
        return compute_result

    missing_fs_result = _income_missing_filing_status_answer(question, amount, base)
    if missing_fs_result:
        return missing_fs_result

    caleitc_result = _income_caleitc_answer(conn, question, amount, base)
    if caleitc_result:
        return caleitc_result

    missing_children_result = _income_missing_children_answer(question, amount, base)
    if missing_children_result:
        return missing_children_result

    ycta_result = _income_ycta_answer(conn, question, amount, base)
    if ycta_result:
        return ycta_result

    renters_result = _income_renters_credit_answer(conn, question, amount, base)
    if renters_result:
        return renters_result

    missing_renters_fs_result = _income_renters_credit_missing_fs_answer(question, amount, base)
    if missing_renters_fs_result:
        return missing_renters_fs_result

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
    Returns (eff_rate, rate_basis, loc_label)."""
    loc = location or local_rates.detect(conn, question)
    loc_info = local_rates.resolve(conn, loc) if loc else None
    if not taxable:
        return base_rate, "exempt", None
    if base_rate in SPECIAL_RATES:
        return base_rate, "special/partial rate; local district tax not auto-applied", \
            (loc_info["label"] if loc_info else None)
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
