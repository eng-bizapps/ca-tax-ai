"""Ring 2 extension -- per-PAYCHECK federal + California withholding
reference data (distinct from income_brackets.py's ANNUAL tax-LIABILITY
figures). ALL numbers below were pulled directly from primary-source .gov
documents this session (NOT LLM-drafted, NOT a secondary aggregator):

  - Federal income tax withholding: IRS Publication 15-T (2026), final,
    dated Dec 3 2025, Worksheet 1A ("Employer's Withholding Worksheet for
    Percentage Method Tables for Automated Payroll Systems"), p.12's
    "Percentage Method Tables" (both the Standard schedule and the
    Form W-4 Step-2-Checkbox schedule).
    https://www.irs.gov/pub/irs-pdf/p15t.pdf
  - California withholding: EDD "California Withholding Schedules for
    2026," Method B -- Exact Calculation Method, Tables 1-7 (annual
    values; EDD's own document explicitly sanctions computing the annual
    tax and dividing by the number of pay periods as equivalent to using
    period-specific tables directly -- see payroll_withholding.py).
    https://edd.ca.gov/siteassets/files/pdf_pub_ctr/26methb.pdf
  - California SDI: EDD "Contribution Rates and Benefit Amounts" page --
    2026 employee rate is 1.3% (up from 1.2% in 2025), confirmed NO wage
    cap since SB 951 eliminated the taxable wage ceiling effective
    2024-01-01, still in effect for 2026.
    https://edd.ca.gov/en/disability/Contribution_Rates_and_Benefit_Amounts/

FICA (Social Security/Medicare) is deliberately NOT here -- it comes from
SSA.gov/IRS Form 8959, not FTB/EDD, and is a hardcoded Python constant in
payroll_withholding.py, same precedent as income_brackets.py's own
SE_SOCIAL_SECURITY_WAGE_BASE.

Usage:
  python load_payroll_withholding_data.py load     # insert all 2026 data (idempotent)
  python load_payroll_withholding_data.py status
"""
import sys

import income_db as db

TAX_YEAR = 2026

IRS_15T_URL = "https://www.irs.gov/pub/irs-pdf/p15t.pdf"
IRS_15T_CITATION = "IRS Publication 15-T (2026), Worksheet 1A"
IRS_15T_NRA_CITATION = "IRS Publication 15-T (2026), Withholding Adjustment for Nonresident Alien Employees"
EDD_METHOD_B_URL = "https://edd.ca.gov/siteassets/files/pdf_pub_ctr/26methb.pdf"
EDD_METHOD_B_CITATION = "EDD California Withholding Schedules for 2026, Method B"
EDD_SDI_URL = "https://edd.ca.gov/en/disability/Contribution_Rates_and_Benefit_Amounts/"
EDD_SDI_CITATION = "EDD Contribution Rates and Benefit Amounts, 2026"

IRS_401K_URL = "https://www.irs.gov/newsroom/401k-limit-increases-to-24500-for-2026-ira-limit-increases-to-7500"
IRS_401K_CITATION = "IRS Notice 2025-67 (2026 401(k) elective deferral / catch-up limits)"
IRS_HSA_URL = "https://www.irs.gov/publications/p969"
IRS_HSA_CITATION = "IRS Publication 969 (2026), consistent with Rev. Proc. 2025-19 HSA limits"
IRS_15B_URL = "https://www.irs.gov/publications/p15b"
IRS_15B_CITATION = "IRS Publication 15-B (2026), Health FSA / Dependent Care FSA limits"

