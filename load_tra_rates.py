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
    "Colusa": dict(
        csv="tra_rates_data/colusa.csv", default_tax_year=2025,
        citation="Colusa County Auditor-Controller, Multi-Year Tax Rates (Tax Rates by TRA), 2025-26",
        url="https://www.countyofcolusaca.gov/DocumentCenter/View/19738/2025-Tax-Rates-By-TRA"),
    "Humboldt": dict(
        csv="tra_rates_data/humboldt.csv", default_tax_year=2025,
        citation="Humboldt County Auditor-Controller, Multi-Year Tax Rates, 2025-26",
        url="https://humboldtgov.org/DocumentCenter/View/141439/Multi-Year-Tax-Rates-2025-26-PDF"),
    "Imperial": dict(
        csv="tra_rates_data/imperial.csv", default_tax_year=2025,
        citation="Imperial County Auditor-Controller, Multi-Year Tax Rates, 2025-26",
        url="https://auditor.imperialcounty.org/wp-content/uploads/2025/08/2025-26-Multi-Year-Tax-Rates-2.pdf"),
    "Mariposa": dict(
        csv="tra_rates_data/mariposa.csv", default_tax_year=2025,
        citation="Mariposa County Auditor-Controller, Multi-Year Tax Rates, 2025-26",
        url="https://www.mariposacounty.gov/DocumentCenter/View/101391"),
    "Mono": dict(
        csv="tra_rates_data/mono.csv", default_tax_year=2025,
        citation="Mono County Board of Supervisors Resolution R25-075, Exhibit A (2025-26 Property Tax Rates; scanned exhibit, hand-transcribed, layered by TRA group)",
        url="https://monocounty.ca.gov/DocumentCenter/View/1387/Property-Tax-Rates-2025-2026"),
    "Nevada": dict(
        csv="tra_rates_data/nevada.csv", default_tax_year=2026,
        citation="Nevada County Auditor-Controller, Multi-Year Tax Rates, FY2026-27",
        url="https://www.nevadacountyca.gov/DocumentCenter/View/71265/Multi-Year-Tax-Rates-26-27"),
    "San Mateo": dict(
        csv="tra_rates_data/san-mateo.csv", default_tax_year=2025,
        citation="San Mateo County Controller, Tax Rates and Valuation of Taxable Property (Composite Rate by TRA), FY2025-26",
        url="https://www.smcgov.org/media/157077/download?attachment"),
    "Santa Cruz": dict(
        csv="tra_rates_data/santa-cruz.csv", default_tax_year=2025,
        citation="County of Santa Cruz Auditor-Controller, 2025-2026 Property Tax Rates (Tax Rate Book)",
        url="https://www.santacruzcountyca.gov/Portals/0/County/auditor/Tax_Rate_Book_2025-26.pdf"),
    "Sierra": dict(
        csv="tra_rates_data/sierra.csv", default_tax_year=2025,
        citation="Sierra County Auditor, Property Tax Rates 2025-26",
        url="https://www.sierracounty.ca.gov/DocumentCenter/View/11483/tax-rates-2025-26"),
    "Tehama": dict(
        csv="tra_rates_data/tehama.csv", default_tax_year=2025,
        citation="Tehama County Treasurer-Tax Collector, Tax Rates 2025-2026",
        url="https://www.tehama.gov/wp-content/uploads/2025/09/TAX-RATES-25-26.pdf"),
    "Butte": dict(
        csv="tra_rates_data/butte.csv", default_tax_year=2025,
        citation="Butte County Auditor-Controller, Multi-Year Tax Rates (Multi-Rate Taxes), 2025-26",
        url="https://www.buttecounty.net/DocumentCenter/View/16517/Multi-Rate-Taxes-2025-2026"),
    "El Dorado": dict(
        csv="tra_rates_data/el-dorado.csv", default_tax_year=2025,
        citation="El Dorado County Auditor-Controller, Tax Rates by Tax Rate Area, 2025-26",
        url="https://www.eldoradocounty.ca.gov/files/assets/county/v/1/documents/government/auditor-controller/property-tax-division/ad-valorem-tax-rates/tax-rates-by-tax-rate-area/2025-26taxrates.pdf"),
    "Kings": dict(
        csv="tra_rates_data/kings.csv", default_tax_year=2025,
        citation="Kings County Auditor-Controller, Tax Rates Chart Book (Multi-Year Tax Rates), 2025-26",
        url="https://www.countyofkingsca.gov/home/showpublisheddocument/38666/638947364926970000"),
    "Napa": dict(
        csv="tra_rates_data/napa.csv", default_tax_year=2025,
        citation="Napa County Auditor-Controller, Multi-Year Tax Rates, 2025-26",
        url="https://www.napacounty.gov/ArchiveCenter/ViewFile/Item/1116"),
    "Shasta": dict(
        csv="tra_rates_data/shasta.csv", default_tax_year=2026,
        citation="Shasta County Auditor-Controller, 2026 Ad Valorem Rates by TRA (Multi-Year Tax Rates), FY2026-27",
        url="https://www.shastacounty.gov/media/75096"),
    "Yolo": dict(
        csv="tra_rates_data/yolo.csv", default_tax_year=2025,
        citation="Yolo County Financial Services, 2025-26 Tax Rates by TRA (Multi-Year Tax Rates)",
        url="https://www.yolocounty.gov/home/showpublisheddocument/85755/638936358784830000"),
    "Alameda": dict(
        csv="tra_rates_data/alameda.csv", default_tax_year=2025,
        citation="Alameda County Auditor-Controller, 2025-26 Tax Rate Book (Code Area rates; text report)",
        url="https://auditor.alamedacountyca.gov/property-reports-2025-26-auditor-controller-alameda-county/"),
    "Contra Costa": dict(
        csv="tra_rates_data/contra-costa.csv", default_tax_year=2025,
        citation="Contra Costa County Auditor-Controller, 2025-2026 Detail of Tax Rates (Tax Rates by Tax Rate Area)",
        url="https://www.contracosta.ca.gov/DocumentCenter/View/89669/Detail-of-Tax-Rates-2025-2026-PDF"),
    "Fresno": dict(
        csv="tra_rates_data/fresno.csv", default_tax_year=2025,
        citation="County of Fresno Auditor-Controller/Treasurer-Tax Collector, Tax Rate Book FY2025-26 (Tax Rates by Tax Rate Area)",
        url="https://www.fresnocountyca.gov/files/810984c8-8552-468f-a7c3-f0ec48f30650/2025-2026-Tax-Rate-Book.pdf"),
    "Lake": dict(
        csv="tra_rates_data/lake.csv", default_tax_year=2025,
        citation="Lake County Auditor-Controller, 2025-2026 Tax Rate Book (Multi-Year Tax Rates)",
        url="https://www.lakecountyca.gov/DocumentCenter/View/15304/2025-2026-Tax-Rate-Book-PDF"),
    "Marin": dict(
        csv="tra_rates_data/marin.csv", default_tax_year=2025,
        citation="County of Marin Department of Finance, FY2025-26 Tax Rate Book",
        url="https://assets.marincounty.gov/marincounty-prod/public/2026-04/FY2025-26%20TaxRateBook.pdf"),
    "Placer": dict(
        csv="tra_rates_data/placer.csv", default_tax_year=2025,
        citation="Placer County Auditor-Controller, Property Tax Rates for 2025/26 (Resolutions 2025-181 and 2025-189)",
        url="https://www.placer.ca.gov/ArchiveCenter/ViewFile/Item/988"),
    "Riverside": dict(
        csv="tra_rates_data/riverside.csv", default_tax_year=2025,
        citation="County of Riverside Auditor-Controller, FY 2025-26 Tax Rates by District (Net Taxable Value rates summed by TRA; TRAs with a land-only levy omitted)",
        url="https://auditorcontroller.org/sites/g/files/aldnop171/files/2026-05/FY%2025-26%20Tax%20Rate%20by%20District.xlsx"),
    "Sacramento": dict(
        csv="tra_rates_data/sacramento.csv", default_tax_year=2025,
        citation="Sacramento County Auditor-Controller, 2025-2026 Rate Book (Compilation of Tax Rates by Code Area)",
        url="https://finance.saccounty.gov/AuditorController/Documents/AcctRpt/Tax%20Accting%202025-26/RATE%20BOOK%2025-26%20v4.pdf"),
    "Santa Barbara": dict(
        csv="tra_rates_data/santa-barbara.csv", default_tax_year=2025,
        citation="County of Santa Barbara Auditor-Controller, Tax Rate Book 2025-26 (Taxable Value rates; land-only levies not added)",
        url="https://taxes.co.santa-barbara.ca.us/propertytax/2025-26TaxRatebook.pdf"),
    "Santa Clara": dict(
        csv="tra_rates_data/santa-clara.csv", default_tax_year=2025,
        citation="County of Santa Clara Controller-Treasurer, Tax Rates 2025-2026 (Tax Rate Book; net-value accounts plus the land-and-improvements State Water Project rate)",
        url="https://files.santaclaracounty.gov/exjcpb1531/2025-11/tax-rate-book-2025-2026.pdf"),
    "Sutter": dict(
        csv="tra_rates_data/sutter.csv", default_tax_year=2025,
        citation="Sutter County Auditor-Controller, 2025-26 County of Sutter Tax Rates",
        url="https://www.sutter.gov/home/showpublisheddocument/8857/638962207201230000"),
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
