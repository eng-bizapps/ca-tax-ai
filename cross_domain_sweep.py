"""Phase 4 cross-domain regression suite -- the formal test category the
original Ring-2 plan called for and that, until now, only existed as a
handful of scattered cases inside income_item_sweep.py.

Two things this suite exists to catch, that neither item_sweep.py (sales
only) nor income_item_sweep.py (income only, domain asserted almost as an
afterthought) can catch on their own:

1. AMBIGUOUS-WORD COLLISIONS: words that mean genuinely different things in
   each domain (gift, credit, return/refund, filing) routing to the WRONG
   domain's content. Every pair here is grounded against REAL content on
   BOTH sides (queried product_rules/income_tax_topics directly before
   writing any expected value -- see the module's git history for the
   verification queries), not guessed. This is exactly the failure class
   that already bit this project once for real (gifts_and_inheritance vs
   promotional_gifts, see engine.CROSS_DOMAIN_INCOME_OVERRIDE) -- this
   suite is the generalization of that one-off fix into a standing test.

2. THE tax_type SELECTOR'S GRACEFUL-DEGRADATION GUARANTEE: tax_type is
   documented as a HINT that reorders priority but never hard-excludes a
   domain (engine._answer's docstring). This suite proves that guarantee
   directly by forcing the WRONG tax_type on a clearly-single-domain
   question and confirming the correct domain still answers -- including
   the two hardest cases (a giver-phrased gift question with tax_type=
   "income" forced, and a receiver-phrased gift question with tax_type=
   "sales" forced), which stress the cross-domain override and the income
   embedding router respectively, neither of which had ever been exercised
   together with a manual tax_type override before this suite existed.

Found and fixed ONE real bug while building this (2026-07-28): "my
grandmother gave me money as a gift" -- clearly receiver-phrased -- routed
to sales/promotional_gifts (confidently wrong) because
CROSS_DOMAIN_INCOME_OVERRIDE's receiver word-set (received/receive/got/get)
didn't include "gave me" as a receiver signal. Fixed with a phrase-adjacency
regex (_RECEIVER_GAVE_PATTERN in engine.py) rather than adding "gave" as a
bare word (which would have wrongly flipped genuinely giver-phrased
questions like "I gave a gift"). See item below for the permanent
regression case.

Usage:
  python cross_domain_sweep.py run
  python cross_domain_sweep.py report
  python cross_domain_sweep.py reset
"""
import json
import os
import sys

import engine

CACHE = os.path.join(os.path.dirname(__file__), "cross_domain_sweep_results.json")
TOL = 0.02

