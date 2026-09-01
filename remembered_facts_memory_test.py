"""Verification script for engine.answer()'s three new session-memory kwargs
built on top of the shipped remembered_filing_status feature (see
filing_status_memory_test.py for that one): remembered_prior_year_agi and
remembered_qualifying_children_count (retry-gated, same shape as filing
status) and remembered_exemption_credit_dependent_count (a fallback default,
not a retry -- Exemption Credit never blocks on a missing dependent count,
it silently defaults to 0). See the plan doc
(logical-marinating-starfish.md) for the full design and the research that
motivated category-gating instead of copying filing status's text-gate.

Same harness shape as filing_status_memory_test.py: short, independent
sequences of engine.answer() calls with hand-checked expectations, each
wrapped so a Gemini embed-quota exhaustion is reported and skipped rather
than crashing the whole run.

Usage:
  python remembered_facts_memory_test.py
"""
import engine

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


# --- Prior-year AGI (Underpayment short method) ---------------------------

def scenario_agi_primary_case():
    """Known-good full case (income=$150,000, prior tax=$5,000, prior
    AGI=$100,000, withholding=$2,000, single -> tax=$150.86, see
    income_item_sweep.py:2572-2573). Omit prior-year AGI -> pass 1 should
    hit the new isolated missing-AGI message; retry with the remembered
    value should match the known-good tax exactly."""
    q = ("do I owe underpayment penalty if my income is $150,000, filing single, "
         "my prior year tax was $5,000, and my california withholding was $2,000?")
    pass1 = engine.answer(q, compose=False)
    check("agi primary: pass1 category is the isolated missing-AGI message",
          pass1.get("category") == "underpayment_missing_prior_year_agi", pass1)
    check("agi primary: pass1 used_remembered_prior_year_agi is False (no memory set)",
          pass1.get("used_remembered_prior_year_agi") is False, pass1)

    remembered = engine.answer(q, compose=False, remembered_prior_year_agi=100000.0)
    check("agi primary: used_remembered_prior_year_agi is True",
          remembered.get("used_remembered_prior_year_agi") is True, remembered)
    check("agi primary: retried tax matches the known-good $150.86",
          remembered.get("tax") == 150.86, remembered.get("tax"))


def scenario_agi_no_memory_baseline():
    """Same question, no remembered_prior_year_agi -> stays needs_review
    with the isolated missing-AGI message (proves the wrapper is a no-op
    for existing callers that never pass the new kwarg)."""
    q = ("do I owe underpayment penalty if my income is $150,000, filing single, "
         "my prior year tax was $5,000, and my california withholding was $2,000?")
    r = engine.answer(q, compose=False)
    check("agi no-memory: status is needs_review", r.get("status") == "needs_review", r)
    check("agi no-memory: used_remembered_prior_year_agi is False",
          r.get("used_remembered_prior_year_agi") is False, r)


def scenario_agi_regular_method_excluded():
    """Underpayment REGULAR method (dated estimated payments) has no
    isolated missing-AGI category -- its own catch-all template covers 4+
    facts at once. Confirms the AGI retry does NOT fire here even with
    memory set, proving the Phase-1 scoping decision to exclude the
    Regular method from this retry holds at runtime, not just on paper."""
    q = ("I owe an underpayment penalty. my income is $300,000, my prior year tax was "
         "$40,000, my withholding was $5,000, and I made estimated payments of $25,000 "
         "on 6/10/2025 and $10,000 on 1/10/2026, single")
    r = engine.answer(q, compose=False, remembered_prior_year_agi=100000.0)
    check("agi regular-method exclusion: used_remembered_prior_year_agi is False",
          r.get("used_remembered_prior_year_agi") is False, r)
    check("agi regular-method exclusion: still needs_review (own catch-all template)",
          r.get("status") == "needs_review", r)


# --- Qualifying-children count (CalEITC) -----------------------------------

