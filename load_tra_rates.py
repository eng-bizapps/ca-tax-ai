"""Seeds tra_rates (exact per-Tax-Rate-Area combined rates) for every county
in COUNTY_SOURCES from the CSVs produced by extract_kern_tra_rates.py
(Kern) and extract_tra_rates.py (every other county). Replaces the earlier
Kern-only load_kern_tra_rates.py. Schema lives in property_schema.py.

Every row's TRA number is checked against its county's own declared format
(property_tax.TRA_FORMATS via normalize_tra_number) before anything is
written, and COUNTY_SOURCES must match property_tax.TRA_FORMATS exactly --
a county can't be loaded without the engine knowing its TRA number format,
and the engine can't claim coverage for a county nobody loaded.

Rows are upserted in batches (executemany) -- the Kern-only loader issued
one round trip per row, which took minutes for 2,455 rows against the
remote database; ~25k+ rows across these counties would take far longer
that way.

VINTAGE is per county, not global: each CSV row carries its own tax_year
(this codebase's DEFAULT_LIEN_YEAR convention -- lien year N's bill is FY
N-(N+1), so a FY2025-26 book is 2025 and FY2026-27 is 2026); Kern's older
CSV has no such column and uses the registry's default_tax_year. Counties
publish at different times, so refresh each independently: re-run its
extractor against the new book, re-run this loader -- never guess a
vintage forward. compute_tra_rate's "most recent tax_year <= requested"
lookup then picks the newest row per TRA.

Usage:
  python load_tra_rates.py load [county ...]   # all counties, or just those named
  python load_tra_rates.py status
"""
import csv
import sys

import property_db as db
import property_tax

AS_OF = "2026-10-31"

COUNTY_SOURCES = {
    "Kern": dict(
        csv="kern_tra_rates.csv", default_tax_year=2025,
        citation="Kern County Auditor-Controller-County Clerk, Annual Property Tax Rate Book, FY2025-26",
        url="https://www.auditor.co.kern.ca.us/RateBook/TaxRates2526.pdf"),
    "San Diego": dict(
        csv="tra_rates_data/san-diego.csv", default_tax_year=2025,
        citation="San Diego County Auditor and Controller, Tax Rate Book Table 27 (Tax Rate by Tax Rate Area), FY2025-26",
        url="https://www.sandiegocounty.gov/content/dam/sdc/auditor/trb2526/tralist.pdf"),
    "Orange": dict(
        csv="tra_rates_data/orange.csv", default_tax_year=2025,
        citation="County of Orange Auditor-Controller, Property Tax Rates (TRA Tax Rate Book), FY2025-26",
        url="https://ocauditor.gov/wp-content/uploads/2025/09/2025-2026-TRA-Tax-Rates-Revised3.pdf"),
    "San Joaquin": dict(
        csv="tra_rates_data/san-joaquin.csv", default_tax_year=2026,
        citation="San Joaquin County Auditor-Controller, Multi-Year Tax Rates, FY2026-27",
        url="https://www.sjgov.org/docs/default-source/auditor-controller-documents/property-tax/"
            "assessed-values-and-tax-rates/2026-2027/2026-27-property-tax-rates.pdf"),
    "Solano": dict(
        csv="tra_rates_data/solano.csv", default_tax_year=2025,
        citation="Solano County Auditor-Controller, TRA Fund Composition and Rates (summed across funds), FY2025-26",
        url="https://content.solanocounty.gov/sites/default/files/2025-10/FY2526_TRA_Fund_Composition_and_Rates.pdf"),
    "Sonoma": dict(
        csv="tra_rates_data/sonoma.csv", default_tax_year=2026,
        citation="Sonoma County Auditor-Controller-Treasurer-Tax Collector, Multi-Year Tax Rates, FY2026-27",
        url="https://sonomacounty.gov/Main%20County%20Site/Administrative%20Support%20&%20Fiscal%20Services/"
            "ACTTC/Documents/Property%20Tax%20Accounting/Multi-Year%20Tax%20Rates/"
            "Multi%20Year%20Tax%20Rates%20FY%202026-2027.pdf"),
    "Tulare": dict(
        csv="tra_rates_data/tulare.csv", default_tax_year=2026,
        citation="Tulare County Auditor-Controller, Multi-Year Tax Rates, FY2026-27",
        url="https://tc-web.widen.net/s/qpdbrn8jbr/2026.27-multi-year-tax-rates"),
    "Stanislaus": dict(
        csv="tra_rates_data/stanislaus.csv", default_tax_year=2025,
        citation="Stanislaus County Auditor-Controller, Secured Tax Rates, Tax Year 2025-2026",
        url="https://www.stancounty.com/auditor/pdf/propertytaxdivision/taxrates/2025-secured-tax-rates.pdf"),
    "San Luis Obispo": dict(
        csv="tra_rates_data/san-luis-obispo.csv", default_tax_year=2025,
        citation="County of San Luis Obispo Auditor-Controller, Property Tax Rates (Bonds Levied, Tax Rates by Fund & TRA), 2025-2026",
        url="https://www.slocounty.ca.gov/departments/auditor-controller-treasurer-tax-collector-public/"
            "forms-documents/property-tax-reports-and-documents/property-tax-rates/2025-26-property-tax-rates"),
    "Ventura": dict(
        csv="tra_rates_data/ventura.csv", default_tax_year=2025,
        citation="County of Ventura Auditor-Controller, Tax Rate Book, Fiscal Year 2025-26",
        url="https://venturacounty.gov/auditor-controllers-office/tax-rates-and-other-information-2025-2026/"),
}


