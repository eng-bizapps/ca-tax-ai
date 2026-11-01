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


# -------------------------------------------- shared 'wRMultiYrRates' counties
# Humboldt, Nevada, Mariposa and Imperial publish the same AUD70-2360-020
# report as San Joaquin/Sonoma/Tulare, so only the URL differs.
HUM_URL = "https://humboldtgov.org/DocumentCenter/View/141439/Multi-Year-Tax-Rates-2025-26-PDF"
NEV_URL = "https://www.nevadacountyca.gov/DocumentCenter/View/71265/Multi-Year-Tax-Rates-26-27"
MAR_URL = "https://www.mariposacounty.gov/DocumentCenter/View/101391"
IMP_URL = "https://auditor.imperialcounty.org/wp-content/uploads/2025/08/2025-26-Multi-Year-Tax-Rates-2.pdf"


def extract_humboldt(pdfs):
    return _extract_multi_year_report(pdfs, "Humboldt")


def extract_nevada(pdfs):
    return _extract_multi_year_report(pdfs, "Nevada")


def extract_mariposa(pdfs):
    return _extract_multi_year_report(pdfs, "Mariposa")


def extract_imperial(pdfs):
    return _extract_multi_year_report(pdfs, "Imperial")


# --------------------------------------------------------------- San Mateo
SM_URL = "https://www.smcgov.org/media/157077/download?attachment"
_SM_HDR = re.compile(r"(\d{3}-\d{3})$")
_SM_COMPOSITE = re.compile(r"^Composite Rate (\d+\.\d{4})$")


def extract_san_mateo(pdfs):
    """San Mateo's 771-page 'Tax Rates and Valuation' book prints two TRA
    blocks side by side (columns split at x=306, found from the 'Acc.#'
    header x0s 67/315). Each block opens with 'NAME NNN-NNN' and closes with
    'Composite Rate X.XXXX' -- the combined rate, printed to 4 decimals of a
    percent (so +/-0.00005%). The composite is cross-checked against the sum
    of the block's per-fund subtotal lines ('.0454 *'), which must agree to
    within 0.0002 (the subtotals are themselves rounded)."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for box in ((0, 0, 306, pg.height), (306, 0, pg.width, pg.height)):
                text = pg.crop(box).extract_text() or ""
                code, subtotal = None, 0.0
                for line in text.splitlines():
                    line = line.strip()
                    m = _SM_HDR.search(line)
                    if m and "Rate" not in line and "Composite" not in line and not line.startswith("Acc"):
                        code, subtotal = m.group(1), 0.0
                        continue
                    sm = re.fullmatch(r"(\d*\.\d{4}) \*", line)
                    if sm and code:
                        subtotal += float(sm.group(1))
                        continue
                    cm = _SM_COMPOSITE.match(line)
                    if cm and code:
                        rate = float(cm.group(1))
                        if abs(rate - subtotal) > 0.0003:
                            raise ValueError(f"San Mateo {code}: composite {rate} != subtotals {subtotal:.4f}")
                        if code in rows and rows[code] != rate:
                            raise ValueError(f"San Mateo {code}: conflicting composites {rows[code]} vs {rate}")
                        rows[code] = rate
                        code = None
    return [(c, None, r) for c, r in rows.items() if not c.startswith("000-")]


# -------------------------------------------------------------- Santa Cruz
SC_URL = "https://www.santacruzcountyca.gov/Portals/0/County/auditor/Tax_Rate_Book_2025-26.pdf"
_SC_HDR = re.compile(r"^(\d{2}-\d{3}) (.+)$")


def extract_santa_cruz(pdfs):
    """Santa Cruz's rate book lists each TRA as '01-006 NAME', its fund
    lines ('640141 2024 A NAME 0.028204'), and then a lone total line
    ('1.107594'). The total is cross-checked against the sum of the
    block's fund-line rates (every fund line ends in a 6-decimal rate)."""
    rows, names, skipped = {}, {}, []
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        code, parts, total = None, [], None

        def close():
            if code is None or total is None:
                return
            if abs(sum(parts) - total) > 0.000003:
                skipped.append((code, total, round(sum(parts), 6)))
                return
            if code in rows and rows[code] != total:
                raise ValueError(f"Santa Cruz {code}: conflicting totals")
            rows[code] = total

        for pg in pdf.pages:
            raw = [l.strip() for l in (pg.extract_text() or "").splitlines()]
            lines = []
            for l in raw:
                # a long fund name can wrap the rate itself ('... GOB 0.0' / '24000'): rejoin
                if lines and re.fullmatch(r"\d{1,5}", l) and re.search(r" \d\.\d{1,5}$", lines[-1]) \
                        and re.fullmatch(r"\d\.\d{6}", lines[-1].rsplit(" ", 1)[1] + l):
                    lines[-1] += l
                else:
                    lines.append(l)
            for line in lines:
                m = _SC_HDR.match(line)
                if m and not re.match(r"^\d{6}", line):
                    if m.group(1) == code:
                        continue   # header repeated at the top of a continuation page
                    close()
                    code, parts, total = m.group(1), [], None
                    names[code] = m.group(2).strip()
                    continue
                if code is None:
                    continue
                if re.fullmatch(r"\d\.\d{6}", line):
                    total = float(line)
                    continue
                fm = re.match(r"^\d{5,6} .*? (\d\.\d{6})$", line)
                if fm:
                    parts.append(float(fm.group(1)))
        close()
    if skipped:
        print(f"Santa Cruz: SKIPPED {len(skipped)} TRA(s) whose printed total != sum of listed funds "
              f"(never guess which is right): {skipped}")
        if len(skipped) > 0.01 * (len(rows) + len(skipped)):
            raise ValueError("Santa Cruz: more than 1% of TRAs fail the total==sum(funds) check")
    low = sorted(c for c, r in rows.items() if r < 1.0)
    if low:
        print(f"Santa Cruz: excluded {len(low)} TRA(s) whose printed total omits the 1% general levy: {low}")
    return [(c, names[c], r) for c, r in rows.items() if not c.startswith("00-") and r >= 1.0]


