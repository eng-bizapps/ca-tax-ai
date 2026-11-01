"""Deterministic CA employer payroll-tax (EDD) computations -- Ring 5.

All figures come from the employer database (employer_schema.py), seeded
from EDD's 2026 Employer's Guide (DE 44 Rev. 52, 4-26) and EDD's interest
rate page. Nothing here is guessed: the one input the guide cannot supply --
an employer's experience-rated UI rate -- is taken from the caller, else the
new-employer rate is used and that is disclosed by the caller.

NOT modeled (disclose): legal-holiday due-date extensions, UI rate assignment from the reserve-
account formula, the 977(c) ETT exemption/penalty cases.
"""
import datetime

PAYROLL_TAX_YEAR = 2026
CITATION = "EDD DE 44 Employer's Guide, Rev. 52 (4-26)"
SOURCE_URL = "https://edd.ca.gov/pdf_pub_ctr/de44.pdf"
INTEREST_URL = "https://edd.ca.gov/en/payroll_taxes/interest_rate"

FEDERAL_SCHEDULES = ("next_day", "semi_weekly", "monthly", "quarterly_or_annual")


def get_rate(conn, rate_key, tax_year=PAYROLL_TAX_YEAR):
    row = conn.execute(
        "SELECT rate, wage_limit FROM ca_employer_payroll_rates WHERE tax_year=%s AND rate_key=%s",
        (tax_year, rate_key)).fetchone()
    if not row:
        return None
    return float(row[0]), (float(row[1]) if row[1] is not None else None)


def compute_employer_costs(conn, annual_wages, num_employees=1, ui_rate=None, tax_year=PAYROLL_TAX_YEAR):
    """Employer UI + ETT and employee SDI for num_employees each paid
    annual_wages. UI and ETT apply to only the first $7,000 per employee;
    SDI (employee-withheld) applies to all wages, no cap since 1/1/2024.
    ui_rate (fraction) defaults to the 3.4% new-employer rate."""
    if annual_wages is None or annual_wages < 0 or num_employees is None or num_employees < 1:
        return None
    ui_new = get_rate(conn, "ui_new_employer", tax_year)
    ett = get_rate(conn, "ett", tax_year)
    sdi = get_rate(conn, "sdi", tax_year)
    ui_lo = get_rate(conn, "ui_min", tax_year)
    ui_hi = get_rate(conn, "ui_max", tax_year)
    if not (ui_new and ett and sdi and ui_lo and ui_hi):
        return None
    used_default = ui_rate is None
    rate = ui_new[0] if used_default else ui_rate
    if not ui_lo[0] <= rate <= ui_hi[0]:
        return None
    limit = ui_new[1]
    taxable = min(annual_wages, limit)
    ui = taxable * rate
    ett_amt = taxable * ett[0]
    sdi_amt = annual_wages * sdi[0]
    return {
        "tax_year": tax_year, "annual_wages": annual_wages, "num_employees": num_employees,
        "taxable_wage_limit": limit, "taxable_wages_each": round(taxable, 2),
        "ui_rate": rate, "ui_rate_is_default_new_employer": used_default,
        "ett_rate": ett[0], "sdi_rate": sdi[0],
        "ui_each": round(ui, 2), "ett_each": round(ett_amt, 2),
        "employer_cost_each": round(ui + ett_amt, 2),
        "employer_cost_total": round((ui + ett_amt) * num_employees, 2),
        "sdi_withheld_each": round(sdi_amt, 2),
        "sdi_withheld_total": round(sdi_amt * num_employees, 2),
        "max_ui_each": round(limit * ui_hi[0], 2),
        "citation": CITATION, "source_url": SOURCE_URL,
    }


def compute_futa(conn, annual_wages, num_employees=1, tax_year=PAYROLL_TAX_YEAR):
    """Federal unemployment tax (FUTA) per the Form 940 rules: 6.0% on the
    first $7,000 per employee, less up to a 5.4% credit for state
    unemployment tax paid on time (net 0.6%), plus California's FUTA credit
    reduction. The reduction is final for 2022-2025; for 2026 it is only
    PRELIMINARY (DOL sets it Nov 10, 2026), so the result carries a
    low (outstanding-advance reduction only) and a high (with the BCR
    add-on) figure instead of one number. Assumes the full 5.4% credit,
    i.e. state UI paid on time; late state payments reduce it. Returns None
    for a year without seeded rates."""
    if annual_wages is None or annual_wages < 0 or num_employees is None or num_employees < 1:
        return None
    gross = get_rate(conn, "futa_gross", tax_year)
    credit = get_rate(conn, "futa_max_credit", tax_year)
    red = conn.execute("SELECT reduction_rate, potential_max_rate, status, notes, source_url "
                       "FROM ca_futa_credit_reductions WHERE tax_year=%s", (tax_year,)).fetchone()
    if not (gross and credit and red):
        return None
    limit = gross[1]
    taxable = min(annual_wages, limit)
    net = gross[0] - credit[0]
    base = taxable * net
    low_rate = float(red[0])
    high_rate = float(red[1]) if red[1] is not None else low_rate
    return {
        "tax_year": tax_year, "taxable_wages_each": round(taxable, 2), "num_employees": num_employees,
        "wage_limit": limit, "gross_rate": gross[0], "max_credit": credit[0], "net_rate": round(net, 6),
        "base_each": round(base, 2),
        "reduction_status": red[2], "reduction_rate": low_rate, "reduction_rate_max": high_rate,
        "reduction_each": round(taxable * low_rate, 2), "reduction_each_max": round(taxable * high_rate, 2),
        "total_each": round(base + taxable * low_rate, 2), "total_each_max": round(base + taxable * high_rate, 2),
        "total": round((base + taxable * low_rate) * num_employees, 2),
        "total_max": round((base + taxable * high_rate) * num_employees, 2),
        "reduction_notes": red[3],
        "citation": "IRS Instructions for Form 940; DOL FUTA credit reduction tables",
        "source_url": "https://oui.doleta.gov/unemploy/futa_credit.asp",
    }


