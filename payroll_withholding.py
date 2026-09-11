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
rather than hidden: multi-state employment. Real year-to-date wage
tracking is used only when the caller states it via
`ytd_wages_before_this_period` (compute_fica) -- absent that, the
annualize-and-smooth estimate still applies (see compute_fica's own
docstring). Pre-tax benefit deductions (401(k) traditional, HSA, Health/
Dependent Care FSA, Section 125 medical/dental/vision premiums) ARE
modeled (compute_pretax_benefits) for FEDERAL, CALIFORNIA, and FICA
wage-base purposes -- see BENEFIT_WAGE_BASES for exactly which bases each
benefit reduces (notably, HSA reduces federal+FICA wages but NOT
California wages, since California does not conform to the federal HSA
tax exclusion). CALIFORNIA SDI wages are deliberately NOT reduced by any
pre-tax benefit in this pass -- a narrower, explicitly disclosed scope
than federal/CA-PIT/FICA (SDI conformity for these benefit types wasn't
independently confirmed this session), consistent with this project's
"when simplifying, err toward overestimating" precedent.

CONFIRMED NOT APPLICABLE (not merely unbuilt): local/city income tax
WITHHOLDING. EDD's own enumeration of California payroll taxes
(https://edd.ca.gov/en/payroll_taxes/what_are_state_payroll_taxes/) lists
exactly four (UI and ETT, both employer-paid; SDI and PIT, both
employee-withheld) -- no California city imposes an employee-side
income-tax WITHHOLDING obligation on wages. (San Francisco's payroll
expense/gross-receipts taxes are business taxes on the EMPLOYER, not wage
withholding, and are out of scope for a per-paycheck EMPLOYEE withholding
calculator for that separate reason.)
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
                                 is_nonresident_alien: bool = False, exempt: bool = False,
                                 tax_year: int = PAYROLL_TAX_YEAR):
    """IRS Pub 15-T (2026) Worksheet 1A, Steps 1-4. Returns None if the
    filing_status/W-4-vintage combination is invalid (see
    map_federal_filing_status) or there's no bracket/constant data for
    this tax_year -- caller must treat that as 'can't compute', never a
    fabricated $0.

    `exempt`: an employee who claims exemption from federal income tax
    withholding (Form W-4's own "Exempt" line) skips the rest of the
    worksheet entirely, per IRS's own instructions -- base withholding is
    $0 regardless of any other stated W-4 figure. `step4c_extra_withholding`
    is still added on top, since it represents the employee's own
    independently-stated request for additional withholding, not part of
    the formula an exemption bypasses.

    `is_nonresident_alien`: Pub 15-T's own "Withholding Adjustment for
    Nonresident Alien Employees" procedure -- add a per-pay-period dollar
    amount (federal_nra_wage_additions, Table 1 for a pre-2020 W-4 or
    Table 2 for 2020+) to THIS PERIOD's wages before any other Worksheet
    1A step, applied ONLY to this federal income tax computation. Pub
    15-T's own text is explicit that this addition does NOT affect Social
    Security, Medicare, or FUTA liability (compute_fica/compute_ca_*
    intentionally keep using the caller's unadjusted wages -- this
    function never mutates a shared value, only a local one). NOT applied
    when `exempt` is also set (exemption bypasses the whole worksheet,
    making the addition moot). KNOWN LIMITATION, disclosed rather than
    modeled: Pub 15-T itself carves out "nonresident alien students from
    India and business apprentices from India" from this procedure
    (a narrow tax-treaty exception) -- this function does not special-case
    that population and will incorrectly apply the addition to them."""
    if wages_this_period is None or wages_this_period < 0 or not pay_periods_per_year:
        return None
    federal_status = map_federal_filing_status(filing_status, w4_is_2020_or_later)
    if federal_status is None:
        return None

    if is_nonresident_alien and not exempt:
        w4_vintage = "2020plus" if w4_is_2020_or_later else "pre2020"
        row = conn.execute(
            "SELECT amount FROM federal_nra_wage_additions "
            "WHERE tax_year=%s AND w4_vintage=%s AND pay_periods_per_year=%s",
            (tax_year, w4_vintage, pay_periods_per_year)).fetchone()
        if not row:
            return None
        wages_this_period = wages_this_period + float(row[0])

    annual_wage = wages_this_period * pay_periods_per_year

    if exempt:
        return {
            "withholding_this_period": round(max(0.0, step4c_extra_withholding), 2),
            "reason": "exempt",
            "annual_wage": round(annual_wage, 2),
            "adjusted_annual_wage": None,
            "federal_filing_status": federal_status,
            "schedule": None,
            "citation": "Form W-4 exemption from federal income tax withholding claimed",
            "source_url": None,
        }

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
        "is_nonresident_alien": is_nonresident_alien,
        "citation": citation,
        "source_url": source_url,
    }