# (filing_status, schedule, bracket_floor, bracket_ceiling, base_amount, rate)
# filing_status: 'single_mfs' | 'mfj' | 'hoh'; schedule: 'standard' | 'step2_checkbox'
FEDERAL_WITHHOLDING_BRACKETS = [
    # --- Standard schedule (Step 2 box NOT checked, or a pre-2020 W-4) ---
    ("single_mfs", "standard", 0, 7500, 0.00, 0.00),
    ("single_mfs", "standard", 7500, 19900, 0.00, 0.10),
    ("single_mfs", "standard", 19900, 57900, 1240.00, 0.12),
    ("single_mfs", "standard", 57900, 113200, 5800.00, 0.22),
    ("single_mfs", "standard", 113200, 209275, 17966.00, 0.24),
    ("single_mfs", "standard", 209275, 263725, 41024.00, 0.32),
    ("single_mfs", "standard", 263725, 648100, 58448.00, 0.35),
    ("single_mfs", "standard", 648100, None, 192979.25, 0.37),

    ("mfj", "standard", 0, 19300, 0.00, 0.00),
    ("mfj", "standard", 19300, 44100, 0.00, 0.10),
    ("mfj", "standard", 44100, 120100, 2480.00, 0.12),
    ("mfj", "standard", 120100, 230700, 11600.00, 0.22),
    ("mfj", "standard", 230700, 422850, 35932.00, 0.24),
    ("mfj", "standard", 422850, 531750, 82048.00, 0.32),
    ("mfj", "standard", 531750, 788000, 116896.00, 0.35),
    ("mfj", "standard", 788000, None, 206583.50, 0.37),

    ("hoh", "standard", 0, 15550, 0.00, 0.00),
    ("hoh", "standard", 15550, 33250, 0.00, 0.10),
    ("hoh", "standard", 33250, 83000, 1770.00, 0.12),
    ("hoh", "standard", 83000, 121250, 7740.00, 0.22),
    ("hoh", "standard", 121250, 217300, 16155.00, 0.24),
    ("hoh", "standard", 217300, 271750, 39207.00, 0.32),
    ("hoh", "standard", 271750, 656150, 56631.00, 0.35),
    ("hoh", "standard", 656150, None, 191171.00, 0.37),

    # --- Step-2-checkbox schedule (2020+ W-4 with Step 2 box checked) ---
    ("single_mfs", "step2_checkbox", 0, 8050, 0.00, 0.00),
    ("single_mfs", "step2_checkbox", 8050, 14250, 0.00, 0.10),
    ("single_mfs", "step2_checkbox", 14250, 33250, 620.00, 0.12),
    ("single_mfs", "step2_checkbox", 33250, 60900, 2900.00, 0.22),
    ("single_mfs", "step2_checkbox", 60900, 108938, 8983.00, 0.24),
    ("single_mfs", "step2_checkbox", 108938, 136163, 20512.00, 0.32),
    ("single_mfs", "step2_checkbox", 136163, 328350, 29224.00, 0.35),
    ("single_mfs", "step2_checkbox", 328350, None, 96489.63, 0.37),

    ("mfj", "step2_checkbox", 0, 16100, 0.00, 0.00),
    ("mfj", "step2_checkbox", 16100, 28500, 0.00, 0.10),
    ("mfj", "step2_checkbox", 28500, 66500, 1240.00, 0.12),
    ("mfj", "step2_checkbox", 66500, 121800, 5800.00, 0.22),
    ("mfj", "step2_checkbox", 121800, 217875, 17966.00, 0.24),
    ("mfj", "step2_checkbox", 217875, 272325, 41024.00, 0.32),
    ("mfj", "step2_checkbox", 272325, 400450, 58448.00, 0.35),
    ("mfj", "step2_checkbox", 400450, None, 103291.75, 0.37),

    ("hoh", "step2_checkbox", 0, 12075, 0.00, 0.00),
    ("hoh", "step2_checkbox", 12075, 20925, 0.00, 0.10),
    ("hoh", "step2_checkbox", 20925, 45800, 885.00, 0.12),
    ("hoh", "step2_checkbox", 45800, 64925, 3870.00, 0.22),
    ("hoh", "step2_checkbox", 64925, 112950, 8077.50, 0.24),
    ("hoh", "step2_checkbox", 112950, 140175, 19603.50, 0.32),
    ("hoh", "step2_checkbox", 140175, 332375, 28315.50, 0.35),
    ("hoh", "step2_checkbox", 332375, None, 95585.50, 0.37),
]

