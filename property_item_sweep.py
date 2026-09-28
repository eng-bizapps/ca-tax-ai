"""Adversarial item sweep for the Ring 4 property-tax domain, mirroring
income_item_sweep.py's proven pattern (cached, resumable, mandatory
regression gate after any change to property_tax.py/engine.py's property
path).

Covers all 7 built slices (core Prop 13 estimate, Disabled Veterans'
Exemption, Prop 19 base-year-value transfer, Prop 19 parent-child/
grandparent-grandchild exclusion, supplemental assessment, county-average
local override rate, Prop 8 decline-in-value for the ordinary market-
decline case) plus the composed purchase+DV path, the remaining 2
out-of-scope redirects (local-tra-rate -- the remaining EXACT-per-parcel
gap, distinct from the county-average slice above -- and prop8-decline-
damage-destruction, the genuinely different disaster/destruction case,
distinct from the ordinary-decline slice above; Mello-Roos is a 3rd,
not_applicable rather than deferred, see property_tax_inventory.py), and
the missing-fact clarification -- using CORRECTED hand-verified values
(the Prop 19 100%/105%/110% timing mechanic was wrong in an early design
and fixed before any of these cases were locked in; see property_tax.py's
own docstring for the correction).

The Prop 8 decline-in-value cases are the SECOND such reversal this
session: this item was assessed "genuinely out of reach" 3 separate times
before being verified tractable by reading Rev. & Tax. Code Sec. 51
directly in full -- see property_tax.py's own SECOND correction-found note
for the exact statutory reasoning (the factored base year value ceiling
never resets or depends on intervening-year history).

The county-override-rate dollar cases use REAL data, independently
verified this session (not just BOE-worked-example-adjacent like the
Prop-19 fix above): the CA State Controller's Office's own JSON API,
cross-checked county-by-county for plausibility, with the 2 anomalous
counties (San Benito, Plumas) independently confirmed as data errors
against each county's own bond tax-rate statement/resolution before being
excluded -- see load_county_override_rates.py's own docstring.

The parent-child/supplemental-assessment dollar cases below use FRESH,
self-computed round numbers, NOT the BOE LTA 2026/026 Q50 worked example --
that example's own figures don't reconcile against the current
$1,044,586 value-cap constant (see property_tax.compute_parent_child_
exclusion_value's docstring), so it's deliberately not used as a fixture.
Only the underlying MECHANIC and the supplemental proration factor table
are independently BOE-confirmed; the dollar amounts here are chosen for
clean arithmetic, not sourced from a lower-confidence AI-summarized fetch.

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

    # --- D: Prop 19 parent-child/grandparent-grandchild exclusion ---
    # family home, all facts stated -> qualifies
    ("My son moved into the family home within a year of the transfer and filed for the homeowners exemption within a year -- do I qualify for the parent-child exclusion?",
     {"status": "answered", "domain": "property", "category": "property_tax_parent_child_exclusion", "taxable": True}),
    # family farm -- NO occupancy/filing requirement, regression-guards
    # that the family-home-only gate doesn't leak into the farm category
    ("I transferred my family farm to my son -- does he qualify for the parent-child exclusion?",
     {"status": "answered", "domain": "property", "category": "property_tax_parent_child_exclusion", "taxable": True}),
    # clean False: unrelated party
    ("The property was transferred to an unrelated party -- do they qualify for the parent-child exclusion?",
     {"status": "answered", "domain": "property", "category": "property_tax_parent_child_exclusion", "taxable": False}),
    # clean False: claim window closed (>3 years ago + already resold)
    ("This was transferred to my son more than 5 years ago and I already sold it to someone else -- do I still qualify for the parent-child exclusion?",
     {"status": "answered", "domain": "property", "category": "property_tax_parent_child_exclusion", "taxable": False}),
    # checklist-incomplete: relationship + category stated, but occupancy/
    # filing timing omitted -- must ask, not guess
    ("My son moved into the family home -- does he qualify for the parent-child exclusion?",
     {"status": "needs_review", "domain": "property"}),
    # value-cap-exceeded, composed dollar answer: old FBYV $200,000, FMV
    # $1,400,000 -> value_cap = 200,000 + 1,044,586 = $1,244,586; excess =
    # 1,400,000 - 1,244,586 = $155,414; new_taxable_value = $355,414;
    # tax = 1% x 355,414 = $3,554.14 (fresh self-consistent numbers, not
    # the non-reconciling BOE LTA 2026/026 Q50 example -- see module note)
    ("My daughter moved into the family home within a year of the transfer and filed for the homeowners exemption within a year. The old base year value is $200,000 and the full cash value is $1,400,000. Do I qualify for the parent-child exclusion?",
     {"status": "answered", "domain": "property", "category": "property_tax_parent_child_exclusion",
      "taxable": True, "tax": 3554.14}),
    # under the cap: FMV $900,000 < value_cap $1,244,586 -> NO reassessment,
    # new_taxable_value = old_fbyv = $200,000, tax = 1% x 200,000 = $2,000.00
    ("My daughter moved into the family home within a year of the transfer and filed for the homeowners exemption within a year. The old base year value is $200,000 and the full cash value is $900,000. Do I qualify for the parent-child exclusion?",
     {"status": "answered", "domain": "property", "category": "property_tax_parent_child_exclusion",
      "taxable": True, "tax": 2000.00}),

    # --- E: supplemental assessment ---
    # Jan-May event (March 2026) -> presumed effective April (factor 0.25),
    # BIMODAL, full interest -> TWO supplementals: added_value = 500,000 -
    # 200,000 = $300,000; supplemental_1 tax = 1% x (300,000 x 0.25) =
    # $750.00; supplemental_2 (entire next FY, unprorated) tax = 1% x
    # 300,000 = $3,000.00
    ("In March 2026, there was a change of ownership. The new base year value is $500,000 and the prior taxable value was $200,000. What's my supplemental assessment?",
     {"status": "answered", "domain": "property", "category": "property_tax_supplemental_assessment", "tax": 750.00}),
    # Jun-Dec event (September 2026) -> presumed effective October (factor
    # 0.75), NOT bimodal -> ONE supplemental only: added_value = 650,000 -
    # 500,000 = $150,000; tax = 1% x (150,000 x 0.75) = $1,125.00
    ("In September 2026, there was a change of ownership. The new base year value is $650,000 and the prior taxable value was $500,000. What's my supplemental assessment bill?",
     {"status": "answered", "domain": "property", "category": "property_tax_supplemental_assessment", "tax": 1125.00}),
    # June-event rollover edge case: presumed effective date rolls into
    # the NEXT fiscal year (July, same calendar year) but stays in the
    # "one supplemental" bucket (factor 1.00, unprorated): added_value =
    # 700,000 - 580,000 = $120,000; tax = 1% x 120,000 = $1,200.00
    ("In June 2026, there was a change of ownership. The new base year value is $700,000 and the prior taxable value was $580,000. What's my supplemental assessment?",
     {"status": "answered", "domain": "property", "category": "property_tax_supplemental_assessment", "tax": 1200.00}),
    # December-event rollover edge case: presumed effective date rolls
    # into the NEXT calendar year (January) but stays in the SAME fiscal
    # year as the event -- factor 0.50: added_value = 700,000 - 580,000 =
    # $120,000; tax = 1% x (120,000 x 0.50) = $600.00
    ("In December 2026, there was a change of ownership. The new base year value is $700,000 and the prior taxable value was $580,000. What's my supplemental assessment?",
     {"status": "answered", "domain": "property", "category": "property_tax_supplemental_assessment", "tax": 600.00}),

    # --- F: county-average local override rate ---
    # composed estimate with a recognized county (Kern, override_rate
    # 0.002529): total_rate 0.012529, FBYV $497,349.72 (same $400k/2015
    # scenario as case A above) -> tax = round(0.012529*497349.72, 2) = $6,231.29
    ("How much California property tax will I owe on a house I bought for $400,000 in 2015 in Kern County?",
     {"status": "answered", "domain": "property", "category": "property_tax_estimate_with_county_rate", "tax": 6231.29}),
    # recognized-but-EXCLUDED county (San Benito, confirmed SCO data error)
    # -> falls back to the plain 1%-only estimate, identical to no-county case
    ("How much California property tax will I owe on a house I bought for $400,000 in 2015 in San Benito County?",
     {"status": "answered", "domain": "property", "category": "property_tax_estimate", "tax": 4973.50}),
    # NOT a recognized CA county at all -> same generic fallback, no crash
    ("How much California property tax will I owe on a house I bought for $400,000 in 2015 in Multnomah County?",
     {"status": "answered", "domain": "property", "category": "property_tax_estimate", "tax": 4973.50}),
    # bare rate-only question, recognized county -> specific answer instead
    # of the generic deferral
    ("What is the property tax rate in Kern County?",
     {"status": "answered", "domain": "property", "category": "property_tax_county_rate", "rate": 0.002529}),
    # existing out-of-scope case below (no literal "County" suffix) is the
    # regression guard that this feature's strict suffix requirement wasn't
    # accidentally loosened -- see "what is the property tax rate in los angeles"

    # --- out-of-scope redirects (disclose why, not a generic refusal) ---
    ("what is the property tax rate in los angeles",
     {"status": "needs_review", "domain": "property"}),
    ("do I live in a mello-roos CFD district",
     {"status": "needs_review", "domain": "property"}),
    # zero-personal-fact Prop 8 question -> now reached via the missing-
    # fact path (detect_prop8_decline_signal True, all 3 facts missing),
    # NOT the retired out-of-scope stub -- same expected dict, different
    # underlying path; regression-guards the retirement didn't break this
    ("my home's value declined under prop 8, what happens to my assessment",
     {"status": "needs_review", "domain": "property"}),

    # --- G: Prop 8 decline-in-value (ordinary market decline) ---
    # below FBYV -> Prop 8 active: $500,000/2015 -> FBYV $621,687.15 (11
    # yrs @ 2%, same formula as case A); market value $450,000 < FBYV ->
    # assessed_value = $450,000.00, tax = 1% x 450,000 = $4,500.00
    ("I bought my house for $500,000 in 2015 and it's currently only worth $450,000 due to a decline in value. What is my assessed value under Prop 8?",
     {"status": "answered", "domain": "property", "category": "property_tax_prop8_decline",
      "taxable": True, "tax": 4500.00}),
    # above FBYV -> Prop 8 NOT active (regression guard against an "always
    # take market value" bug): market value $700,000 > FBYV $621,687.15 ->
    # assessed_value = FBYV = $621,687.15, tax = 1% x 621,687.15 = $6,216.87
    ("I bought my house for $500,000 in 2015 and its current market value is $700,000. What is my assessed value under Prop 8?",
     {"status": "answered", "domain": "property", "category": "property_tax_prop8_decline",
      "taxable": False, "tax": 6216.87}),
    # damage/destruction exclusion -> genuinely different mechanic, walled
    # off before the ordinary-decline formula ever runs
    ("My house was destroyed in a wildfire. I bought it for $500,000 in 2010. What's my assessed value now under Prop 8?",
     {"status": "needs_review", "domain": "property"}),
    # missing market value -> Prop8 vocab + price/year stated, no market
    # value -> must ask, not guess
    ("I bought my house for $500,000 in 2015. Has it declined in value?",
     {"status": "needs_review", "domain": "property"}),

    # zero-personal-fact parent-child question -> informational fallback
    # (preserves this exact case's pre-existing needs_review outcome)
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
        elif k == "rate":
            if got is None or abs(float(got) - want) > 0.000001:
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
