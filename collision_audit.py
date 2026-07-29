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

Usage:
  python collision_audit.py [max_per_band]   # default 25 pairs shown per band
"""
import sys

import numpy as np

import db
import engine


def _load(conn):
    rows = conn.execute(
        "SELECT product_key, text, embedding FROM rule_embeddings "
        "WHERE kind <> 'admin' AND embedding IS NOT NULL").fetchall()
    keys = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    vecs = np.stack([np.asarray(r[2], dtype=float) for r in rows])
    return keys, texts, vecs


def _verdicts(conn, keys):
    out = {}
    for k in set(keys):
        r = engine._lookup(conn, k)
        out[k] = bool(r[1]) if r else None
    return out


def audit(max_per_band=25):
    conn = db.get_conn()
    keys, texts, vecs = _load(conn)
    n = len(keys)
    print(f"auditing {n} consumer rules from rule_embeddings\n")

    # cosine distance, matching pgvector's <=> operator
    unit = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    dist = 1 - (unit @ unit.T)
    np.fill_diagonal(dist, np.inf)

    verdicts = _verdicts(conn, keys)

    high, medium, low = [], [], []
    for i in range(n):
        close_js = np.where(dist[i] <= engine.UNCERTAIN_BRANCH_MARGIN)[0]
        for j in close_js:
            if j <= i:
                continue  # each unordered pair once
            d = float(dist[i, j])
            ki, kj = keys[i], keys[j]
            vi, vj = verdicts.get(ki), verdicts.get(kj)
            ti = engine._specific_toks(conn, engine._toks(texts[i]))
            tj = engine._specific_toks(conn, engine._toks(texts[j]))
            shared = ti & tj
            row = (d, ki, kj, vi, vj, shared)
            if vi != vj:
                (high if not shared else medium).append(row)
            else:
                low.append(row)
    conn.close()

    def dump(title, items, note):
        items.sort(key=lambda r: r[0])
        print(f"=== {title} ({len(items)}) -- {note} ===")
        for d, ki, kj, vi, vj, shared in items[:max_per_band]:
            vlabel = f"{'TAXABLE' if vi else 'exempt'} vs {'TAXABLE' if vj else 'exempt'}"
            sh = ",".join(sorted(shared)) or "-"
            print(f"  dist={d:.4f}  [{vlabel}]  {ki}  <->  {kj}   shared: {sh}")
        if len(items) > max_per_band:
            print(f"  ... and {len(items) - max_per_band} more")
        print()

    dump("HIGH -- undetected verdict risk", high,
         "opposite verdicts, close, NO shared specific vocabulary")
    dump("MEDIUM -- likely already a working branch pair", medium,
         "opposite verdicts, close, but do share specific vocabulary")
    dump("LOW -- citation-mixup risk only", low,
         "same verdict, close (not a verdict-danger)")
    print(f"summary: {len(high)} high, {len(medium)} medium, {len(low)} low "
          f"(margin={engine.UNCERTAIN_BRANCH_MARGIN})")


if __name__ == "__main__":
    audit(int(sys.argv[1]) if len(sys.argv) > 1 else 25)
