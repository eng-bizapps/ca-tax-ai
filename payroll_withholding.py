"""Ring 2 extension -- per-PAYCHECK federal + California withholding
estimate (IRS Pub 15-T Worksheet 1A + EDD Method B/Exact Calculation
Method), distinct from income_brackets.py's ANNUAL tax-LIABILITY math.
Reuses the existing income_db.py connection/income_schema.py tables --
NOT a new physically-separate database. FORM-ONLY entry point (Streamlit
"Paycheck Calculator" tab, app.py) -- deliberately NOT wired into
engine.py's NL routing this pass: no detect_*/vocabulary functions exist
for this feature, so a chat question about paycheck withholding will not
reach this module. See load_payroll_withholding_data.py for the primary
sources behind every dollar figure this module looks up.

NOT MODELED, disclosed via compute_paycheck's own `assumptions` list
rather than hidden: multi-state employment, pre-tax benefit deductions
(401(k)/HSA/FSA/cafeteria-plan premiums), local/city withholding, and real
year-to-date wage tracking across multiple paychecks (see compute_fica's
own docstring for why annualizing is used instead).
"""

# WITHHOLDING runs on a different publication calendar than
# income_brackets.DEFAULT_TAX_YEAR's ANNUAL-LIABILITY figures: IRS Pub 15-T
# and EDD's Withholding Schedules for a given calendar year are published
# the PRECEDING autumn/winter (employers need them working on day one of
# the new year), whereas FTB's own income-tax Rate Schedules for that same
# year aren't finalized/loaded here until well into the FOLLOWING year (see
# income_brackets.py's own DEFAULT_TAX_YEAR docstring). As of this writing
# (Sept 2026), the 2026 Pub 15-T/EDD schedules are ALREADY FINAL and loaded
# here, while income_brackets.DEFAULT_TAX_YEAR is still 2025 because FTB's
# 2026 annual figures aren't out yet. These two constants are DELIBERATELY
# DECOUPLED -- bump each only when ITS OWN primary source actually
# publishes, never together on the same calendar date.
PAYROLL_TAX_YEAR = 2026

PAY_PERIODS_PER_YEAR = {
    "daily": 260, "weekly": 52, "biweekly": 26, "semimonthly": 24,
    "monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1,
}
PAY_FREQUENCY_LABELS = {
    "weekly": "Weekly", "biweekly": "Biweekly", "semimonthly": "Semimonthly",
    "monthly": "Monthly", "quarterly": "Quarterly", "semiannual": "Semiannual",
    "annual": "Annual", "daily": "Daily",
}
FILING_STATUS_LABELS = {
    "single": "Single", "mfs": "Married Filing Separately",
    "mfj": "Married Filing Jointly", "hoh": "Head of Household",
}

# FICA -- hardcoded module constants, updated yearly, same precedent as
# income_brackets.SE_SOCIAL_SECURITY_WAGE_BASE: these come from SSA.gov/
# IRS Form 8959, not FTB/EDD, so they don't belong in income_schema.py's
# FTB/EDD-sourced tables.
FICA_SS_RATE = 0.062
FICA_SS_WAGE_BASE = 184500.0                      # 2026; update yearly alongside PAYROLL_TAX_YEAR
FICA_MEDICARE_RATE = 0.0145
FICA_ADDITIONAL_MEDICARE_RATE = 0.009
# ALWAYS $200,000 regardless of the employee's own filing status -- this is
# the EMPLOYER's withholding-obligation trigger (IRS Form 8959 instructions:
# "Your employer is responsible for withholding the 0.9% Additional
# Medicare Tax on your Medicare wages... in excess of $200,000"). The
# $250k MFJ / $125k MFS / $200k other thresholds matter ONLY for the
# EMPLOYEE's own year-end Form 8959 true-up, never for what an employer
# withholds per paycheck. NOT indexed for inflation, unchanged since 2013 --
# a structural constant, not annually-republished data (same class as
# property_tax.HOMEOWNERS_EXEMPTION_AMOUNT).
FICA_ADDITIONAL_MEDICARE_THRESHOLD = 200000.0
FICA_CITATION = "SSA.gov Contribution and Benefit Base (2026); IRS Form 8959 Instructions"
FICA_SOURCE_URL = "https://www.ssa.gov/oact/cola/cbb.html"

FEDERAL_FILING_STATUS_MAP = {"single": "single_mfs", "mfs": "single_mfs",
                              "mfj": "mfj", "hoh": "hoh"}