def _rows(county, src):
    out = []
    with open(src["csv"], newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            groups = r["tra_number"].split("-")
            if property_tax.normalize_tra_number(county, groups) != r["tra_number"]:
                raise ValueError(f"{county}: TRA {r['tra_number']!r} doesn't match its county's format "
                                 f"{property_tax.TRA_FORMATS.get(county)}")
            tax_year = int(r.get("tax_year") or src["default_tax_year"])
            out.append((tax_year, county, r["tra_number"], r["area_name"] or None,
                        r["total_rate"], src["citation"], src["url"], AS_OF))
    return out


def load(only=None):
    if set(COUNTY_SOURCES) != set(property_tax.TRA_FORMATS):
        raise ValueError("COUNTY_SOURCES and property_tax.TRA_FORMATS must list the same counties: "
                         f"{sorted(COUNTY_SOURCES)} vs {sorted(property_tax.TRA_FORMATS)}")
    counties = only or list(COUNTY_SOURCES)
    with db.get_conn() as conn:
        for county in counties:
            rows = _rows(county, COUNTY_SOURCES[county])
            cur = conn.cursor()
            for i in range(0, len(rows), 1000):
                cur.executemany(
                    "INSERT INTO tra_rates "
                    "(tax_year, county, tra_number, area_name, total_rate, citation, source_url, as_of) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tax_year, county, tra_number) DO UPDATE SET "
                    "area_name=EXCLUDED.area_name, total_rate=EXCLUDED.total_rate, "
                    "citation=EXCLUDED.citation, source_url=EXCLUDED.source_url, as_of=EXCLUDED.as_of",
                    rows[i:i + 1000])
            print(f"loaded {len(rows)} {county} TRAs (tax_year={rows[0][0]})")
    status()


def status():
    conn = db.get_conn()
    for county, ty, n in conn.execute(
            "SELECT county, tax_year, count(*) FROM tra_rates GROUP BY county, tax_year "
            "ORDER BY county, tax_year").fetchall():
        print(f"  tra_rates {county:16} tax_year={ty}  {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load(sys.argv[2:] or None)
    elif cmd == "status":
        status()
    else:
        print(__doc__)
