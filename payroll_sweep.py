"""Regression sweep for payroll_withholding.py's per-paycheck compute path.
Mirrors income_item_sweep.py's cached/resumable harness (_check/run/report/
CLI dispatch, JSON cache, float tolerance for dollar comparisons), but ITEMS
here are structured keyword-arg dicts fed directly to
payroll_withholding.compute_paycheck, NOT chat questions through
engine.answer -- this feature is form-only (see payroll_withholding.py's
module docstring), so there is no NL surface to sweep.

Every expected value below was hand-verified against IRS Pub 15-T
Worksheet 1A / EDD Method B arithmetic by hand before being locked in here
(same discipline as every other feature this session) -- case 1 is also an
EXACT reproduction of the user's own real ADP salary paycheck calculator
screenshot ($100,000/yr, weekly, single, CA, no adjustments -> federal
$253.27, SS $119.23, Medicare $27.88, CA $110.17, SDI $25.00, take-home
$1,387.53, matching to the cent). That exact match is what caught a real
bug: an initial "round federal withholding to the nearest whole dollar"
step (misattributed from Pub 15-T's manual Wage Bracket Method tables,
which don't apply to Worksheet 1A's own "Automated Payroll Systems"
percentage method) gave $253.00 instead of $253.27 -- removed in favor of
ordinary cent-precision rounding once the ADP screenshot proved it wrong.

Usage:
  python payroll_sweep.py run
  python payroll_sweep.py report
  python payroll_sweep.py reset
"""
import json
import os
import sys

import income_db
import payroll_withholding as pw

CACHE = os.path.join(os.path.dirname(__file__), "payroll_sweep_results.json")
TOL = 0.02
DOLLAR_KEYS = {"gross_pay", "federal_withholding", "social_security", "medicare",
               "additional_medicare", "ca_withholding", "ca_sdi", "total_taxes", "take_home_pay",
               "pretax_benefits_this_period", "federal_wage_reduction_this_period",
               "ca_wage_reduction_this_period", "fica_wage_reduction_this_period"}

