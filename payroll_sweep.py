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
DOLLAR_KEYS = {"federal_withholding", "social_security", "medicare", "additional_medicare",
               "ca_withholding", "ca_sdi", "total_taxes", "take_home_pay"}

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
