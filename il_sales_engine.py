"""Chat answers for the Illinois sales and use tax domain (Illinois Department of Revenue).

Deterministic and keyword-gated like il_engine: every rate comes from IDOR's county/municipality
file (il_sales.py), every item fact from a PIO-101 matrix row, no LLM composes a number. Anything
the data cannot settle -- an address-specific location, an ambiguous place name, a service, a
date outside the two rate periods IDOR publishes -- is explained rather than guessed.
"""
import re
from decimal import Decimal

import il_engine as ie
import il_sales as s
import il_vehicle_engine as veh

_MATRIX_URL = "https://tax.illinois.gov/content/dam/soi/en/web/tax/research/taxinformation/sales/documents/pio-101-illinois-tax-matrix.pdf"
_RATES_URL = "https://tax.illinois.gov/research/taxrates/sales-tax-rate-machine-readable-files.html"
_FINDER = "IDOR's MyTax Illinois Tax Rate Finder (search by street address)"
_STATE_GM = Decimal("0.0625")
_STATE_LOW = Decimal("0.01")

_EXPLICIT = ("sales tax", "sales and use", "retailers' occupation", "retailers occupation", "grocery tax", "taxability",
             "sales/use")
_INCOME_WORDS = re.compile(r"\bincome\b|\bagi\b|wages|salary|filing|\breturn\b|withh|paycheck|property tax|penalt|credit"
                           r"|\beitc\b|refund|pension|retirement|social security|\bira\b|\b401")
_SERVICE_WORDS = re.compile(r"haircut|massage|repair|labor|consult|lawyer|attorney|accountant|tutor|lesson|membership|"
                            r"subscription|hotel|lodging|uber|lyft|taxi|ticket|admission|cleaning service|plumber|"
                            r"electrician|landscap|babysit|daycare|streaming")


def _result(text, category, tax=None, amount=None, status="answered", citation=None, source_url=None):
    return ie._result(text, category, tax=tax, amount=amount, status=status, citation=citation,
                      source_url=source_url) | {"domain": "sales"}


def _review(text, category):
    return _result(text, category, status="needs_review")


def _be(desc):
    return "are" if desc.rstrip().lower().endswith("s") and not desc.rstrip().lower().endswith("ss") else "is"


def _dec(x):
    return Decimal(str(x))


def _explicit(q):
    return any(w in q for w in _EXPLICIT) or bool(re.search(r"\buse tax\b", q) and "agi" not in q)


def _signal(q, found_places, items, force):
    explicit = _explicit(q)
    if _INCOME_WORDS.search(q) and not explicit:
        return False
    if force or explicit:
        return True
    tax_word = bool(re.search(r"\btax(?:ed|able|es)?\b|exempt", q))
    return tax_word and bool(found_places or items or _SERVICE_WORDS.search(q))


def _rate_range(pair):
    lo, hi = pair
    return s.pct(lo) if lo == hi else f"{s.pct(min(lo, hi))} to {s.pct(max(lo, hi))}"


def _place_label(rec):
    if rec["name"].upper().endswith(" COUNTY"):
        return f"{s.proper(rec['county'])} County (unincorporated areas)"
    return f"{s.proper(rec['name'])} ({s.proper(rec['county'])} County)" if rec["county"] else s.proper(rec["name"])


def _unresolved_address(rec):
    return _review(
        f"{_place_label(rec)} has business-district or address-specific sales-tax rates, so IDOR's city-level rate file "
        f"cannot give one rate for it (its entry is only a placeholder). Look up the exact street address in {_FINDER}. "
        f"What is fixed statewide: 6.25% state tax on general merchandise and 1% on qualifying drugs and medical "
        f"appliances; local taxes are added on top.", "il_sales_unresolved_address")


def _location(question, locations, loose=False):
    """(record, note, early_result)."""
    places = [(n, recs) for n, recs in s.find_places(question, locations, loose=loose) if n != "ILLINOIS"]
    if not places:
        return None, None, None
    hints = s.county_hints(question, locations)
    distinct = [(n, r) for n, r in places if not (n.endswith(" COUNTY") and len(places) > 1)]
    if len(distinct) > 1:
        return None, None, _review("I found more than one place in your question (" + ", ".join(s.proper(n) for n, _ in distinct)
                                   + "). Sales tax depends on where the retailer is, so ask about one place at a time.",
                                   "il_sales_multiple_places")
    rec, note, ambiguous = s.resolve(distinct[0][1], hints)
    if rec is None:
        return None, None, _review(f"There is more than one {s.proper(distinct[0][0])} in IDOR's files (in "
                                   f"{', '.join(c + ' County' for c in ambiguous)}), and their rates differ. Which county "
                                   f"do you mean?", "il_sales_ambiguous_place")
    return rec, note, None