def compute_fica(wages_this_period: float, pay_periods_per_year: int,
                  exempt_social_security: bool = False, exempt_medicare: bool = False,
                  ytd_wages_before_this_period: float = None):
    """Pure math, no DB lookup.

    When `ytd_wages_before_this_period` is None (the default): annualizes
    wages_this_period assuming CONSTANT pay every period all year to
    determine how much of the Social Security wage base / Additional
    Medicare threshold is 'used', then de-annualizes back to a per-period
    figure -- the SAME simplification Worksheet 1A itself uses for federal
    withholding, extended here to FICA. This is OUR design choice for a
    YTD-free single-paycheck estimate, not something SSA/IRS specifies: it
    SMOOTHS the Social Security cap evenly across every paycheck rather
    than reproducing the real mid-year cliff a genuine payroll system
    shows (a high earner's real per-paycheck SS withholding would drop to
    $0 partway through the year once the annual cap is hit; this estimate
    instead shows a slightly-reduced amount every single paycheck).
    Disclosed in compute_paycheck's 'assumptions' list.

    When `ytd_wages_before_this_period` IS stated (a caller-supplied real
    cumulative FICA-wages-so-far figure -- a pay stub's Social
    Security/Medicare YTD box, NOT gross YTD pay, since these diverge once
    pre-tax benefits are involved): uses MARGINAL capping against the real
    running total instead of the smoothed estimate -- correctly
    reproducing the actual mid-year cliff (e.g. exactly $0 Social Security
    once the wage base was already exhausted by prior paychecks) rather
    than an approximation.

    `exempt_medicare` zeroes BOTH regular and Additional Medicare Tax --
    the Additional Medicare Tax is a surtax on Medicare wages specifically
    (IRC 3101(b)(2)), so an employee genuinely exempt from Medicare owes
    neither."""
    if wages_this_period is None or wages_this_period < 0 or not pay_periods_per_year:
        return None
    if ytd_wages_before_this_period is not None and ytd_wages_before_this_period < 0:
        return None
    annual_wage = wages_this_period * pay_periods_per_year

    if exempt_social_security:
        social_security = 0.0
    elif ytd_wages_before_this_period is None:
        ss_taxable_annual = min(annual_wage, FICA_SS_WAGE_BASE)
        social_security = round((ss_taxable_annual * FICA_SS_RATE) / pay_periods_per_year, 2)
    else:
        cumulative_before = ytd_wages_before_this_period
        cumulative_after = cumulative_before + wages_this_period
        ss_taxable_this_period = (min(cumulative_after, FICA_SS_WAGE_BASE)
                                  - min(cumulative_before, FICA_SS_WAGE_BASE))
        social_security = round(max(0.0, ss_taxable_this_period) * FICA_SS_RATE, 2)

    if exempt_medicare:
        medicare = 0.0
        additional_medicare = 0.0
    else:
        medicare = round(wages_this_period * FICA_MEDICARE_RATE, 2)
        if ytd_wages_before_this_period is None:
            addl_medicare_taxable_annual = max(0.0, annual_wage - FICA_ADDITIONAL_MEDICARE_THRESHOLD)
            additional_medicare = round(
                (addl_medicare_taxable_annual * FICA_ADDITIONAL_MEDICARE_RATE) / pay_periods_per_year, 2)
        else:
            cumulative_before = ytd_wages_before_this_period
            cumulative_after = cumulative_before + wages_this_period
            addl_taxable_this_period = (max(0.0, cumulative_after - FICA_ADDITIONAL_MEDICARE_THRESHOLD)
                                        - max(0.0, cumulative_before - FICA_ADDITIONAL_MEDICARE_THRESHOLD))
            additional_medicare = round(addl_taxable_this_period * FICA_ADDITIONAL_MEDICARE_RATE, 2)

    return {
        "social_security": social_security,
        "medicare": medicare,
        "additional_medicare": additional_medicare,
        "method": "ytd_actual" if ytd_wages_before_this_period is not None else "annualized_estimate",
        "annual_wage": round(annual_wage, 2),
        "citation": FICA_CITATION,
        "source_url": FICA_SOURCE_URL,
    }