# (constant_type, filing_status, amount) -- filing_status=None applies regardless
FEDERAL_WITHHOLDING_CONSTANTS = [
    ("step2_unchecked_deduction", "mfj", 12900.00),
    ("step2_unchecked_deduction", "single_mfs", 8600.00),
    ("step2_unchecked_deduction", "hoh", 8600.00),
    ("pre2020_allowance", None, 4300.00),
]

# Pub 15-T (2026) p.6-7, "Withholding Adjustment for Nonresident Alien
# Employees," Table 1 (pre-2020 W-4) and Table 2 (2020+ W-4).
# (w4_vintage, pay_periods_per_year, amount)
FEDERAL_NRA_WAGE_ADDITIONS = [
    ("pre2020", 52, 226.90), ("pre2020", 26, 453.80), ("pre2020", 24, 491.70),
    ("pre2020", 12, 983.30), ("pre2020", 4, 2950.00), ("pre2020", 2, 5900.00),
    ("pre2020", 1, 11800.00), ("pre2020", 260, 45.40),
    ("2020plus", 52, 309.60), ("2020plus", 26, 619.20), ("2020plus", 24, 670.80),
    ("2020plus", 12, 1341.70), ("2020plus", 4, 4025.00), ("2020plus", 2, 8050.00),
    ("2020plus", 1, 16100.00), ("2020plus", 260, 61.90),
]

# (rate_category, bracket_floor, bracket_ceiling, base_amount, rate)
# rate_category: 'single_dual_multiple' | 'married' | 'unmarried_hoh'
CA_WITHHOLDING_BRACKETS = [
    ("single_dual_multiple", 0, 11079, 0.00, 0.011),
    ("single_dual_multiple", 11079, 26264, 121.87, 0.022),
    ("single_dual_multiple", 26264, 41452, 455.94, 0.044),
    ("single_dual_multiple", 41452, 57542, 1124.21, 0.066),
    ("single_dual_multiple", 57542, 72724, 2186.15, 0.088),
    ("single_dual_multiple", 72724, 371479, 3522.17, 0.1023),
    ("single_dual_multiple", 371479, 445771, 34084.81, 0.1133),
    ("single_dual_multiple", 445771, 742953, 42502.09, 0.1243),
    ("single_dual_multiple", 742953, 1000000, 79441.81, 0.1353),
    ("single_dual_multiple", 1000000, None, 114220.27, 0.1463),

    ("married", 0, 22158, 0.00, 0.011),
    ("married", 22158, 52528, 243.74, 0.022),
    ("married", 52528, 82904, 911.88, 0.044),
    ("married", 82904, 115084, 2248.42, 0.066),
    ("married", 115084, 145448, 4372.30, 0.088),
    ("married", 145448, 742958, 7044.33, 0.1023),
    ("married", 742958, 891542, 68169.60, 0.1133),
    ("married", 891542, 1000000, 85004.17, 0.1243),
    ("married", 1000000, 1485906, 98485.50, 0.1353),
    ("married", 1485906, None, 164228.58, 0.1463),

    ("unmarried_hoh", 0, 22173, 0.00, 0.011),
    ("unmarried_hoh", 22173, 52530, 243.90, 0.022),
    ("unmarried_hoh", 52530, 67716, 911.75, 0.044),
    ("unmarried_hoh", 67716, 83805, 1579.93, 0.066),
    ("unmarried_hoh", 83805, 98990, 2641.80, 0.088),
    ("unmarried_hoh", 98990, 505208, 3978.08, 0.1023),
    ("unmarried_hoh", 505208, 606251, 45534.18, 0.1133),
    ("unmarried_hoh", 606251, 1000000, 56982.35, 0.1243),
    ("unmarried_hoh", 1000000, 1010417, 105925.35, 0.1353),
    ("unmarried_hoh", 1010417, None, 107334.77, 0.1463),
]

