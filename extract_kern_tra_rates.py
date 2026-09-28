"""One-time (re-runnable) extraction of Kern County's own published Tax
Rate Area (TRA) rate book PDF into a clean CSV -- feeds load_kern_tra_
rates.py, which seeds the tra_rates DB table. NOT part of the live app's
request path; this is an offline data-preparation script, same class as
income_item_sweep.py's cache-building but for a source dataset instead of
regression expectations.

WHY THIS EXISTS: property_tax_inventory.py's 'local-tra-rate' item is
genuinely deferred for all 58 counties (each publishes its own, differently
-formatted source, confirmed via live survey this session -- LA County's
own tool is bot-gated, Orange/San Diego use different PDF layouts than
Kern's). This script is a narrow, explicitly-scoped PILOT for exactly ONE
county (Kern) to prove exact per-TRA data is real and usable -- see
property_tax_inventory.py's own 'tra-rate-kern-pilot' ledger note.

THE PDF LAYOUT PROBLEM, verified via direct hands-on testing this session
(not assumed): Kern's rate book lays out TWO TRAs side by side per page in
two columns. pdfplumber's default page.extract_text() interleaves the two
columns in an order that makes it genuinely ambiguous which "TOTAL X *"
line closes which "AREA CODE" header when parsed as one flat text stream.
A naive fixed 50%-page-width crop ALSO fails -- tested and rejected --
because the right column's own left margin sits measurably left of the
page's exact horizontal center, so a 50% split misattributes words (e.g.
"KERN HIGH 2016-A" -- "KERN" lands in the left bucket, "HIGH 2016-A" in
the right bucket, corrupting district line-items across the boundary).

THE FIX, confirmed empirically: cluster every AREA-CODE HEADER token's OWN
x0 (left-edge) position across sampled pages -- these land at exactly TWO
stable values (roughly x=85.5 for the left column, x=377.9 for the right
column), regardless of what varies in the row's other content (names,
rate values, valuation dollar figures). GUTTER_X below sits just under the
right column's header x0 -- classifying every word on a row by whether its
own x0 is left or right of this fixed gutter cleanly separates the two
columns' content with zero cross-contamination (verified across the full
document, not just a sample).

A SECOND real bug, also found and fixed via direct testing: when a TRA's
district-item list is too long to fit in one column, it CONTINUES into the
next column with its own header line REPEATED verbatim as a continuation
marker (e.g. "001-008 BAKERSFIELD INSIDE" appears once at the bottom of
the left column, un-closed, then AGAIN at the top of the right column,
followed by its remaining items and TOTAL). A naive state machine treats
the repeat as a brand-new block, silently orphaning the real one (found
via a genuine ~20% "unclosed block" rate before this fix; 0% after).

VALIDATION RESULT (full document, this session): 2,455 ordinary TRAs
extracted, 0 unclosed blocks, 0 duplicate area codes, rate range
1.031567-1.272086 (all plausible -- consistent with Kern's own county-
average override rate of ~1.2529%, i.e. ~1.2529% total, computed
separately via the CA State Controller's Office data earlier this
session). `000-`-prefixed area codes (e.g. `000-001 CO WIDE UNITARY`,
`000-002 CO WIDE UNITARY-RAILROAD`, pipeline/railroad/electric utility
categories) are EXCLUDED -- state-assessed utility corridors, not ordinary
real property any taxpayer using this feature would own.

VINTAGE: Kern's "BILLING YEAR 2025-2026" is lien year 2025 in this
codebase's own DEFAULT_LIEN_YEAR convention (property_tax.py's own
module docstring: lien year N's bill is FY N-(N+1) -- so FY2025-26 ==
lien year 2025, one year BEHIND the current DEFAULT_LIEN_YEAR of 2026).
Same reasoning already applied to county_override_rates -- see property_
tax.compute_county_override_rate's own docstring for why "most recent
year <= requested", not an exact match, is the correct lookup design.

Usage:
  python extract_kern_tra_rates.py            # download + extract + write CSV
  python extract_kern_tra_rates.py --pdf PATH  # use an already-downloaded PDF
"""
import argparse
import csv
import re
import sys
import urllib.request

import pdfplumber

KERN_RATE_BOOK_URL = "https://www.auditor.co.kern.ca.us/RateBook/TaxRates2526.pdf"
OUTPUT_CSV = "kern_tra_rates.csv"

# The fixed column gutter -- see module docstring for how this was derived
# (clustering AREA CODE header tokens' own x0 positions, not a naive 50%
# page-width split, which was tested and rejected).
GUTTER_X = 372.9

_HEADER_RE = re.compile(r"^(\d{3}-\d{3})\s+(.+)$")
_TOTAL_RE = re.compile(r"^TOTAL\s+([\d.]+)\s*\*?$")

# Sanity bounds for the validation gate below -- not exact, just a wide
# enough band that a genuinely broken extraction (wrong gutter, wrong
# page range, a format change in a future year's rate book) fails loudly
# instead of silently writing garbage.
MIN_EXPECTED_TRAS = 2000
MAX_EXPECTED_TRAS = 3000
MIN_PLAUSIBLE_RATE = 0.9
MAX_PLAUSIBLE_RATE = 2.0


