"""Illinois per-paycheck estimate: federal income-tax withholding (IRS Pub 15-T) and
FICA from the verified federal payroll module, plus Illinois withholding from the
IDOR 2026 automated payroll method (il_income.compute_il_withholding).

Scope, disclosed in every result: no pre-tax benefit elections for Illinois (the
Illinois treatment of each benefit is not loaded), weekly/biweekly/semimonthly/
monthly pay only (the frequencies IDOR's booklet defines), no local taxes.
Illinois has no state disability insurance to withhold.
"""
import il_income as ii
import payroll_withholding as pw

IL_PAY_FREQUENCIES = ("weekly", "biweekly", "semimonthly", "monthly")


def compute_il_paycheck(il_conn, income_conn, gross_pay, pay_frequency, filing_status, w4_is_2020_or_later=True,
                        step2_checkbox=False, step3_dependent_credits=0.0, step4a_other_income=0.0,
                        step4b_deductions=0.0, step4c_extra_withholding=0.0, pre2020_allowances=0,
                        il_basic_allowances=0, il_additional_allowances=0, il_extra_withholding=0.0,
                        exempt_il=False, ytd_wages_before_this_period=None):
    """Returns None (never a guessed number) for a missing/non-positive gross pay, an unsupported pay frequency, or a
    failed federal component."""
    if gross_pay is None or gross_pay <= 0 or pay_frequency not in IL_PAY_FREQUENCIES:
        return None
    periods = pw.PAY_PERIODS_PER_YEAR[pay_frequency]
    federal = pw.compute_federal_withholding(
        income_conn, gross_pay, periods, filing_status, w4_is_2020_or_later=w4_is_2020_or_later,
        step2_checkbox=step2_checkbox, step3_dependent_credits=step3_dependent_credits,
        step4a_other_income=step4a_other_income, step4b_deductions=step4b_deductions,
        step4c_extra_withholding=step4c_extra_withholding, pre2020_allowances=pre2020_allowances)
    if federal is None:
        return None
    fica = pw.compute_fica(gross_pay, periods, ytd_wages_before_this_period=ytd_wages_before_this_period)
    if fica is None:
        return None
    il = ii.compute_il_withholding(il_conn, gross_pay, pay_frequency, il_basic_allowances, il_additional_allowances,
                                   il_extra_withholding, exempt_il)
    if il is None:
        return None
    fed_w = federal["withholding_this_period"]
    il_w = float(il["withholding"])
    total = round(fed_w + fica["social_security"] + fica["medicare"] + fica["additional_medicare"] + il_w, 2)
    return {
        "gross_pay": round(gross_pay, 2), "pay_frequency": pay_frequency, "pay_periods_per_year": periods,
        "federal_withholding": fed_w, "social_security": fica["social_security"], "medicare": fica["medicare"],
        "additional_medicare": fica["additional_medicare"], "il_withholding": il_w,
        "il_exemption_this_period": float(il["exemption_this_period"]), "total_taxes": total,
        "take_home_pay": round(gross_pay - total, 2),
        "assumptions": [
            "Illinois withholding uses IDOR's 2026 automated payroll method (4.95% x wages less allowances divided by pay "
            "periods). IDOR's printed tables use wage-band midpoints, so a table lookup can differ by a few cents.",
            "Pre-tax benefit elections (401(k), HSA, FSA, insurance) are not modeled in the Illinois estimate.",
            "Assumes this pay amount is constant every period for the full year; no local taxes. Illinois has no state "
            "disability insurance to withhold.",
        ],
    }
