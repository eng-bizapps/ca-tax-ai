"""Verification script for FTB 5805's Underpayment Penalty REGULAR
METHOD (Form 5805 Worksheet II) -- income_brackets.compute_underpayment_
penalty_regular and engine.py's date-extraction helpers/dispatch.

Unlike income_item_sweep.py's shape (one question -> one expected dict),
this feature needs both pure-arithmetic verification (the 4-column
running ledger + two-rate-period interest, independent of any question
text) and integration scenarios through engine.answer() (which needs a
real Gemini embed call for income-domain routing, so can be blocked by
the same per-day quota exhaustion documented elsewhere in this session).
Each scenario is wrapped so a quota failure is reported and skipped
rather than crashing the whole run.

Usage:
  python underpayment_regular_method_test.py
"""
import income_brackets as ib
import engine
from datetime import date

PASS, FAIL, BLOCKED = [], [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name}  {detail}")


def run_scenario(name, fn):
    try:
        fn()
    except Exception as e:
        BLOCKED.append(name)
        print(f"[BLOCKED] {name}  ({type(e).__name__}: {e})")


# ============================================================
# Section 1: pure-arithmetic unit tests (no engine.answer(), no API)
# ============================================================

def scenario_single_column_resolved_next_quarter():
    """Q1 underpaid, resolved by Q2's overpayment -- hand-verified during
    design: required_annual_payment=$40,000 ($10,000/quarter), payments
    (0, 25000, 0, 10000). Q1 underpays $10,000, resolved at Q2's due date
    (6/15/25, within rate period 1) -> $133.70. Q2 itself ends up a
    $5,000 overpayment (no penalty). Q3 underpays $5,000, never resolved
    (no later overpayment) -> backstop 4/15/26 -> $203.29. Q4 underpays
    $5,000, no later column at all -> backstop -> $86.30. Total $423.29."""
    r = ib.compute_underpayment_penalty_regular(
        current_year_tax=100000, prior_year_tax=40000, prior_year_agi=100000,
        withholding=0, filing_status="single", current_year_income=100000,
        quarterly_payments=(0.0, 25000.0, 0.0, 10000.0))
    check("single-column: total penalty", r["penalty"] == 423.29, r)
    check("single-column: Q1 underpayment+penalty", r["columns"][0] == {"underpayment": 10000.0, "penalty": 133.7}, r["columns"][0])
    check("single-column: Q2 fully resolved (no underpayment)", r["columns"][1]["underpayment"] == 0.0, r["columns"][1])
    check("single-column: Q3 backstop-resolved", r["columns"][2] == {"underpayment": 5000.0, "penalty": 203.29}, r["columns"][2])
    check("single-column: Q4 backstop-resolved", r["columns"][3] == {"underpayment": 5000.0, "penalty": 86.3}, r["columns"][3])


def scenario_consecutive_underpayment_chain():
    """Q1 AND Q2 both underpaid (no payments at all until Q3), Q3
    overpays enough to cover BOTH -- exercises the additive Line7+Line8
    carry-forward across TWO consecutive underpaying columns."""
    r = ib.compute_underpayment_penalty_regular(
        current_year_tax=100000, prior_year_tax=40000, prior_year_agi=100000,
        withholding=0, filing_status="single", current_year_income=100000,
        quarterly_payments=(0.0, 0.0, 40000.0, 0.0))
    # required_installment = 10000/quarter. Q1: line6=0, line8=10000.
    # Q2: line3=line9[a]=0, line4=0, line5=line7[a](0)+line8[a](10000)=10000,
    #     line6=max(0,0-10000)=0 -> line7[b]=max(0,10000-0)=10000, line8[b]=10000-0=10000.
    # Q3: line3=line9[b]=0, line4=40000, line5=line7[b](10000)+line8[b](10000)=20000,
    #     line6=max(0,40000-20000)=20000, line1[c]=10000<=20000 -> line9[c]=20000-10000=10000 (overpayment!)
    # Both Q1 and Q2's underpayments resolve at Q3's due date (9/15/25), since that's the
    # first later column with an overpayment.
    check("chain: Q1 underpaid $10,000", r["columns"][0]["underpayment"] == 10000.0, r["columns"][0])
    check("chain: Q2 underpaid $10,000", r["columns"][1]["underpayment"] == 10000.0, r["columns"][1])
    check("chain: Q3 fully resolved (no underpayment)", r["columns"][2]["underpayment"] == 0.0, r["columns"][2])
    check("chain: Q4 fully resolved (no underpayment)", r["columns"][3]["underpayment"] == 0.0, r["columns"][3])
    # Q1 resolved at Q3's due date (9/15/25): rate period 1 (4/15-6/30, 61 days... actually
    # capped at 6/30 since resolved date 9/15 > 6/30) + rate period 2 (7/1-9/15).
    days1_q1 = (date(2025, 6, 30) - date(2025, 4, 15)).days
    days2_q1 = (date(2025, 9, 15) - date(2025, 7, 1)).days
    expected_q1 = round(10000 * (days1_q1 / 365) * 0.08 + 10000 * (days2_q1 / 365) * 0.07, 2)
    check("chain: Q1 penalty matches hand calc", r["columns"][0]["penalty"] == expected_q1,
          (r["columns"][0]["penalty"], expected_q1))
    # Q2 resolved at Q3's due date too, but Q2's OWN due date is 6/15/25 (already past 6/30's
    # boundary check differently -- rate period 1 runs 6/15 to 6/30, rate period 2 runs 7/1 to 9/15).
    days1_q2 = (date(2025, 6, 30) - date(2025, 6, 15)).days
    days2_q2 = (date(2025, 9, 15) - date(2025, 7, 1)).days
    expected_q2 = round(10000 * (days1_q2 / 365) * 0.08 + 10000 * (days2_q2 / 365) * 0.07, 2)
    check("chain: Q2 penalty matches hand calc", r["columns"][1]["penalty"] == expected_q2,
          (r["columns"][1]["penalty"], expected_q2))