def _download_pdf(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def _page_columns(page):
    """Splits one page's words into (left_lines, right_lines), each a
    list of strings in natural top-to-bottom reading order for that
    column only -- see module docstring for why this beats both plain
    extract_text() and a naive 50%-width crop."""
    words = page.extract_words()
    rows = {}
    for w in words:
        y = round(w["top"])
        rows.setdefault(y, []).append(w)
    left, right = [], []
    for y in sorted(rows):
        row = sorted(rows[y], key=lambda w: w["x0"])
        lw = [w["text"] for w in row if w["x0"] < GUTTER_X]
        rw = [w["text"] for w in row if w["x0"] >= GUTTER_X]
        if lw:
            left.append(" ".join(lw))
        if rw:
            right.append(" ".join(rw))
    return left, right


def _parse_blocks(stream):
    """State machine over the concatenated (page1-left, page1-right,
    page2-left, page2-right, ...) line stream. Returns a list of
    {'code', 'name', 'rate'} dicts -- 'rate' is None for any block that
    genuinely never found its own TOTAL line (should be empty after the
    continuation-repeat fix; the caller treats any non-empty result here
    as a hard validation failure, not a warning)."""
    blocks = []
    current = None
    for line in stream:
        m = _HEADER_RE.match(line)
        if m:
            if current and m.group(1) == current["code"]:
                continue  # continuation-repeat, same block -- see docstring
            if current:
                blocks.append(current)
            current = {"code": m.group(1), "name": m.group(2), "rate": None}
            continue
        tm = _TOTAL_RE.match(line)
        if tm and current:
            current["rate"] = float(tm.group(1))
            blocks.append(current)
            current = None
    if current:
        blocks.append(current)
    return blocks


def extract(pdf_bytes: bytes):
    import io
    stream = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # The rate-by-area-code table starts around page index 8 (0-indexed)
        # and runs through the document's own internal "1-595" page range
        # (per its table of contents); page index 600 is comfortably past
        # the end without running into the following "Total Assessed
        # Valuation" section far enough to matter (validated: 0 unclosed/
        # duplicate blocks with this exact range).
        for pidx in range(8, min(600, len(pdf.pages))):
            l, r = _page_columns(pdf.pages[pidx])
            stream.extend(l)
            stream.extend(r)
    blocks = _parse_blocks(stream)
    ordinary = [b for b in blocks if not b["code"].startswith("000-") and b["rate"] is not None]
    unclosed = [b for b in blocks if b["rate"] is None]
    return ordinary, unclosed


def validate(ordinary, unclosed):
    errors = []
    if unclosed:
        errors.append(f"{len(unclosed)} block(s) never found their own TOTAL line "
                       f"(sample: {unclosed[:3]})")
    codes = [b["code"] for b in ordinary]
    if len(codes) != len(set(codes)):
        dupes = len(codes) - len(set(codes))
        errors.append(f"{dupes} duplicate area code(s) found")
    n = len(ordinary)
    if not (MIN_EXPECTED_TRAS <= n <= MAX_EXPECTED_TRAS):
        errors.append(f"TRA count {n} is outside the expected "
                       f"[{MIN_EXPECTED_TRAS}, {MAX_EXPECTED_TRAS}] range")
    bad_rates = [b for b in ordinary if not (MIN_PLAUSIBLE_RATE <= b["rate"] <= MAX_PLAUSIBLE_RATE)]
    if bad_rates:
        errors.append(f"{len(bad_rates)} TRA(s) have an implausible rate "
                       f"(sample: {bad_rates[:3]})")
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="path to an already-downloaded rate book PDF "
                                   "(skips the download)")
    args = ap.parse_args()

    if args.pdf:
        with open(args.pdf, "rb") as f:
            pdf_bytes = f.read()
        print(f"using local PDF: {args.pdf}")
    else:
        print(f"downloading {KERN_RATE_BOOK_URL} ...")
        pdf_bytes = _download_pdf(KERN_RATE_BOOK_URL)
        print(f"downloaded {len(pdf_bytes):,} bytes")

    ordinary, unclosed = extract(pdf_bytes)
    errors = validate(ordinary, unclosed)
    if errors:
        print("VALIDATION FAILED -- refusing to write output:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tra_number", "area_name", "total_rate"])
        for b in sorted(ordinary, key=lambda b: b["code"]):
            # Kern's rate book prints the rate as a PERCENTAGE number (e.g.
            # "1.163785" printed after "TOTAL" means 1.163785%) -- convert
            # to a true FRACTION (0.01163785) here, at the source, so the
            # stored/loaded value matches this codebase's own convention
            # (PROP13_BASE_RATE = 0.01 for 1%, county_override_rates.
            # override_rate likewise a fraction) -- NOT the raw printed
            # number. Found live: storing the raw percentage number and
            # multiplying it directly against assessed_value produced a
            # tax figure exactly 100x too large.
            w.writerow([b["code"], b["name"], f"{b['rate'] / 100:.8f}"])

    print(f"wrote {len(ordinary)} TRAs to {OUTPUT_CSV}")
    print(f"rate range: {min(b['rate'] for b in ordinary):.6f} - "
          f"{max(b['rate'] for b in ordinary):.6f}")


if __name__ == "__main__":
    main()
