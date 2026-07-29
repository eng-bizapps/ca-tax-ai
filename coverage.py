"""Coverage harness for the CA sales-tax responder.

Measures the metric that matters: of realistic taxpayer questions, how many get a
confident, cited answer vs. "Needs review" -- and of those answered, how many get
the correct taxable/exempt verdict.

Quota-aware: every graded question is cached to coverage_results.json, so re-runs
skip already-graded probes. Grade a few per day within the Gemini free tier, and
the harness accumulates full coverage over time.

Usage:
  python coverage.py inventory        # offline: rule distribution, no API calls
  python coverage.py run [N]          # grade up to N NEW probes (default 8), cached
  python coverage.py report           # summarize everything graded so far
  python coverage.py reset            # clear the cache
"""
import json
import os
import sys
from collections import defaultdict

CACHE = os.path.join(os.path.dirname(__file__), "coverage_results.json")

# Realistic questions phrased the way a taxpayer would ask (NOT using our internal
# product_key vocabulary), each tagged with the expected verdict and a topic area.
# expected_taxable: True / False / None (None = genuinely ambiguous / out of scope)
PROBES = [
    # --- food & groceries ---
    ("Is bottled water taxable in California?", False, "food"),
    ("Do I charge sales tax on a candy bar?", False, "food"),
    ("Is soda subject to sales tax?", True, "food"),
    ("Are hot prepared meals to go taxable?", True, "food"),
    ("Is a bag of ice for a cooler taxable?", True, "food"),
    # --- medicines / health ---
    ("Are prescription medicines taxable in California?", False, "health"),
    ("Is over-the-counter aspirin taxable?", True, "health"),
    ("Do I owe tax on an item paid for by Medicare Part A?", False, "health"),
    # --- software / digital ---
    ("Is downloaded software taxable in California?", False, "software"),
    ("Is prewritten software on a CD taxable?", True, "software"),
    ("Is custom software taxable?", False, "software"),
    ("Is a website design service taxable?", False, "digital"),
    ("Is electronic artwork delivered by email taxable?", False, "digital"),
    # --- leases ---
    ("Is leasing equipment subject to sales tax on the rental payments?", True, "leases"),
    ("Are lease payments on a car taxable in California?", True, "leases"),
    # --- construction ---
    ("Does a contractor pay tax on materials like lumber?", True, "construction"),
    ("Are construction fixtures like light fixtures taxable?", True, "construction"),
    ("Is installation labor taxable?", False, "construction"),
    # --- vehicles / vessels ---
    ("Do I owe tax on a used car bought from a private party?", True, "vehicles"),
    ("Is a car sold between a parent and child taxable?", False, "vehicles"),
    ("Is a boat bought from a family member taxable?", False, "vehicles"),
    # --- interstate / export ---
    ("Is a sale taxable if the item is shipped out of state by the retailer?", False, "interstate"),
    ("Are goods sold for export shipped directly abroad taxable?", False, "interstate"),
    ("Is a sale taxable if the buyer picks it up in California but takes it out of state?", True, "interstate"),
    # --- printing / publishing ---
    ("Is printed matter like brochures taxable?", True, "printing"),
    ("Are typesetting and typography services taxable?", False, "printing"),
    ("Is a newspaper subscription delivered by mail taxable?", False, "newspapers"),
    ("Is access to digital newspaper content taxable?", False, "newspapers"),
    # --- advertising / art ---
    ("Is finished art delivered on paper taxable?", True, "advertising"),
    ("Are advertising agency media placement commissions taxable?", False, "advertising"),
    # --- occasional / business sales ---
    ("Is selling my personal furniture a taxable sale?", False, "occasional"),
    ("Is the sale of corporate stock subject to sales tax?", False, "occasional"),
    ("Is the sale of a whole business's assets taxable?", True, "occasional"),
    # --- fuel ---
    ("Is gasoline taxed at the full sales tax rate?", True, "fuel"),
    ("Is jet fuel for an international flight taxable?", False, "fuel"),
    # --- motion pictures ---
    ("Are film editing and post-production services taxable?", False, "film"),
    ("Is a wedding video taxable?", True, "film"),
    ("Is a release print sold for theater exhibition taxable?", True, "film"),
    # --- common carriers ---
    ("Is fuel sold to an airline for an international flight taxable?", False, "carriers"),
    # --- nonprofit / government ---
    ("Are Girl Scout cookie sales taxable?", False, "nonprofit"),
    ("Are sales to the U.S. government taxable?", False, "government"),
    ("Is a sale to a foreign air carrier for export taxable?", False, "carriers"),
    # --- shipping / containers ---
    ("Is ice used to ship food products taxable?", False, "shipping"),
    ("Are containers sold to ship food products taxable?", False, "shipping"),
    # --- credit / bad debt / discounts ---
    ("Can I recover sales tax on a customer's bad debt?", False, "credit"),
    ("Are cash discounts excluded from sales tax?", False, "credit"),
    ("Are separately stated finance charges on a credit sale taxable?", False, "credit"),
    # --- special partial exemptions ---
    ("Is timber harvesting equipment taxable?", True, "partial-exempt"),
    ("Is a racehorse bought for breeding taxable?", True, "partial-exempt"),
    ("Is teleproduction equipment taxable?", True, "partial-exempt"),
    # --- damaged goods ---
    ("If goods are destroyed in transit before delivery, is the sale taxable?", False, "damaged"),
    # --- likely out-of-scope (should be Needs review) ---
    ("How much is California property tax on my house?", None, "out-of-scope"),
    ("What is the California income tax rate?", None, "out-of-scope"),
]