# ------------------------------------------------------------------ Sierra
SIE_URL = "https://www.sierracounty.ca.gov/DocumentCenter/View/11483/tax-rates-2025-26"
_SIE_RE = re.compile(r"^(\d{3}-\d{3}) (.+?) ((?:\d\.\d{4} ?)+)(?:\b[Xx]\b|Partial|\s)*$")


def extract_sierra(pdfs):
    """Sierra's one-page 'Property Tax Rates' table: TRA, description, then
    the 1% countywide rate, an optional utility-district loan rate, and the
    total (always the LAST decimal); total must equal the sum of the others."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for line in (pdf.pages[0].extract_text() or "").splitlines():
            m = _SIE_RE.match(line.strip())
            if not m:
                continue
            code, desc, nums = m.group(1), m.group(2), [float(x) for x in m.group(3).split()]
            if len(nums) > 1 and abs(sum(nums[:-1]) - nums[-1]) > 0.00011:
                raise ValueError(f"Sierra {code}: total {nums[-1]} != sum of components {nums[:-1]}")
            rows[code] = (desc, nums[-1])
    return [(c, d, r) for c, (d, r) in rows.items() if not c.startswith("000-")]


# ------------------------------------------------------------------ Colusa
COL_URL = "https://www.countyofcolusaca.gov/DocumentCenter/View/19738/2025-Tax-Rates-By-TRA"


def extract_colusa(pdfs):
    """Colusa publishes the shared 'wRMultiYrRates' report (dated 9/24/2025)."""
    return _extract_multi_year_report(pdfs, "Colusa")


# ------------------------------------------------------------------ Tehama
TEH_URL = "https://www.tehama.gov/wp-content/uploads/2025/09/TAX-RATES-25-26.pdf"
_TEH_RE = re.compile(r"(\d{3}-\d{3}) ([A-Z][A-Z .&'/-]*?) (\d\.\d{3,4})(?=\s|$)")


def extract_tehama(pdfs):
    """Tehama's two-page 'TAX RATES - 2025-2026' sheet lists 'CODE DISTRICT
    RATE' twice per line (two side-by-side columns); every entry is matched
    independently. Rates print to 3-4 decimals of a percent."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for code, name, rate in _TEH_RE.findall(pg.extract_text() or ""):
                if code in rows and rows[code][1] != float(rate):
                    raise ValueError(f"Tehama {code}: conflicting rates")
                rows[code] = (name.strip(), float(rate))
    return [(c, n, r) for c, (n, r) in rows.items() if not c.startswith("000-")]


# -------------------------------------------------------------------- Mono
MONO_URL = "https://monocounty.ca.gov/DocumentCenter/View/1387/Property-Tax-Rates-2025-2026"


def extract_mono(pdfs):
    """Mono's 2-page Resolution R25-075 / Exhibit A is a SCANNED IMAGE (no text
    layer, no OCR available), so these figures were TRANSCRIBED BY HAND from
    the rendered Exhibit A page. Each group's printed TOTAL was checked
    against 1.000000 + its bond line items (1.043183, 1.021565, 1.092615,
    1.016072, 1.069762 all reproduce).

    The exhibit lists overlapping TRA groups -- e.g. 010-000 appears under the
    Mammoth Campus (SFID), Mammoth Unified and Southern Mono Healthcare
    groups, each with a different printed total -- so a TRA's rate is the 1%
    limit PLUS the bond rate of EVERY group listing it (a TRA cannot have
    three different totals). 'THRU' ranges are taken as every number in the
    range. Only the page count is machine-checked, to notice a republished
    exhibit."""
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        if len(pdf.pages) != 2:
            raise ValueError("Mono exhibit changed shape (expected 2 pages) -- re-transcribe by hand")

    def rng(prefix, lo, hi):
        return [f"{prefix}-{n:03d}" for n in range(lo, hi + 1)]

    esusd = rng("051", 0, 34)
    campus = rng("010", 0, 12) + ["010-014", "010-015"]
    musd = rng("010", 0, 15) + ["059-000", "059-005", "059-007", "059-012", "059-018"]
    health = musd + ["060-000"]
    inyo = rng("060", 0, 6)
    layers = [(esusd, 0.043183), (campus, 0.021565), (musd, 0.092615), (health, 0.016072),
              (inyo, 0.017872 + 0.051890)]
    total = {}
    for tras, bond in layers:
        for t in tras:
            total[t] = total.get(t, 1.0) + bond
    return [(t, None, round(r, 6)) for t, r in sorted(total.items())]


