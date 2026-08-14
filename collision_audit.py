"""Proactive routing-safety audit: inspect the GEOMETRY of the whole rule
catalog directly, instead of waiting for a specific question to trip a bug.

item_sweep/route_eval are reactive -- they only find a collision once someone
(a human or a generated probe) happens to ask a question that lands between
two rules. Every real "confidently wrong" bug this project has hit (furniture,
toothpaste-class items, cannabis, house paint, office/office-desk...) started
as two rules sitting too close together in embedding space. This script finds
those close pairs directly from rule_embeddings, no question needed.

A close pair isn't automatically a bug -- several of the closest pairs in this
catalog are INTENTIONAL (hearing_aid vs auditory_ophthalmic_ocular_devices,
vegetable vs ornamental seeds), correctly handled by the live "it depends"
branching mechanism. So each close pair is classified using the SAME
corpus-derived specific-token-overlap logic engine._find_branches already uses
to decide whether a branch is a genuine alternate reading (no new heuristics,
no hardcoded word lists):

  HIGH   opposite taxable/exempt verdicts, distance within the margin the live
         router already treats as "close enough to matter", AND the two rules'
         own texts share no distinguishing vocabulary -- a real question could
         land on either with no lexical signal to separate them, and
         _find_branches would NOT surface the second one either (it requires
         that same shared-vocabulary evidence). Undetected verdict risk.
  MEDIUM opposite verdicts, close, but the texts DO share distinguishing
         vocabulary -- this is very likely already surfacing as a live branch
         (working as intended); listed for confirmation, not urgent.
  LOW    same verdict, close -- no verdict risk, but the wrong one of the pair
         could get cited, a "right answer, wrong regulation" style bug.

Domain-parameterized (2026-08-10) to also audit the income-tax catalog
(income_tax_topics/income_rule_embeddings via income_db), which never went
through this pass while it was being built credit-by-credit. Sales and
income each get their OWN embedding space, verdict lookup, and DF-weighting
cache (engine._specific_toks/_token_doc_freq already take a `table` param
specifically for this, added in Phase 0) -- never compared against each
other or pooled, since a word common in one domain's prose isn't necessarily
common in the other's.

Usage:
  python collision_audit.py [max_per_band] [--domain=sales|income]
  # default: max_per_band=25, domain=sales
"""
import sys

import numpy as np

import engine

_DOMAINS = {
    "sales": {
        "conn_module": "db",
        "embed_table": "rule_embeddings",
        "key_col": "product_key",
        "kind_filter": "WHERE kind <> 'admin' AND embedding IS NOT NULL",
        "lookup": lambda conn, k: engine._lookup(conn, k),
    },
    "income": {
        "conn_module": "income_db",
        "embed_table": "income_rule_embeddings",
        "key_col": "topic_key",
        "kind_filter": "WHERE embedding IS NOT NULL",
        "lookup": lambda conn, k: engine._income_lookup(conn, k),
    },
}


def _load(conn, spec):
    rows = conn.execute(
        f"SELECT {spec['key_col']}, text, embedding FROM {spec['embed_table']} "
        f"{spec['kind_filter']}").fetchall()
    keys = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    vecs = np.stack([np.asarray(r[2], dtype=float) for r in rows])
    return keys, texts, vecs


def _verdicts(conn, keys, spec):
    out = {}
    for k in set(keys):
        r = spec["lookup"](conn, k)
        out[k] = bool(r[1]) if r and r[1] is not None else None
    return out


def audit(max_per_band=25, domain="sales"):
    import importlib
    spec = _DOMAINS[domain]
    conn_module = importlib.import_module(spec["conn_module"])
    conn = conn_module.get_conn()
    keys, texts, vecs = _load(conn, spec)
    n = len(keys)
    print(f"auditing {n} {domain} rules from {spec['embed_table']}\n")

    # cosine distance, matching pgvector's <=> operator
    unit = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    dist = 1 - (unit @ unit.T)
    np.fill_diagonal(dist, np.inf)

    verdicts = _verdicts(conn, keys, spec)

    # Some topics (e.g. income's head_of_household_eligibility, an
    # eligibility question with no taxable/exempt shape at all, or
    # alimony_spousal_support, whose treatment genuinely depends on the
    # divorce instrument's date) have NO boolean verdict -- None, not
    # False. `vi != vj` would wrongly treat None-vs-bool as "opposite
    # verdicts"; those pairs go in their own SKIPPED bucket instead of
    # high/medium/low, since "opposite" isn't a meaningful comparison when
    # one side has no verdict to be opposite to.
    high, medium, low, skipped = [], [], [], []
    for i in range(n):
        close_js = np.where(dist[i] <= engine.UNCERTAIN_BRANCH_MARGIN)[0]
        for j in close_js:
            if j <= i:
                continue  # each unordered pair once
            d = float(dist[i, j])
            ki, kj = keys[i], keys[j]
            vi, vj = verdicts.get(ki), verdicts.get(kj)
            ti = engine._specific_toks(conn, engine._toks(texts[i]), table=spec["embed_table"])
            tj = engine._specific_toks(conn, engine._toks(texts[j]), table=spec["embed_table"])
            shared = ti & tj
            row = (d, ki, kj, vi, vj, shared)
            if vi is None or vj is None:
                skipped.append(row)
            elif vi != vj:
                (high if not shared else medium).append(row)
            else:
                low.append(row)
    conn.close()

    def vlabel(v):
        return "n/a" if v is None else ("TAXABLE" if v else "exempt")

    def dump(title, items, note):
        items.sort(key=lambda r: r[0])
        print(f"=== {title} ({len(items)}) -- {note} ===")
        for d, ki, kj, vi, vj, shared in items[:max_per_band]:
            sh = ",".join(sorted(shared)) or "-"
            print(f"  dist={d:.4f}  [{vlabel(vi)} vs {vlabel(vj)}]  {ki}  <->  {kj}   shared: {sh}")
        if len(items) > max_per_band:
            print(f"  ... and {len(items) - max_per_band} more")
        print()

    dump("HIGH -- undetected verdict risk", high,
         "opposite verdicts, close, NO shared specific vocabulary")
    dump("MEDIUM -- likely already a working branch pair", medium,
         "opposite verdicts, close, but do share specific vocabulary")
    dump("LOW -- citation-mixup risk only", low,
         "same verdict, close (not a verdict-danger)")
    dump("SKIPPED -- no comparable verdict", skipped,
         "at least one side has no taxable/exempt boolean (informational/eligibility topic)")
    print(f"summary: {len(high)} high, {len(medium)} medium, {len(low)} low, "
          f"{len(skipped)} skipped (margin={engine.UNCERTAIN_BRANCH_MARGIN})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--domain")]
    domain_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--domain=")), "sales")
    audit(int(args[0]) if args else 25, domain=domain_arg)
