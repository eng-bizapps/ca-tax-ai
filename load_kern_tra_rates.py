"""Seeds tra_rates from kern_tra_rates.csv (produced by extract_kern_tra_
rates.py -- see that script's own docstring for the extraction methodology
and its validation). Mirrors load_county_override_rates.py's shape: schema
lives in property_schema.py, this file owns the externally-sourced dataset
and its own idempotent upsert.

NARROW SCOPE, worth repeating here too: this is a Kern-County-only pilot,
not a general 58-county solution -- see property_tax_inventory.py's
'tra-rate-kern-pilot' item.

VINTAGE: Kern's "BILLING YEAR 2025-2026" == lien year 2025 in this
codebase's own DEFAULT_LIEN_YEAR convention -- same reasoning as
load_county_override_rates.py's own TAX_YEAR choice.

REFRESH CADENCE: Kern publishes a new rate book annually (its own board
resolution is typically adopted in the fall, per the same cadence other
counties' own resolutions follow). Refresh by re-running extract_kern_tra_
rates.py against the new year's PDF (update KERN_RATE_BOOK_URL there),
regenerating kern_tra_rates.csv, then re-running this loader with a bumped
TAX_YEAR -- never guess a vintage forward.

Usage:
  python load_kern_tra_rates.py load     # upsert all rows from the CSV
  python load_kern_tra_rates.py status   # row count
"""
import csv
import sys

import property_db as db
import property_tax

TAX_YEAR = 2025  # the LIEN YEAR this FY2025-26 rate book represents -- see module docstring
AS_OF = "2026-09-28"
KERN_TRA_CITATION = "Kern County Auditor-Controller-County Clerk, Annual Property Tax Rate Book, FY2025-26"
KERN_TRA_SOURCE_URL = "https://www.auditor.co.kern.ca.us/RateBook/TaxRates2526.pdf"
CSV_PATH = "kern_tra_rates.csv"
COUNTY = "Kern"


def _read_csv():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load():
    if COUNTY not in property_tax.CA_COUNTIES:
        raise ValueError(f"not a recognized CA county: {COUNTY}")
    rows = _read_csv()
    with db.get_conn() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO tra_rates "
                "(tax_year, county, tra_number, area_name, total_rate, citation, source_url, as_of) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tax_year, county, tra_number) DO UPDATE SET "
                "area_name=EXCLUDED.area_name, total_rate=EXCLUDED.total_rate, "
                "citation=EXCLUDED.citation, source_url=EXCLUDED.source_url, as_of=EXCLUDED.as_of",
                (TAX_YEAR, COUNTY, row["tra_number"], row["area_name"], row["total_rate"],
                 KERN_TRA_CITATION, KERN_TRA_SOURCE_URL, AS_OF))
    print(f"loaded {len(rows)} tra_rates rows for county={COUNTY} tax_year={TAX_YEAR}")
    status()


def status():
    conn = db.get_conn()
    n = conn.execute("SELECT count(*) FROM tra_rates WHERE county=%s AND tax_year=%s",
                      (COUNTY, TAX_YEAR)).fetchone()[0]
    print(f"  tra_rates (county={COUNTY}, tax_year={TAX_YEAR})   {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