# ----------------------------------------- Butte, El Dorado, Kings, Yolo, Napa, Shasta
# All six publish the shared 'AUD70-2360-020 wRMultiYrRates' report. These files
# were downloaded by hand (the county sites reject automated access); some are
# the county-hosted original bytes recovered from an Internet Archive capture.
BUT_URL = "https://www.buttecounty.net/DocumentCenter/View/16517/Multi-Rate-Taxes-2025-2026"
ELD_URL = ("https://www.eldoradocounty.ca.gov/files/assets/county/v/1/documents/government/auditor-controller/"
           "property-tax-division/ad-valorem-tax-rates/tax-rates-by-tax-rate-area/2025-26taxrates.pdf")
KIN_URL = "https://www.countyofkingsca.gov/home/showpublisheddocument/38666/638947364926970000"
YOL_URL = "https://www.yolocounty.gov/home/showpublisheddocument/85755/638936358784830000"
NAP_URL = "https://www.napacounty.gov/ArchiveCenter/ViewFile/Item/1116"
SHA_URL = "https://www.shastacounty.gov/media/75096"


def extract_butte(pdfs):
    return _extract_multi_year_report(pdfs, "Butte")


def extract_el_dorado(pdfs):
    return _extract_multi_year_report(pdfs, "El Dorado")


def extract_kings(pdfs):
    """Kings' report nets a negative 'TAX REDUCT/WESTLANDS' line (-0.268873)
    into the TRAs inside Westlands Water District, so a handful of genuine
    combined rates fall below 1% (0.77-0.85%); the printed 'Net of All' is
    taken as published and the validation rate band is widened to admit them."""
    return _extract_multi_year_report(pdfs, "Kings")


def extract_yolo(pdfs):
    return _extract_multi_year_report(pdfs, "Yolo")


def extract_napa(pdfs):
    """Also excludes '090-': TRA 090-000 is 'UNITARY 1% TAX' + 'UNITARY DEBT
    SERVICE' (2.3658%), the state-assessed rate, not ordinary property."""
    return _extract_multi_year_report(pdfs, "Napa", exclude=("000-", "090-"))


def extract_shasta(pdfs):
    """Shasta already posted its FY2026-27 report (dated 09/14/2026)."""
    return _extract_multi_year_report(pdfs, "Shasta")


# ------------------------------------------------------------ Santa Barbara
SB_URL = "https://taxes.co.santa-barbara.ca.us/propertytax/2025-26TaxRatebook.pdf"
_SB_TRA = re.compile(r"^TRA Title: (\d{3}-\d{3}) (.+)$")
_SB_BASIS = re.compile(r"^Levy Basis Type: (.+)$")
_SB_FUND = re.compile(r"^\d{4} - .+ (\d\.\d{5})$")
_SB_BTOTAL = re.compile(r"^Total (Taxable Value|Land Only value) (\d\.\d{5})$")
_SB_TOTAL = re.compile(r"^Total (\d{3}-\d{3}) .+ (\d\.\d{5})$")


def extract_santa_barbara(pdfs):
    """Santa Barbara's 'Tax Rate Book' lists, per TRA, fund lines grouped by
    'Levy Basis Type'. The rate that applies to a home's assessed value is the
    'Total Taxable Value' group (the 1% plus every bond fund). 42 TRAs also
    carry a separate 'Land Only value' group (Santa Ynez River Water
    Conservation, 0.04268%) that applies to the land portion alone and is NOT
    added here (the report's own 'Total NNN-NNN' line does add it, as if it
    applied to the whole value). A block can wrap across pages, so fund lines
    accumulate per (TRA, basis); each group's printed total must equal the sum
    of its funds and the TRA total must equal the sum of its group totals."""
    funds, btot, tot = {}, {}, {}
    code, basis = None, None
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for line in (pg.extract_text() or "").splitlines():
                line = line.strip()
                m = _SB_TRA.match(line)
                if m:
                    code, basis = m.group(1), None
                    continue
                m = _SB_BASIS.match(line)
                if m:
                    basis = m.group(1)
                    continue
                m = _SB_BTOTAL.match(line)
                if m and code:
                    key = (code, m.group(1))
                    if key in btot:
                        raise ValueError(f"Santa Barbara {code}: duplicate '{m.group(1)}' total")
                    btot[key] = float(m.group(2))
                    continue
                m = _SB_TOTAL.match(line)
                if m:
                    tot[m.group(1)] = float(m.group(2))
                    continue
                m = _SB_FUND.match(line)
                if m and code and basis:
                    funds.setdefault((code, basis), []).append(float(m.group(1)))
    out = []
    for c, total in tot.items():
        groups = {b: t for (cc, b), t in btot.items() if cc == c}
        if abs(sum(groups.values()) - total) > 0.000015:
            raise ValueError(f"Santa Barbara {c}: total {total} != group totals {groups}")
        for b, t in groups.items():
            fs = funds.get((c, b), [])
            if abs(sum(fs) - t) > 0.000015:
                raise ValueError(f"Santa Barbara {c} {b}: total {t} != fund sum {sum(fs):.5f}")
        if "Taxable Value" not in groups:
            continue
        out.append((c, None, groups["Taxable Value"]))
    return out