def _bracket_scan(rows, amount: float):
    """rows: a list of (bracket_floor, bracket_ceiling, base_amount, rate,
    citation, source_url) tuples ordered by bracket_floor. Returns the
    matched row, defaulting to the LAST (unbounded-ceiling) row if amount
    exceeds every stated ceiling -- same segment-scan pattern as
    income_brackets.compute_ca_tax. Returns None if rows is empty."""
    if not rows:
        return None
    seg = rows[-1]
    for row in rows:
        floor = float(row[0])
        ceiling = float(row[1]) if row[1] is not None else None
        if amount >= floor and (ceiling is None or amount <= ceiling):
            seg = row
            break
    return seg


def map_federal_filing_status(filing_status: str, w4_is_2020_or_later: bool = True):
    """Maps a user-facing filing_status ('single'|'mfs'|'mfj'|'hoh') to
    Pub 15-T's 3 bracket-table buckets ('single_mfs'|'mfj'|'hoh'). A
    PRE-2020 W-4 has NO Head-of-Household option at all (the old form only
    offered "Single or Married but withhold at higher Single rate" and
    "Married") -- returns None for that combination so the caller treats
    it as unanswerable rather than silently reinterpreting it as Single.
    The Streamlit form must not offer HoH when "Before 2020" is selected."""
    fs = filing_status.lower()
    if fs == "hoh" and not w4_is_2020_or_later:
        return None
    return FEDERAL_FILING_STATUS_MAP.get(fs)


def map_ca_categories(filing_status: str, regular_allowances: int = 0):
    """Maps ONE user-facing filing_status plus the DE-4 regular-allowance
    count to CA's TWO INDEPENDENT EDD category schemes: rate_category (3
    buckets, ca_withholding_brackets) and exemption_category (4 buckets,
    ca_withholding_constants) -- MFJ further splits by allowance count
    (0-1 vs 2+) at the exemption_category level only; rate_category stays
    'married' either way.

    MFS judgment call: CA's own DE-4 form has NO federal-style "married
    filing separately" option at all -- only Single, Married (one income),
    Married (multiple incomes), Head of Household. This maps MFS to
    'single_dual_multiple' for BOTH schemes, mirroring how payroll
    providers commonly treat MFS for STATE withholding. This is a
    disclosed assumption, surfaced in compute_paycheck's own
    'assumptions' list, never silently applied."""
    fs = filing_status.lower()
    if fs in ("single", "mfs"):
        return "single_dual_multiple", "single_dual_multiple"
    if fs == "hoh":
        return "unmarried_hoh", "unmarried_hoh"
    if fs == "mfj":
        exemption_category = "married_0_1" if regular_allowances <= 1 else "married_2_plus"
        return "married", exemption_category
    raise ValueError(f"unknown filing_status: {filing_status}")


