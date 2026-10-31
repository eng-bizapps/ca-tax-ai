"""Multi-county extraction of each county's own published Tax Rate Area
(TRA) combined-rate table into tra_rates_data/<county>.csv, which feeds
load_tra_rates.py. Offline data-preparation utility (not part of the
live request path), the multi-county generalization of extract_kern_tra_
rates.py (which stays as the Kern-specific, fully-documented original --
its docstring explains the column-gutter and continuation-header problems
that the layout-sensitive extractors below reuse).

WHY PER-COUNTY EXTRACTORS: a live survey found NO shared format across
California counties' rate books (each Auditor-Controller publishes its own
-- LA's is a bot-gated web tool; Kern/Orange are two-column PDFs with
different row structures; San Diego is a flat TRA->rate list; others
differ again). Each county here is therefore its own small parser, all
producing the same CSV shape (tra_number, area_name, total_rate) with
total_rate stored as a true FRACTION (0.0116... for 1.16...%), matching
this codebase's universal convention (found live in the Kern build: a raw
percentage stored un-converted produced a 100x-inflated tax).

State-assessed utility/railroad/pipeline TRAs (not ordinary real property)
are excluded per county -- see each extractor.

Usage:
  python extract_tra_rates.py <county-slug> [--pdf PATH]
  python extract_tra_rates.py list
"""
import argparse
import csv
import io
import os
import re
import sys
import urllib.parse
import urllib.request

import pdfplumber

OUT_DIR = "tra_rates_data"
UA = {"User-Agent": "Mozilla/5.0"}


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _row_columns(page, gutter_x):
    """Splits one page into (left_lines, right_lines) by clustering words
    into rows by 'top' and splitting each row at a FIXED gutter derived
    per county from that county's own header-token x0 clustering (see
    extract_kern_tra_rates.py's docstring for why a naive 50% crop fails)."""
    rows = {}
    for w in page.extract_words():
        rows.setdefault(round(w["top"]), []).append(w)
    left, right = [], []
    for y in sorted(rows):
        r = sorted(rows[y], key=lambda w: w["x0"])
        lw = " ".join(w["text"] for w in r if w["x0"] < gutter_x)
        rw = " ".join(w["text"] for w in r if w["x0"] >= gutter_x)
        if lw:
            left.append(lw)
        if rw:
            right.append(rw)
    return left, right


# --------------------------------------------------------------- San Diego
SD_URL = "https://www.sandiegocounty.gov/content/dam/sdc/auditor/trb2526/tralist.pdf"
_SD_PAIR_RE = re.compile(r"\b(\d{5})\s+(\d\.\d{5})\b")


def extract_san_diego(pdfs):
    """San Diego publishes 'Table 27 -- Tax Rate by Tax Rate Area' as a
    flat seven-column-wide list of '<5-digit TRA> <rate>' pairs (no line-
    item breakdown) -- the simplest layout of any county surveyed. Rates
    print as percentages ('1.18748' = 1.18748%). TRAs starting '000'
    (00001, 00002, 00095: rate 2.02239 -- the state-assessed unitary
    categories) are excluded, and so is 49000 -- found via this script's
    own validation gate (not by inspection): an isolated code (no
    neighbors) carrying the identical 2.02239 unitary rate, i.e. another
    state-assessed pseudo-area, not ordinary property."""
    out = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for m in _SD_PAIR_RE.finditer(pg.extract_text() or ""):
                out[m.group(1)] = float(m.group(2))
    rows = [(c, None, r) for c, r in out.items()
            if not c.startswith("000") and c != "49000"]
    return rows


# ------------------------------------------------------------------ Orange
OC_URL = "https://ocauditor.gov/wp-content/uploads/2025/09/2025-2026-TRA-Tax-Rates-Revised3.pdf"
_OC_GUTTER = 265.0   # header tokens cluster at x0=36.1 (left) / 270.1 (right)
_OC_HEADER = re.compile(r"^(\d{2}-\d{3})$")
_OC_TOTAL = re.compile(r"^TOTAL TAX RATE\s+([\d.]+)$")


def extract_orange(pdfs):
    """Orange's 'TRA Tax Rates' section: two columns per page; each TRA
    block is a bare 'NN-NNN' header line, district line items, then
    'TOTAL RATES ON ALL PROPERTY A1 x' and 'TOTAL TAX RATE x' (the same
    figure for ordinary TRAs). A block too long for one column repeats its
    header on the continuation (same quirk as Kern's). TRAs with prefix
    '00-' (state-assessed unitary) are excluded."""
    stream = []
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            if "TRA TAX RATES" not in (pg.extract_text() or ""):
                continue
            l, r = _row_columns(pg, _OC_GUTTER)
            stream += l + r
    blocks, cur = [], None
    for ln in stream:
        ln = ln.strip()
        m = _OC_HEADER.match(ln)
        if m:
            if cur and m.group(1) == cur["code"]:
                continue
            if cur:
                raise ValueError(f"Orange: block {cur['code']} never found its TOTAL")
            cur = {"code": m.group(1), "rate": None}
            continue
        t = _OC_TOTAL.match(ln)
        if t and cur:
            cur["rate"] = float(t.group(1))
            blocks.append(cur)
            cur = None
    return [(b["code"], None, b["rate"]) for b in blocks if not b["code"].startswith("00-")]


