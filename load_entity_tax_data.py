"""Ring 3, business entities Phase A -- entity-level California annual/
minimum tax and LLC fee data.

ALL figures below are hand-verified against primary FTB sources fetched
directly this session (NOT LLM-drafted, status='verified'):
  - General partnerships owe NO California annual tax; LPs/LLPs owe the
    $800 minimum annual tax; LLCs owe $800 + a tiered fee; S-corps owe
    $800 + 1.5% of net CA income (3.5% for financial S-corps).
    https://www.ftb.ca.gov/file/business/types/partnerships.html
    ("General partnerships do not pay annual tax; however, limited
    partnerships are subject to the annual tax of $800.")
    https://www.ftb.ca.gov/file/business/types/limited-liability-company/limited-liability-partnership.html
    ("LLPs do not pay income tax but they are subject to the annual tax
    of $800.")
  - LLC $800 annual tax + fee schedule, Form 568:
    https://www.ftb.ca.gov/file/business/types/limited-liability-company/index.html
    Fee tiers: $0 under $250,000; $900 $250,000-$499,999; $2,500
    $500,000-$999,999; $6,000 $1,000,000-$4,999,999; $11,790 $5,000,000+.
    Single-member/disregarded LLCs get IDENTICAL treatment: "We require an
    SMLLC to file Form 568, even though they are considered a disregarded
    entity for tax purposes. They are subject to the annual tax, LLC fee
    and credit limitations."
  - S-corp rate + minimum tax, Form 100S:
    https://www.ftb.ca.gov/file/business/tax-rates.html
    https://www.ftb.ca.gov/file/business/types/corporations/s-corporations.html
    ("We tax every S corporation that has California source income 1.5%.")
  - C-corp rate + minimum tax, Form 100 (added 2026-08-13, same session --
    verified via https://www.ftb.ca.gov/file/business/tax-rates.html:
    "Corporations other than banks and financials 8.84%" / "Banks and
    financials 10.84%").
  - First-year $800 waiver (PERMANENT, still active for 2025): entities
    formed/qualified on or after 2020-01-01. Re-verified this session --
    this is a GENERAL CORPORATION rule, NOT S-corp-specific as originally
    worded here: https://www.ftb.ca.gov/file/business/types/corporations/index.html
    ("Every corporation that is incorporated, registered, or doing
    business in California must pay the $800 minimum franchise tax...
    On or after January 1, 2020, newly incorporated or qualified
    corporations are not required to pay the minimum franchise tax in
    their first taxable year.") Applies to C-corps and S-corps alike. The
    income tax itself (1.5%/3.5%/8.84%/10.84%) still applies in year one
    -- only the $800 floor is waived.
  - LLC/LP/LLP first-year waiver (AB 85) has EXPIRED for tax years
    beginning on/after 2024-01-01 -- a 2025-formed LLC/LP/LLP owes the
    full $800 in year one. Same LLC page: "For tax years beginning on or
    after January 1, 2021, and before January 1, 2024" (i.e. not 2025).

NOT MODELED (disclosed scope limit, same discipline as every other feature
this session): multi-state apportionment (Schedule R), combined/unitary
group returns, the 15-day short-period exception (CA tax year <=15 days +
no CA business), and the 6.65% Alternative Minimum Tax (AMT) -- all real
FTB rules, none attempted in this pass.

Usage:
  python load_entity_tax_data.py load     # insert all Phase A data (idempotent)
  python load_entity_tax_data.py status
"""
import sys

import income_db as db

TAX_YEAR = 2025

PARTNERSHIPS_URL = "https://www.ftb.ca.gov/file/business/types/partnerships.html"
LLP_URL = "https://www.ftb.ca.gov/file/business/types/limited-liability-company/limited-liability-partnership.html"
LLC_URL = "https://www.ftb.ca.gov/file/business/types/limited-liability-company/index.html"
S_CORP_URL = "https://www.ftb.ca.gov/file/business/types/corporations/s-corporations.html"
S_CORP_WAIVER_URL = "https://www.ftb.ca.gov/file/business/types/corporations/index.html"
RATES_URL = "https://www.ftb.ca.gov/file/business/tax-rates.html"

