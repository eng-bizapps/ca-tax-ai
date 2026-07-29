"""Evaluate + calibrate the embedding router in one pass.

For each probe it embeds the question ONCE, finds the nearest rule embedding
(key + cosine distance), and looks up that rule's verdict. Results are cached, so
threshold selection is done offline (no re-embedding): it sweeps candidate
distance thresholds and reports, for each, in-scope coverage, verdict accuracy,
and how many out-of-scope probes correctly defer. Pick the threshold that keeps
out-of-scope deferring while maximizing in-scope coverage, then set
engine.EMBED_ROUTER_THRESHOLD.

Usage:
  python route_eval.py run [N]     # embed + grade up to N new probes (cached)
  python route_eval.py report      # threshold sweep + summary from cache
  python route_eval.py reset
"""
import json
import os
import sys

import db
import engine
from coverage import PROBES

CACHE = os.path.join(os.path.dirname(__file__), "route_eval_results.json")


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def run(limit=999):
    conn = db.get_conn()
    cache = _load()
    graded = 0
    for q, exp, topic in PROBES:
        if q in cache or graded >= limit:
            continue
        hit = engine._disambiguate(q)                   # curated tier first (no embed cost)
        if hit:
            key, dist = hit, 0.0
        else:
            try:
                qv = engine._embed(q)
            except Exception as e:
                print(f"STOP after {graded} (embed limit): {str(e)[:120]}")
                break
            rows = engine._route_candidates(conn, qv)   # same candidates the router uses
            dist = float(rows[0][2])                     # best distance -> for threshold sweep
            key = engine._rerank(q, rows)                # actual routed key (with rerank)
            if engine.TIEBREAK_ENABLED:                  # (LLM tie-break off by default)
                key = engine._llm_tiebreak(conn, q, rows, key)
        rule = engine._lookup(conn, key)
        taxable = bool(rule[1]) if rule else None
        cache[q] = {"expected": exp, "topic": topic, "key": key,
                    "dist": round(dist, 4), "taxable": taxable}
        graded += 1
        print(f"  {dist:.3f}  {q[:52]:52} -> {key}")
        _save(cache)
    conn.close()
    print(f"\ngraded {graded} new; cached {len(cache)}/{len(PROBES)}")
    report()


def report():
    cache = _load()
    if not cache:
        print("nothing graded; run: python route_eval.py run")
        return
    inscope = [v for v in cache.values() if v["expected"] is not None]
    oos = [v for v in cache.values() if v["expected"] is None]

    print(f"\n=== THRESHOLD SWEEP ({len(cache)} probes: {len(inscope)} in-scope, {len(oos)} out-of-scope) ===")
    print(f"{'thresh':>7} {'answered':>9} {'correct':>8} {'wrong':>6} {'oos-defer':>10}")
    best = None
    # gemini-embedding-2 distance scale (recalibrated 2026-07-08): in-scope sits
    # ~0.15-0.25, out-of-scope ~0.29+ -- sweep the range that actually matters
    for th in [0.20, 0.22, 0.24, 0.25, 0.26, 0.27, 0.28, 0.30, 0.35]:
        ans = [v for v in inscope if v["dist"] <= th]
        correct = [v for v in ans if v["taxable"] == v["expected"]]
        wrong = [v for v in ans if v["taxable"] != v["expected"]]
        oos_defer = [v for v in oos if v["dist"] > th]
        print(f"{th:>7} {len(ans):>9} {len(correct):>8} {len(wrong):>6} "
              f"{len(oos_defer):>4}/{len(oos):<5}")
        # score: correct in-scope + correctly-deferred oos, penalize wrong verdicts
        score = len(correct) + len(oos_defer) - 2 * len(wrong)
        if best is None or score > best[1]:
            best = (th, score)
    print(f"\nsuggested threshold: {best[0]}")

    # detail at suggested threshold
    th = best[0]
    print(f"\n--- detail at threshold {th} ---")
    wrong = [(q, v) for q, v in cache.items()
             if v["expected"] is not None and v["dist"] <= th and v["taxable"] != v["expected"]]
    blind = [(q, v) for q, v in cache.items()
             if v["expected"] is not None and v["dist"] > th]
    oos_leak = [(q, v) for q, v in cache.items()
                if v["expected"] is None and v["dist"] <= th]
    if wrong:
        print("WRONG VERDICT (routed to a rule with the opposite verdict):")
        for q, v in wrong:
            print(f"  dist={v['dist']} got={v['taxable']} exp={v['expected']} key={v['key']}  {q}")
    if blind:
        print("BLIND (in-scope but deferred as too-far):")
        for q, v in blind:
            print(f"  dist={v['dist']} key={v['key']}  {q}")
    if oos_leak:
        print("OUT-OF-SCOPE LEAK (should have deferred):")
        for q, v in oos_leak:
            print(f"  dist={v['dist']} key={v['key']}  {q}")
    if not (wrong or oos_leak):
        print("no wrong verdicts, no out-of-scope leaks at this threshold.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "run":
        run(int(sys.argv[2]) if len(sys.argv) > 2 else 999)
    elif cmd == "report":
        report()
    elif cmd == "reset":
        if os.path.exists(CACHE):
            os.remove(CACHE)
        print("cache cleared")
    else:
        print(__doc__)