def _next_business_day(d):
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def compute_deposit_schedule(conn, federal_schedule, accumulated_pit, payday=None, tax_year=PAYROLL_TAX_YEAR):
    """California deposit schedule for PIT withholding + SDI, per the DE 44
    deposit table: federal deposit schedule x accumulated PIT withholding.
    With payday (datetime.date) also returns the due date for that payday's
    deposit (weekend -> next business day; legal holidays NOT modeled)."""
    if federal_schedule not in FEDERAL_SCHEDULES or accumulated_pit is None or accumulated_pit < 0:
        return None
    row = conn.execute(
        "SELECT ca_schedule, due_rule, min_accumulated, max_accumulated FROM ca_edd_deposit_rules "
        "WHERE tax_year=%s AND federal_schedule=%s AND min_accumulated <= %s "
        "AND (max_accumulated IS NULL OR %s < max_accumulated) "
        "ORDER BY min_accumulated DESC LIMIT 1",
        (tax_year, federal_schedule, accumulated_pit, accumulated_pit)).fetchone()
    if not row:
        return None
    ca_schedule, due_rule = row[0], row[1]
    out = {"federal_schedule": federal_schedule, "accumulated_pit": accumulated_pit,
           "ca_schedule": ca_schedule, "due_rule": due_rule, "payday": payday, "due_date": None,
           "citation": CITATION, "source_url": SOURCE_URL}
    if payday is None:
        return out
    if due_rule == "next_day":
        due = _next_business_day(payday + datetime.timedelta(days=1))
    elif due_rule == "semi_weekly":
        # Wed-Fri payday -> following Wednesday; Sat-Tue payday -> following Friday
        wd = payday.weekday()
        if wd in (2, 3, 4):
            due = payday + datetime.timedelta(days=(2 - wd) % 7 or 7)
        else:
            due = payday + datetime.timedelta(days=(4 - wd) % 7 or 7)
        due = _next_business_day(due)
    elif due_rule == "15th_following_month":
        y, m = (payday.year + 1, 1) if payday.month == 12 else (payday.year, payday.month + 1)
        due = _next_business_day(datetime.date(y, m, 15))
    else:  # quarterly: delinquent date of the quarter containing the payday
        q = (payday.month - 1) // 3 + 1
        r = conn.execute("SELECT delinquent_date FROM ca_edd_due_dates WHERE tax_year=%s AND quarter=%s",
                         (payday.year if payday.year == tax_year else tax_year, q)).fetchone()
        due = r[0] if r else None
    out["due_date"] = due
    return out


def compute_supplemental_withholding(conn, amount, wage_type="other", tax_year=PAYROLL_TAX_YEAR):
    """Flat-rate PIT on a supplemental wage paid separately from regular
    wages (DE 44 option 2): 10.23% bonuses/stock options, 6.6% other. No
    withholding allowances apply under this method."""
    if amount is None or amount < 0 or wage_type not in ("bonus", "other"):
        return None
    got = get_rate(conn, "pit_supp_bonus" if wage_type == "bonus" else "pit_supp_other", tax_year)
    if not got:
        return None
    return {"amount": amount, "wage_type": wage_type, "rate": got[0],
            "withholding": round(amount * got[0], 2), "citation": CITATION, "source_url": SOURCE_URL}


def compute_late_payment(conn, tax_due, due_date, paid_date, tax_year=PAYROLL_TAX_YEAR):
    """15% penalty plus interest compounded daily at EDD's semi-annual rate
    (looked up per day, so a late period spanning Jan 1 / Jul 1 uses each
    period's own daily factor). Interest is computed on tax + penalty, which
    is an approximation: EDD says interest applies to unpaid tax, interest
    and certain penalties -- which penalties is not confirmed here. Returns
    None if the payment is not late or no rate covers a day."""
    if tax_due is None or tax_due < 0 or paid_date <= due_date:
        return None
    pen = get_rate(conn, "late_penalty", tax_year)
    if not pen:
        return None
    penalty = tax_due * pen[0]
    rates = conn.execute("SELECT period_start, period_end, daily_factor FROM ca_edd_interest_rates").fetchall()
    base = tax_due + penalty
    balance = base
    day = due_date + datetime.timedelta(days=1)
    days = 0
    while day <= paid_date:
        f = next((float(r[2]) for r in rates if r[0] <= day <= r[1]), None)
        if f is None:
            return None
        balance *= 1 + f
        days += 1
        day += datetime.timedelta(days=1)
    return {"tax_due": tax_due, "days_late": days, "penalty_rate": pen[0],
            "penalty": round(penalty, 2), "interest": round(balance - base, 2),
            "total": round(tax_due + penalty + (balance - base), 2),
            "citation": CITATION, "source_url": INTEREST_URL}