def compute_federal_withholding(conn, wages_this_period: float, pay_periods_per_year: int,
                                 filing_status: str, w4_is_2020_or_later: bool = True,
                                 step2_checkbox: bool = False, step3_dependent_credits: float = 0.0,
                                 step4a_other_income: float = 0.0, step4b_deductions: float = 0.0,
                                 step4c_extra_withholding: float = 0.0, pre2020_allowances: int = 0,
                                 tax_year: int = PAYROLL_TAX_YEAR):
    """IRS Pub 15-T (2026) Worksheet 1A, Steps 1-4. Returns None if the
    filing_status/W-4-vintage combination is invalid (see
    map_federal_filing_status) or there's no bracket/constant data for
    this tax_year -- caller must treat that as 'can't compute', never a
    fabricated $0."""
    if wages_this_period is None or wages_this_period < 0 or not pay_periods_per_year:
        return None
    federal_status = map_federal_filing_status(filing_status, w4_is_2020_or_later)
    if federal_status is None:
        return None

    annual_wage = wages_this_period * pay_periods_per_year

    if w4_is_2020_or_later:
        schedule = "step2_checkbox" if step2_checkbox else "standard"
        if step2_checkbox:
            # Worksheet 1A's own rule: $0 when the box IS checked -- a
            # STRUCTURAL rule of the formula, not annually-republished
            # data, so it's a literal here, not a table lookup.
            step1_deduction = 0.0
        else:
            row = conn.execute(
                "SELECT amount FROM federal_withholding_constants "
                "WHERE tax_year=%s AND constant_type='step2_unchecked_deduction' "
                "AND filing_status=%s", (tax_year, federal_status)).fetchone()
            if not row:
                return None
            step1_deduction = float(row[0])
        adjusted_annual = max(
            0.0, annual_wage + step4a_other_income - step4b_deductions - step1_deduction)
    else:
        schedule = "standard"  # pre-2020 W-4s never use the step2_checkbox schedule
        row = conn.execute(
            "SELECT amount FROM federal_withholding_constants "
            "WHERE tax_year=%s AND constant_type='pre2020_allowance'", (tax_year,)).fetchone()
        if not row:
            return None
        adjusted_annual = max(0.0, annual_wage - pre2020_allowances * float(row[0]))

    rows = conn.execute(
        "SELECT bracket_floor, bracket_ceiling, base_amount, rate, citation, source_url "
        "FROM federal_withholding_brackets WHERE tax_year=%s AND filing_status=%s "
        "AND schedule=%s ORDER BY bracket_floor",
        (tax_year, federal_status, schedule)).fetchall()
    seg = _bracket_scan(rows, adjusted_annual)
    if seg is None:
        return None
    floor, _ceiling, base_amount, rate, citation, source_url = seg
    floor = float(floor)
    tentative_annual = float(base_amount) + float(rate) * (adjusted_annual - floor)
    tentative_period = tentative_annual / pay_periods_per_year

    if w4_is_2020_or_later:
        tentative_period = max(
            0.0, tentative_period - step3_dependent_credits / pay_periods_per_year)

    # Pub 15-T's own whole-dollar rounding note applies to the manual Wage
    # Bracket Method tables (built for non-computerized payroll, whose
    # bands are already whole-dollar), NOT Worksheet 1A itself (titled "for
    # AUTOMATED Payroll Systems") -- confirmed live: reproducing the user's
    # own ADP screenshot ($100,000/yr weekly single, no adjustments) with
    # ordinary cent-precision rounding gives $253.27, an EXACT match with
    # ADP's real, computerized calculator; whole-dollar rounding gave
    # $253.00, a $0.27 miss. Real payroll software keeps cent precision.
    final_period = round(max(0.0, tentative_period + step4c_extra_withholding), 2)

    return {
        "withholding_this_period": final_period,
        "annual_wage": round(annual_wage, 2),
        "adjusted_annual_wage": round(adjusted_annual, 2),
        "federal_filing_status": federal_status,
        "schedule": schedule,
        "citation": citation,
        "source_url": source_url,
    }


def compute_fica(wages_this_period: float, pay_periods_per_year: int):
    """Pure math, no DB lookup. Annualizes wages_this_period assuming
    CONSTANT pay every period all year (no per-employee year-to-date wage
    ledger exists in this codebase) to determine how much of the Social
    Security wage base / Additional Medicare threshold is 'used', then
    de-annualizes back to a per-period figure -- the SAME simplification
    Worksheet 1A itself uses for federal withholding, extended here to
    FICA. This is OUR design choice for a YTD-free single-paycheck
    estimate, not something SSA/IRS specifies: it SMOOTHS the Social
    Security cap evenly across every paycheck rather than reproducing the
    real mid-year cliff a genuine payroll system shows (a high earner's
    real per-paycheck SS withholding would drop to $0 partway through the
    year once the annual cap is hit; this estimate instead shows a
    slightly-reduced amount every single paycheck). Disclosed in
    compute_paycheck's 'assumptions' list."""
    if wages_this_period is None or wages_this_period < 0 or not pay_periods_per_year:
        return None
    annual_wage = wages_this_period * pay_periods_per_year

    ss_taxable_annual = min(annual_wage, FICA_SS_WAGE_BASE)
    social_security = round((ss_taxable_annual * FICA_SS_RATE) / pay_periods_per_year, 2)

    medicare = round(wages_this_period * FICA_MEDICARE_RATE, 2)

    addl_medicare_taxable_annual = max(0.0, annual_wage - FICA_ADDITIONAL_MEDICARE_THRESHOLD)
    additional_medicare = round(
        (addl_medicare_taxable_annual * FICA_ADDITIONAL_MEDICARE_RATE) / pay_periods_per_year, 2)

    return {
        "social_security": social_security,
        "medicare": medicare,
        "additional_medicare": additional_medicare,
        "annual_wage": round(annual_wage, 2),
        "citation": FICA_CITATION,
        "source_url": FICA_SOURCE_URL,
    }