def _classify(item):
    if s.grocery_class(item):
        return "grocery"
    if item["taxable"] == "taxable" and item["rate"] == "high":
        return "general"
    if item["taxable"] == "taxable" and item["rate"] == "low":
        return "drug_medical"
    if item["taxable"] == "exempt":
        return "exempt"
    if item["taxable"] == "non-taxable":
        return "nontaxable"
    if item["taxable"] == "taxable":
        return "conditional"
    return "reference"


def _item_cite(items):
    return "IDOR PIO-101 Sales & Use Tax Matrix (R-01/26), item " + ", ".join(i["item_id"] for _, i in items)


def _components(conn, rec, kind, as_of):
    """Rate pair and a plain-words breakdown for one category at one location, or an early _review result."""
    per = s.period(rec, as_of)
    if per is None:
        return _review(f"IDOR's rate file for {_place_label(rec)} covers only the rate in force since "
                       f"{s.long_date(rec['effective'])} and the one before it, and {s.long_date(as_of)} is outside both.",
                       "il_sales_unsupported_date")
    if kind == "grocery":
        g = s.grocery(conn, rec["location_id"], as_of)
        state_rate = s.state_grocery_rate(conn, as_of)
        if g is None or state_rate is None or state_rate != 0:
            return _review("The state's 1% grocery tax ended January 1, 2026, and I only have grocery rates from that date "
                           "on.", "il_sales_unsupported_date")
        pair = (g["low"], g["high"])
        if pair == (0, 0):
            comp = "no state tax since January 1, 2026 and no local grocery tax listed for this location"
        else:
            comp = ("no state tax since January 1, 2026; the whole rate is the local grocery tax"
                    + (", which varies by business within this location" if pair[0] != pair[1] else ""))
        return {"pair": pair, "comp": comp, "per": per, "label": "groceries"}
    if rec["address_flag"]:
        return _unresolved_address(rec)
    if kind == "general":
        pair = per["gm"]
        comp = (f"6.25% state + {s.pct(pair[1] - _STATE_GM)} local and transit-district taxes"
                if pair[0] == pair[1] else "state 6.25% plus local taxes that differ by business")
        return {"pair": pair, "comp": comp, "per": per, "label": "general merchandise"}
    pair = per["dm"]
    comp = (f"1% state + {s.pct(pair[1] - _STATE_LOW)} local transit-district tax" if pair[1] > _STATE_LOW
            else "the 1% state rate; no local tax")
    return {"pair": pair, "comp": comp, "per": per, "label": "qualifying drugs and medical appliances"}


def _period_note(rec, per, as_of, kind="general"):
    if per["kind"] == "current":
        text = f"The rate has been in effect since {s.long_date(per['start'])}"
        prior = s.prior_of(rec, as_of)
        key = "dm" if kind == "drug_medical" else "gm"
        cur = per[key][1]
        if prior and prior[key] != cur:
            label = "the drug and medical rate" if kind == "drug_medical" else "general merchandise"
            text += f" ({label} was {s.pct(prior[key])} through {s.long_date(prior['end'])})"
        return text + "."
    return f"This rate applied from {s.long_date(per['start'])} through {s.long_date(per['end'])}."


def _retrieved(conn):
    d = s.retrieved()
    return s.long_date(d) if d else "an earlier date"