# Each item: (question, tax_type, expected). tax_type is passed straight to
# engine.answer(); expected keys are checked only if present. Questions are
# deliberately UNIQUE across items (dict-keyed cache) even when they revisit
# a theme already covered elsewhere (e.g. gift/inheritance) -- each variant
# here targets a specific mechanism (default routing vs. forced tax_type),
# not just repeat coverage.
ITEMS = [
    # === ambiguous-word pairs, default routing (tax_type=None) ===
    # "credit": sales trade-in allowance vs. income renter's credit --
    # grounded against product_rules.trade_in_allowance (verified: CA does
    # NOT reduce sales tax for a trade-in) and ca_income_credits.renters_credit.
    ("if I trade in my old car, do I get a sales tax credit on the new one", None,
     {"status": "answered", "domain": "sales", "category": "trade_in_allowance", "taxable": True}),
    ("what is my renters credit if I make $40,000 filing single, unique phrasing check", None,
     {"status": "answered", "domain": "income", "category": "renters_credit", "tax": 60.00}),
    ("does california have a renters credit", None,
     {"domain": "income"}),  # no amount given -> falls to informational tier, not a wrong verdict

    # "return"/"refund": sales returned-merchandise exclusion vs. income
    # filing-status ("file"/"return" both appear in head_of_household's text)
    ("I returned a defective product, do I get my sales tax back", None,
     {"status": "answered", "domain": "sales", "category": "returned_merchandise", "taxable": False}),
    ("what is my filing status if I am unmarried and have a child living with me", None,
     {"status": "answered", "domain": "income", "category": "head_of_household_eligibility"}),

    # "gift": the known collision, revisited with FRESH phrasing (not the
    # exact strings already in income_item_sweep.py) to confirm the fix
    # generalizes rather than being overfit to specific wording.
    ("I'm giving my old couch away as a gift, do I owe use tax", None,
     {"status": "answered", "domain": "sales", "category": "promotional_gifts", "taxable": True}),
    # the real bug found while building this suite (see module docstring):
    # "gave me" with none of the original receiver trigger words present.
    ("my grandmother gave me money as a gift, do I have to pay tax on it", None,
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),
    ("my parents gave me a car as a gift, is that taxable", None,
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),
    # confirm the fix's precision holds: "gave" WITHOUT an adjacent me/us
    # object must stay giver-phrased (sales), not flip on the bare word.
    ("I gave a gift to my friend, do I owe tax", None,
     {"status": "answered", "domain": "sales", "category": "promotional_gifts", "taxable": True}),
    ("I gave my mom a birthday gift, is that taxable", None,
     {"status": "answered", "domain": "sales", "category": "promotional_gifts", "taxable": True}),

    # === tax_type selector: forced-WRONG-hint graceful degradation ===
    # sales questions, tax_type="income" forced -- must still answer sales.
    ("is a t-shirt taxable in california", "income",
     {"status": "answered", "domain": "sales", "taxable": True}),
    ("I am giving away merchandise as a gift, do I owe tax", "income",
     {"status": "answered", "domain": "sales", "category": "promotional_gifts", "taxable": True}),
    # income questions, tax_type="sales" forced -- must still answer income.
    ("what is the california standard deduction for single filers", "sales",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),
    ("I got a gift from my parents, is that taxable", "sales",
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),

    # correct-hint sanity checks (tax_type matches the true domain) --
    # confirms the selector doesn't just "always fall back," it actually
    # uses the hint to try the right domain first.
    ("is a t-shirt taxable in california", "sales",
     {"status": "answered", "domain": "sales", "taxable": True}),
    ("what is the california standard deduction for single filers", "income",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def _check(result, expected):
    for k, want in expected.items():
        got = result.get(k)
        if k == "tax":
            if got is None or abs(float(got) - want) > TOL:
                return False, k
        elif got != want:
            return False, k
    return True, None


def run():
    cache = _load()
    graded = 0
    for q, tax_type, exp in ITEMS:
        cache_key = f"{q}|{tax_type}"
        if cache_key in cache:
            continue
        try:
            r = engine.answer(q, compose=False, tax_type=tax_type, source="cross_domain_sweep")
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:120]}")
            break
        ok, fail_key = _check(r, exp)
        cache[cache_key] = {
            "question": q, "tax_type": tax_type, "expected": exp, "ok": ok, "fail_key": fail_key,
            "status": r.get("status"), "domain": r.get("domain"),
            "category": r.get("category"), "taxable": r.get("taxable"), "tax": r.get("tax"),
        }
        graded += 1
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] tax_type={str(tax_type):8} {q[:58]:58} -> "
              f"status={r.get('status')} domain={r.get('domain')} category={r.get('category')} "
              f"taxable={r.get('taxable')} tax={r.get('tax')}")
        _save(cache)
    print(f"\ngraded {graded} new; cached {len(cache)}/{len(ITEMS)}")
    report()


def report():
    cache = _load()
    if not cache:
        print("nothing graded yet")
        return
    ok = [item for item in cache.items() if item[1]["ok"]]
    bad = [item for item in cache.items() if not item[1]["ok"]]
    print(f"\n===== CROSS-DOMAIN SWEEP ({len(cache)} items) =====")
    print(f"correct : {len(ok)}")
    print(f"WRONG   : {len(bad)}")
    if bad:
        print("\n--- WRONG (cross-domain leak or selector failure -- fix these) ---")
        for key, v in bad:
            print(f"  {v['question']}  (tax_type={v['tax_type']})")
            print(f"    expected={v['expected']}  got status={v['status']} domain={v['domain']} "
                  f"category={v['category']} taxable={v['taxable']} tax={v['tax']} "
                  f"(mismatch on: {v['fail_key']})")


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