def compute_ca_withholding(conn, wages_this_period: float, pay_periods_per_year: int,
                            filing_status: str, regular_allowances: int = 0,
                            estimated_deduction_allowances: int = 0,
                            tax_year: int = PAYROLL_TAX_YEAR):
    """EDD Method B, Steps 1-5, using the annualize-then-divide shortcut
    EDD's own "California Withholding Schedules" document sanctions as
    equivalent to using period-specific tables directly (its own worked
    examples confirm this). Step 1's low-income exemption is a
    SHORT-CIRCUIT: if annual wages are at or under the threshold for this
    exemption_category, returns $0 withholding without touching the
    bracket table at all. Returns None if there's no bracket/constant
    data for this tax_year."""
    if wages_this_period is None or wages_this_period < 0 or not pay_periods_per_year:
        return None
    rate_category, exemption_category = map_ca_categories(filing_status, regular_allowances)
    annual_wage = wages_this_period * pay_periods_per_year

    exemption_row = conn.execute(
        "SELECT amount, citation, source_url FROM ca_withholding_constants "
        "WHERE tax_year=%s AND constant_type='low_income_exemption' AND exemption_category=%s",
        (tax_year, exemption_category)).fetchone()
    if not exemption_row:
        return None
    low_income_exemption, citation, source_url = (
        float(exemption_row[0]), exemption_row[1], exemption_row[2])

    if annual_wage <= low_income_exemption:
        return {
            "withholding_this_period": 0.0,
            "reason": "low_income_exemption",
            "annual_wage": round(annual_wage, 2),
            "rate_category": rate_category,
            "exemption_category": exemption_category,
            "citation": citation,
            "source_url": source_url,
        }

    est_ded_amount = 0.0
    if estimated_deduction_allowances:
        row = conn.execute(
            "SELECT amount FROM ca_withholding_constants "
            "WHERE tax_year=%s AND constant_type='estimated_deduction_per_allowance'",
            (tax_year,)).fetchone()
        if not row:
            return None
        est_ded_amount = float(row[0]) * estimated_deduction_allowances

    std_ded_row = conn.execute(
        "SELECT amount FROM ca_withholding_constants "
        "WHERE tax_year=%s AND constant_type='standard_deduction' AND exemption_category=%s",
        (tax_year, exemption_category)).fetchone()
    if not std_ded_row:
        return None
    taxable_income = max(0.0, annual_wage - est_ded_amount - float(std_ded_row[0]))

    rows = conn.execute(
        "SELECT bracket_floor, bracket_ceiling, base_amount, rate, citation, source_url "
        "FROM ca_withholding_brackets WHERE tax_year=%s AND rate_category=%s "
        "ORDER BY bracket_floor", (tax_year, rate_category)).fetchall()
    seg = _bracket_scan(rows, taxable_income)
    if seg is None:
        return None
    floor, _ceiling, base_amount, rate, bracket_citation, bracket_source_url = seg
    floor = float(floor)
    computed_tax_annual = float(base_amount) + float(rate) * (taxable_income - floor)

    exemption_credit = 0.0
    if regular_allowances:
        row = conn.execute(
            "SELECT amount FROM ca_withholding_constants "
            "WHERE tax_year=%s AND constant_type='exemption_credit_per_allowance'",
            (tax_year,)).fetchone()
        if not row:
            return None
        exemption_credit = float(row[0]) * regular_allowances

    withholding_annual = max(0.0, computed_tax_annual - exemption_credit)
    withholding_this_period = round(withholding_annual / pay_periods_per_year, 2)

    return {
        "withholding_this_period": withholding_this_period,
        "annual_wage": round(annual_wage, 2),
        "taxable_income_annual": round(taxable_income, 2),
        "rate_category": rate_category,
        "exemption_category": exemption_category,
        "citation": bracket_citation,
        "source_url": bracket_source_url,
    }


def compute_ca_sdi(conn, wages_this_period: float, tax_year: int = PAYROLL_TAX_YEAR):
    """Flat rate x wages_this_period, no cap (SB 951 eliminated CA SDI's
    taxable wage ceiling effective 2024-01-01). Returns None if
    ca_sdi_rate has no row for tax_year."""
    if wages_this_period is None or wages_this_period < 0:
        return None
    row = conn.execute(
        "SELECT rate, citation, source_url FROM ca_sdi_rate WHERE tax_year=%s",
        (tax_year,)).fetchone()
    if not row:
        return None
    rate, citation, source_url = float(row[0]), row[1], row[2]
    return {
        "withholding_this_period": round(wages_this_period * rate, 2),
        "rate": rate,
        "citation": citation,
        "source_url": source_url,
    }