# Each item: (label, kwargs dict, expected dict). expected keys checked only if present.
ITEMS = [
    # User's own ADP screenshot -- $100,000/yr salary, weekly, single, CA,
    # no adjustments. EXACT match to the cent across every component.
    ("adp_100k_weekly_single_no_adjustments", dict(
        gross_pay=100000.0 / 52, pay_frequency="weekly", filing_status="single",
    ), {
        "federal_withholding": 253.27, "social_security": 119.23, "medicare": 27.88,
        "additional_medicare": 0.0, "ca_withholding": 110.17, "ca_sdi": 25.00,
        "total_taxes": 535.55, "take_home_pay": 1387.53,
    }),

    # MFJ with the Step 2 checkbox checked, biweekly -- exercises the
    # second federal bracket schedule and the $0 Step-1 deduction branch.
    # Hand-verified: annual=$78,000; adjusted=$78,000 (step2 box -> $0
    # deduction); MFJ step2_checkbox bracket $66,500-$121,800 (base
    # $5,800, 22%): (78000-66500)*0.22+5800=$8,330 annual -> /26=$320.38.
    ("mfj_step2_checked_biweekly", dict(
        gross_pay=3000.0, pay_frequency="biweekly", filing_status="mfj", step2_checkbox=True,
    ), {"federal_withholding": 320.38, "ca_withholding": 68.52, "take_home_pay": 2342.60}),

    # HoH with CA regular + estimated-deduction allowances, monthly --
    # exercises the exemption-credit reduction and the estimated-deduction
    # table. Hand-verified: annual=$72,000; federal HoH standard bracket
    # $33,250-$83,000 (base $1,770, 12%): (72000-8600-33250)*0.12+1770=
    # $5,388 annual -> /12=$449.00 exactly.
    ("hoh_with_ca_allowances_monthly", dict(
        gross_pay=6000.0, pay_frequency="monthly", filing_status="hoh",
        ca_regular_allowances=2, ca_estimated_deduction_allowances=1,
    ), {"federal_withholding": 449.00, "ca_withholding": 73.81, "take_home_pay": 4940.19}),

    # High earner crossing BOTH the SS wage base ($184,500) and the
    # Additional Medicare threshold ($200,000) in the same annualized
    # computation. Hand-verified: SS capped at $184,500*6.2%/12=$953.25
    # (matches SSA's own published max annual SS tax of $11,439.00 / 12
    # exactly); Additional Medicare on (250000-200000)*0.9%/12=$37.50.
    ("high_earner_crosses_ss_cap_and_additional_medicare", dict(
        gross_pay=250000.0 / 12, pay_frequency="monthly", filing_status="single",
    ), {"social_security": 953.25, "medicare": 302.08, "additional_medicare": 37.50,
        "federal_withholding": 4275.33}),

    # Low-income case hitting CA's Table 1 exemption short-circuit
    # (annual ~$15,000 < single_dual_multiple's $18,896 threshold) -> $0
    # CA withholding, no bracket computation performed at all.
    ("low_income_ca_exemption_biweekly", dict(
        gross_pay=576.92, pay_frequency="biweekly", filing_status="single",
    ), {"ca_withholding": 0.0}),

    # Pre-2020 W-4 with allowances -- exercises the non-2020+ branch
    # (allowances x $4,300 subtracted instead of Step 4a/b/c). Hand-
    # verified: annual=$78,000; adjusted=78000-3*4300=$65,100; MFJ
    # standard bracket $44,100-$120,100 (base $2,480, 12%):
    # (65100-44100)*0.12+2480=$5,000 annual -> /52=$96.15.
    ("pre2020_w4_allowances_weekly", dict(
        gross_pay=1500.0, pay_frequency="weekly", filing_status="mfj",
        w4_is_2020_or_later=False, pre2020_allowances=3,
    ), {"federal_withholding": 96.15}),

    # A pre-2020 W-4 has NO Head-of-Household option on the actual form --
    # must be REJECTED (None), never silently reinterpreted as Single.
    ("pre2020_w4_hoh_must_be_rejected", dict(
        gross_pay=1500.0, pay_frequency="weekly", filing_status="hoh",
        w4_is_2020_or_later=False,
    ), None),

    # MFJ with 2+ CA regular allowances -- exercises the married_2_plus
    # vs married_0_1 exemption-category split (a HIGHER standard
    # deduction applies once allowances reach 2). Hand-verified: annual=
    # $108,000; federal MFJ standard bracket $44,100-$120,100:
    # (108000-12900-44100)*0.12+2480=$8,600 annual -> /24=$358.33.
    ("mfj_2plus_ca_allowances_semimonthly", dict(
        gross_pay=4500.0, pay_frequency="semimonthly", filing_status="mfj",
        ca_regular_allowances=3,
    ), {"federal_withholding": 358.33, "ca_withholding": 110.28}),

    # Step 4(b) deductions + Step 4(c) extra withholding together. Hand-
    # verified: annual=$104,000; adjusted=104000-5000-8600=$90,400;
    # single_mfs standard bracket $57,900-$113,200 (base $5,800, 22%):
    # (90400-57900)*0.22+5800=$12,950 annual -> /52=$249.04 + $50
    # extra withholding = $299.04.
    ("step4b_and_4c_weekly", dict(
        gross_pay=2000.0, pay_frequency="weekly", filing_status="single",
        step4b_deductions=5000.0, step4c_extra_withholding=50.0,
    ), {"federal_withholding": 299.04}),

    # --- exemption toggles + extra-withholding fields (reference-calculator parity) ---

    # All 5 exemptions active at once -- every component drops to $0 and
    # take-home exactly equals gross pay.
    ("all_five_exemptions_weekly", dict(
        gross_pay=1923.08, pay_frequency="weekly", filing_status="single",
        exempt_federal_income_tax=True, exempt_social_security=True, exempt_medicare=True,
        exempt_state_income_tax=True, exempt_sdi=True,
    ), {"federal_withholding": 0.0, "social_security": 0.0, "medicare": 0.0,
        "additional_medicare": 0.0, "ca_withholding": 0.0, "ca_sdi": 0.0,
        "total_taxes": 0.0, "take_home_pay": 1923.08}),

    # Federal exemption claimed, but a Step 4(c) extra-withholding amount
    # is still stated -- the extra amount is an independent instruction,
    # not part of the formula the exemption bypasses, so it still applies.
    ("federal_exempt_with_step4c_extra_weekly", dict(
        gross_pay=1923.08, pay_frequency="weekly", filing_status="single",
        exempt_federal_income_tax=True, step4c_extra_withholding=50.0,
    ), {"federal_withholding": 50.0}),

    # Same pattern for CA: exemption claimed but an additional-withholding
    # amount is separately stated.
    ("ca_exempt_with_extra_withholding_weekly", dict(
        gross_pay=1923.08, pay_frequency="weekly", filing_status="single",
        exempt_state_income_tax=True, ca_extra_withholding=25.0,
    ), {"ca_withholding": 25.0}),

    # Social Security exempt alone must not affect Medicare (they're
    # independently togglable, mirroring the reference calculator).
    ("social_security_exempt_only_weekly", dict(
        gross_pay=1923.08, pay_frequency="weekly", filing_status="single",
        exempt_social_security=True,
    ), {"social_security": 0.0, "medicare": 27.88}),

    # Medicare exemption zeroes BOTH regular Medicare AND Additional
    # Medicare Tax for a high earner who'd otherwise owe Additional
    # Medicare -- without disturbing Social Security.
    ("medicare_exempt_high_earner_monthly", dict(
        gross_pay=250000.0 / 12, pay_frequency="monthly", filing_status="single",
        exempt_medicare=True,
    ), {"medicare": 0.0, "additional_medicare": 0.0, "social_security": 953.25}),

    # --- moderate tier: NRA withholding, separate CA filing status, multi-line earnings ---

    # Reproduces Pub 15-T's OWN worked example (2026 ed., p.7): $300/week
    # wages, pre-2020 W-4, single, 1 allowance, nonresident alien -> the
    # PUBLICATION's own Wage Bracket Method answer is $31 (a different,
    # manual method from the Percentage Method Worksheet 1A this module
    # implements, so exact-cent agreement isn't guaranteed -- $31.23 is
    # well within the expected gap between the two IRS-sanctioned
    # methods). FICA and CA must be COMPLETELY UNCHANGED by the NRA flag
    # (confirmed: Pub 15-T's own text says the addition doesn't affect
    # Social Security, Medicare, or FUTA liability).
    ("nra_pub15t_worked_example_weekly", dict(
        gross_pay=300.0, pay_frequency="weekly", filing_status="single",
        w4_is_2020_or_later=False, pre2020_allowances=1, is_nonresident_alien=True,
    ), {"federal_withholding": 31.23, "social_security": 18.60, "medicare": 4.35,
        "ca_withholding": 0.0, "ca_sdi": 3.90}),

    # Separate CA filing status genuinely differing from federal (MFJ
    # federal, Single CA) -- exercises ca_filing_status routing to
    # map_ca_categories independently of the federal filing_status.
    # Hand-verified: CA taxable=104000-5706=98294; single_dual_multiple
    # bracket $72,724-$371,479 (base $3,522.17, 10.23%):
    # (98294-72724)*0.1023+3522.17=$6,137.98 annual -> /52=$118.04.
    ("separate_ca_filing_status_weekly", dict(
        gross_pay=2000.0, pay_frequency="weekly", filing_status="mfj", ca_filing_status="single",
    ), {"federal_withholding": 156.15, "ca_withholding": 118.04}),

    # Multiple earning lines (regular + overtime + other) are summed
    # BEFORE any withholding math -- confirmed no special "overtime rate"
    # exists in Pub 15-T/EDD's own withholding formulas, only total wages
    # matter. This case is functionally identical to a single $1,900
    # gross_pay call; it exists to document/pin that equivalence as a
    # regression case for app.py's own summing logic.
    ("summed_earning_lines_weekly", dict(
        gross_pay=1500.0 + 300.0 + 100.0, pay_frequency="weekly", filing_status="single",
    ), {"gross_pay": 1900.0, "federal_withholding": 248.19}),

    # --- large tier: pre-tax benefits + optional YTD wages ---

    # Basic traditional 401(k): reduces federal+CA wages but NOT FICA
    # (IRC 3121(v)(1) -- still FICA wages). Hand-verified: federal wages
    # 4000-500=3500/period, annual=91000, single_mfs standard bracket
    # (adjusted=91000-8600=82400): (82400-57900)*0.22+5800=$11,190
    # annual -> /26=$430.38. SS/Medicare computed on the FULL $4,000
    # (fica_wage_reduction=$0): SS=4000*0.062=$248.00, Medicare=
    # 4000*0.0145=$58.00. CA wages also 3500 (taxable=91000-5706=85294,
    # bracket base $3,522.17+10.23%): (85294-72724)*0.1023+3522.17=
    # $4,808.23 annual -> /26=$184.93.
    ("basic_401k_traditional_biweekly", dict(
        gross_pay=4000.0, pay_frequency="biweekly", filing_status="single",
        pretax_401k_traditional=500.0,
    ), {"federal_withholding": 430.38, "social_security": 248.00, "medicare": 58.00,
        "ca_withholding": 184.93, "federal_wage_reduction_this_period": 500.0,
        "ca_wage_reduction_this_period": 500.0, "fica_wage_reduction_this_period": 0.0}),

    # HSA: reduces federal+FICA wages but NOT California (CA does not
    # conform to the federal HSA exclusion) -- the sharpest test of that
    # divergence. This election also happens to exceed the self-only HSA
    # limit once annualized ($300*24=$7,200 > $4,400), so it exercises
    # capping too: within_limit_this_period=$4,400/24=$183.33 (not the
    # full $300). ca_wage_reduction_this_period must be exactly $0.00.
    ("hsa_ca_divergence_semimonthly", dict(
        gross_pay=3000.0, pay_frequency="semimonthly", filing_status="single",
        pretax_hsa=300.0,
    ), {"federal_withholding": 251.75, "ca_withholding": 123.18,
        "federal_wage_reduction_this_period": 183.33, "ca_wage_reduction_this_period": 0.0,
        "fica_wage_reduction_this_period": 183.33}),

    # 401(k) election exceeding the 2026 annual limit ($24,500) once
    # annualized ($600*52=$31,200) -- the $128.85/period excess must
    # become ordinary taxable wages, never silently exempted past the
    # legal limit nor silently rejected. within_limit_this_period=
    # $24,500/52=$471.15 (not the full $600).
    ("401k_exceeds_annual_limit_weekly", dict(
        gross_pay=1500.0, pay_frequency="weekly", filing_status="single",
        pretax_401k_traditional=600.0,
    ), {"federal_withholding": 81.54, "ca_withholding": 29.67,
        "federal_wage_reduction_this_period": 471.15}),

    # Direct contrast pair with the existing
    # high_earner_crosses_ss_cap_and_additional_medicare case (same
    # gross_pay/frequency/filing_status, smoothed SS=$953.25 there) --
    # here, stating YTD wages exactly at the SS wage base proves REAL
    # marginal capping: Social Security must be exactly $0.00 (the cap was
    # already exhausted before this paycheck), while Additional Medicare
    # still applies on the portion of (ytd + this period) over $200,000:
    # (204,833.33-200,000)-(184,500-200,000 floored at 0)=$4,833.33 (wait:
    # cumulative_after=184500+20833.33=205,333.33; addl=(205,333.33-
    # 200,000)-0=$5,333.33 * 0.9% = $48.00). Medicare itself is never
    # capped either way ($302.08, same as the existing case).
    ("ytd_wages_ss_cap_already_reached_monthly", dict(
        gross_pay=250000.0 / 12, pay_frequency="monthly", filing_status="single",
        ytd_wages_before_this_period=184500.0,
    ), {"social_security": 0.0, "additional_medicare": 48.00, "medicare": 302.08}),

    # Dependent Care FSA's limit is filing-status-derived (no new UI
    # fact): the SAME $500/period biweekly election (annualized $13,000,
    # exceeding BOTH limits) produces a DIFFERENT within-limit amount for
    # MFS ($3,750 limit -> $144.23/period) vs. Single ($7,500 limit ->
    # $288.46/period), proving _resolve_limit_keys correctly reads
    # filing_status alone.
    ("dependent_care_fsa_mfs_limit_biweekly", dict(
        gross_pay=3000.0, pay_frequency="biweekly", filing_status="mfs",
        pretax_dependent_care_fsa=500.0,
    ), {"federal_wage_reduction_this_period": 144.23}),
    ("dependent_care_fsa_single_limit_biweekly", dict(
        gross_pay=3000.0, pay_frequency="biweekly", filing_status="single",
        pretax_dependent_care_fsa=500.0,
    ), {"federal_wage_reduction_this_period": 288.46}),

    # Combined 401(k) + HSA (family) + Dependent Care FSA, all within
    # their limits -- proves the 3 wage-base reductions aggregate
    # correctly across benefit types with DIFFERENT basis subsets
    # (federal: all 3 summed = $650; CA: 401k+DCFSA only = $500, HSA
    # excluded; FICA: HSA+DCFSA only = $350, 401k excluded), and that the
    # FULL raw election total ($650) -- not just the within-limit portion
    # -- is what actually leaves take-home pay.
    ("combined_401k_hsa_dependent_care_biweekly", dict(
        gross_pay=3500.0, pay_frequency="biweekly", filing_status="mfj",
        pretax_401k_traditional=300.0, pretax_hsa=150.0, pretax_hsa_family_coverage=True,
        pretax_dependent_care_fsa=200.0,
    ), {"pretax_benefits_this_period": 650.0, "federal_wage_reduction_this_period": 650.0,
        "ca_wage_reduction_this_period": 500.0, "fica_wage_reduction_this_period": 350.0}),

    # YTD wages combined with a 401(k) election on a high earner --
    # proves 401(k) has ZERO effect on the FICA/YTD math specifically
    # (401(k) doesn't reduce FICA wages): fica taxable wages stay the full
    # $20,000 despite the $2,000 federal/CA reduction, so Social Security
    # is computed on the true running total
    # (160,000+20,000=180,000, both under the $184,500 cap):
    # (180,000-160,000)*0.062=$1,240.00.
    ("ytd_wages_with_401k_reduction_monthly", dict(
        gross_pay=20000.0, pay_frequency="monthly", filing_status="single",
        pretax_401k_traditional=2000.0, ytd_wages_before_this_period=160000.0,
    ), {"social_security": 1240.00}),

    # Regression pin: the ORIGINAL ADP-screenshot baseline case, re-run
    # with every new pre-tax-benefit/YTD parameter passed EXPLICITLY at
    # its default value -- must be byte-identical to the un-parameterized
    # original, proving the new code paths are true no-ops when unused.
    ("all_pretax_defaults_matches_adp_baseline", dict(
        gross_pay=100000.0 / 52, pay_frequency="weekly", filing_status="single",
        pretax_401k_traditional=0.0, pretax_401k_age_50_or_older=False, pretax_hsa=0.0,
        pretax_hsa_family_coverage=False, pretax_hsa_age_55_or_older=False,
        pretax_health_fsa=0.0, pretax_dependent_care_fsa=0.0, pretax_medical=0.0,
        pretax_dental=0.0, pretax_vision=0.0, ytd_wages_before_this_period=None,
    ), {"federal_withholding": 253.27, "social_security": 119.23, "medicare": 27.88,
        "ca_withholding": 110.17, "ca_sdi": 25.00, "take_home_pay": 1387.53}),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def _check(result, expected):
    if expected is None:
        return result is None, "expected None (rejected input)"
    if result is None:
        return False, "compute_paycheck returned None unexpectedly"
    for k, want in expected.items():
        got = result.get(k)
        if k in DOLLAR_KEYS:
            if got is None or abs(float(got) - want) > TOL:
                return False, k
        elif got != want:
            return False, k
    return True, None


def run():
    cache = _load()
    conn = income_db.get_conn()
    graded = 0
    for label, kwargs, exp in ITEMS:
        if label in cache:
            continue
        try:
            r = pw.compute_paycheck(conn, **kwargs)
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:120]}")
            break
        ok, fail_key = _check(r, exp)
        cache[label] = {"expected": exp, "ok": ok, "fail_key": fail_key,
                         "result": ({k: r.get(k) for k in DOLLAR_KEYS} if r else None)}
        graded += 1
        print(f"  [{'OK ' if ok else 'BAD'}] {label:45} -> {cache[label]['result']}")
        _save(cache)
    conn.close()
    print(f"\ngraded {graded} new; cached {len(cache)}/{len(ITEMS)}")
    report()


def report():
    cache = _load()
    if not cache:
        print("nothing graded yet")
        return
    ok = [i for i in cache.items() if i[1]["ok"]]
    bad = [i for i in cache.items() if not i[1]["ok"]]
    print(f"\n===== PAYROLL SWEEP ({len(cache)} items) =====")
    print(f"correct : {len(ok)}")
    print(f"WRONG   : {len(bad)}")
    for label, v in bad:
        print(f"  {label}: expected={v['expected']} got={v['result']} (mismatch on: {v['fail_key']})")


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