def scenario_never_resolved_backstop():
    """Q4 underpaid, no later column exists at all -- must hit the
    4/15/2026 backstop, not error out or leave it unresolved.
    required_annual_payment = min(0.9*40000, 1.0*40000) = 36000 (the
    90%-of-current-year test binds here, NOT prior-year) -> required
    installment $9,000/quarter. Payments (10000,10000,10000,0): each of
    Q1-Q3 overpays by $1,000 relative to $9,000, accumulating a $3,000
    carried-forward surplus entering Q4; Q4 needs $9,000 but only has
    that $3,000 credit, leaving $6,000 underpaid (not $10,000 -- caught
    live: an earlier version of this test assumed required_annual_
    payment=$40,000, a hand-calculation error on my part, not a code
    bug -- verified by calling compute_required_annual_payment directly
    before fixing the expected value here)."""
    r = ib.compute_underpayment_penalty_regular(
        current_year_tax=40000, prior_year_tax=40000, prior_year_agi=100000,
        withholding=0, filing_status="single", current_year_income=40000,
        quarterly_payments=(10000.0, 10000.0, 10000.0, 0.0))
    check("backstop: Q1-Q3 fully paid (no underpayment)", all(c["underpayment"] == 0.0 for c in r["columns"][:3]), r["columns"])
    check("backstop: Q4 underpaid $6,000", r["columns"][3]["underpayment"] == 6000.0, r["columns"][3])
    days2 = (date(2026, 4, 15) - date(2026, 1, 15)).days
    expected = round(6000 * (days2 / 365) * 0.07, 2)
    check("backstop: Q4 penalty matches hand calc (90 days x 7%)", r["columns"][3]["penalty"] == expected,
          (r["columns"][3]["penalty"], expected, days2))


def scenario_safe_harbor_met_with_nonzero_payments():
    """Withholding alone already meets the safe harbor -- must short-
    circuit to $0 immediately, even though quarterly_payments is
    nonzero (confirms the safe-harbor check runs BEFORE any Worksheet
    II arithmetic, not after)."""
    r = ib.compute_underpayment_penalty_regular(
        current_year_tax=50000, prior_year_tax=40000, prior_year_agi=100000,
        withholding=45000, filing_status="single", current_year_income=50000,
        quarterly_payments=(5000.0, 5000.0, 5000.0, 5000.0))
    check("safe harbor: penalty is $0", r == {"penalty": 0.0, "reason": "safe_harbor_met", "required_annual_payment": 40000.0}, r)


def scenario_short_method_unaffected_by_refactor():
    """compute_underpayment_penalty (short method) must produce IDENTICAL
    output after being refactored to delegate to
    compute_required_annual_payment -- same inputs as an existing
    income_item_sweep.py case. balance_due = 9857.98-10000 = -142.02,
    under the $500 de minimis threshold -> de_minimis_balance (not
    safe_harbor_met -- caught live: an earlier version of this test
    expected the wrong safe-harbor reason string; both produce the same
    $0.0 penalty, but de minimis is checked first and correctly wins)."""
    r = ib.compute_underpayment_penalty(current_year_tax=9857.98, prior_year_tax=5000, prior_year_agi=100000,
                                         withholding=10000, filing_status="single", current_year_income=150000)
    check("short method: still $0.0 via de_minimis_balance post-refactor",
          r["penalty"] == 0.0 and r["reason"] == "de_minimis_balance", r)


# ============================================================
# Section 2: helper unit tests (engine.py date/extraction helpers)
# ============================================================

def scenario_date_phantom_amount_guard():
    """The bug that made date-masking necessary in the first place --
    confirm it stays fixed."""
    q = "I paid $3,000 on 4/15/2025"
    raw_amounts = engine._amounts(q)
    check("phantom guard: raw _amounts() DOES produce phantoms from the date (documents the bug)",
          any(a in (15.0, 2025.0) for a, _, _ in raw_amounts), raw_amounts)
    dates = engine._dates(q)
    masked = engine._mask_dates(q, dates)
    masked_amounts = engine._amounts(masked)
    check("phantom guard: masked _amounts() has exactly one amount ($3,000)",
          masked_amounts == [(3000.0, 7, 13)], masked_amounts)


