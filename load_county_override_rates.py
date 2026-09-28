"""Seeds county_override_rates -- one row per CA county's voter-approved
local ad-valorem OVERRIDE rate on top of the 1% Prop 13 base, sourced from
the CA State Controller's Office "CA Property Tax Data" portal
(propertytax.bythenumbers.sco.ca.gov). Mirrors load_payroll_withholding_
data.py's shape: schema lives in property_schema.py, this file owns the
externally-sourced dataset and its own idempotent upsert.

DATA SOURCE AND METHODOLOGY (verified live, not guessed): the portal's
JSON API (api/revenue/chart_data.json for "Allocations" -- the 1% base,
letting you back out each county's total assessed value via
allocations/0.01 -- and api/opex/chart_data.json for "Levies" -- voter-
approved local overrides) gives per-county totals for all 58 counties.
override_rate = levy / (allocations / 0.01). 56 of 58 counties land in a
smooth, plausible 0.001%-0.25% range. San Benito (5.32%) and Plumas
(1.00%) were confirmed DATA ERRORS -- independently cross-checked against
San Benito's own bond tax-rate statement (~0.025%, normal scale) and
Plumas's own county resolution (countywide bond measures total 0.042%,
~24x below the portal's implied 0.996%) -- deliberately EXCLUDED here,
not guessed at. The portal's own glossary disclaims responsibility for
the accuracy of county-submitted data ("posted as submitted by each local
government... not responsible for the accuracy").

VINTAGE: the portal labels FY2025-26 as "year 2026" (fiscal year's ENDING
calendar year) -- a DIFFERENT convention from property_tax.DEFAULT_LIEN_
YEAR (the lien DATE's calendar year, where lien year 2026 == FY2026-27).
FY2025-26 == lien year 2025 in this codebase's terms. TAX_YEAR below is
deliberately 2025, not 2026 -- get this backwards and every row silently
claims to be a fiscal year's data that doesn't exist yet.

REFRESH CADENCE: county bond resolutions are typically adopted Aug-Sept
for that fiscal year's bills (confirmed live via Plumas's own resolution,
adopted "September 8"), and the SCO portal aggregates afterward -- so a
new vintage won't be available until well into the following calendar
year. Refresh by re-running this script with a new TAX_YEAR/dataset once
a genuinely newer vintage is confirmed via the portal directly -- never
guess a vintage forward, same "bump each cycle, not before" precedent as
property_tax.DEFAULT_LIEN_YEAR.

Usage:
  python load_county_override_rates.py load     # upsert all rows
  python load_county_override_rates.py status    # row count
"""
import sys

import property_db as db
import property_tax

TAX_YEAR = 2025  # the LIEN YEAR this FY2025-26 SCO snapshot represents -- see module docstring
AS_OF = "2026-09-28"
SCO_CITATION = ('California State Controller\'s Office, "CA Property Tax Data" portal, '
                 'FY2025-26 Allocations + Levies by county')
SCO_SOURCE_URL = "https://propertytax.bythenumbers.sco.ca.gov/"

# (county, override_rate) -- fraction form (0.002529 == 0.2529%). San
# Benito and Plumas deliberately excluded -- see module docstring.
COUNTY_OVERRIDE_RATES = [
    ("Kern", 0.002529), ("Alameda", 0.002294), ("Fresno", 0.002222),
    ("Santa Clara", 0.002051), ("San Diego", 0.001869), ("Los Angeles", 0.001827),
    ("San Francisco", 0.001821), ("Riverside", 0.001715), ("Imperial", 0.001679),
    ("Mendocino", 0.001592), ("Contra Costa", 0.001524), ("Sacramento", 0.0015),
    ("San Bernardino", 0.001468), ("Sonoma", 0.001457), ("Marin", 0.001445),
    ("Butte", 0.0014), ("Solano", 0.001395), ("Santa Cruz", 0.001372),
    ("San Joaquin", 0.001335), ("Monterey", 0.001318), ("Stanislaus", 0.001283),
    ("Mono", 0.001249), ("Colusa", 0.001233), ("San Mateo", 0.00123),
    ("Madera", 0.001194), ("Ventura", 0.001187), ("Humboldt", 0.001175),
    ("Napa", 0.00116), ("Yolo", 0.001119), ("Inyo", 0.00108),
    ("Lake", 0.001061), ("Shasta", 0.001048), ("Tulare", 0.001024),
    ("Glenn", 0.001005), ("Merced", 0.000989), ("Calaveras", 0.000982),
    ("San Luis Obispo", 0.00097), ("Yuba", 0.000876), ("Sutter", 0.000858),
    ("Kings", 0.000768), ("Santa Barbara", 0.000718), ("El Dorado", 0.000703),
    ("Orange", 0.000693), ("Tuolumne", 0.000679), ("Placer", 0.000597),
    ("Tehama", 0.000533), ("Del Norte", 0.000518), ("Nevada", 0.000516),
    ("Siskiyou", 0.000398), ("Mariposa", 0.000362), ("Alpine", 0.000302),
    ("Trinity", 0.000281), ("Lassen", 0.000208), ("Amador", 0.00011),
    ("Sierra", 0.000022), ("Modoc", 0.000011),
]


def load():
    unknown = [c for c, _ in COUNTY_OVERRIDE_RATES if c not in property_tax.CA_COUNTIES]
    if unknown:
        raise ValueError(f"not a recognized CA county (typo?): {unknown}")
    with db.get_conn() as conn:
        for county, override_rate in COUNTY_OVERRIDE_RATES:
            conn.execute(
                "INSERT INTO county_override_rates "
                "(tax_year, county, override_rate, citation, source_url, as_of) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tax_year, county) DO UPDATE SET "
                "override_rate=EXCLUDED.override_rate, citation=EXCLUDED.citation, "
                "source_url=EXCLUDED.source_url, as_of=EXCLUDED.as_of",
                (TAX_YEAR, county, override_rate, SCO_CITATION, SCO_SOURCE_URL, AS_OF))
    print(f"loaded {len(COUNTY_OVERRIDE_RATES)} county_override_rates rows for tax_year={TAX_YEAR}")
    status()


def status():
    conn = db.get_conn()
    n = conn.execute("SELECT count(*) FROM county_override_rates WHERE tax_year=%s", (TAX_YEAR,)).fetchone()[0]
    print(f"  county_override_rates (tax_year={TAX_YEAR})   {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