# ------------------------------------------------------------- San Joaquin
SJ_URL = ("https://www.sjgov.org/docs/default-source/auditor-controller-documents/property-tax/"
          "assessed-values-and-tax-rates/2026-2027/2026-27-property-tax-rates.pdf")
_SJ_RE = re.compile(r"^TRA (\d{3}-\d{3}) (\d+) ([\d.]+) Net of All$", re.M)


def _extract_multi_year_report(pdfs, county, exclude=("000-",)):
    """Shared parser for the 'AUD70-2360-020 wRMultiYrRates' report that
    San Joaquin and Sonoma both publish (the same county-finance software
    output): each TRA's line items, closed by 'TRA NNN-NNN <value base>
    <rate> Net of All' -- the combined rate, already net of every levy.
    Rates print as percentages ('1.080100' = 1.0801%). TRAs prefixed '000-'
    (state-assessed unitary) are excluded."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for code, _base, rate in _SJ_RE.findall(pg.extract_text() or ""):
                if code in rows:
                    raise ValueError(f"{county}: duplicate TRA {code}")
                rows[code] = float(rate)
    return [(c, None, r) for c, r in rows.items() if not c.startswith(tuple(exclude))]


def extract_san_joaquin(pdfs):
    return _extract_multi_year_report(pdfs, "San Joaquin")


# ------------------------------------------------------------------ Sonoma
SON_URL = ("https://sonomacounty.gov/Main%20County%20Site/Administrative%20Support%20&%20Fiscal%20Services/"
           "ACTTC/Documents/Property%20Tax%20Accounting/Multi-Year%20Tax%20Rates/"
           "Multi%20Year%20Tax%20Rates%20FY%202026-2027.pdf")


def extract_sonoma(pdfs):
    """Also excludes '900-' -- found via this script's validation gate: TRA
    900-001 (2.0579%) is described in the report itself as 'PUB UTILITY
    AB454' (state-assessed public-utility rate), not ordinary property."""
    return _extract_multi_year_report(pdfs, "Sonoma", exclude=("000-", "900-"))


# ------------------------------------------------------------------ Tulare
TUL_URL = "https://tc-web.widen.net/s/qpdbrn8jbr/2026.27-multi-year-tax-rates"


def extract_tulare(pdfs):
    """Tulare publishes the same 'wRMultiYrRates' report as San Joaquin/
    Sonoma (FY2026-27 was already posted, dated 9/17/2026). Its report also
    carries value-base-6 'L&I w/ Minrl' lines -- separate land-and-
    improvements-only levies -- which the shared 'Net of All' parser
    correctly ignores (only the value-base-7 combined rate is taken)."""
    return _extract_multi_year_report(pdfs, "Tulare")


# --------------------------------------------------------------- Stanislaus
STAN_URL = "https://www.stancounty.com/auditor/pdf/propertytaxdivision/taxrates/2025-secured-tax-rates.pdf"
_STAN_RE = re.compile(r"\b(\d{3}-\d{3}) (\d\.\d{6})\b")


