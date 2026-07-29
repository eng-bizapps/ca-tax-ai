"""Adversarial item sweep for the Ring 2 income-tax domain (Phase 3/4).

Mirrors item_sweep.py's proven pattern (cached, resumable, mandatory
regression gate after any change to income_brackets.py/engine.py's income
path) but for the heterogeneous income-domain content: bracket computations
(hand-verified against FTB's own worked example, one case crossing the $1M
Behavioral Health Services Tax surtax threshold per the plan's explicit
Phase 3 verification requirement), standard-deduction lookups, structured
topic verdicts, deliberate-defer cases (complexity disqualifiers), and
CROSS-DOMAIN safety (a sales-tax question must never be answered by the
income domain, and vice versa -- the plan's Phase 4 requirement, started
here rather than deferred further since the risk is concrete and testable
now).

Usage:
  python income_item_sweep.py run
  python income_item_sweep.py report
  python income_item_sweep.py reset
"""
import json
import os
import sys

import engine

CACHE = os.path.join(os.path.dirname(__file__), "income_item_sweep_results.json")
TOL = 0.02   # float rounding tolerance for dollar comparisons

# Each item: (question, expected dict). expected keys are checked only if present.
#   status: 'answered' | 'informational' | 'needs_review'
#   domain: 'income' | 'sales'
#   category, taxable, tax (within TOL)
ITEMS = [
    # --- bracket computation (hand-verified) ---
    # $125,000 MFJ: dedu $11,412 -> taxable $113,588 (Sched Y $82,904-$115,084
    # band: $2,044.02 + 6% x ($113,588-$82,904) = $3,885.06)
    ("what is my california tax bracket if I make $125,000 filing married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3885.06}),
    # $60,000 HOH: dedu $11,412 -> taxable $48,588 (Sched Z $22,173-$52,530
    # band: $221.73 + 2% x ($48,588-$22,173) = $750.03)
    ("how much tax do I owe on $60,000 as head of household",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 750.03}),
    # $30,000 single: dedu $5,706 -> taxable $24,294 (Sched X $11,079-$26,264
    # band: $110.79 + 2% x ($24,294-$11,079) = $375.09)
    ("how much california tax do I owe on $30,000 single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 375.09}),
    # $1,500,000 single: dedu $5,706 -> taxable $1,494,294, CROSSES the $1M
    # surtax threshold. Bracket: $72,219.84 + 12.3% x ($1,494,294-$742,953) =
    # $164,634.78; surtax: 1% x ($1,494,294-$1,000,000) = $4,942.94.
    # Total = $169,577.72.
    ("how much california tax do I owe on $1,500,000 single filing",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 169577.72}),

    # --- standard deduction (real structured lookup, not LLM paraphrase) ---
    ("what is the california standard deduction",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),
    ("what is the standard deduction for single filers",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),
    ("what is the standard deduction for married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),

    # --- CalEITC (verified 658-row 2025 table, see load_income_content.py) ---
    # $9,975, 2 children: table row 9,951-10,000, column 2 = $3,288 --
    # spot-checked directly against the visually-read PDF page during
    # extraction (independent of the extraction script's own validation).
    ("what is my CalEITC if I make $9,975 with 2 qualifying children",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 3288.00}),
    # $100, 0 children: table row 51-100, column 0 = $5
    ("what is my california earned income tax credit if I earn $100 with no children",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 5.00}),
    # missing children count -> specific clarifying message, not generic defer
    ("what is my caleitc if I make $10,000",
     {"status": "needs_review", "domain": "income"}),

    # --- Young Child Tax Credit (exact FTB Form 3514 Part VII arithmetic) ---
    # below the $27,425 phase-out threshold -> flat $1,189
    ("what is my young child tax credit if I earn $20,000",
     {"status": "answered", "domain": "income", "category": "young_child_tax_credit", "tax": 1189.00}),
    # near the top of the phase-out range ($32,900, same ceiling as CalEITC):
    # excess=5475, step1=54.75, reduction=54.75*21.71=1188.68 (FTB's own
    # 2-step rounding), credit=1189-1188.68=0.32 -> between $0 and $1 -> $1
    ("what is my yctc if I make $32,900",
     {"status": "answered", "domain": "income", "category": "young_child_tax_credit", "tax": 1.00}),

    # --- Nonrefundable Renter's Credit (flat amount, hard income ceiling) ---
    ("what is my renters credit if I make $40,000 single",
     {"status": "answered", "domain": "income", "category": "renters_credit", "tax": 60.00}),
    ("what is my renters credit if I make $40,000 filing married filing jointly",
     {"status": "answered", "domain": "income", "category": "renters_credit", "tax": 120.00}),
    # missing filing status -> specific clarifying message, not generic defer
    ("what is my renters credit if I make $40,000",
     {"status": "needs_review", "domain": "income"}),

    # --- structured conformity-topic verdicts ---
    ("is unemployment compensation taxable in california",
     {"status": "answered", "domain": "income", "category": "unemployment_compensation", "taxable": False}),
    ("is social security income taxable in california",
     {"status": "answered", "domain": "income", "category": "social_security_income", "taxable": False}),
    ("is california paid family leave taxable",
     {"status": "answered", "domain": "income", "category": "paid_family_leave", "taxable": False}),
    ("is an inheritance taxable in california",
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),

    # --- second conformity batch (verified against FTB pages fetched via
    # browser this pass; income_route_eval.py confirmed the gambling/lottery
    # same-domain collision pair resolves correctly at every threshold) ---
    ("are gambling winnings taxable in california",
     {"status": "answered", "domain": "income", "category": "gambling_winnings", "taxable": True}),
    ("are california lottery winnings taxable",
     {"status": "answered", "domain": "income", "category": "california_lottery_winnings", "taxable": False}),
    ("is interest from us treasury bonds taxable in california",
     {"status": "answered", "domain": "income", "category": "us_government_bond_interest", "taxable": False}),
    ("is interest from an out of state municipal bond taxable in california",
     {"status": "answered", "domain": "income", "category": "out_of_state_municipal_bond_interest", "taxable": True}),
    ("are hsa contributions taxable in california",
     {"status": "answered", "domain": "income", "category": "hsa_contributions_and_earnings", "taxable": True}),

    # --- Head of Household eligibility: DELIBERATELY informational-only
    # (no taxable key asserted -- it's None, same as ca_standard_deduction's
    # pattern above). Confirms the compute path (which needs a dollar
    # amount) and this pure-eligibility topic never collide.
    ("can I file as head of household in california",
     {"status": "answered", "domain": "income", "category": "head_of_household_eligibility"}),
    ("am I eligible for head of household filing status",
     {"status": "answered", "domain": "income", "category": "head_of_household_eligibility"}),
    # (the existing "how much tax do I owe on $60,000 as head of household"
    # bracket-compute case above already proves the compute path and this
    # eligibility topic don't collide -- amount-bearing questions never
    # reach topic routing at all, see _answer_income's call order.)

    # --- cross-domain override: found via direct testing, sales & income
    # both fired confidently on "gift" (sales: promotional_gifts -- the
    # GIVER's use-tax liability, wrong for this phrasing; income: correctly
    # nontaxable). Must resolve to income for receiver-phrased questions,
    # and must NOT hijack genuinely giver-phrased sales questions.
    ("do I have to pay tax on a gift I received",
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),
    ("I got a gift from my parents, is that taxable",
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),
    ("I am giving away merchandise as a gift, do I owe tax",
     {"status": "answered", "domain": "sales", "category": "promotional_gifts", "taxable": True}),

    # --- deliberate defers: complexity disqualifiers (never guess) ---
    ("how much california tax do I owe on $100,000 self-employed married filing jointly",
     {"status": "needs_review"}),
    ("what is my tax bracket if I make $80,000",   # no filing status given
     {"status": "needs_review"}),
    # found via real usage: a clear compute-shaped question missing only
    # filing status must get a SPECIFIC clarifying message (domain=income),
    # not the generic sales-side needs_review text (domain=sales) -- domain
    # is the regression signal that would catch this falling through again
    ("how much tax to pay for income of 100000 in california",
     {"status": "needs_review", "domain": "income"}),
    ("how much tax do I owe on $200,000 in capital gains single filer",
     {"status": "needs_review"}),

    # --- genuinely out of scope (neither domain covers it) ---
    ("what is the property tax rate in los angeles", {"status": "needs_review"}),
    ("how do I register my car with the dmv", {"status": "needs_review"}),

    # --- cross-domain safety: sales-tax questions must NOT be captured by income ---
    ("is furniture taxable in california",
     {"status": "answered", "domain": "sales", "taxable": True}),
    ("is cannabis taxable in california", {"domain": "sales"}),
    ("is bread taxable in california", {"domain": "sales", "taxable": False}),
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
    for q, exp in ITEMS:
        if q in cache:
            continue
        try:
            r = engine.answer(q, compose=False, source="income_item_sweep")
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:120]}")
            break
        ok, fail_key = _check(r, exp)
        cache[q] = {
            "expected": exp, "ok": ok, "fail_key": fail_key,
            "status": r.get("status"), "domain": r.get("domain"),
            "category": r.get("category"), "taxable": r.get("taxable"), "tax": r.get("tax"),
        }
        graded += 1
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] {q[:70]:70} -> status={r.get('status')} domain={r.get('domain')} "
              f"category={r.get('category')} taxable={r.get('taxable')} tax={r.get('tax')}")
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
    print(f"\n===== INCOME ITEM SWEEP ({len(cache)} items) =====")
    print(f"correct : {len(ok)}")
    print(f"WRONG   : {len(bad)}")
    if bad:
        print("\n--- WRONG (fix these) ---")
        for q, v in bad:
            print(f"  {q}")
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