# (constant_type, exemption_category, amount) -- exemption_category=None
# applies regardless of category (the 2 per-allowance constant_types).
# exemption_category: 'single_dual_multiple' | 'married_0_1' | 'married_2_plus' | 'unmarried_hoh'
CA_WITHHOLDING_CONSTANTS = [
    ("low_income_exemption", "single_dual_multiple", 18896.00),
    ("low_income_exemption", "married_0_1", 18896.00),
    ("low_income_exemption", "married_2_plus", 37791.00),
    ("low_income_exemption", "unmarried_hoh", 37791.00),

    ("standard_deduction", "single_dual_multiple", 5706.00),
    ("standard_deduction", "married_0_1", 5706.00),
    ("standard_deduction", "married_2_plus", 11412.00),
    ("standard_deduction", "unmarried_hoh", 11412.00),

    ("exemption_credit_per_allowance", None, 168.30),
    ("estimated_deduction_per_allowance", None, 1000.00),
]

CA_SDI_RATE = 0.013  # 2026, up from 0.012 (2025); no wage cap (SB 951)

# 2026 pre-tax benefit annual contribution/election limits.
# (benefit_type, amount, citation, source_url)
PRETAX_BENEFIT_LIMITS = [
    ("401k_elective_deferral", 24500.00, IRS_401K_CITATION, IRS_401K_URL),
    ("401k_catchup_50", 8000.00, IRS_401K_CITATION, IRS_401K_URL),
    ("hsa_self_only", 4400.00, IRS_HSA_CITATION, IRS_HSA_URL),
    ("hsa_family", 8750.00, IRS_HSA_CITATION, IRS_HSA_URL),
    ("hsa_catchup_55", 1000.00, IRS_HSA_CITATION, IRS_HSA_URL),
    ("health_fsa", 3400.00, IRS_15B_CITATION, IRS_15B_URL),
    ("dependent_care_fsa", 7500.00, IRS_15B_CITATION, IRS_15B_URL),
    ("dependent_care_fsa_mfs", 3750.00, IRS_15B_CITATION, IRS_15B_URL),
]