def extract_stanislaus(pdfs):
    """Stanislaus's 'Secured Tax Rates' is a compact three-column list of
    'NNN-NNN <bill rate>' pairs (rates print as percentages, '1.169445' =
    1.169445%; the bill rate already reflects the county's Sec. 100 negative
    levy, which is why a general levy of 0.8714 + bonds still totals ~1.17).
    TRAs prefixed '000-' (state-assessed unitary) are excluded."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for code, rate in _STAN_RE.findall(pg.extract_text() or ""):
                if code in rows:
                    raise ValueError(f"Stanislaus: duplicate TRA {code}")
                rows[code] = float(rate)
    return [(c, None, r) for c, r in rows.items() if not c.startswith("000-")]


# ------------------------------------------------------- San Luis Obispo
SLO_URL = ("https://www.slocounty.ca.gov/departments/auditor-controller-treasurer-tax-collector-public/"
           "forms-documents/property-tax-reports-and-documents/property-tax-rates/2025-26-property-tax-rates")
_SLO_LINE = re.compile(r"(?:^|\s)(\d{3}-\d{3})((?: \d*\.\d{5})+)\s*$")


def extract_san_luis_obispo(pdfs):
    """SLO's 'Property Tax Rates for Fiscal Year 2025-2026' lists each TRA as
    '<NNN-NNN> <total> <Prop 13 1.00000> <component bonds...>' (some lines
    carry a school-district/city label before the TRA number). Because every
    line ALSO lists its components, each total is cross-checked against the
    sum of its components (a built-in integrity check no other county's
    format offers) -- a mismatch fails the extraction. TRAs prefixed '000-'
    (the Unitary roll) are excluded."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for ln in (pg.extract_text() or "").splitlines():
                m = _SLO_LINE.search(ln.strip())
                if not m:
                    continue
                code = m.group(1)
                if code.startswith("000-"):
                    continue  # Unitary roll lines list a bare rate, no components
                nums = [float(n) for n in m.group(2).split()]
                total, parts = nums[0], nums[1:]
                if not parts or abs(total - sum(parts)) > 0.00002:
                    raise ValueError(f"San Luis Obispo: TRA {code} total {total} != "
                                     f"sum of components {sum(parts):.5f} ({ln!r})")
                if code in rows:
                    raise ValueError(f"San Luis Obispo: duplicate TRA {code}")
                rows[code] = total
    return [(c, None, r) for c, r in rows.items() if not c.startswith("000-")]


# ----------------------------------------------------------------- Ventura
_VC_BASE = "https://vcportal.venturacounty.gov/auditor/docs/property-tax-reports/tax-rates/2025-2026/"
# One file per city/area (the county splits its book by TRA range); the
# 'TRAs 00001-00511 Unitary' file is deliberately NOT listed -- state-
# assessed utility areas, not ordinary property.
_VC_FILES = [
    "TRAs 01001-01999 Fillmore.pdf", "TRAs 02000-02019 Ojai.pdf",
    "TRAs 03000-03999 Oxnard.pdf", "TRAs 04001-04999 Santa Paula.pdf",
    "TRAs 05001-05999 San Buenaventura.pdf", "TRAs 06000-06026 Port Hueneme.pdf",
    "TRAs 07001-07999 Camarillo.pdf", "TRAs 08000-08999 Thousand Oaks.pdf",
    "TRAs 09000-09999 Simi Valley.pdf", "TRAs 10000-10999 Moorpark.pdf",
    "TRAs 51001-93041 Unincorporated Areas.pdf",
]
VC_URLS = [_VC_BASE + urllib.parse.quote(f) for f in _VC_FILES]
_VC_HEADER = re.compile(r"^(.*?)\s*TAX RATE AREA (\d{5})$")
_VC_TOTAL = re.compile(r"^TOTAL GENERAL COUNTY ([\d.]+) \*")


def extract_ventura(pdfs):
    """Ventura publishes one single-column PDF per city/area; each TRA is
    '<AREA NAME> TAX RATE AREA NNNNN', its subfund line items, then
    'TOTAL GENERAL COUNTY <rate> *' -- the combined rate (printed as a
    percentage, '1.092029' = 1.092029%). A TRA's header can repeat after a
    page break, so a header matching the current block's own code is a
    continuation, not a new block (same quirk as Kern/Orange)."""
    rows, cur = {}, None
    for data in pdfs:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pg in pdf.pages:
                for ln in (pg.extract_text() or "").splitlines():
                    ln = ln.strip()
                    m = _VC_HEADER.match(ln)
                    if m:
                        if cur and m.group(2) == cur[0]:
                            continue
                        if cur:
                            raise ValueError(f"Ventura: TRA {cur[0]} never found its TOTAL")
                        cur = (m.group(2), m.group(1).strip() or None)
                        continue
                    t = _VC_TOTAL.match(ln)
                    if t and cur:
                        entry = (cur[1], float(t.group(1)))
                        # The county's per-area files are slices of ONE
                        # 720-page book, so the page at each file boundary
                        # appears in BOTH adjacent files (found via this
                        # guard: 16 TRAs duplicated that way). An identical
                        # repeat is expected and skipped; a CONFLICTING one
                        # would mean real data trouble, so it still fails.
                        if cur[0] in rows and rows[cur[0]] != entry:
                            raise ValueError(f"Ventura: conflicting duplicate TRA {cur[0]}: "
                                             f"{rows[cur[0]]} vs {entry}")
                        rows[cur[0]] = entry
                        cur = None
    # Unitary (state-assessed utility) areas, found via this script's own
    # validation gate: '00001'/'00095' named UNITARY at 1.82507%.
    return [(c, nm, r) for c, (nm, r) in rows.items()
            if not (nm and "UNITARY" in nm.upper()) and not c.startswith("000")]


# ------------------------------------------------------------------ Solano
SOL_URL = "https://content.solanocounty.gov/sites/default/files/2025-10/FY2526_TRA_Fund_Composition_and_Rates.pdf"
_SOL_LINE = re.compile(r"^(\d{6}) (\S+) (.+) (-?\d+\.\d{6})$")