# --------------------------------------------------------------- Sacramento
SAC_URL = ("https://finance.saccounty.gov/AuditorController/Documents/AcctRpt/"
           "Tax%20Accting%202025-26/RATE%20BOOK%2025-26%20v4.pdf")
_SAC_HDR = re.compile(r"^CODE AREA (\d\d-\d{3})$")
_SAC_TOT = re.compile(r"^TOTAL RATE ON NET VALUE (\d\.\d{4})$")
_SAC_FUND = re.compile(r"^.+? (\d\.\d{4})$")


def extract_sacramento(pdfs):
    """Sacramento's 'Compilation of Tax Rates by Code Area' prints three code
    areas side by side, each a column of fund lines closed by 'TOTAL RATE ON NET
    VALUE'. Words are assigned to columns by the x-position of each page's
    'CODE AREA' headers, so a short column never shifts another. Each printed
    total must equal the sum of its listed funds; a code area that fails is
    SKIPPED, never guessed (found: 54-188, 54-195 and 54-196 list no Los Rios
    College line but print the total that includes it). Rates print to 4
    decimals of a percent. '00-' (utility) code areas are excluded."""
    rows, skipped = {}, []
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            words = pg.extract_words()
            starts = sorted({round(w["x0"]) for i, w in enumerate(words)
                             if w["text"] == "CODE" and i + 1 < len(words) and words[i + 1]["text"] == "AREA"})
            cs = []
            for x in starts:
                if not cs or x - cs[-1] > 20:
                    cs.append(x)
            if not cs:
                continue
            cols = {}
            for w in words:
                ci = max((k for k, x in enumerate(cs) if w["x0"] >= x - 3), default=None)
                if ci is not None:
                    cols.setdefault(ci, {}).setdefault(round(w["top"]), []).append(w)
            for ci in sorted(cols):
                code, parts = None, []
                for top in sorted(cols[ci]):
                    line = " ".join(w["text"] for w in sorted(cols[ci][top], key=lambda w: w["x0"]))
                    m = _SAC_HDR.match(line)
                    if m:
                        if code:
                            raise ValueError(f"Sacramento: code area {code} never found its TOTAL")
                        code, parts = m.group(1), []
                        continue
                    m = _SAC_TOT.match(line)
                    if m and code:
                        if code in rows:
                            raise ValueError(f"Sacramento: duplicate code area {code}")
                        total = float(m.group(1))
                        if abs(sum(parts) - total) > 0.00015:
                            skipped.append((code, total, round(sum(parts), 4)))
                            rows[code] = None
                        else:
                            rows[code] = total
                        code = None
                        continue
                    m = _SAC_FUND.match(line)
                    if m and code:
                        parts.append(float(m.group(1)))
                if code:
                    raise ValueError(f"Sacramento: code area {code} never found its TOTAL")
    if skipped:
        print(f"Sacramento: SKIPPED {len(skipped)} code area(s) whose printed total != sum of listed "
              f"funds (never guess which is right): {skipped}")
        if len(skipped) > 0.01 * len(rows):
            raise ValueError("Sacramento: more than 1% of code areas fail the total==sum(funds) check")
    return [(c, None, r) for c, r in rows.items() if r is not None and not c.startswith("00-")]


# -------------------------------------------------------------- Santa Clara
SCL_URL = "https://files.santaclaracounty.gov/exjcpb1531/2025-11/tax-rate-book-2025-2026.pdf"
_SCL_HDR = re.compile(r"^(.+?) (\d{3}-\d{3})$")
_SCL_FUND = re.compile(r"^(\d{5}) (.+?) (\d\.\d{5})(?: (L & I|L))?$")
_SCL_SUB = re.compile(r"^(\d\.\d{5})(\*+)$")