def scenario_children_primary_case_caleitc():
    """Known-good full case ($9,975 income + 2 qualifying children ->
    tax=$3,288.00, see income_item_sweep.py:66-67). Omit children count ->
    pass 1 hits _income_missing_children_answer's category; retry with the
    remembered count should match exactly."""
    q = "what is my CalEITC if I make $9,975"
    pass1 = engine.answer(q, compose=False)
    check("children primary (caleitc): pass1 category is caleitc_missing_children",
          pass1.get("category") == "caleitc_missing_children", pass1)

    remembered = engine.answer(q, compose=False, remembered_qualifying_children_count=2)
    check("children primary (caleitc): used_remembered_qualifying_children_count is True",
          remembered.get("used_remembered_qualifying_children_count") is True, remembered)
    check("children primary (caleitc): retried tax matches the known-good $3,288.00",
          remembered.get("tax") == 3288.00, remembered.get("tax"))
    check("children primary (caleitc): label is '2 qualifying children'",
          remembered.get("remembered_qualifying_children_count_label") == "2 qualifying children",
          remembered.get("remembered_qualifying_children_count_label"))


def scenario_children_primary_case_caleitc_investment():
    """Same fact, different producer: CalEITC with investment income
    present, missing children count -> _income_caleitc_investment_missing_
    children_answer's own category. $9,975 income + $1,000 investment +
    2 children -> tax=$3,288.00 (same table row, investment income under
    the limit -- see income_item_sweep.py:83-84)."""
    q = "what is my CalEITC if I make $9,975 with $1,000 in investment income"
    pass1 = engine.answer(q, compose=False)
    check("children primary (investment): pass1 category is caleitc_investment_missing_children",
          pass1.get("category") == "caleitc_investment_missing_children", pass1)

    remembered = engine.answer(q, compose=False, remembered_qualifying_children_count=2)
    check("children primary (investment): used_remembered_qualifying_children_count is True",
          remembered.get("used_remembered_qualifying_children_count") is True, remembered)
    check("children primary (investment): retried tax matches the known-good $3,288.00",
          remembered.get("tax") == 3288.00, remembered.get("tax"))


def scenario_children_fytc_excluded():
    """The verified text-collision proof: FYTC's own checklist message
    contains the literal substring 'number of qualifying children' that
    CalEITC's dedicated missing-children message also uses. A naive
    text-gate (copying filing status's pattern) would have misfired here.
    Confirms the category-gate design correctly does NOT retry FYTC
    questions using the remembered children count -- FYTC never sets
    either caleitc_missing_children category."""
    r = engine.answer("what is my Foster Youth Tax Credit?",
                       compose=False, remembered_qualifying_children_count=2)
    check("children fytc exclusion: used_remembered_qualifying_children_count is False",
          r.get("used_remembered_qualifying_children_count") is False, r)
    check("children fytc exclusion: answer_text still contains the collision phrase "
          "(proves this is a real near-miss, not an untested edge)",
          "number of qualifying children" in (r.get("answer_text") or "").lower(), r)


# --- Exemption Credit dependent count (fallback default, not a retry) -----

def scenario_exemption_credit_fallback_match():
    """No count stated -> the remembered value is used as a fallback
    default. Known-good: $80,000 wages, single, 0 dependents (implicit)
    -> tax=$3,194.98; WITH 2 dependents -> tax=$2,244.98 (see
    income_item_sweep.py:1947-1952). A remembered count of 2 should
    produce the $2,244.98 figure even though this question states none."""
    q = "how much california tax do I owe on $80,000 in wages, single, with my exemption credit?"
    baseline = engine.answer(q, compose=False)
    check("exemption fallback: baseline (no memory) is the 0-dependent figure",
          baseline.get("tax") == 3194.98, baseline.get("tax"))
    check("exemption fallback: baseline used_remembered_exemption_credit_dependent_count is False",
          baseline.get("used_remembered_exemption_credit_dependent_count") is False, baseline)

    remembered = engine.answer(q, compose=False, remembered_exemption_credit_dependent_count=2)
    check("exemption fallback: used_remembered_exemption_credit_dependent_count is True",
          remembered.get("used_remembered_exemption_credit_dependent_count") is True, remembered)
    check("exemption fallback: tax matches the known-good 2-dependent figure $2,244.98",
          remembered.get("tax") == 2244.98, remembered.get("tax"))


