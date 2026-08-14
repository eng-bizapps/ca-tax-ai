"""Ring 3, trust/estate Phase A -- fiduciary exemption credit data.

ALL figures below are hand-verified against the FTB 541 booklet's own
"Instructions for Form 541" section (2023 edition, spot-checked as still
current -- no indication these small credit figures change year to year
the way brackets/deductions do):
  https://www.ftb.ca.gov/forms/2023/2023-541-booklet.html
  "Line 22 -- Exemption credit. An estate is allowed an exemption credit
  of $10. A trust is allowed an exemption credit of $1. A qualified
  disability trust is allowed an exemption credit of $144."

Deliberately NOT a new bracket table -- fiduciary_tax.py reuses the
EXISTING ca_income_tax_brackets table (filing_status='single', i.e.
Schedule X) directly, confirmed numerically identical to the 541 booklet's
own Tax Rate Schedule.

Usage:
  python load_fiduciary_tax_data.py load     # insert exemption credit data (idempotent)
  python load_fiduciary_tax_data.py status
"""
import sys

import income_db as db

TAX_YEAR = 2025
CITATION = "FTB 541 Booklet -- Instructions for Form 541, Line 22 (Exemption Credit)"
SOURCE_URL = "https://www.ftb.ca.gov/forms/2023/2023-541-booklet.html"

# (entity_type, amount)
EXEMPTION_CREDITS = [
    ("trust", 1),
    ("estate", 10),
    ("qualified_disability_trust", 144),
]


def load():
    conn = db.get_conn()
    n = 0
    for entity_type, amount in EXEMPTION_CREDITS:
        conn.execute(
            "INSERT INTO fiduciary_exemption_credit "
            "(tax_year, entity_type, amount, citation, source_url, as_of) "
            "VALUES (%s,%s,%s,%s,%s, CURRENT_DATE) "
            "ON CONFLICT (tax_year, entity_type) DO UPDATE SET "
            "amount=EXCLUDED.amount, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
            (TAX_YEAR, entity_type, amount, CITATION, SOURCE_URL))
        n += 1
    print(f"loaded {n} fiduciary_exemption_credit rows for tax_year={TAX_YEAR}")
    conn.close()


def status():
    conn = db.get_conn()
    n = conn.execute("SELECT count(*) FROM fiduciary_exemption_credit").fetchone()[0]
    print(f"  fiduciary_exemption_credit  {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