def extract_santa_clara(pdfs):
    """Santa Clara's 'Tax Rates 2025/2026' pages print two TRAs side by side
    (pages cropped at the midline). Accounts 1-40000 apply to net value, 60000s
    to land only, 70000s to land and improvements. Each TRA's account-1..40000
    funds must sum to its printed '**' total; a home is taxed on land and
    improvements, so the TRA rate is that total PLUS the 70000-series
    (Santa Clara Valley Water District State Water Project, 0.0039%, 'L & I')
    line. No 60000-series (land-only) line exists in this edition and one would
    raise. The book states 816 tax rate areas and 816 are read. '000-' (utility
    / unitary) TRAs are excluded."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            if "ACCOUNT DISTRICT TAX RATE" not in (pg.extract_text() or ""):
                continue
            W = pg.width
            for i in range(2):
                strip = pg.crop((W * i / 2, 0, W * (i + 1) / 2, pg.height))
                code = None
                for ln in (strip.extract_text() or "").splitlines():
                    ln = ln.strip()
                    if ln.startswith(("ACCOUNT DISTRICT", "TAX RATES")) or re.fullmatch(r"Page \d+", ln):
                        continue
                    m = _SCL_HDR.match(ln)
                    if m and not _SCL_FUND.match(ln):
                        code = m.group(2)
                        if code in rows:
                            raise ValueError(f"Santa Clara: duplicate TRA {code}")
                        rows[code] = {"net": [], "li": [], "dbl": []}
                        continue
                    if not code:
                        continue
                    m = _SCL_FUND.match(ln)
                    if m:
                        acct, rate = int(m.group(1)), float(m.group(3))
                        if 60000 <= acct < 70000:
                            raise ValueError(f"Santa Clara {code}: land-only account {acct} -- not modeled")
                        rows[code]["li" if acct >= 70000 else "net"].append(rate)
                        continue
                    m = _SCL_SUB.match(ln)
                    if m and len(m.group(2)) == 2:
                        rows[code]["dbl"].append(float(m.group(1)))
    out = []
    for c, r in rows.items():
        net = round(sum(r["net"]), 5)
        if not r["dbl"] or abs(r["dbl"][0] - net) > 0.000015:
            raise ValueError(f"Santa Clara {c}: printed total {r['dbl'][:1]} != fund sum {net}")
        if not c.startswith("000-"):
            out.append((c, None, round(net + sum(r["li"]), 5)))
    return out


# ------------------------------------------------------------------ Fresno
FRE_URL = "https://www.fresnocountyca.gov/files/810984c8-8552-468f-a7c3-f0ec48f30650/2025-2026-Tax-Rate-Book.pdf"
_FRE_PAGE = "* * COUNTY OF FRESNO * *\nTAX RATES BY TAX RATE AREA"
_FRE_ENTRY = re.compile(r"(\d{3}-\d{3})(?: (\d?\.\d{6}))?")


def extract_fresno(pdfs):
    """Fresno's Tax Rate Book ends with a 'Tax Rates by Tax Rate Area' table
    (three TRA/RATE pairs per line) giving each TRA's total secured rate in
    percent; some print below 1% (e.g. '.979926', a negative Westlands /
    levee adjustment). Rates sometimes print without the leading digit.
    TRAs listed with no rate (16 in this edition, e.g. 003-015, 012-029) are
    left out -- never guessed. '000-' (unitary) TRAs are excluded."""
    rows, norate = {}, []
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text() or ""
            if not t.startswith(_FRE_PAGE):
                continue
            for ln in t.splitlines()[4:]:
                for code, rate in _FRE_ENTRY.findall(ln):
                    if not rate:
                        norate.append(code)
                    elif code in rows:
                        raise ValueError(f"Fresno: duplicate TRA {code}")
                    else:
                        rows[code] = float(rate)
    if norate:
        print(f"Fresno: {len(norate)} TRA(s) print no rate and are left out: {norate}")
    return [(c, None, r) for c, r in rows.items() if not c.startswith("000-")]


# ------------------------------------------------------------------ Placer
PLA_URL = "https://www.placer.ca.gov/ArchiveCenter/ViewFile/Item/988"
_PLA_ROW = re.compile(r"^(\d{3}-\d{3}) ")
_PLA_NUM = re.compile(r"(?<![\d.])(\d\.\d{6})(?![\d])")


def extract_placer(pdfs):
    """Placer's 'Property Tax Rates for 2025/26' (Resolutions 2025-181 and
    2025-189) lists, per TRA, the 1% county-wide rate, each bond column that
    applies, and the 'Total Net A/V Tax Rate' last. Every row's first number
    must be 1.000000 and the numbers before the last must sum to the last."""
    rows = {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for ln in (pg.extract_text() or "").splitlines():
                m = _PLA_ROW.match(ln)
                if not m:
                    continue
                nums = [float(x) for x in _PLA_NUM.findall(ln)]
                code = m.group(1)
                if code.startswith("000-"):
                    continue
                if len(nums) < 2 or nums[0] != 1.0 or abs(sum(nums[:-1]) - nums[-1]) > 0.0000015:
                    raise ValueError(f"Placer {code}: row does not reconcile: {ln!r}")
                if code in rows:
                    raise ValueError(f"Placer: duplicate TRA {code}")
                rows[code] = nums[-1]
    return [(c, None, r) for c, r in rows.items() if not c.startswith("000-")]


# -------------------------------------------------------------------- Lake
LAK_URL = "https://www.lakecountyca.gov/DocumentCenter/View/15304/2025-2026-Tax-Rate-Book-PDF"


def extract_lake(pdfs):
    """Lake publishes the shared 'wRMultiYrRates' report (dated 11/2025)."""
    return _extract_multi_year_report(pdfs, "Lake")


# ------------------------------------------------------------- Contra Costa
CC_URL = "https://www.contracosta.ca.gov/DocumentCenter/View/89669/Detail-of-Tax-Rates-2025-2026-PDF"
_CC_SUMMARY = re.compile(r"\b(\d{5}) (\d\.\d{4})\b")
_CC_DETAIL = re.compile(r"^(\d{5}) (\d\.\d{4}) ")


def extract_contra_costa(pdfs):
    """Contra Costa's 'Detail of Tax Rates' opens with a 'Tax Rates by Tax Rate
    Area' summary (four TRA/RATE pairs per line, 5-digit TRA numbers) and then
    prints each TRA's full detail, starting '<TRA> <rate> COUNTYWIDE TAX'. The
    summary and every detail block must agree on every TRA's rate, and the
    two TRA sets must be identical -- anything else raises."""
    summary, detail = {}, {}
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text() or ""
            if t.startswith("2025-2026 TAX RATES BY TAX RATE AREA"):
                for code, rate in _CC_SUMMARY.findall(t):
                    if code in summary:
                        raise ValueError(f"Contra Costa: duplicate TRA {code} in summary")
                    summary[code] = float(rate)
            elif t.startswith("CONTRA COSTA COUNTY\n2025-2026 DETAIL OF TAX RATES"):
                for ln in t.splitlines():
                    m = _CC_DETAIL.match(ln)
                    if m:
                        if m.group(1) in detail:
                            raise ValueError(f"Contra Costa: duplicate TRA {m.group(1)} in detail")
                        detail[m.group(1)] = float(m.group(2))
    if summary != detail:
        diff = sorted(set(summary) ^ set(detail))[:5] or [c for c in summary if summary[c] != detail[c]][:5]
        raise ValueError(f"Contra Costa: summary and detail disagree, e.g. {diff}")
    return [(c, None, r) for c, r in summary.items()]


# ------------------------------------------------------------------ Alameda
ALA_URL = "https://auditor.alamedacountyca.gov/property-reports-2025-26-auditor-controller-alameda-county/"
_ALA_CODE = re.compile(r"CODE AREA (\d\d-\d{3})")
_ALA_TOTAL = re.compile(r"CODE AREA AND SPEC ASSESSMENT RATES\s+(\d*\.\d{4})%")
_ALA_SUB = re.compile(r"(\d*\.\d{2,4})\s*\*\*\*")
_ALA_CUT = 66  # character column where the right-hand code area's text begins


def extract_alameda(pdfs):
    """Alameda's 2025-26 tax rate book is a fixed-width TEXT report (not a PDF)
    that prints two code areas side by side: a header line naming both, then
    fund lines, '***' sub-totals, and 'CODE AREA AND SPEC ASSESSMENT RATES
    x.xxxx%'. Each line is split at character column 66 into its two columns.
    Every code area's printed total must equal the sum of its '***' sub-totals
    (county, education, miscellaneous, city ...); any that does not raises.
    '00-' (utility) code areas are excluded."""
    text = pdfs[0].decode("utf-8", errors="replace")
    blocks, cur = [], None
    for ln in text.splitlines():
        if ln.startswith("51427422") and "CODE AREA" in ln:
            cur = []
            for part in (ln[:_ALA_CUT], ln[_ALA_CUT:]):
                m = _ALA_CODE.search(part)
                cur.append([m.group(1) if m else None, [], None])
            blocks.append(cur)
            continue
        if cur is None:
            continue
        for k, part in enumerate((ln[:_ALA_CUT], ln[_ALA_CUT:])):
            if cur[k][0] is None:
                continue
            m = _ALA_TOTAL.search(part)
            if m:
                cur[k][2] = float(m.group(1))
                continue
            cur[k][1].extend(float(x) for x in _ALA_SUB.findall(part))
    rows = {}
    for b in blocks:
        for code, subs, total in b:
            if code is None:
                continue
            if total is None:
                raise ValueError(f"Alameda {code}: no total found")
            if abs(sum(subs) - total) > 0.00015:
                raise ValueError(f"Alameda {code}: printed total {total} != sum of sub-totals {sum(subs):.4f}")
            if code in rows:
                raise ValueError(f"Alameda: duplicate code area {code}")
            rows[code] = total
    return [(c, None, r) for c, r in rows.items() if not c.startswith("00-")]


# --------------------------------------------------------------- Riverside
RIV_URL = "https://auditorcontroller.org/sites/g/files/aldnop171/files/2026-05/FY%2025-26%20Tax%20Rate%20by%20District.xlsx"


def extract_riverside(pdfs):
    """Riverside's 'FY 2025-26 Tax Rates by District' workbook (sheet 'Tax Year
    2025') has one row per tax-rate-group (TAG = TRA) and fund; a TRA's rate is
    the sum of its 'Net Taxable Value' rows (the 1% general-purpose line plus
    each voter-approved debt service fund). A TRA that also carries a 'Taxable
    Land' row (a land-only levy, 214 rows, e.g. 0.3 for RCWD R Div DS in
    Temecula) is LEFT OUT entirely: the land-only part cannot be applied to a
    whole-property assessed value, and omitting it would understate the rate.
    '000-' (state-assessed unitary) TRAs are excluded."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(pdfs[0]), read_only=True)
    ws = wb["Tax Year 2025"]
    net, land = {}, set()
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i < 7 or r[3] is None:
            continue
        tag = str(r[3]).strip()
        if r[2] == "Net Taxable Value":
            net[tag] = net.get(tag, 0.0) + float(r[1])
        elif r[2] == "Taxable Land":
            land.add(tag)
        else:
            raise ValueError(f"Riverside {tag}: unknown rate value type {r[2]!r}")
    print(f"Riverside: {len(land & set(net))} TAGs carry a land-only levy and are left out")
    # one non-TRA tag ('UA', a 1% line with no area) appears; only NNN-NNN tags are TRAs
    return [(t, None, round(v, 5)) for t, v in net.items()
            if t not in land and not t.startswith("000-") and re.fullmatch(r"\d{3}-\d{3}", t)]


