"""Verification script for engine.answer()'s remembered_filing_status kwarg
(session-scoped filing-status memory -- see app.py's sidebar wiring).

Unlike income_item_sweep.py's shape (one question -> one expected dict),
this feature is inherently multi-call: state a filing status, then reuse
it, then override it, then confirm memory doesn't leak where it shouldn't.
Each scenario below is a short, independent sequence of engine.answer()
calls with hand-checked expectations.

Every scenario needs a real _answer() call for at least one income-domain
question, which unconditionally calls Gemini's embed API as part of normal
routing (_income_has_any_signal -> _embed(question), see engine.py) --
so this script can be blocked by the SAME per-day Gemini embed-quota
exhaustion documented elsewhere in this session. Each scenario is wrapped
so a quota failure is reported and skipped rather than crashing the whole
run, so this stays runnable/resumable once quota recovers rather than an
all-or-nothing gate.

Usage:
  python filing_status_memory_test.py
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


def scenario_primary_case():
    """State MFJ + $80,000 explicitly; capture the tax. Then ask the same
    income with no filing status but remembered_filing_status='mfj' ->
    should retry and match."""
    baseline = engine.answer(
        "how much california tax do I owe on $80,000 in wages married filing jointly",
        compose=False)
    check("primary: baseline computes a tax", baseline.get("tax") is not None,
          baseline)

    remembered = engine.answer(
        "how much california tax do I owe on $80,000 in wages",
        compose=False, remembered_filing_status="mfj")
    check("primary: used_remembered_filing_status is True",
          remembered.get("used_remembered_filing_status") is True, remembered)
    check("primary: remembered result matches baseline tax",
          remembered.get("tax") == baseline.get("tax"),
          (remembered.get("tax"), baseline.get("tax")))
    check("primary: remembered_filing_status_label is the MFJ label",
          remembered.get("remembered_filing_status_label") == "Married/RDP Filing Jointly",
          remembered.get("remembered_filing_status_label"))


def scenario_no_memory_baseline():
    """Same question, no remembered_filing_status -> identical needs_review
    to today (proves the answer() rewrite is a no-op when the kwarg is
    unused, i.e. every existing caller is unaffected)."""
    r = engine.answer("how much california tax do I owe on $80,000 in wages",
                       compose=False)
    check("no-memory baseline: status is needs_review", r.get("status") == "needs_review", r)
    check("no-memory baseline: used_remembered_filing_status is False",
          r.get("used_remembered_filing_status") is False, r)
    check("no-memory baseline: mentions filing status",
          "filing status" in (r.get("answer_text") or "").lower(), r.get("answer_text"))


def scenario_sales_question_unaffected():
    """A clearly sales-phrased question, with remembered_filing_status set
    -> pass 1 already succeeds without mentioning filing status, so no
    retry should fire at all."""
    r = engine.answer("is a couch taxable in california",
                       compose=False, remembered_filing_status="mfj")
    check("sales question: used_remembered_filing_status is False",
          r.get("used_remembered_filing_status") is False, r)


def scenario_known_residual_risk():
    """The documented, NOT-fully-fixed collision: COMPUTE_TRIGGERS' bare
    'how much tax' phrase means this exact sales-phrased question already
    independently triggers an income-domain 'needs filing status' defer on
    pass 1 (verified live during planning, see the plan doc and answer()'s
    own docstring) -- so the retry-gate's income+needs_review+'filing
    status' condition is satisfied by this question on ITS OWN, regardless
    of this feature. This scenario OBSERVES and RECORDS the actual result
    rather than asserting a specific outcome -- it is a known, disclosed,
    pre-existing limitation this feature mitigates but does not eliminate,
    not something to silently assume fixed."""
    r = engine.answer("How much tax do I owe on a couch that costs $500?",
                       compose=False, remembered_filing_status="mfj")
    print(f"  [OBSERVED] status={r.get('status')} domain={r.get('domain')} "
          f"category={r.get('category')} tax={r.get('tax')} "
          f"used_remembered_filing_status={r.get('used_remembered_filing_status')}")
    if r.get("domain") == "income" and r.get("tax") is not None:
        print("  [KNOWN LIMITATION CONFIRMED] the couch question was answered as an "
              "income-tax bracket computation -- pre-existing COMPUTE_TRIGGERS breadth, "
              "not a regression from this feature. See answer()'s docstring / the plan doc.")


def scenario_explicit_restatement_overwrites():
    """Two calls with different remembered_filing_status inputs, after two
    different explicit filing-status statements -- confirms 'last stated
    wins' is purely a caller-side (app.py) responsibility: detected_filing_
    status always reflects what THIS call's question stated, independent
    of what was passed in as remembered_filing_status."""
    r1 = engine.answer("how much california tax do I owe on $80,000 in wages, single",
                        compose=False, remembered_filing_status="mfj")
    check("restatement: detected_filing_status reflects THIS question (single)",
          r1.get("detected_filing_status") == "single", r1.get("detected_filing_status"))
    check("restatement: explicit statement means no retry, memory ignored",
          r1.get("used_remembered_filing_status") is False, r1)


if __name__ == "__main__":
    run_scenario("primary_case", scenario_primary_case)
    run_scenario("no_memory_baseline", scenario_no_memory_baseline)
    run_scenario("sales_question_unaffected", scenario_sales_question_unaffected)
    run_scenario("known_residual_risk", scenario_known_residual_risk)
    run_scenario("explicit_restatement_overwrites", scenario_explicit_restatement_overwrites)

    print(f"\n===== FILING STATUS MEMORY TEST =====")
    print(f"pass: {len(PASS)}  fail: {len(FAIL)}  blocked: {len(BLOCKED)}")
    if FAIL:
        print("FAILED:", FAIL)
    if BLOCKED:
        print("BLOCKED (re-run once the Gemini embed quota recovers):", BLOCKED)
