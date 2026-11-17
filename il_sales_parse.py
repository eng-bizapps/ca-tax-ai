"""Parsers for IDOR's Illinois sales/use-tax source files (no database needed).

  * Retailers' Occupation Tax county/municipality machine-readable file (fixed width, 211
    characters per record; layout effective January 1, 2026):
    https://tax.illinois.gov/research/taxrates/sales-tax-rate-machine-readable-files/sales-tax-machine-readable-file-guide-county-municipality-01-01-2026.html
  * Grocery-tax locations and rates file (fixed width, 106 characters per record):
    https://tax.illinois.gov/research/taxrates/sales-tax-rate-machine-readable-files/grocery-machine-readable-file-guide-01012026.html
  * PIO-101 Illinois Sales & Use Tax Matrix (PDF tables; requires pdfplumber).

Rates are stored as fractions (a 6.25% rate is 0.0625); the files carry them as v99999.
"""
import re
from decimal import Decimal


def _rate(text):
    return Decimal(int(text)) / Decimal(100000)


def _date(text):
    """CCYYMMDD -> 'YYYY-MM-DD' (None for blank)."""
    t = text.strip()
    return f"{t[0:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 and t.isdigit() else None


def parse_location_line(line):
    """One county/municipality record. Rates are for retailers' occupation tax ('receipts', sales made at the retailer's
    location) and use tax ('purchases'); each has a current and a prior period with its own dates."""
    p = 0

    def take(n):
        nonlocal p
        v = line[p:p + n]
        p += n
        return v

    def rates(n):
        return [_rate(take(5)) for _ in range(n)]

    r = {"location_id": take(10), "name": take(25).strip(), "county": take(25).strip(), "address_flag": take(1) == "Y",
         "effective": _date(take(8))}
    r["receipts_gm_high"], r["receipts_dm_high"], r["receipts_gm_low"], r["receipts_dm_low"] = rates(4)
    r["receipts_flag"] = take(1) == "Y"
    r["purchases_gm_high"], r["purchases_dm_high"], r["purchases_gm_low"], r["purchases_dm_low"] = rates(4)
    r["purchases_flag"] = take(1) == "Y"
    r["auto_high"], r["auto_low"], r["auto_med_fee_pct"] = rates(3)
    r["auto_max_med_fee"] = Decimal(int(take(5))) / Decimal(100)
    r["auto_flag"] = take(1) == "Y"
    r["prior_effective"], r["prior_end"] = _date(take(8)), _date(take(8))
    (r["prior_receipts_gm_high"], r["prior_receipts_dm_high"], r["prior_receipts_gm_low"],
     r["prior_receipts_dm_low"]) = rates(4)
    r["prior_receipts_flag"] = take(1) == "Y"
    (r["prior_purchases_gm_high"], r["prior_purchases_dm_high"], r["prior_purchases_gm_low"],
     r["prior_purchases_dm_low"]) = rates(4)
    r["prior_purchases_flag"] = take(1) == "Y"
    r["prior_auto_high"], r["prior_auto_low"], r["prior_auto_med_fee_pct"] = rates(3)
    r["prior_auto_max_med_fee"] = Decimal(int(take(5))) / Decimal(100)
    r["prior_auto_flag"] = take(1) == "Y"
    if p != 211:
        raise ValueError(f"unexpected location record length {p} for {r['location_id']}")
    return r


def parse_location_file(path):
    with open(path, encoding="latin-1") as f:
        return [parse_location_line(l.rstrip("\n")) for l in f if len(l.rstrip("\n")) == 211]


def parse_grocery_line(line):
    p = 0

    def take(n):
        nonlocal p
        v = line[p:p + n]
        p += n
        return v

    r = {"location_id": take(10), "name": take(25).strip(), "county": take(25).strip(), "effective": _date(take(8)),
         "grocery_high": _rate(take(5)), "grocery_low": _rate(take(5)), "flag": take(1) == "Y",
         "prior_effective": _date(take(8)), "prior_end": _date(take(8)), "prior_grocery_high": _rate(take(5)),
         "prior_grocery_low": _rate(take(5)), "prior_flag": take(1) == "Y"}
    if p != 106:
        raise ValueError(f"unexpected grocery record length {p} for {r['location_id']}")
    return r


def parse_grocery_file(path):
    with open(path, encoding="latin-1") as f:
        return [parse_grocery_line(l.rstrip("\n")) for l in f if len(l.rstrip("\n")) == 106]


_TAXABLE = re.compile(r"(Exempt|Taxable|Non-\s*Taxable)", re.I)
_RATE = re.compile(r"(High|Low)\s*Rate", re.I)
_ID = re.compile(r"\d{4}(\.\d+)?")


def parse_matrix(path):
    """PIO-101 matrix rows: [{id, page, description, taxable, rate, explanation, references}]. 'taxable' is
    'taxable' / 'exempt' / 'non-taxable' or None where the matrix row has no taxability column (references to other
    taxes); rate is 'high' / 'low' / None."""
    import pdfplumber
    items = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables():
                for row in table:
                    cells = [(c or "").replace("\n", " ").strip() for c in row]
                    cells = [c for c in cells if c]
                    if not cells or not _ID.fullmatch(cells[0]):
                        continue
                    rest = cells[1:]
                    taxable = next((c for c in rest if _TAXABLE.fullmatch(c)), None)
                    rate = next((c for c in rest if _RATE.fullmatch(c)), None)
                    items.append({
                        "item_id": cells[0], "page": page_no, "description": rest[0] if rest else "",
                        "taxable": re.sub(r"\s+", "", taxable).lower() if taxable else None,
                        "rate": rate.split()[0].lower() if rate else None,
                        "explanation": max(rest, key=len) if rest else "",
                        "references": rest[-1] if len(rest) > 1 else ""})
    return items


if __name__ == "__main__":
    import sys
    base = "il_sales_data/"
    locs = parse_location_file(base + "ordmache-current-2026-11-17.txt")
    groc = parse_grocery_file(base + "grocerymache-current-2026-11-17.txt")
    print(len(locs), "locations;", len(groc), "grocery rows;", sum(1 for r in locs if r["address_flag"]), "address-specific")
    if "--matrix" in sys.argv:
        print(len(parse_matrix(base + "pio-101-R-01-26.pdf")), "matrix items")