def compute_ca_withholding(conn, wages_this_period: float, pay_periods_per_year: int,
                            filing_status: str, regular_allowances: int = 0,
                            estimated_deduction_allowances: int = 0,
                            extra_withholding: float = 0.0, exempt: bool = False,
                            tax_year: int = PAYROLL_TAX_YEAR):
    """EDD Method B, Steps 1-5, using the annualize-then-divide shortcut
    EDD's own "California Withholding Schedules" document sanctions as
    equivalent to using period-specific tables directly (its own worked
    examples confirm this). Step 1's low-income exemption is a
    SHORT-CIRCUIT: if annual wages are at or under the threshold for this
    exemption_category, returns $0 base withholding without touching the
    bracket table at all. Returns None if there's no bracket/constant
    data for this tax_year.

    `exempt` (DE-4 exemption from CA withholding) short-circuits the same
    way, before any bracket lookup. `extra_withholding` (an employee's own
    stated additional withholding request) is added on top of the base
    figure in EVERY case -- exempt, low-income, and normally-computed --
    since it represents an independent instruction, not part of the
    formula an exemption or low income bypasses."""
    if wages_this_period is None or wages_this_period < 0 or not pay_periods_per_year:
        return None
    rate_category, exemption_category = map_ca_categories(filing_status, regular_allowances)
    annual_wage = wages_this_period * pay_periods_per_year
    extra_withholding = max(0.0, extra_withholding)

    if exempt:
        return {
            "withholding_this_period": round(extra_withholding, 2),
            "reason": "exempt",
            "annual_wage": round(annual_wage, 2),
            "rate_category": rate_category,
            "exemption_category": exemption_category,
            "citation": "DE-4 exemption from California state income tax withholding claimed",
            "source_url": None,
        }

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
            "withholding_this_period": round(extra_withholding, 2),
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
    withholding_this_period = round(withholding_annual / pay_periods_per_year + extra_withholding, 2)

    return {
        "withholding_this_period": withholding_this_period,
        "annual_wage": round(annual_wage, 2),
        "taxable_income_annual": round(taxable_income, 2),
        "rate_category": rate_category,
        "exemption_category": exemption_category,
        "citation": bracket_citation,
        "source_url": bracket_source_url,
    }


def compute_ca_sdi(conn, wages_this_period: float, exempt: bool = False,
                    tax_year: int = PAYROLL_TAX_YEAR):
    """Flat rate x wages_this_period, no cap (SB 951 eliminated CA SDI's
    taxable wage ceiling effective 2024-01-01). Returns None if
    ca_sdi_rate has no row for tax_year. `exempt` short-circuits to $0 --
    unlike federal/CA income tax withholding, SDI has no "extra
    withholding" concept in the reference calculator this mirrors, so
    there's nothing to add back on top."""
    if wages_this_period is None or wages_this_period < 0:
        return None
    if exempt:
        return {
            "withholding_this_period": 0.0,
            "reason": "exempt",
            "rate": None,
            "citation": "SDI exemption claimed",
            "source_url": None,
        }
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


# Which of the 3 wage bases each pre-tax benefit type EXCLUDES from
# taxable wages, per IRC statute text + EDD DE 231EB's own "Cafeteria
# Plans" benefits-taxability table. Deliberately does NOT include CA SDI
# as a 4th basis -- SDI conformity for these benefit types wasn't part of
# this session's verified-facts research (which covered Federal/CA
# PIT/FICA specifically, not SDI), so compute_paycheck leaves SDI wages on
# unreduced gross_pay rather than guess (see compute_paycheck's own
# docstring/assumptions) -- consistent with this project's "when
# simplifying, err toward overestimating" precedent, since this means CA
# SDI is computed on a HIGHER wage base than it might strictly need to be.
# Roth 401(k) is absent ON PURPOSE: it reduces NONE of these 3 bases
# (already after-tax under IRC 402A), so there is nothing for a
# withholding calculator to do with it -- no field, no parameter, no row.
BENEFIT_WAGE_BASES = {
    "401k_traditional":   frozenset({"federal", "ca"}),            # NOT fica -- IRC 3121(v)(1)
    "hsa":                frozenset({"federal", "fica"}),           # NOT ca -- CA non-conformity
    "health_fsa":         frozenset({"federal", "ca", "fica"}),
    "dependent_care_fsa": frozenset({"federal", "ca", "fica"}),
    "medical":            frozenset({"federal", "ca", "fica"}),
    "dental":             frozenset({"federal", "ca", "fica"}),
    "vision":             frozenset({"federal", "ca", "fica"}),
}