def _answer_use_tax(conn, question, q, items, amounts_all, as_of):
    paid_m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:of|in)?\s*(?:sales\s+)?tax", question, re.IGNORECASE)
    paid = float(paid_m.group(1).replace(",", "")) if paid_m else 0.0
    price = next((v for v, a, _ in amounts_all
                  if not (paid_m and abs(v - paid) < 1e-9 and paid_m.start() <= a <= paid_m.end())), None)
    kinds = {_classify(i) for _, i in items}
    if kinds & {"exempt", "nontaxable"} and not kinds & {"general", "drug_medical", "conditional"}:
        return _result("That item is exempt or non-taxable under IDOR's matrix, so no Illinois use tax is due on it.",
                       "il_sales_use_tax", amount=0.0, citation=_item_cite(items), source_url=_MATRIX_URL)
    if kinds == {"grocery"}:
        return _result("Groceries are exempt from State tax under IDOR's matrix, and the state's 1% grocery rate ended "
                       "January 1, 2026, so there is no state use tax on them. A local grocery tax is a sales tax collected "
                       "by retailers in certain locations, not a use tax.", "il_sales_use_tax", amount=0.0,
                       citation=_item_cite(items), source_url=_MATRIX_URL)
    low = "drug_medical" in kinds and "general" not in kinds
    if price is None:
        return _result("Illinois use tax is the other half of sales tax: if you bought something to use in Illinois and the "
                       "seller didn't charge Illinois tax, you owe use tax at the state rate, 6.25% on general merchandise "
                       "(1% on qualifying drugs and medical appliances), less any sales tax you paid to another state (not "
                       "another country). There is no local use tax on general merchandise administered by IDOR. Tell me "
                       "the purchase price and any tax you paid and I'll compute it.", "il_sales_use_tax_info",
                       citation="IDOR PIO-101 (R-01/26) Introduction; IL-1040 instructions, UT Worksheet", source_url=_MATRIX_URL)
    rate = s.param(conn, "use_tax_low_rate" if low else "use_tax_general_rate", as_of)
    gross = s.money(_dec(price) * rate)
    due = max(Decimal(0), gross - s.money(paid))
    text = f"Illinois use tax on {ie._fmt(price)}: {s.pct(rate)} x {ie._fmt(price)} = {ie._fmt(gross)}"
    if paid:
        text += f", less {ie._fmt(paid)} of sales tax paid to another state, so {ie._fmt(due)} is due"
    text += (". Use tax is owed only if the seller did not collect Illinois tax; most remote retailers and marketplace "
             "facilitators now do, at the destination rate. Report it on Form IL-1040 Line 21 (or Form ST-44 if your use "
             "tax for the year is over $600, $1,200 if married filing jointly).")
    return _result(text, "il_sales_use_tax", tax=float(due),
                   citation="IDOR PIO-101 (R-01/26); IL-1040 instructions, Line 21", source_url=_MATRIX_URL)