def _load():
    if os.path.exists(CACHE):
        return json.load(open(CACHE, encoding="utf-8"))
    return {}


def _save(cache):
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), indent=2)


def inventory():
    """Offline: show rule distribution across regs and topics. No API calls."""
    import db
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT count(*), count(distinct reg), "
                "sum(case when taxable then 1 else 0 end), "
                "sum(case when not taxable then 1 else 0 end) FROM product_rules")
    tot, regs, tax, ex = cur.fetchone()
    print(f"product_rules: {tot} rules across {regs} regs ({tax} taxable / {ex} exempt)")
    cur.execute("SELECT reg, count(*) FROM product_rules GROUP BY reg ORDER BY count(*) DESC")
    rows = cur.fetchall()
    print(f"\nTop regs by depth:")
    for reg, n in rows[:12]:
        print(f"  reg {reg}: {n}")
    print(f"  ... {len(rows)} regs total")
    conn.close()


def run(limit=8):
    import engine
    cache = _load()
    graded = 0
    for q, exp, topic in PROBES:
        if q in cache:
            continue
        if graded >= limit:
            break
        try:
            r = engine.answer(q, compose=False, source="coverage")
        except Exception as e:
            print(f"STOP (API error after {graded} graded): {e}")
            break
        cache[q] = {"status": r["status"], "taxable": r["taxable"],
                    "key": r["category"], "expected": exp, "topic": topic}
        graded += 1
        verdict = "review" if r["status"] not in ("answered", "conditional") else str(r["taxable"])
        print(f"  graded: {q[:55]:55} -> {verdict}")
    _save(cache)
    remaining = sum(1 for q, _, _ in PROBES if q not in cache)
    print(f"\nGraded {graded} new this run. Cached total: {len(cache)}/{len(PROBES)}. "
          f"Remaining to grade: {remaining}")
    report()


def report():
    cache = _load()
    if not cache:
        print("Nothing graded yet. Run: python coverage.py run")
        return
    n = len(cache)
    answered = [v for v in cache.values() if v["status"] in ("answered", "conditional")]
    review = [v for v in cache.values() if v["status"] not in ("answered", "conditional")]
    in_scope = [v for v in cache.values() if v["expected"] is not None]
    oos = [v for v in cache.values() if v["expected"] is None]

    # coverage among in-scope questions (out-of-scope SHOULD be needs-review)
    in_scope_answered = [v for v in in_scope if v["status"] in ("answered", "conditional")]
    correct = [v for v in in_scope_answered if v["taxable"] == v["expected"]]
    wrong = [v for v in in_scope_answered if v["taxable"] != v["expected"]]
    blind = [v for v in in_scope if v["status"] not in ("answered", "conditional")]

    print(f"\n===== COVERAGE REPORT ({n}/{len(PROBES)} probes graded) =====")
    print(f"In-scope probes:        {len(in_scope)}")
    print(f"  answered (covered):   {len(in_scope_answered)}  "
          f"({100*len(in_scope_answered)//max(len(in_scope),1)}%)")
    print(f"  correct verdict:      {len(correct)}  "
          f"({100*len(correct)//max(len(in_scope_answered),1)}% of answered)")
    print(f"  wrong verdict:        {len(wrong)}")
    print(f"  needs-review (gap):   {len(blind)}")
    print(f"Out-of-scope probes:    {len(oos)}  "
          f"(correctly deferred: {sum(1 for v in oos if v['status']!='answered')}/{len(oos)})")

    if wrong:
        print("\n--- WRONG VERDICTS (rule/routing errors to investigate) ---")
        for q, v in cache.items():
            if v in wrong:
                print(f"  [{v['topic']}] got={v['taxable']} exp={v['expected']} key={v['key']}")
                print(f"     {q}")
    if blind:
        print("\n--- BLIND SPOTS (in-scope but Needs review) ---")
        for q, v in cache.items():
            if v in blind:
                print(f"  [{v['topic']}] {q}")
    oos_leak = [(q, v) for q, v in cache.items() if v["expected"] is None and v["status"] in ("answered", "conditional")]
    if oos_leak:
        print("\n--- OUT-OF-SCOPE LEAKS (answered when it should defer) ---")
        for q, v in oos_leak:
            print(f"  key={v['key']}  {q}")

    # by-topic
    bt = defaultdict(lambda: [0, 0])  # topic -> [answered_correct, total_in_scope]
    for v in in_scope:
        bt[v["topic"]][1] += 1
        if v["status"] in ("answered", "conditional") and v["taxable"] == v["expected"]:
            bt[v["topic"]][0] += 1
    print("\n--- BY TOPIC (correct / in-scope) ---")
    for topic in sorted(bt):
        c, t = bt[topic]
        print(f"  {topic:16} {c}/{t}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "inventory":
        inventory()
    elif cmd == "run":
        run(int(sys.argv[2]) if len(sys.argv) > 2 else 8)
    elif cmd == "report":
        report()
    elif cmd == "reset":
        if os.path.exists(CACHE):
            os.remove(CACHE)
        print("cache cleared")
    else:
        print(__doc__)