def load():
    conn = db.get_conn()
    n_fed_brackets = 0
    for filing_status, schedule, floor, ceiling, base, rate in FEDERAL_WITHHOLDING_BRACKETS:
        conn.execute(
            "INSERT INTO federal_withholding_brackets "
            "(tax_year, filing_status, schedule, bracket_floor, bracket_ceiling, "
            "base_amount, rate, citation, source_url) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tax_year, filing_status, schedule, bracket_floor) DO UPDATE SET "
            "bracket_ceiling=EXCLUDED.bracket_ceiling, base_amount=EXCLUDED.base_amount, "
            "rate=EXCLUDED.rate, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, filing_status, schedule, floor, ceiling, base, rate,
             IRS_15T_CITATION, IRS_15T_URL))
        n_fed_brackets += 1

    n_fed_constants = 0
    for constant_type, filing_status, amount in FEDERAL_WITHHOLDING_CONSTANTS:
        if filing_status is None:
            # Postgres does NOT treat NULL=NULL for UNIQUE-constraint conflict
            # detection, so ON CONFLICT below silently INSERTS a fresh
            # duplicate every re-run for a NULL-keyed row instead of updating
            # the existing one -- confirmed live (3 duplicate 'pre2020_
            # allowance' rows accumulated across 3 earlier `load` runs this
            # session before this fix). Delete any existing NULL-keyed row
            # for this constant_type first, so the INSERT below always
            # starts from zero matching rows.
            conn.execute(
                "DELETE FROM federal_withholding_constants "
                "WHERE tax_year=%s AND constant_type=%s AND filing_status IS NULL",
                (TAX_YEAR, constant_type))
        conn.execute(
            "INSERT INTO federal_withholding_constants "
            "(tax_year, constant_type, filing_status, amount, citation, source_url) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tax_year, constant_type, filing_status) DO UPDATE SET "
            "amount=EXCLUDED.amount, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, constant_type, filing_status, amount, IRS_15T_CITATION, IRS_15T_URL))
        n_fed_constants += 1

    n_nra = 0
    for w4_vintage, periods, amount in FEDERAL_NRA_WAGE_ADDITIONS:
        conn.execute(
            "INSERT INTO federal_nra_wage_additions "
            "(tax_year, w4_vintage, pay_periods_per_year, amount, citation, source_url) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tax_year, w4_vintage, pay_periods_per_year) DO UPDATE SET "
            "amount=EXCLUDED.amount, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, w4_vintage, periods, amount, IRS_15T_NRA_CITATION, IRS_15T_URL))
        n_nra += 1

    n_ca_brackets = 0
    for rate_category, floor, ceiling, base, rate in CA_WITHHOLDING_BRACKETS:
        conn.execute(
            "INSERT INTO ca_withholding_brackets "
            "(tax_year, rate_category, bracket_floor, bracket_ceiling, base_amount, rate, "
            "citation, source_url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tax_year, rate_category, bracket_floor) DO UPDATE SET "
            "bracket_ceiling=EXCLUDED.bracket_ceiling, base_amount=EXCLUDED.base_amount, "
            "rate=EXCLUDED.rate, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, rate_category, floor, ceiling, base, rate,
             EDD_METHOD_B_CITATION, EDD_METHOD_B_URL))
        n_ca_brackets += 1

    n_ca_constants = 0
    for constant_type, exemption_category, amount in CA_WITHHOLDING_CONSTANTS:
        if exemption_category is None:
            # Same NULL-uniqueness fix as federal_withholding_constants above.
            conn.execute(
                "DELETE FROM ca_withholding_constants "
                "WHERE tax_year=%s AND constant_type=%s AND exemption_category IS NULL",
                (TAX_YEAR, constant_type))
        conn.execute(
            "INSERT INTO ca_withholding_constants "
            "(tax_year, constant_type, exemption_category, amount, citation, source_url) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tax_year, constant_type, exemption_category) DO UPDATE SET "
            "amount=EXCLUDED.amount, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, constant_type, exemption_category, amount,
             EDD_METHOD_B_CITATION, EDD_METHOD_B_URL))
        n_ca_constants += 1

    conn.execute(
        "INSERT INTO ca_sdi_rate (tax_year, rate, citation, source_url) "
        "VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (tax_year) DO UPDATE SET rate=EXCLUDED.rate, "
        "citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
        (TAX_YEAR, CA_SDI_RATE, EDD_SDI_CITATION, EDD_SDI_URL))

    n_benefit_limits = 0
    for benefit_type, amount, citation, url in PRETAX_BENEFIT_LIMITS:
        conn.execute(
            "INSERT INTO pretax_benefit_limits (tax_year, benefit_type, amount, citation, source_url) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (tax_year, benefit_type) DO UPDATE SET "
            "amount=EXCLUDED.amount, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, benefit_type, amount, citation, url))
        n_benefit_limits += 1

    conn.close()
    print(f"loaded {n_fed_brackets} federal_withholding_brackets rows, "
          f"{n_fed_constants} federal_withholding_constants rows, "
          f"{n_nra} federal_nra_wage_additions rows, "
          f"{n_ca_brackets} ca_withholding_brackets rows, "
          f"{n_ca_constants} ca_withholding_constants rows, "
          f"1 ca_sdi_rate row, {n_benefit_limits} pretax_benefit_limits rows "
          f"for tax_year={TAX_YEAR}")


def status():
    conn = db.get_conn()
    for tbl in ("federal_withholding_brackets", "federal_withholding_constants",
                "federal_nra_wage_additions",
                "ca_withholding_brackets", "ca_withholding_constants", "ca_sdi_rate",
                "pretax_benefit_limits"):
        n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:30} {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