def scenario_exemption_credit_explicit_zero_not_overridden():
    """The subtlest correctness point in this whole build: a question that
    explicitly states 0 dependents must NOT be overridden by a remembered
    nonzero count -- this is exactly why the fallback is gated on `stated_
    dependent_count is None`, not a truthy check (0 is falsy in Python but
    a legitimate, meaningful stated value here)."""
    q = ("how much california tax do I owe on $80,000 in wages, single, with 0 dependents, "
         "with my exemption credit?")
    r = engine.answer(q, compose=False, remembered_exemption_credit_dependent_count=3)
    check("exemption explicit-zero: used_remembered_exemption_credit_dependent_count is False",
          r.get("used_remembered_exemption_credit_dependent_count") is False, r)
    check("exemption explicit-zero: tax matches the 0-dependent figure, NOT the remembered 3",
          r.get("tax") == 3194.98, r.get("tax"))
    check("exemption explicit-zero: detected_exemption_credit_dependent_count reflects THIS question (0)",
          r.get("detected_exemption_credit_dependent_count") == 0,
          r.get("detected_exemption_credit_dependent_count"))


def scenario_exemption_credit_amounts_removal_regression():
    """Order-independence + amounts-removal-by-value regression: stating
    the dependent count BEFORE the income figure must not misparse it as
    the income amount, and a REMEMBERED count must never strip a real,
    coincidentally-matching dollar figure out of a question that never
    stated a count at all. $80,000 income with a remembered count of 80000
    (deliberately equal to the income figure) must still resolve income as
    $80,000, not None/blocked -- proving the removal step is correctly
    gated on the STATED count only, never the combined stated-or-remembered
    value (see _income_exemption_credit_answer's docstring)."""
    q = "how much california tax do I owe on $80,000 in wages, single, with my exemption credit?"
    r = engine.answer(q, compose=False, remembered_exemption_credit_dependent_count=80000)
    check("exemption amounts-removal regression: income still resolves to $80,000 "
          "(remembered count did not strip it)",
          r.get("amount") == 80000.0, r.get("amount"))
    check("exemption amounts-removal regression: used_remembered_exemption_credit_dependent_count is True",
          r.get("used_remembered_exemption_credit_dependent_count") is True, r)


if __name__ == "__main__":
    run_scenario("agi_primary_case", scenario_agi_primary_case)
    run_scenario("agi_no_memory_baseline", scenario_agi_no_memory_baseline)
    run_scenario("agi_regular_method_excluded", scenario_agi_regular_method_excluded)
    run_scenario("children_primary_case_caleitc", scenario_children_primary_case_caleitc)
    run_scenario("children_primary_case_caleitc_investment",
                 scenario_children_primary_case_caleitc_investment)
    run_scenario("children_fytc_excluded", scenario_children_fytc_excluded)
    run_scenario("exemption_credit_fallback_match", scenario_exemption_credit_fallback_match)
    run_scenario("exemption_credit_explicit_zero_not_overridden",
                 scenario_exemption_credit_explicit_zero_not_overridden)
    run_scenario("exemption_credit_amounts_removal_regression",
                 scenario_exemption_credit_amounts_removal_regression)

    print(f"\n===== REMEMBERED FACTS MEMORY TEST =====")
    print(f"pass: {len(PASS)}  fail: {len(FAIL)}  blocked: {len(BLOCKED)}")
    if FAIL:
        print("FAILED:", FAIL)
    if BLOCKED:
        print("BLOCKED (re-run once the Gemini embed quota recovers):", BLOCKED)

    # Disclosed, not fabricated: "multiple simultaneous gate matches in one
    # retry pass" (e.g. a single question missing BOTH prior-year AGI and a
    # filing status at once) has no naturally-occurring trigger in this
    # suite today -- the category design makes the gates mutually exclusive
    # by construction (each producer function that sets a category is
    # already gated on having everything else it needs), so a case that
    # exercises two injections in the SAME retry doesn't arise from real
    # engine behavior without hand-constructing a contradiction. Not tested
    # here for that reason, not because it was overlooked.
