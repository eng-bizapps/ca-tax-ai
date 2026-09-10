"""Adversarial item sweep for the Ring 4 property-tax domain, mirroring
income_item_sweep.py's proven pattern (cached, resumable, mandatory
regression gate after any change to property_tax.py/engine.py's property
path).

Covers all 3 built slices (core Prop 13 estimate, Disabled Veterans'
Exemption, Prop 19 base-year-value transfer) plus the composed purchase+DV
path, the 4 out-of-scope redirects, and the missing-fact clarification --
using CORRECTED hand-verified values (the Prop 19 100%/105%/110% timing
mechanic was wrong in an early design and fixed before any of these cases
were locked in; see property_tax.py's own docstring for the correction).

Also regression-covers 3 real bugs found live during this domain's build,
each with a dedicated case below so they can never silently regress:
  - a bare purchase/tax year phantom-parsed as a dollar amount
    (_property_purchase_year_match's overlap-based removal)
  - a bare age ("I am 62 years old") phantom-parsed as a dollar amount,
    which previously broke the Prop 19 dispatcher's amount count and fell
    through to an unrelated sales-tax answer (_property_prop19_strip_age_
    phantoms)
  - a composed purchase-price + DV-exemption question falling through
    every dispatcher to an unrelated sales/informational non-answer,
    because compute_property_tax_with_dv_exemption existed but was never
    wired up (_property_dv_composed_answer)

Usage:
  python property_item_sweep.py run
  python property_item_sweep.py report
  python property_item_sweep.py reset
"""
import json
import os
import sys

import engine

CACHE = os.path.join(os.path.dirname(__file__), "property_item_sweep_results.json")
TOL = 0.02  # float rounding tolerance for dollar comparisons