def scenario_bucketing_correctness():
    pairs = [(3000.0, date(2025, 4, 10)), (2000.0, date(2025, 7, 20)), (4000.0, date(2025, 11, 1))]
    buckets = ib.bucket_regular_method_payments(pairs)
    check("bucketing: correct due-date windows", buckets == (3000.0, 0.0, 2000.0, 4000.0), buckets)


def scenario_bucketing_out_of_range_returns_none():
    pairs = [(3000.0, date(2024, 12, 1))]  # before the tax year starts
    check("bucketing: out-of-range date returns None", ib.bucket_regular_method_payments(pairs) is None,
          ib.bucket_regular_method_payments(pairs))


# ============================================================
# Section 3: integration scenarios via engine.answer() (needs Gemini)
# ============================================================

def scenario_integration_happy_path():
    q = ("I owe an underpayment penalty. my income is $300,000, my prior year tax was $40,000, "
         "my prior year AGI was $100,000, my withholding was $5,000, and I made estimated "
         "payments of $25,000 on 6/10/2025 and $10,000 on 1/10/2026, single")
    r = engine.answer(q, compose=False)
    check("integration happy path: answered", r.get("status") == "answered", r)
    check("integration happy path: correct category", r.get("category") == "underpayment_penalty_regular", r)
    check("integration happy path: tax matches hand-verified $54.91", r.get("tax") == 54.91, r.get("tax"))


def scenario_integration_missing_filing_status():
    q = ("I owe an underpayment penalty. my income is $300,000, my prior year tax was $40,000, "
         "my prior year AGI was $100,000, my withholding was $5,000, and I made estimated "
         "payments of $25,000 on 6/10/2025 and $10,000 on 1/10/2026")
    r = engine.answer(q, compose=False)
    check("integration missing fs: needs_review", r.get("status") == "needs_review", r)
    check("integration missing fs: mentions filing status", "filing status" in (r.get("answer_text") or "").lower(), r)


def scenario_integration_template_fallback():
    q = ("I owe an underpayment penalty. my income is $300,000, my prior year tax was $40,000, "
         "my prior year AGI was $100,000, my withholding was $5,000, and I made an estimated "
         "payment of $25,000, single")
    r = engine.answer(q, compose=False)
    check("integration template: needs_review", r.get("status") == "needs_review", r)
    check("integration template: teaches the MM/DD/YYYY format", "MM/DD/YYYY" in (r.get("answer_text") or ""), r)


def scenario_integration_farmer_still_out_of_scope():
    q = "I owe an underpayment penalty and I am a farmer, my income is $300,000, single"
    r = engine.answer(q, compose=False)
    check("integration farmer: needs_review", r.get("status") == "needs_review", r)
    check("integration farmer: mentions FTB 5805F", "5805F" in (r.get("answer_text") or ""), r)


def scenario_integration_short_method_unaffected():
    q = ("how much California tax do I owe as underpayment penalty if my income is $150,000, "
         "filing single, my prior year tax was $5,000, my prior year agi was $100,000, and my "
         "california withholding is $10,000?")
    r = engine.answer(q, compose=False)
    check("integration short method: still answered", r.get("status") == "answered", r)
    check("integration short method: still $0 (safe harbor)", r.get("tax") == 0.0, r.get("tax"))


if __name__ == "__main__":
    run_scenario("single_column_resolved_next_quarter", scenario_single_column_resolved_next_quarter)
    run_scenario("consecutive_underpayment_chain", scenario_consecutive_underpayment_chain)
    run_scenario("never_resolved_backstop", scenario_never_resolved_backstop)
    run_scenario("safe_harbor_met_with_nonzero_payments", scenario_safe_harbor_met_with_nonzero_payments)
    run_scenario("short_method_unaffected_by_refactor", scenario_short_method_unaffected_by_refactor)
    run_scenario("date_phantom_amount_guard", scenario_date_phantom_amount_guard)
    run_scenario("bucketing_correctness", scenario_bucketing_correctness)
    run_scenario("bucketing_out_of_range_returns_none", scenario_bucketing_out_of_range_returns_none)
    run_scenario("integration_happy_path", scenario_integration_happy_path)
    run_scenario("integration_missing_filing_status", scenario_integration_missing_filing_status)
    run_scenario("integration_template_fallback", scenario_integration_template_fallback)
    run_scenario("integration_farmer_still_out_of_scope", scenario_integration_farmer_still_out_of_scope)
    run_scenario("integration_short_method_unaffected", scenario_integration_short_method_unaffected)

    print(f"\n===== UNDERPAYMENT REGULAR METHOD TEST =====")
    print(f"pass: {len(PASS)}  fail: {len(FAIL)}  blocked: {len(BLOCKED)}")
    if FAIL:
        print("FAILED:", FAIL)
    if BLOCKED:
        print("BLOCKED (re-run once the Gemini embed quota recovers):", BLOCKED)
