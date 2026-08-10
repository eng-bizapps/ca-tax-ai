"""Calibrate INCOME_EMBED_ROUTER_THRESHOLD (engine.py) against REAL topic
content, mirroring route_eval.py's proven pattern for the sales-tax router.

Overdue: the threshold has been sitting at 0.27 -- "provisional: same
starting point as sales tax" -- since Phase 2, when income_rule_embeddings
had 0 rows. It now has 9 (4 original + 5 added this pass), so there is real
distance evidence to sweep against for the first time.

Special focus: gambling_winnings (taxable=True, general rule) and
california_lottery_winnings (taxable=False, narrow carve-out) are a
deliberate same-domain collision pair -- this is the first real test of
whether embedding-distance routing disambiguates them correctly by wording
proximity alone, the same mechanism the sales-tax router already relies on
for hundreds of similar pairs.

Usage:
  python income_route_eval.py run
  python income_route_eval.py report
  python income_route_eval.py reset
"""
import json
import os
import sys

import engine
import income_db as db

CACHE = os.path.join(os.path.dirname(__file__), "income_route_eval_results.json")

# (question, expected taxable True/False/None, topic_key or None). None
# expected == out-of-scope, must defer (income OR sales OR unrelated).
PROBES = [
    # --- in-scope: paraphrases of all 9 loaded topics ---
    ("is unemployment compensation taxable in california", False, "unemployment_compensation"),
    ("do I pay state tax on unemployment benefits I received", False, "unemployment_compensation"),
    ("is social security income taxable in california", False, "social_security_income"),
    ("do retirees pay california tax on their social security checks", False, "social_security_income"),
    ("is california paid family leave taxable", False, "paid_family_leave"),
    ("do I owe tax on PFL benefits in california", False, "paid_family_leave"),
    ("is an inheritance taxable in california", False, "gifts_and_inheritance"),
    ("I received a gift from my parents, do I owe tax on it", False, "gifts_and_inheritance"),
    ("are gambling winnings taxable in california", True, "gambling_winnings"),
    ("do I pay tax on casino winnings in california", True, "gambling_winnings"),
    ("is money I won at the horse track taxable in california", True, "gambling_winnings"),
    ("are california lottery winnings taxable", False, "california_lottery_winnings"),
    ("do I owe california tax on my superlotto winnings", False, "california_lottery_winnings"),
    ("I won powerball through the california lottery, is that taxable", False, "california_lottery_winnings"),
    ("is interest from us treasury bonds taxable in california", False, "us_government_bond_interest"),
    ("do I pay california tax on interest from us savings bonds", False, "us_government_bond_interest"),
    ("is interest from an out of state municipal bond taxable in california", True, "out_of_state_municipal_bond_interest"),
    ("I own municipal bonds from texas, does california tax the interest", True, "out_of_state_municipal_bond_interest"),
    ("are hsa contributions taxable in california", True, "hsa_contributions_and_earnings"),
    ("does california let me deduct my health savings account contribution", True, "hsa_contributions_and_earnings"),
    ("can I file as head of household in california", None, "head_of_household_eligibility"),
    ("am I eligible for head of household filing status", None, "head_of_household_eligibility"),
    ("what are the requirements to file head of household", None, "head_of_household_eligibility"),
    ("do I qualify for hoh filing status", None, "head_of_household_eligibility"),
    ("is workers compensation taxable in california", False, "workers_compensation"),
    ("do I pay tax on my workers comp benefits", False, "workers_compensation"),
    ("is california state disability insurance taxable", False, "state_disability_insurance"),
    ("do I owe tax on my sdi benefits", False, "state_disability_insurance"),
    ("is alimony taxable in california", None, "alimony_spousal_support"),
    ("do I pay tax on spousal support I received", None, "alimony_spousal_support"),

    # --- out-of-scope: must defer (sales-tax-flavored, unrelated, or generic) ---
    ("is furniture taxable in california", None, None),
    ("is cannabis taxable in california", None, None),
    ("what is the property tax rate in los angeles", None, None),
    ("how do I register my car with the dmv", None, None),
    ("what is the weather like in sacramento", None, None),
    ("is bread taxable in california", None, None),
    ("how much is the filing fee for an llc in california", None, None),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def run():
    conn = db.get_conn()
    cache = _load()
    graded = 0
    for q, exp, topic in PROBES:
        if q in cache:
            continue
        try:
            qv = engine._embed(q)
        except Exception as e:
            print(f"STOP after {graded} (embed limit): {str(e)[:120]}")
            break
        rows = engine._income_route_candidates(conn, qv)
        if rows:
            key, dist = rows[0][0], float(rows[0][2])
        else:
            key, dist = None, 999.0
        row = engine._income_lookup(conn, key) if key else None
        taxable = bool(row[1]) if row and row[1] is not None else None
        cache[q] = {"expected": exp, "topic": topic, "key": key,
                    "dist": round(dist, 4), "taxable": taxable}
        graded += 1
        print(f"  {dist:.3f}  {q[:60]:60} -> {key}")
        _save(cache)
    conn.close()
    print(f"\ngraded {graded} new; cached {len(cache)}/{len(PROBES)}")
    report()


def _is_correct(v):
    """Correct means routed to the RIGHT topic_key. For topics with a real
    boolean verdict, the taxable value must also match -- for
    head_of_household_eligibility (expected=None, a legitimately-null
    verdict, not an out-of-scope marker) matching the topic key is enough."""
    if v["key"] != v["topic"]:
        return False
    return v["expected"] is None or v["taxable"] == v["expected"]


def report():
    cache = _load()
    if not cache:
        print("nothing graded; run: python income_route_eval.py run")
        return
    # in-scope is determined by topic (the topic_key this probe SHOULD route
    # to), not by expected -- expected=None means "informational, no
    # taxable verdict" (head_of_household_eligibility) for in-scope probes,
    # and "should defer, no topic at all" for the true out-of-scope probes.
    inscope = [v for v in cache.values() if v["topic"] is not None]
    oos = [v for v in cache.values() if v["topic"] is None]

    print(f"\n=== INCOME THRESHOLD SWEEP ({len(cache)} probes: {len(inscope)} in-scope, {len(oos)} out-of-scope) ===")
    print(f"{'thresh':>7} {'answered':>9} {'correct':>8} {'wrong':>6} {'oos-defer':>10}")
    best = None
    for th in [0.18, 0.20, 0.22, 0.24, 0.25, 0.26, 0.27, 0.28, 0.30, 0.35]:
        ans = [v for v in inscope if v["dist"] <= th]
        correct = [v for v in ans if _is_correct(v)]
        wrong = [v for v in ans if not _is_correct(v)]
        oos_defer = [v for v in oos if v["dist"] > th]
        print(f"{th:>7} {len(ans):>9} {len(correct):>8} {len(wrong):>6} "
              f"{len(oos_defer):>4}/{len(oos):<5}")
        score = len(correct) + len(oos_defer) - 3 * len(wrong)
        if best is None or score > best[1]:
            best = (th, score)
    print(f"\nsuggested threshold: {best[0]}")

    th = best[0]
    print(f"\n--- detail at threshold {th} ---")
    wrong = [(q, v) for q, v in cache.items()
             if v["topic"] is not None and v["dist"] <= th and not _is_correct(v)]
    blind = [(q, v) for q, v in cache.items()
             if v["topic"] is not None and v["dist"] > th]
    oos_leak = [(q, v) for q, v in cache.items()
                if v["topic"] is None and v["dist"] <= th]
    if wrong:
        print("WRONG VERDICT (routed to a topic with the opposite verdict):")
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
        run()
    elif cmd == "report":
        report()
    elif cmd == "reset":
        if os.path.exists(CACHE):
            os.remove(CACHE)
        print("cache cleared")
    else:
        print(__doc__)