# -------------------------------------------------------------------- Marin
MARIN_URL = "https://assets.marincounty.gov/marincounty-prod/public/2026-04/FY2025-26%20TaxRateBook.pdf"
_MARIN_EVENT = re.compile(
    r"BASIC RATE ON NET VALUATION \(Prop 13 Rate\) (?P<b>\d\.\d{4})"
    r"|TOTAL VOTER-APPROVED RATES - NET VALUE (?P<v>\d\.\d{4})"
    r"|TOTAL for TRA (?P<code>\d{1,3}-\d{3}) (?P<t>\d\.\d{4})")


def extract_marin(pdfs):
    """Marin's 2025 Tax Rate Book prints, per TRA, 'BASIC RATE ON NET VALUATION
    (Prop 13 Rate)', 'TOTAL VOTER-APPROVED RATES - NET VALUE' and 'TOTAL for TRA
    n-nnn' (the layout interleaves two columns, so each is collected in page
    order and paired off). A TRA's total must equal basic + voter-approved; a
    TRA that does not is SKIPPED, never guessed (found: 94-023, 94-056 and
    94-061 print 1.0853 where the two parts sum to 1.0653). Marin prints the
    first group without its leading zero ('5-000'); it is stored zero-padded
    ('05-000') like the other two-digit-prefix counties. '0-' (utility) TRAs
    are excluded."""
    basic, voter, totals = [], [], []
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for ln in (pg.extract_text() or "").splitlines():
                for m in _MARIN_EVENT.finditer(ln):
                    if m.group("b"):
                        basic.append(float(m.group("b")))
                    elif m.group("v"):
                        voter.append(float(m.group("v")))
                    else:
                        totals.append((m.group("code"), float(m.group("t"))))
    if not (len(basic) == len(voter) == len(totals)):
        raise ValueError(f"Marin: {len(basic)} basic / {len(voter)} voter / {len(totals)} total lines")
    rows, skipped = {}, []
    for b, v, (code, total) in zip(basic, voter, totals):
        pre, post = code.split("-")
        code = f"{int(pre):02d}-{post}"
        if code in rows:
            raise ValueError(f"Marin: duplicate TRA {code}")
        if abs(b + v - total) > 0.00015:
            skipped.append((code, total, round(b + v, 4)))
            rows[code] = None
        else:
            rows[code] = total
    if skipped:
        print(f"Marin: SKIPPED {len(skipped)} TRA(s) whose printed total != basic + voter-approved "
              f"(never guess which is right): {skipped}")
        if len(skipped) > 0.01 * len(rows):
            raise ValueError("Marin: more than 1% of TRAs fail the basic + voter == total check")
    return [(c, None, r) for c, r in rows.items() if r is not None and not c.startswith("00-")]