def _resolve_limit_keys(benefit_type, age_50_or_older, hsa_family_coverage,
                         age_55_or_older, filing_status):
    """Returns a list of pretax_benefit_limits.benefit_type keys to SUM
    for this benefit's applicable annual limit, or None if the benefit has
    no IRS dollar limit at all (medical/dental/vision premiums -- pay
    whatever the actual premium costs, no cap)."""
    if benefit_type == "401k_traditional":
        keys = ["401k_elective_deferral"]
        if age_50_or_older:
            # Only the flat $8,000 age-50+ catch-up -- the narrower
            # age-60-63 "super catch-up" ($11,250 instead) is deliberately
            # NOT modeled, disclosed in compute_paycheck's assumptions.
            keys.append("401k_catchup_50")
        return keys
    if benefit_type == "hsa":
        keys = ["hsa_family" if hsa_family_coverage else "hsa_self_only"]
        if age_55_or_older:
            keys.append("hsa_catchup_55")
        return keys
    if benefit_type == "health_fsa":
        return ["health_fsa"]
    if benefit_type == "dependent_care_fsa":
        return ["dependent_care_fsa_mfs" if filing_status.lower() == "mfs" else "dependent_care_fsa"]
    return None  # medical, dental, vision -- no IRS limit


def compute_pretax_benefits(conn, pay_periods_per_year: int, filing_status: str,
                             pretax_401k_traditional: float = 0.0,
                             pretax_401k_age_50_or_older: bool = False,
                             pretax_hsa: float = 0.0,
                             pretax_hsa_family_coverage: bool = False,
                             pretax_hsa_age_55_or_older: bool = False,
                             pretax_health_fsa: float = 0.0,
                             pretax_dependent_care_fsa: float = 0.0,
                             pretax_medical: float = 0.0,
                             pretax_dental: float = 0.0,
                             pretax_vision: float = 0.0,
                             tax_year: int = PAYROLL_TAX_YEAR):
    """Per-period pre-tax benefit elections -> how much to exclude from
    FEDERAL, CALIFORNIA, and FICA taxable wages this period (three
    potentially DIFFERENT amounts -- see BENEFIT_WAGE_BASES, especially
    the HSA CA-non-conformity divergence). Uses the SAME annualize/cap/
    de-annualize pattern compute_fica's annualized-estimate branch uses
    for the Social Security wage base: each benefit's ANNUAL IRS limit
    (looked up lazily -- an all-zero election never touches the DB) caps
    the annualized election, and only the CAPPED (within-limit) portion is
    excluded from wages -- the excess (if any) stays IN taxable wages for
    every basis that benefit would have reduced, never silently dropped
    and never silently exempted past the legal limit.

    medical/dental/vision share IDENTICAL tax treatment (reduce federal+
    ca+fica, no IRS limit) but stay 3 separate parameters/result entries
    for per-line transparency, matching the reference calculator's own
    3-field layout.

    Returns None if any elected amount is negative, pay_periods_per_year
    is falsy, or a required pretax_benefit_limits row is missing for
    tax_year -- never a fabricated number."""
    if not pay_periods_per_year:
        return None
    elections = {
        "401k_traditional": pretax_401k_traditional,
        "hsa": pretax_hsa,
        "health_fsa": pretax_health_fsa,
        "dependent_care_fsa": pretax_dependent_care_fsa,
        "medical": pretax_medical,
        "dental": pretax_dental,
        "vision": pretax_vision,
    }
    if any(v is None or v < 0 for v in elections.values()):
        return None

    benefits = {}
    federal_reduction = ca_reduction = fica_reduction = total_elected = 0.0
    for benefit_type, elected_this_period in elections.items():
        total_elected += elected_this_period
        annual_elected = elected_this_period * pay_periods_per_year
        wage_bases = BENEFIT_WAGE_BASES[benefit_type]

        if elected_this_period == 0:
            annual_limit = None
            within_limit_this_period = 0.0
            excess_this_period = 0.0
            citation = source_url = None
        else:
            limit_keys = _resolve_limit_keys(
                benefit_type, pretax_401k_age_50_or_older, pretax_hsa_family_coverage,
                pretax_hsa_age_55_or_older, filing_status)
            citation = source_url = None
            if limit_keys is None:
                annual_limit = None
                within_limit_annual = annual_elected
            else:
                annual_limit = 0.0
                for key in limit_keys:
                    row = conn.execute(
                        "SELECT amount, citation, source_url FROM pretax_benefit_limits "
                        "WHERE tax_year=%s AND benefit_type=%s", (tax_year, key)).fetchone()
                    if not row:
                        return None
                    annual_limit += float(row[0])
                    citation, source_url = row[1], row[2]
                within_limit_annual = min(annual_elected, annual_limit)
            excess_annual = 0.0 if annual_limit is None else max(0.0, annual_elected - annual_limit)
            within_limit_this_period = round(within_limit_annual / pay_periods_per_year, 2)
            excess_this_period = round(excess_annual / pay_periods_per_year, 2)

        for basis in wage_bases:
            if basis == "federal":
                federal_reduction += within_limit_this_period
            elif basis == "ca":
                ca_reduction += within_limit_this_period
            elif basis == "fica":
                fica_reduction += within_limit_this_period

        benefits[benefit_type] = {
            "elected_this_period": round(elected_this_period, 2),
            "annual_elected": round(annual_elected, 2),
            "annual_limit": round(annual_limit, 2) if annual_limit is not None else None,
            "within_limit_this_period": within_limit_this_period,
            "excess_this_period": excess_this_period,
            "reduces_wage_bases": sorted(wage_bases),
            "citation": citation,
            "source_url": source_url,
        }

    return {
        "benefits": benefits,
        "federal_wage_reduction_this_period": round(federal_reduction, 2),
        "ca_wage_reduction_this_period": round(ca_reduction, 2),
        "fica_wage_reduction_this_period": round(fica_reduction, 2),
        "total_elected_this_period": round(total_elected, 2),
    }