def answer_sales(question, force=False):
    q = question.lower()
    with s.lazy_conn() as conn:
        locations = s.all_locations()
        loose = _explicit(q) and len(q.split()) <= 10
        places = [p for p in s.find_places(question, locations, loose=loose) if p[0] != "ILLINOIS"]
        items = s.match_items(conn, question)
        veh_q = veh.is_vehicle_question(q) and not (_INCOME_WORDS.search(q) and not _explicit(q))
        if not (veh_q or _signal(q, places, items, force)):
            return None
        amounts_all = ie._amounts(question)
        dates = ie._dates(question)
        as_of = dates[0][0] if dates else s.today()
        if as_of > s.today():
            return _review("I can only give rates already in IDOR's published files, not future ones.",
                           "il_sales_unsupported_date")
        if veh_q:
            return veh.answer_vehicle(question, q, locations, as_of)
        year_only = re.search(r"\b(19|20)\d\d\b", question)
        if year_only and not dates and int(year_only.group(0)) < s.today().year:
            return _review("To give a rate for a past period I need the exact date, because IDOR changes rates each January "
                           "and July and I only hold the current and prior rate periods.", "il_sales_unsupported_date")

        if re.search(r"\buse tax\b", q) and "agi" not in q:
            return _answer_use_tax(conn, question, q, items, amounts_all, as_of)

        if _SERVICE_WORDS.search(q) and not items:
            return _review("That looks like a service rather than a sale of tangible personal property. IDOR's sales-tax "
                           "matrix covers goods; I haven't loaded the rules for services, so I can't say whether it is "
                           "taxable.", "il_sales_services")

        rec, note, early = _location(question, locations, loose)
        if early:
            return early

        kinds = {}
        for alias, item in items:
            kinds.setdefault(_classify(item), []).append((alias, item))
        amount = next((v for v, a, e in amounts_all if not re.match(r"\s*(?:%|percent)", question[e:e + 9])), None)

        if len(kinds) > 1 or len({i["item_id"] for _, i in items}) > 1:
            rows = "; ".join(f"{i['description'][:70]} (item {i['item_id']}): {i['taxable'] or 'see matrix'}"
                             + (f", {i['rate']} rate" if i["rate"] else "") for _, i in items)
            return _review(f"Your question matches several different rows of IDOR's matrix, and they are taxed differently, so I "
                           f"won't pick one: {rows}. Tell me which you mean.", "il_sales_item_ambiguous")

        kind = next(iter(kinds)) if kinds else "general"
        item = items[0][1] if items else None
        desc = item["description"].split("(")[0].split(" – ")[0].strip() if item else "General merchandise"
        cite = _item_cite(items) if items else "IDOR county/municipality sales tax rate file; PIO-101 (R-01/26)"

        if kind in ("reference", "conditional"):
            extra = ""
            if item["item_id"] in ("4004", "4009"):
                extra = (" Vehicle, aircraft and watercraft sales use their own forms and rates (Form RUT-25), which I "
                         "haven't loaded.")
            elif item["taxable"] is None:
                first = re.split(r"(?<=[.!?])\s", (item["explanation"] or "").strip())[0][:220]
                extra = (f" IDOR's matrix note on this row: \"{first}\"" if first else
                         " The matrix refers you to a separate rule for this; see the matrix row.")
            return _review(f"IDOR's matrix lists '{item['description'][:80]}' (item {item['item_id']}) but its treatment depends "
                           f"on the facts, so I can't give a flat answer.{extra}", "il_sales_item_conditional")

        if kind in ("exempt", "nontaxable"):
            word = "exempt" if kind == "exempt" else "not subject to Illinois sales tax"
            return _result(f"{desc} is {word} under IDOR's matrix (item {item['item_id']}). Exemptions can depend on how the "
                           f"item is used or who buys it: see the matrix row for conditions.", "il_sales_item", amount=0.0,
                           citation=cite, source_url=_MATRIX_URL)

        if rec is None:
            if not items:
                return _review("Tell me the city (and the purchase price if you want the tax computed) and I'll give the "
                               "Illinois sales tax.", "il_sales_missing_place")
            if kind == "grocery":
                base = (f"{desc}: the state's 1% grocery tax ended January 1, 2026, so these now carry no state tax; a "
                        f"city or county may impose a local grocery tax of exactly 1% (transit districts such as Chicago's "
                        f"have their own). Tell me the city and I'll give the rate.")
            elif kind == "drug_medical":
                base = (f"{desc} {_be(desc)} taxed at the low rate: 1% state tax plus any local transit-district tax. Tell me "
                        f"the city and I'll give the exact rate.")
            else:
                base = (f"{desc} {_be(desc)} taxable at the general merchandise rate: 6.25% state tax plus local and "
                        f"transit-district taxes that vary by location. Tell me the city and I'll give the exact rate.")
            return _result(base, "il_sales_item", citation=cite, source_url=_MATRIX_URL)

        comp = _components(conn, rec, kind, as_of)
        if isinstance(comp, dict) and "answer_text" in comp:
            if items and kind in ("general", "drug_medical"):
                lead = (f"{desc} {_be(desc)} taxable at the " + ("general merchandise" if kind == "general" else "low (drug and medical)")
                        + " rate. ")
                comp["answer_text"] = lead + comp["answer_text"]
            return comp
        pair, per = comp["pair"], comp["per"]
        place = _place_label(rec)
        asof_txt = f"as of {s.long_date(as_of)}"
        tax_amount = None

        if amount is not None:
            lo, hi = s.money(_dec(amount) * pair[0]), s.money(_dec(amount) * pair[1])
            if lo == hi:
                text = (f"Sales tax on {ie._fmt(amount)} of {comp['label'] if items else 'general merchandise'} at {place}, "
                        f"{asof_txt}: {_rate_range(pair)} ({comp['comp']}) = {ie._fmt(lo)}, for a total of "
                        f"{ie._fmt(_dec(amount) + lo)}.")
            else:
                text = (f"Sales tax on {ie._fmt(amount)} of {comp['label']} at {place}, {asof_txt}: {_rate_range(pair)} "
                        f"depending on the business, so between {ie._fmt(lo)} and {ie._fmt(hi)}.")
            tax_amount = float(hi)
            category = "il_sales_tax"
        elif items:
            text = f"{desc} at {place}, {asof_txt}: {_rate_range(pair)} ({comp['comp']})."
            category = "il_sales_item"
        else:
            text = f"Illinois sales tax at {place}, {asof_txt}: general merchandise {_rate_range(pair)} ({comp['comp']})."
            for extra_kind in ("drug_medical", "grocery"):
                extra = _components(conn, rec, extra_kind, as_of)
                if isinstance(extra, dict) and "pair" in extra:
                    text += f" {extra['label'].capitalize()}: {_rate_range(extra['pair'])} ({extra['comp']})."
            category = "il_sales_rate"

        if kind != "grocery":
            text += " " + _period_note(rec, per, as_of, kind)
        if kind == "general" and per["receipts_flag"]:
            text += (" Part of this location is in a taxing district (such as the Metro East Transit District) that applies "
                     f"only to some businesses, which is why a range is shown; use {_FINDER} for an exact rate.")
        if not items and amount is not None:
            text += (" This assumes general merchandise (tangible personal property); Illinois presumes such property is "
                     "taxable unless an exemption applies, and groceries, medicine and some other items are taxed differently.")
        if kind == "general" and rec["county"] and re.search(r"-0001-", rec["location_id"]) is None and False:
            pass
        if note:
            text += " " + note
        text += f" Rates are from IDOR's file retrieved {_retrieved(conn)}; IDOR updates them each January and July."
        return _result(text, category, tax=tax_amount, citation=cite, source_url=_RATES_URL)