def solano_fund_lines(pdfs):
    """Yields (tra, fund_code, description, rate) for every fund line."""
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for ln in (pg.extract_text() or "").splitlines():
                m = _SOL_LINE.match(ln.strip())
                if m:
                    yield m.group(1), m.group(2), m.group(3), float(m.group(4))


def extract_solano(pdfs):
    """Solano's 'TRA Fund Composition' is a long (1,000+ page) fund-by-fund
    table, '<6-digit TRA> <fund> <description> <rate>' -- there is no printed
    total, so each TRA's combined rate is the SUM of its fund lines. Verified
    independently: TRA 007000 (Vallejo) sums to 1.119373, exactly the figure
    the county's own 'General Tax Rate by City' report carries. Rates print
    as percentages. TRAs prefixed '000' (state-assessed unitary) are
    excluded."""
    totals = {}
    for tra, _fund, _desc, rate in solano_fund_lines(pdfs):
        totals[tra] = totals.get(tra, 0.0) + rate
    return [(c, None, round(r, 6)) for c, r in totals.items() if not c.startswith("000")]


# ---------------------------------------------------------------- registry
# slug -> (display county name, [source urls], extractor(list of pdf bytes),
# expected row-count band for the validation gate, plausible-rate band in
# PERCENT units, LIEN YEAR the source represents -- this codebase's
# DEFAULT_LIEN_YEAR convention, where lien year N's bill is FY N-(N+1), so
# a "FY2025-26" book is 2025 and a "FY2026-27" book is 2026). The lien year
# is written into every CSV row so each county's vintage is explicit data,
# not an assumption -- counties publish at different times (found: San
# Joaquin and Sonoma already had FY2026-27 books in late October 2026 while
# the others were still on FY2025-26).
COUNTIES = {
    "san-diego": ("San Diego", [SD_URL], extract_san_diego, (4000, 7000), (1.0, 1.6), 2025),
    "orange": ("Orange", [OC_URL], extract_orange, (4000, 6500), (1.0, 1.6), 2025),
    "san-joaquin": ("San Joaquin", [SJ_URL], extract_san_joaquin, (800, 1500), (1.0, 1.6), 2026),
    "sonoma": ("Sonoma", [SON_URL], extract_sonoma, (700, 2500), (1.0, 1.6), 2026),
    "tulare": ("Tulare", [TUL_URL], extract_tulare, (900, 2000), (1.0, 1.6), 2026),
    "solano": ("Solano", [SOL_URL], extract_solano, (800, 1500), (1.0, 1.6), 2025),
    "stanislaus": ("Stanislaus", [STAN_URL], extract_stanislaus, (1000, 2500), (1.0, 1.6), 2025),
    "san-luis-obispo": ("San Luis Obispo", [SLO_URL], extract_san_luis_obispo, (300, 1200), (1.0, 1.6), 2025),
    "ventura": ("Ventura", VC_URLS, extract_ventura, (600, 2500), (1.0, 1.6), 2025),
}


def validate(rows, band, rate_band):
    errs = []
    codes = [r[0] for r in rows]
    if len(codes) != len(set(codes)):
        errs.append(f"{len(codes) - len(set(codes))} duplicate TRA numbers")
    if not (band[0] <= len(rows) <= band[1]):
        errs.append(f"row count {len(rows)} outside expected {band}")
    bad = [r for r in rows if not (rate_band[0] <= r[2] <= rate_band[1])]
    if bad:
        errs.append(f"{len(bad)} implausible rates, sample {bad[:3]}")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("county", help="county slug, or 'list'")
    ap.add_argument("--pdf", nargs="+", help="path(s) to already-downloaded PDF(s), in the "
                                              "county's own file order (skips the download)")
    a = ap.parse_args()
    if a.county == "list":
        print(", ".join(COUNTIES))
        return
    name, urls, fn, band, rate_band, lien_year = COUNTIES[a.county]
    if a.pdf:
        data = [open(p, "rb").read() for p in a.pdf]
    else:
        data = []
        for u in urls:
            print(f"downloading {u} ...")
            data.append(_download(u))
    rows = fn(data)
    errs = validate(rows, band, rate_band)
    if errs:
        print("VALIDATION FAILED -- refusing to write output:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{a.county}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tra_number", "area_name", "total_rate", "tax_year"])
        for code, nm, rate in sorted(rows):
            w.writerow([code, nm or "", f"{rate / 100:.8f}", lien_year])
    rs = [r[2] for r in rows]
    print(f"wrote {len(rows)} {name} TRAs to {path}; rate range {min(rs):.5f}-{max(rs):.5f}%")


if __name__ == "__main__":
    main()