# Each item: (question, expected dict). expected keys are checked only if present.
ITEMS = [
    # --- A: core Prop 13 estimate ---
    # $400,000 purchased 2015, current year 2026 (11 yrs): FBYV = 400000 *
    # 1.02^11 = $497,349.72; tax = 1% = $4,973.50
    ("How much California property tax will I owe on a house I bought for $400,000 in 2015?",
     {"status": "answered", "domain": "property", "category": "property_tax_estimate", "tax": 4973.50}),
    # + Homeowners' Exemption: assessed $490,349.72 -> tax $4,903.50
    ("How much California property tax will I owe on a house I bought for $400,000 in 2015 with the homeowners exemption?",
     {"status": "answered", "domain": "property", "category": "property_tax_estimate", "tax": 4903.50}),
    # same-year purchase (0 yrs compounding): FBYV = $600,000 -> tax $6,000.00
    ("How much California property tax will I owe on a house I bought for $600,000 in 2026?",
     {"status": "answered", "domain": "property", "category": "property_tax_estimate", "tax": 6000.00}),

    # --- B: Disabled Veterans' Exemption (standalone) ---
    # 2025, income $60,000 < $78,718 threshold -> low-income tier $262,950
    ("What is the California disabled veterans property tax exemption for 2025 if my household income is $60,000?",
     {"status": "answered", "domain": "property", "category": "property_tax_dv_exemption", "amount": 262950.0}),
    # 2025, income $120,000, over threshold -> basic tier $175,298
    ("What is the California disabled veterans property tax exemption for 2025 if my household income is $120,000?",
     {"status": "answered", "domain": "property", "category": "property_tax_dv_exemption", "amount": 175298.0}),
    # 2026, no income stated -> defaults to basic $180,671 (must NOT
    # silently misread the stated year as household income -- see the
    # "year phantom" bug note above)
    ("What is the California disabled veterans property tax exemption for 2026?",
     {"status": "answered", "domain": "property", "category": "property_tax_dv_exemption", "amount": 180671.0}),

    # --- B composed: purchase price + purchase year + DV exemption ---
    # $500,000 purchased 2020, current year 2025 (5 yrs): FBYV = 500000 *
    # 1.02^5 = $552,040.40; income $50,000 -> low-income tier $262,950;
    # assessed $289,090.40; tax $2,890.90
    ("I am a disabled veteran. I bought my house for $500,000 in 2020, and my household income is $50,000. What is my 2025 California property tax?",
     {"status": "answered", "domain": "property", "category": "property_tax_dv_composed", "tax": 2890.90}),
    # same, no income stated -> basic tier $175,298 -> assessed $376,742.40 -> tax $3,767.42
    ("I am a disabled veteran. I bought my house for $500,000 in 2020. What is my 2025 California property tax?",
     {"status": "answered", "domain": "property", "category": "property_tax_dv_composed", "tax": 3767.42}),

    # --- C: Prop 19 base-year-value transfer ---
    # BOE's own worked example: BYV $100,000, FCV $400,000 -> $600,000,
    # within 1 year (105% comparison): comparison_fcv=$420,000,
    # value_add=$180,000, new_taxable_value=$280,000, tax=$2,800.00
    ("I am 55 years old. My original adjusted base year value is $100,000, my original full cash value is $400,000, and I am buying a replacement home within 1 year at a full cash value of $600,000. What is my new taxable value under Prop 19?",
     {"status": "answered", "domain": "property", "category": "property_tax_prop19_transfer", "tax": 2800.00}),
    # disaster victim, 4th transfer, uncapped: BYV $80,000, FCV
    # $300,000->$350,000, within 1 year (105%): comparison_fcv=$315,000,
    # value_add=$35,000, new_taxable_value=$115,000, tax=$1,150.00
    ("My home was destroyed in a wildfire. This is my 4th time transferring my base year value under Prop 19. My original adjusted base year value is $80,000, my original full cash value is $300,000, and I am buying a replacement home within 1 year at a full cash value of $350,000.",
     {"status": "answered", "domain": "property", "category": "property_tax_prop19_transfer", "tax": 1150.00}),
    # age-55, 4th transfer -- MUST be rejected (age-55/disabled cap at 3;
    # regression case for the "62 years old" age-phantom bug: without the
    # age-phantom strip, this fell through to an unrelated sales answer
    # instead of a needs_review rejection)
    ("I am 62 years old. This is my 4th time transferring my base year value under Prop 19. My original adjusted base year value is $200,000, my original full cash value is $700,000, and I am buying a replacement home within 1 year at a full cash value of $900,000.",
     {"status": "needs_review", "domain": "property"}),
    # timing bucket: before the sale (100% comparison, no bonus): BYV
    # $200,000, FCV $700,000->$900,000: comparison_fcv=$700,000,
    # value_add=$200,000, new_taxable_value=$400,000, tax=$4,000.00
    ("I am 60 years old. My original adjusted base year value is $200,000, my original full cash value is $700,000, and I bought a replacement home before the sale at a full cash value of $900,000. What is my new taxable value under Prop 19?",
     {"status": "answered", "domain": "property", "category": "property_tax_prop19_transfer", "tax": 4000.00}),
    # timing bucket: within 2 years (110% comparison): comparison_fcv=$770,000,
    # value_add=$130,000, new_taxable_value=$330,000, tax=$3,300.00
    ("I am 58 years old. My original adjusted base year value is $200,000, my original full cash value is $700,000, and I bought a replacement home within 2 years at a full cash value of $900,000. What is my new taxable value under Prop 19?",
     {"status": "answered", "domain": "property", "category": "property_tax_prop19_transfer", "tax": 3300.00}),
    # 2nd transfer, age-55 -- NOT capped (cap is >3): 105% comparison,
    # comparison_fcv=$735,000, value_add=$165,000, new_taxable_value=$365,000,
    # tax=$3,650.00
    ("I am 70 years old. This is my 2nd time transferring my base year value under Prop 19. My original adjusted base year value is $200,000, my original full cash value is $700,000, and I am buying a replacement home within 1 year at a full cash value of $900,000.",
     {"status": "answered", "domain": "property", "category": "property_tax_prop19_transfer", "tax": 3650.00}),

    # --- out-of-scope redirects (disclose why, not a generic refusal) ---
    ("what is the property tax rate in los angeles",
     {"status": "needs_review", "domain": "property"}),
    ("do I live in a mello-roos CFD district",
     {"status": "needs_review", "domain": "property"}),
    ("my home's value declined under prop 8, what happens to my assessment",
     {"status": "needs_review", "domain": "property"}),
    ("can I use the parent-child exclusion to avoid reassessment",
     {"status": "needs_review", "domain": "property"}),

    # --- missing-fact clarification (purchase context present, but no
    # price/year stated -- must ask, not guess) ---
    ("How much California property tax will I owe on a house I bought?",
     {"status": "needs_review", "domain": "property"}),

    # --- cross-domain safety: bare "property tax on my house" with NO
    # purchase context at all stays genuinely out-of-scope (not even a
    # clarifying question) -- see coverage.py's own note on this exact
    # phrase; this file exercises it against the full pipeline ---
    ("How much is California property tax on my house?",
     {"status": "needs_review"}),
    # an itemized-deduction/AMT question mentioning property tax must
    # still route to INCOME, unaffected by the new property intercept
    ("do i owe california amt if my income is $200,000 and i have itemized deductions of $150,000 including property tax of $150,000, single?",
     {"status": "answered", "domain": "income", "category": "amt_screen"}),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def _check(result, expected):
    for k, want in expected.items():
        got = result.get(k)
        if k in ("tax", "amount"):
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
            r = engine.answer(q, compose=False, source="property_item_sweep")
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:120]}")
            break
        ok, fail_key = _check(r, exp)
        cache[q] = {
            "expected": exp, "ok": ok, "fail_key": fail_key,
            "status": r.get("status"), "domain": r.get("domain"),
            "category": r.get("category"), "amount": r.get("amount"), "tax": r.get("tax"),
        }
        graded += 1
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] {q[:70]:70} -> status={r.get('status')} domain={r.get('domain')} "
              f"category={r.get('category')} amount={r.get('amount')} tax={r.get('tax')}")
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
    print(f"\n===== PROPERTY ITEM SWEEP ({len(cache)} items) =====")
    print(f"correct : {len(ok)}")
    print(f"WRONG   : {len(bad)}")
    if bad:
        print("\n--- WRONG (fix these) ---")
        for q, v in bad:
            print(f"  {q}")
            print(f"    expected={v['expected']}  got status={v['status']} domain={v['domain']} "
                  f"category={v['category']} amount={v['amount']} tax={v['tax']} "
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