# (entity_type, annual_tax, income_tax_rate, first_year_waiver, form_number, citation, source_url)
ENTITY_RULES = [
    ("general_partnership", 0, None, False, "565",
     "FTB: General partnerships do not pay annual tax", PARTNERSHIPS_URL),
    ("lp", 800, None, False, "565",
     "FTB: limited partnerships are subject to the annual tax of $800", PARTNERSHIPS_URL),
    ("llp", 800, None, False, "565",
     "FTB: LLPs do not pay income tax but are subject to the annual tax of $800", LLP_URL),
    ("llc", 800, None, False, "568",
     "FTB: LLC $800 annual tax (AB 85 first-year waiver expired for 2024+)", LLC_URL),
    ("s_corp", 800, 0.015, True, "100S",
     "FTB: 1.5% of net CA income; $800 minimum tax waived first year (formed/qualified 2020+)",
     S_CORP_URL),
    ("s_corp_financial", 800, 0.035, True, "100S",
     "FTB: 3.5% of net CA income for financial S-corps; $800 minimum tax waived first year",
     S_CORP_URL),
    ("c_corp", 800, 0.0884, True, "100",
     "FTB: 8.84% of net CA income; $800 minimum tax waived first year (formed/qualified 2020+)",
     RATES_URL),
    ("c_corp_financial", 800, 0.1084, True, "100",
     "FTB: 10.84% of net CA income for banks and financial C-corps; $800 minimum tax waived first year",
     RATES_URL),
]

# (income_floor, income_ceiling, fee_amount)
LLC_FEE_TIERS = [
    (0, 249999.99, 0),
    (250000, 499999.99, 900),
    (500000, 999999.99, 2500),
    (1000000, 4999999.99, 6000),
    (5000000, None, 11790),
]


def load():
    conn = db.get_conn()
    n_rules = 0
    for entity_type, annual_tax, rate, waiver, form, citation, url in ENTITY_RULES:
        conn.execute(
            "INSERT INTO entity_annual_tax_rules "
            "(tax_year, entity_type, annual_tax, income_tax_rate, first_year_waiver, "
            "form_number, citation, source_url, as_of) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_DATE) "
            "ON CONFLICT (tax_year, entity_type) DO UPDATE SET "
            "annual_tax=EXCLUDED.annual_tax, income_tax_rate=EXCLUDED.income_tax_rate, "
            "first_year_waiver=EXCLUDED.first_year_waiver, form_number=EXCLUDED.form_number, "
            "citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, entity_type, annual_tax, rate, waiver, form, citation, url))
        n_rules += 1

    n_fees = 0
    for floor, ceiling, fee in LLC_FEE_TIERS:
        conn.execute(
            "INSERT INTO llc_fee_brackets (tax_year, income_floor, income_ceiling, fee_amount, "
            "citation, source_url, as_of) VALUES (%s,%s,%s,%s,%s,%s, CURRENT_DATE) "
            "ON CONFLICT (tax_year, income_floor) DO UPDATE SET "
            "income_ceiling=EXCLUDED.income_ceiling, fee_amount=EXCLUDED.fee_amount",
            (TAX_YEAR, floor, ceiling, fee, "FTB 2025 LLC fee schedule", LLC_URL))
        n_fees += 1

    print(f"loaded {n_rules} entity_annual_tax_rules rows, {n_fees} llc_fee_brackets rows "
          f"for tax_year={TAX_YEAR}")
    conn.close()


def status():
    conn = db.get_conn()
    for tbl in ("entity_annual_tax_rules", "llc_fee_brackets"):
        n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:26} {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
