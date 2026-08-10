"""CDTFA special taxes & fees that stack ALONGSIDE sales tax on the same receipt.

These are separate from sales/use tax (a TV is taxed AND carries an eWaste fee),
so they are a distinct line-item layer, not a rate on the price. Detection is
term-based and deliberately conservative -- a false fee is a confidently-wrong
answer, so 'AA batteries' must NOT trigger the lead-acid battery fee, etc.

AMOUNTS VERIFIED against CDTFA on 2026-07-02:
  - rate page: https://www.cdtfa.ca.gov/taxes-and-fees/tax-rates-stfd.htm
  - eWaste L-701 special notice (Jan 1 2020 schedule)
Fees change -- re-verify against the CDTFA rate page and update AS_OF.
"""
import re

AS_OF = "2026-07-02"
RATE_PAGE = "https://www.cdtfa.ca.gov/taxes-and-fees/tax-rates-stfd.htm"

# Each regime: term OR-groups that must ALL hit, excluded terms, and a spec.
REGIMES = [
    {
        "id": "ewaste",
        "name": "Electronic Waste Recycling (eWaste) Fee",
        "groups": [{"tv", "tvs", "television", "televisions", "monitor", "monitors",
                    "laptop", "laptops", "tablet", "tablets", "ereader", "ereaders",
                    "notebook", "notebooks", "chromebook", "chromebooks",
                    "computer", "computers", "screen", "screens", "display", "displays"}],
        "exclude": {"projector"},
        "type": "tiered_screen",
        # [low_inches, high_inches) -> fee
        "tiers": [(4, 15, 4.0), (15, 35, 5.0), (35, 999, 6.0)],
        "citation": "Public Resources Code section 42464 (Covered eWaste Recycling Fee)",
        "note": "a flat per-device fee on covered electronic devices with a screen "
                "larger than 4 inches (TVs, monitors, laptops, tablets); the amount "
                "depends on screen size",
    },
    {
        "id": "tire",
        "name": "California Tire Fee",
        "groups": [{"tire", "tires"}],
        "exclude": set(),
        "type": "per_unit",
        "amount": 1.75, "unit": "new tire",
        "citation": "California Tire Fee (CDTFA Special Taxes & Fees)",
        "note": "$1.75 on each new tire purchased",
    },
    {
        "id": "lumber",
        "name": "Lumber Products Assessment",
        "groups": [{"lumber", "plywood", "particleboard", "osb", "hardwood",
                    "softwood", "fiberboard", "veneer", "decking"}],
        "exclude": set(),
        "type": "percent",
        "rate": 0.01,
        "citation": "Lumber Products Assessment (CDTFA Special Taxes & Fees)",
        "note": "a 1% assessment on the selling price of lumber and engineered wood products",
    },
    {
        "id": "cannabis_excise",
        "name": "Cannabis Excise Tax",
        "groups": [{"cannabis", "marijuana", "thc", "dispensary", "dispensaries",
                    "cannabinoid", "cannabinoids"}],
        "exclude": set(),
        "type": "percent",
        "rate": 0.15,
        "citation": "Cannabis Tax Law, R&TC section 34011.2 (15%, effective Oct 1, 2025)",
        "note": "a 15% cannabis excise tax on the gross receipts of retail cannabis "
                "sales, in addition to sales tax (applies to adult-use and medicinal)",
    },
    {
        "id": "battery",
        "name": "California Lead-Acid Battery Fee",
        "groups": [{"battery", "batteries"},
                   {"car", "cars", "auto", "automotive", "vehicle", "vehicles",
                    "truck", "trucks", "motorcycle", "motorcycles",
                    "marine", "boat", "boats", "lead", "leadacid", "acid",
                    "rv", "rvs", "forklift", "forklifts"}],
        "exclude": {"aa", "aaa", "lithium", "alkaline", "button",
                    "watch", "watches", "phone", "phones", "laptop", "laptops"},
        "type": "per_unit",
        "amount": 2.00, "unit": "battery",
        "citation": "California Lead-Acid Battery Fee (CDTFA Special Taxes & Fees)",
        "note": "a $2.00 California battery fee on each replacement lead-acid "
                "(vehicle) battery",
    },
]


def _toks(s):
    return set(re.findall(r"[a-z]+", s.lower().replace("-", "")))


def _quantity(question):
    m = re.search(r"\b(\d+)\s+(?:new\s+)?(?:tires?|batteries|battery|"
                  r"tvs?|televisions?|monitors?|laptops?|tablets?)\b", question.lower())
    return int(m.group(1)) if m else 1


def _screen_inches(question):
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-\s]?(?:inch|inches|in|\")\b", question.lower())
    return float(m.group(1)) if m else None


def applicable(question, amount=None):
    """Return a list of applicable-fee dicts for the question. Each dict:
    name, amount (computed $ or None), detail (human string), citation, source_url, as_of."""
    qt = _toks(question)
    out = []
    for r in REGIMES:
        if r["exclude"] & qt:
            continue
        if not all(g & qt for g in r["groups"]):
            continue
        fee = {"id": r["id"], "name": r["name"], "citation": r["citation"],
               "source_url": RATE_PAGE, "as_of": AS_OF, "amount": None}
        if r["type"] == "per_unit":
            q = _quantity(question)
            fee["amount"] = round(r["amount"] * q, 2)
            fee["detail"] = (f"${r['amount']:.2f} per {r['unit']}"
                             + (f" x {q} = ${fee['amount']:.2f}" if q > 1 else ""))
        elif r["type"] == "percent":
            if amount is not None:
                fee["amount"] = round(amount * r["rate"], 2)
                fee["detail"] = f"{r['rate']*100:g}% of ${amount:,.2f} = ${fee['amount']:.2f}"
            else:
                fee["detail"] = f"{r['rate']*100:g}% of the selling price"
        elif r["type"] == "tiered_screen":
            inch = _screen_inches(question)
            if inch is not None:
                amt = next((f for lo, hi, f in r["tiers"] if lo <= inch < hi), None)
                if amt is not None:
                    q = _quantity(question)
                    fee["amount"] = round(amt * q, 2)
                    fee["detail"] = (f"${amt:.2f} for a {inch:g}-inch screen"
                                     + (f" x {q} = ${fee['amount']:.2f}" if q > 1 else ""))
            if fee["amount"] is None:
                fee["detail"] = ("$4 (screen 4-15\"), $5 (15-35\"), or $6 (35\"+), "
                                 "depending on screen size")
        fee["note"] = r["note"]
        out.append(fee)
    return out