# ------------------------------------------------------------------- Sutter
SUT_URL = "https://www.sutter.gov/home/showpublisheddocument/8857/638962207201230000"
_SUT_NUM = re.compile(r"(?<![\d.])(\d\.\d{6})(?!\d)")


def _sutter_codes(spec):
    """Expand Sutter's TRA list 'PP-aaa:bbb,ccc,ddd:eee' (colon = inclusive
    range of the 3-digit suffix; only the first item carries the 'PP-' prefix)."""
    prefix, codes = None, []
    for item in spec.split(","):
        if "-" in item:
            prefix, item = item.split("-", 1)
        lo, _, hi = item.partition(":")
        for n in range(int(lo), int(hi or lo) + 1):
            codes.append(f"{prefix}-{n:03d}")
    return codes


def extract_sutter(pdfs):
    """Sutter's two-page '2025-26 County of Sutter Tax Rates' lists, per
    district heading, TRA numbers as ranges ('51-001:170') that can wrap over
    several lines, then the 1% general rate, each bond rate and the TOTAL
    RATE. A block's rates must be 1.000000 first, and the rest before the
    total must sum to it. Unitary / railroad average lines are not TRAs."""
    rows, blocks = {}, []
    with pdfplumber.open(io.BytesIO(pdfs[0])) as pdf:
        for pg in pdf.pages:
            for ln in (pg.extract_text() or "").splitlines():
                ln = ln.strip()
                first = ln.split(" ")[0] if ln else ""
                if re.fullmatch(r"\d\d-\d{3}[\d,:]*", first):
                    blocks.append({"spec": first, "nums": _SUT_NUM.findall(ln)})
                elif blocks and not blocks[-1].get("closed") and (
                        re.fullmatch(r"[\d,:]+", first) or re.fullmatch(r"\d\.\d{6}", first)):
                    if re.fullmatch(r"[\d,:]+", first):
                        blocks[-1]["spec"] += first
                    blocks[-1]["nums"] += _SUT_NUM.findall(ln if not re.fullmatch(r"[\d,:]+", first) else ln[len(first):])
                elif blocks:
                    blocks[-1]["closed"] = True
    for b in blocks:
        nums = [float(x) for x in b["nums"]]
        if len(nums) < 2 or nums[0] != 1.0 or abs(sum(nums[:-1]) - nums[-1]) > 0.0000015:
            raise ValueError(f"Sutter {b['spec']}: rates do not reconcile: {nums}")
        for code in _sutter_codes(b["spec"]):
            if code in rows:
                raise ValueError(f"Sutter: duplicate TRA {code}")
            rows[code] = nums[-1]
    return [(c, None, r) for c, r in rows.items()]


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
    "humboldt": ("Humboldt", [HUM_URL], extract_humboldt, (300, 700), (1.0, 1.6), 2025),
    "nevada": ("Nevada", [NEV_URL], extract_nevada, (80, 400), (1.0, 1.6), 2026),
    "mariposa": ("Mariposa", [MAR_URL], extract_mariposa, (20, 200), (1.0, 1.6), 2025),
    "imperial": ("Imperial", [IMP_URL], extract_imperial, (150, 600), (1.0, 1.6), 2025),
    "san-mateo": ("San Mateo", [SM_URL], extract_san_mateo, (1000, 2000), (1.0, 1.6), 2025),
    "santa-cruz": ("Santa Cruz", [SC_URL], extract_santa_cruz, (200, 700), (1.0, 1.6), 2025),
    "sierra": ("Sierra", [SIE_URL], extract_sierra, (20, 120), (1.0, 1.6), 2025),
    "colusa": ("Colusa", [COL_URL], extract_colusa, (50, 400), (1.0, 1.6), 2025),
    "tehama": ("Tehama", [TEH_URL], extract_tehama, (100, 700), (1.0, 1.6), 2025),
    "mono": ("Mono", [MONO_URL], extract_mono, (30, 120), (1.0, 1.6), 2025),
    "butte": ("Butte", [BUT_URL], extract_butte, (400, 1200), (1.0, 1.6), 2025),
    "el-dorado": ("El Dorado", [ELD_URL], extract_el_dorado, (200, 800), (1.0, 1.6), 2025),
    "kings": ("Kings", [KIN_URL], extract_kings, (300, 1000), (0.7, 1.6), 2025),
    "yolo": ("Yolo", [YOL_URL], extract_yolo, (200, 600), (1.0, 1.6), 2025),
    "napa": ("Napa", [NAP_URL], extract_napa, (60, 200), (1.0, 1.6), 2025),
    "shasta": ("Shasta", [SHA_URL], extract_shasta, (600, 1600), (1.0, 1.6), 2026),
    "santa-barbara": ("Santa Barbara", [SB_URL], extract_santa_barbara, (400, 1000), (1.0, 1.6), 2025),
    "sacramento": ("Sacramento", [SAC_URL], extract_sacramento, (1200, 2800), (1.0, 1.6), 2025),
    "santa-clara": ("Santa Clara", [SCL_URL], extract_santa_clara, (600, 1000), (1.0, 1.6), 2025),
    "fresno": ("Fresno", [FRE_URL], extract_fresno, (1500, 2500), (0.9, 1.6), 2025),
    "placer": ("Placer", [PLA_URL], extract_placer, (200, 700), (1.0, 1.6), 2025),
    "lake": ("Lake", [LAK_URL], extract_lake, (100, 700), (1.0, 1.6), 2025),
    "contra-costa": ("Contra Costa", [CC_URL], extract_contra_costa, (800, 1500), (1.0, 1.6), 2025),
    "alameda": ("Alameda", [ALA_URL], extract_alameda, (1000, 2000), (1.0, 1.6), 2025),
    "riverside": ("Riverside", [RIV_URL], extract_riverside, (3000, 5500), (1.0, 1.6), 2025),
    "marin": ("Marin", [MARIN_URL], extract_marin, (250, 500), (1.0, 1.6), 2025),
    "sutter": ("Sutter", [SUT_URL], extract_sutter, (200, 900), (1.0, 1.6), 2025),
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