def compute_paycheck(conn, gross_pay: float, pay_frequency: str, filing_status: str,
                      w4_is_2020_or_later: bool = True, step2_checkbox: bool = False,
                      step3_dependent_credits: float = 0.0, step4a_other_income: float = 0.0,
                      step4b_deductions: float = 0.0, step4c_extra_withholding: float = 0.0,
                      pre2020_allowances: int = 0, is_nonresident_alien: bool = False,
                      ca_filing_status: str = None, ca_regular_allowances: int = 0,
                      ca_estimated_deduction_allowances: int = 0, ca_extra_withholding: float = 0.0,
                      exempt_federal_income_tax: bool = False, exempt_social_security: bool = False,
                      exempt_medicare: bool = False, exempt_state_income_tax: bool = False,
                      exempt_sdi: bool = False,
                      pretax_401k_traditional: float = 0.0,
                      pretax_401k_age_50_or_older: bool = False,
                      pretax_hsa: float = 0.0, pretax_hsa_family_coverage: bool = False,
                      pretax_hsa_age_55_or_older: bool = False,
                      pretax_health_fsa: float = 0.0, pretax_dependent_care_fsa: float = 0.0,
                      pretax_medical: float = 0.0, pretax_dental: float = 0.0,
                      pretax_vision: float = 0.0, ytd_wages_before_this_period: float = None,
                      tax_year: int = PAYROLL_TAX_YEAR):
    """THE one function the Paycheck Calculator UI tab calls. Composes
    compute_pretax_benefits + compute_federal_withholding + compute_fica +
    compute_ca_withholding + compute_ca_sdi into one result. Returns None
    (never a fabricated number) if gross_pay is missing/non-positive,
    pay_frequency is unrecognized, elected pre-tax benefits exceed
    gross_pay, or any sub-computation returns None.

    The 5 `exempt_*` flags mirror the reference ADP calculator's own
    "Are you exempt from ... ?" toggles -- each short-circuits its own
    component to $0 (or, for federal/CA income tax, to just the stated
    extra-withholding amount) without skipping the OTHER components.

    `ca_filing_status` defaults to the SAME value as `filing_status` when
    not given (matching this function's original single-filing-status
    behavior exactly) -- pass it explicitly to model an employee whose
    CA DE-4 filing status genuinely differs from their federal W-4 filing
    status (the reference calculator asks these as two separate
    questions).

    The 10 `pretax_*` parameters (see compute_pretax_benefits) reduce
    FEDERAL, CALIFORNIA, and FICA taxable wages by potentially DIFFERENT
    amounts each -- CA SDI is deliberately left on unreduced gross_pay (an
    explicit, disclosed scope boundary, not an oversight). Take-home pay
    subtracts the FULL elected benefit total regardless of its tax
    treatment, since that money leaves the paycheck either way -- only
    whether it's taxable changes based on IRS annual limits.

    `ytd_wages_before_this_period` (see compute_fica) switches Social
    Security/Additional Medicare from the annualized-smoothing estimate to
    real marginal capping against a caller-stated cumulative FICA-wages
    total."""
    if gross_pay is None or gross_pay <= 0:
        return None
    pay_periods_per_year = PAY_PERIODS_PER_YEAR.get(pay_frequency)
    if not pay_periods_per_year:
        return None
    ca_filing_status = ca_filing_status or filing_status

    pretax = compute_pretax_benefits(
        conn, pay_periods_per_year, filing_status,
        pretax_401k_traditional=pretax_401k_traditional,
        pretax_401k_age_50_or_older=pretax_401k_age_50_or_older,
        pretax_hsa=pretax_hsa, pretax_hsa_family_coverage=pretax_hsa_family_coverage,
        pretax_hsa_age_55_or_older=pretax_hsa_age_55_or_older,
        pretax_health_fsa=pretax_health_fsa, pretax_dependent_care_fsa=pretax_dependent_care_fsa,
        pretax_medical=pretax_medical, pretax_dental=pretax_dental, pretax_vision=pretax_vision,
        tax_year=tax_year)
    if pretax is None:
        return None
    if pretax["total_elected_this_period"] > gross_pay:
        return None  # can't divert more to benefits than this paycheck actually pays

    federal_wages_this_period = max(0.0, gross_pay - pretax["federal_wage_reduction_this_period"])
    ca_wages_this_period = max(0.0, gross_pay - pretax["ca_wage_reduction_this_period"])
    fica_wages_this_period = max(0.0, gross_pay - pretax["fica_wage_reduction_this_period"])

    federal = compute_federal_withholding(
        conn, federal_wages_this_period, pay_periods_per_year, filing_status,
        w4_is_2020_or_later=w4_is_2020_or_later, step2_checkbox=step2_checkbox,
        step3_dependent_credits=step3_dependent_credits, step4a_other_income=step4a_other_income,
        step4b_deductions=step4b_deductions, step4c_extra_withholding=step4c_extra_withholding,
        pre2020_allowances=pre2020_allowances, is_nonresident_alien=is_nonresident_alien,
        exempt=exempt_federal_income_tax, tax_year=tax_year)
    if federal is None:
        return None

    fica = compute_fica(fica_wages_this_period, pay_periods_per_year,
                         exempt_social_security=exempt_social_security,
                         exempt_medicare=exempt_medicare,
                         ytd_wages_before_this_period=ytd_wages_before_this_period)
    if fica is None:
        return None

    ca = compute_ca_withholding(
        conn, ca_wages_this_period, pay_periods_per_year, ca_filing_status,
        regular_allowances=ca_regular_allowances,
        estimated_deduction_allowances=ca_estimated_deduction_allowances,
        extra_withholding=ca_extra_withholding, exempt=exempt_state_income_tax, tax_year=tax_year)
    if ca is None:
        return None

    sdi = compute_ca_sdi(conn, gross_pay, exempt=exempt_sdi, tax_year=tax_year)
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
    take_home_pay = round(gross_pay - pretax["total_elected_this_period"] - total_taxes, 2)

    assumptions = [
        "Assumes this pay amount is constant every period for the full year -- a mid-year raise, "
        "bonus, or job change will change your real annual totals. The Social Security wage base "
        "and Additional Medicare Tax threshold are ANNUAL caps applied here by annualizing this "
        "period's wages and dividing the result back down, not by tracking real year-to-date wages."
        if ytd_wages_before_this_period is None else
        "Social Security and Additional Medicare Tax are computed against your STATED "
        "year-to-date wages plus this paycheck (real marginal capping), not an annualized "
        "estimate -- federal and California withholding still assume this pay amount is "
        "constant for the rest of the year.",
        "Does not model local/city withholding (confirmed not applicable for California -- "
        "no CA city withholds employee-side income tax) or multi-state employment.",
    ]
    if pretax["total_elected_this_period"] > 0:
        assumptions.append(
            "Pre-tax benefit elections reduce federal, California, and/or FICA taxable wages "
            "depending on the benefit (see the Details section) -- EXCEPT: an HSA contribution "
            "reduces federal and FICA wages but NOT California wages (California does not "
            "conform to the federal HSA tax exclusion); and no pre-tax benefit reduces "
            "California SDI wages in this estimate (not confirmed one way or the other, left "
            "on the safe/higher side).")
        excess_benefits = [k for k, v in pretax["benefits"].items() if v["excess_this_period"] > 0]
        if excess_benefits:
            assumptions.append(
                f"Annual IRS limit exceeded for: {', '.join(excess_benefits)} -- the excess is "
                "treated as ordinary taxable wages, not pre-tax, for every basis that benefit "
                "would otherwise have reduced.")
    if pretax_401k_age_50_or_older and pretax_401k_traditional > 0:
        assumptions.append(
            "Only the standard $8,000 age-50+ 401(k) catch-up is modeled -- the narrower "
            "age-60-63 'super catch-up' ($11,250 instead) is not.")
    if ca_filing_status.lower() == "mfs":
        assumptions.append(
            "California's DE-4 form has no 'Married Filing Separately' option -- this estimate "
            "treats MFS the same as Single for CALIFORNIA withholding only (federal withholding "
            "does have its own combined Single/MFS bracket table, used correctly either way).")
    if is_nonresident_alien:
        assumptions.append(
            "Nonresident alien withholding adjustment (Pub 15-T) applied to FEDERAL income tax "
            "withholding only -- it does not affect Social Security, Medicare, or California "
            "withholding, per the same IRS guidance. This does not account for the narrow India "
            "student/business-apprentice tax-treaty exception, or any other tax treaty benefit -- "
            "consult a tax professional if either may apply.")

    exempt_labels = {
        exempt_federal_income_tax: "federal income tax",
        exempt_social_security: "Social Security",
        exempt_medicare: "Medicare",
        exempt_state_income_tax: "California state income tax",
        exempt_sdi: "California SDI",
    }
    active_exemptions = [label for flag, label in exempt_labels.items() if flag]
    if active_exemptions:
        assumptions.append(
            f"Exemption claimed for: {', '.join(active_exemptions)} -- withheld as $0 "
            "(plus any stated extra-withholding amount) for that component, as stated in the form.")

    return {
        "gross_pay": round(gross_pay, 2),
        "pay_frequency": pay_frequency,
        "pay_periods_per_year": pay_periods_per_year,
        "filing_status": filing_status,
        "ca_filing_status": ca_filing_status,
        "federal_withholding": federal_withholding,
        "social_security": social_security,
        "medicare": medicare,
        "additional_medicare": additional_medicare,
        "ca_withholding": ca_withholding,
        "ca_sdi": ca_sdi,
        "total_taxes": total_taxes,
        "pretax_benefits_this_period": pretax["total_elected_this_period"],
        "federal_wage_reduction_this_period": pretax["federal_wage_reduction_this_period"],
        "ca_wage_reduction_this_period": pretax["ca_wage_reduction_this_period"],
        "fica_wage_reduction_this_period": pretax["fica_wage_reduction_this_period"],
        "taxable_wages_this_period": {
            "federal": round(federal_wages_this_period, 2),
            "ca": round(ca_wages_this_period, 2),
            "fica": round(fica_wages_this_period, 2),
            "sdi": round(gross_pay, 2),
        },
        "take_home_pay": take_home_pay,
        "tax_year": tax_year,
        "federal_detail": federal,
        "fica_detail": fica,
        "ca_detail": ca,
        "sdi_detail": sdi,
        "pretax_benefits_detail": pretax,
        "ytd_wages_before_this_period": ytd_wages_before_this_period,
        "assumptions": assumptions,
    }