def compute_paycheck(conn, gross_pay: float, pay_frequency: str, filing_status: str,
                      w4_is_2020_or_later: bool = True, step2_checkbox: bool = False,
                      step3_dependent_credits: float = 0.0, step4a_other_income: float = 0.0,
                      step4b_deductions: float = 0.0, step4c_extra_withholding: float = 0.0,
                      pre2020_allowances: int = 0, ca_regular_allowances: int = 0,
                      ca_estimated_deduction_allowances: int = 0,
                      tax_year: int = PAYROLL_TAX_YEAR):
    """THE one function the Paycheck Calculator UI tab calls. Composes
    compute_federal_withholding + compute_fica + compute_ca_withholding +
    compute_ca_sdi into one result. Returns None (never a fabricated
    number) if gross_pay is missing/non-positive, pay_frequency is
    unrecognized, or any sub-computation returns None."""
    if gross_pay is None or gross_pay <= 0:
        return None
    pay_periods_per_year = PAY_PERIODS_PER_YEAR.get(pay_frequency)
    if not pay_periods_per_year:
        return None

    federal = compute_federal_withholding(
        conn, gross_pay, pay_periods_per_year, filing_status,
        w4_is_2020_or_later=w4_is_2020_or_later, step2_checkbox=step2_checkbox,
        step3_dependent_credits=step3_dependent_credits, step4a_other_income=step4a_other_income,
        step4b_deductions=step4b_deductions, step4c_extra_withholding=step4c_extra_withholding,
        pre2020_allowances=pre2020_allowances, tax_year=tax_year)
    if federal is None:
        return None

    fica = compute_fica(gross_pay, pay_periods_per_year)
    if fica is None:
        return None

    ca = compute_ca_withholding(
        conn, gross_pay, pay_periods_per_year, filing_status,
        regular_allowances=ca_regular_allowances,
        estimated_deduction_allowances=ca_estimated_deduction_allowances, tax_year=tax_year)
    if ca is None:
        return None

    sdi = compute_ca_sdi(conn, gross_pay, tax_year=tax_year)
    if sdi is None:
        return None

    federal_withholding = federal["withholding_this_period"]
    social_security = fica["social_security"]
    medicare = fica["medicare"]
    additional_medicare = fica["additional_medicare"]
    ca_withholding = ca["withholding_this_period"]
    ca_sdi = sdi["withholding_this_period"]

    total_taxes = round(
        federal_withholding + social_security + medicare + additional_medicare
        + ca_withholding + ca_sdi, 2)
    take_home_pay = round(gross_pay - total_taxes, 2)

    assumptions = [
        "Assumes this pay amount is constant every period for the full year -- a mid-year raise, "
        "bonus, or job change will change your real annual totals. The Social Security wage base "
        "and Additional Medicare Tax threshold are ANNUAL caps applied here by annualizing this "
        "period's wages and dividing the result back down, not by tracking real year-to-date wages.",
        "Does not model pre-tax benefit deductions (401(k), HSA, FSA, cafeteria-plan premiums) "
        "that would reduce taxable wages before withholding.",
        "Does not model local/city withholding or multi-state employment.",
    ]
    if filing_status.lower() == "mfs":
        assumptions.append(
            "California's DE-4 form has no 'Married Filing Separately' option -- this estimate "
            "treats MFS the same as Single for CALIFORNIA withholding only (federal withholding "
            "does have its own combined Single/MFS bracket table, used correctly either way).")

    return {
        "gross_pay": round(gross_pay, 2),
        "pay_frequency": pay_frequency,
        "pay_periods_per_year": pay_periods_per_year,
        "federal_withholding": federal_withholding,
        "social_security": social_security,
        "medicare": medicare,
        "additional_medicare": additional_medicare,
        "ca_withholding": ca_withholding,
        "ca_sdi": ca_sdi,
        "total_taxes": total_taxes,
        "take_home_pay": take_home_pay,
        "tax_year": tax_year,
        "federal_detail": federal,
        "fica_detail": fica,
        "ca_detail": ca,
        "sdi_detail": sdi,
        "assumptions": assumptions,
    }
