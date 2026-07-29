"""Usage-driven feedback loop: mine query_log (every engine.answer() call) for
real coverage gaps and low-confidence answers, instead of guessing what to
test next. See db.py for the table; engine.answer() logs to it automatically.

Usage:
  python query_log.py report [N]   # summary over the last N rows (default 500)
  python query_log.py recent [N]   # raw dump of the last N rows (default 20)
"""
import sys

import db

NEAR_THRESHOLD_MARGIN = 0.03   # live answers within this of EMBED_ROUTER_THRESHOLD
                                # are worth a second look -- the router barely cleared


def report(limit=500):
    import engine
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT question, status, product_key, route_dist, branched, source, domain "
        "FROM query_log ORDER BY logged_at DESC LIMIT %s", (limit,)).fetchall()
    conn.close()
    if not rows:
        print("query_log is empty -- run the app or any script that calls engine.answer().")
        return

    by_source = {}
    for _q, _s, _k, _d, _b, src, _dom in rows:
        by_source[src] = by_source.get(src, 0) + 1
    print(f"=== query_log: {len(rows)} rows (most recent {limit}) ===")
    for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {src:12} {n}")

    live = [r for r in rows if r[5] == "live"]
    print(f"\n=== LIVE traffic ({len(live)} rows) -- the actual signal ===")
    if not live:
        print("  none yet (all traffic so far is internal test-script runs).")
        return

    # per-domain: sales and income have their own router threshold and (for
    # income) route_dist isn't populated at all yet (only the sales path
    # sets it -- _answer_income's topic-routing distance is never written
    # back into the result dict) -- mixing them under one threshold would
    # silently misjudge income's near-threshold rows, so split first.
    THRESH_BY_DOMAIN = {"sales": engine.EMBED_ROUTER_THRESHOLD,
                         "income": engine.INCOME_EMBED_ROUTER_THRESHOLD}
    for domain in ("sales", "income"):
        drows = [r for r in live if r[6] == domain]
        if not drows:
            continue
        print(f"\n===== domain={domain} ({len(drows)} live rows) =====")
        by_status = {}
        for _q, status, _k, _d, _b, _src, _dom in drows:
            by_status[status] = by_status.get(status, 0) + 1
        print("by status:")
        for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
            print(f"  {status:14} {n}")

        needs_review = [r for r in drows if r[1] == "needs_review"]
        if needs_review:
            print(f"\n--- needs_review questions ({len(needs_review)}) -- real coverage gaps ---")
            for q, _s, _k, _d, _b, _src, _dom in needs_review[:30]:
                print(f"  {q}")

        thresh = THRESH_BY_DOMAIN[domain]
        near = [r for r in drows
                if r[1] in ("answered", "conditional") and r[3] is not None
                and float(r[3]) >= thresh - NEAR_THRESHOLD_MARGIN]
        have_dist = [r for r in drows if r[1] in ("answered", "conditional")]
        if domain == "income" and have_dist and all(r[3] is None for r in have_dist):
            print(f"\n--- low-confidence check skipped: route_dist is NULL for every "
                  f"income row (known gap -- _answer_income never writes it back) ---")
        elif near:
            print(f"\n--- low-confidence answers ({len(near)}) -- dist within "
                  f"{NEAR_THRESHOLD_MARGIN} of threshold {thresh}, worth a second look ---")
            for q, _s, k, d, _b, _src, _dom in sorted(near, key=lambda r: -float(r[3]))[:30]:
                print(f"  dist={float(d):.3f}  key={k}  {q}")

        key_counts = {}
        for _q, status, k, _d, _b, _src, _dom in drows:
            if status in ("answered", "conditional") and k:
                key_counts[k] = key_counts.get(k, 0) + 1
        if key_counts:
            print(f"\n--- most-asked rules (top 15 of {len(key_counts)}) -- "
                  "candidates for a verified-mapping cache ---")
            for k, n in sorted(key_counts.items(), key=lambda kv: -kv[1])[:15]:
                print(f"  {n:4}  {k}")


def recent(limit=20):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT logged_at, source, domain, status, product_key, route_dist, branched, question "
        "FROM query_log ORDER BY logged_at DESC LIMIT %s", (limit,)).fetchall()
    conn.close()
    if not rows:
        print("query_log is empty.")
        return
    for ts, src, domain, status, key, dist, branched, q in rows:
        d = f"{float(dist):.3f}" if dist is not None else "  -  "
        br = "branch" if branched else "      "
        print(f"{ts}  {src:10} {domain:7} {status:12} {d} {br}  {key or '-':40}  {q[:60]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if cmd == "report":
        report(n or 500)
    elif cmd == "recent":
        recent(n or 20)
    else:
        print(__doc__)
