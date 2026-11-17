"""Chat answers for Illinois vehicle tax: private party purchases (RUT-50), purchases from a dealer (ST-556) or an
out-of-state retailer (RUT-25), and moving into Illinois with a vehicle. Deterministic and keyword-gated like
il_sales_engine; every figure comes from il_vehicle.py / the Illinois sales database. The tax depends on who sold the
vehicle and where it will be titled, so a question that leaves either out gets a clarifying answer, not a guess.
"""
import re
from decimal import Decimal

import il_engine as ie
import il_sales as s
import il_vehicle as v

_FORMS = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/sales/documents/vehicleusetax/"
_RUT5_URL = _FORMS + "rut-5.pdf"
_RUT25_URL = _FORMS + "rut-25-instr.pdf"
_ST556_URL = _FORMS + "st-556-instr.pdf"

_VEHICLE = re.compile(r"\b(car|cars|truck|pickup|suv|van|minivan|sedan|coupe|motorcycle|atv|vehicle|automobile|"
                      r"all[- ]terrain|motor[- ]driven cycle|honda|toyota|ford|chevy|chevrolet|nissan|subaru|bmw|tesla|"
                      r"mazda|kia|hyundai|jeep|dodge|gmc|lexus|audi|mercedes|volkswagen|vw|volvo|harley|cadillac|buick|"
                      r"chrysler|acura|infiniti|porsche|camry|civic|corolla|accord|f-150|silverado|mustang)\b")
_TAXISH = re.compile(r"\btax(?:es)?\b|rut-?\d+|st-?556|\bowe\b|use tax")
_PRIVATE = re.compile(r"private|craigslist|marketplace|offerup|by owner|from (?:a |an |my |our )?(?:neighbor|neighbour|"
                      r"friend|coworker|co-worker|stranger|individual|person|someone|relative|family|dad|mom|father|"
                      r"mother|parent|brother|sister|son|daughter|child|spouse|wife|husband|uncle|aunt|cousin|"
                      r"grandfather|grandmother|grandparent|step|in-law)|\bgift(?:ed)?\b|inherit|estate|rut-50|"
                      r"sold (?:it |the car |me )?to me|sold me|gave me")
_DEALER = re.compile(r"dealer|dealership|showroom|carmax|carvana|st-556|new car|brand new|retailer")
_OUT_OF_STATE = re.compile(r"out[- ]of[- ]state|rut-25|from a dealer in|outside (?:of )?illinois")
_FAMILY = re.compile(r"(?:from|gave me|sold(?: it| the \w+)? to me)[^.?!]{0,40}?\b(?:my |a )?(?:dad|mom|father|mother|parent|brother|"
                     r"sister|son|daughter|child|spouse|wife|husband)\b|from a (?:parent|sibling|brother|sister|child|spouse)|"
                     r"\bmy (?:dad|mom|father|mother|parent|brother|sister|son|daughter|spouse|wife|husband)\b[^.?!]{0,25}?"
                     r"\b(?:gave|sold|gifted|transferred|handed)")
_NOT_FAMILY = re.compile(r"step-?(?:dad|mom|father|mother|brother|sister|son|daughter|parent)|in-law|grand(?:father|mother|"
                         r"parent|son|daughter|child)|cousin|uncle|aunt|nephew|niece")
_LEASE = re.compile(r"\blease|\bleasing|\bleased")
_OTHER_CRAFT = re.compile(r"\b(?:boat|watercraft|jet ski|aircraft|airplane|helicopter|trailer|mobile home|snowmobile)\b")
_MOTORCYCLE = re.compile(r"motorcycle|\batv\b|all[- ]terrain|motor[- ]driven cycle")
_MOVING = re.compile(r"moved|moving|relocat|brought (?:it |the \w+ )?(?:in)?to illinois|bringing (?:it |the \w+ )?(?:in)?to illinois")

_ROLES = (
    ("trade", ("trade-in", "trade in", "traded in", "trading in", "trade")),
    ("paid_tax", ("in sales tax", "sales tax", "in tax", "tax paid", "paid tax", "tax to", "tax in", "tax there")),
    ("price", ("for ", "price", "paid", "cost", "bought", "purchased", "buying", "buy ", "fair market value",
               "market value", "worth", "valued")),
)


_NOT_A_SALE = re.compile(r"car wash|oil change|repair|insurance|registration fee|license plate|parking|rental|rent a|"
                         r"gas tax|motor fuel|detailing|towing")


def is_vehicle_question(q):
    if _NOT_A_SALE.search(q):
        return False
    return bool((_VEHICLE.search(q) or _OTHER_CRAFT.search(q)) and _TAXISH.search(q)) or bool(
        re.search(r"rut-?(?:25|50)|st-?556", q))


def _result(text, category, tax=None, amount=None, status="answered", citation=None, source_url=None):
    return ie._result(text, category, tax=tax, amount=amount, status=status, citation=citation,
                      source_url=source_url) | {"domain": "sales"}


def _review(text, category):
    return _result(text, category, status="needs_review")


def _fmt(x):
    return ie._fmt(x)


def _model_year(q, amounts_spans, title_year):
    m = re.search(r"\b(19[89]\d|20[0-3]\d)\b", q)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,2})[- ]?(?:years?|yrs?)[- ]old\b", q)
    if m:
        return title_year - int(m.group(1))
    if re.search(r"\bbrand new\b|\bnew (?:car|truck|suv|van|vehicle|motorcycle)\b", q):
        return title_year
    return None


def _months(q):
    m = re.search(r"(\d+)\s*months?", q)
    return int(m.group(1)) if m else None


def _place(question, locations):
    places = [(n, r) for n, r in s.find_places(question, locations) if n != "ILLINOIS"]
    if not places:
        return None, None, None
    if len(places) > 1:
        return None, None, _review("I found more than one place in your question (" + ", ".join(s.proper(n) for n, _ in places)
                                   + "). A vehicle is taxed where it will be titled; tell me that one place.",
                                   "il_vehicle_multiple_places")
    hints = s.county_hints(question, locations)
    rec, note, ambiguous = s.resolve(places[0][1], hints)
    if rec is None:
        return None, None, _review(f"There is more than one {s.proper(places[0][0])} in IDOR's files (in "
                                   f"{', '.join(c + ' County' for c in ambiguous)}), and vehicle tax rates differ by "
                                   f"county. Which county will the vehicle be titled in?", "il_vehicle_ambiguous_place")
    return rec, note, None


def _jurisdictions(rec):
    out = []
    if rec["name"].upper() == "CHICAGO":
        out.append("chicago")
    if rec["county"] and rec["county"].upper() == "COOK":
        out.append("cook")
    return out


def _place_label(rec):
    return f"{s.proper(rec['name'])} ({s.proper(rec['county'])} County)"


def _rate_text(lo, hi):
    return s.pct(lo) if lo == hi else f"{s.pct(min(lo, hi))} to {s.pct(max(lo, hi))}"


# ---------------------------------------------------------------- answers
def _private_party(question, q, rec, note, as_of, price, age, family, estate, motorcycle):
    if as_of.year != v.table_year():
        return _review(f"IDOR's private party vehicle tax tables that I have are for {v.table_year()}; they are republished "
                       f"each year, so I can't give the amount for {as_of.year}.", "il_vehicle_unsupported_year")
    category = "standard"
    reason = ""
    if "surviving spouse" in q and (estate or "estate" in q or "inherit" in q):
        category, reason = "exempt", "an estate gift to a surviving spouse is exempt"
    elif motorcycle:
        category = "motorcycle"
    elif family or estate:
        category = "exception"
    jur = _jurisdictions(rec) if rec else []
    needs_age = (category == "standard" and (jur or (price is not None and price < v.param("veh_pp_table_threshold")))) or (
        category == "exception" and "chicago" in jur)
    if category == "standard" and price is None:
        return _review("A private party vehicle's tax is based on its purchase price (or fair market value for a gift), so I "
                       "need the price. A trade-in doesn't reduce this tax. Tell me the price, the vehicle's model year, and "
                       "where it will be titled (Chicago and Cook County add local amounts).", "il_vehicle_missing_info")
    if needs_age and age is None:
        return _review("The private party tax for a vehicle under $15,000, and the Chicago and Cook County amounts, depend on "
                       "the vehicle's age. Tell me its model year.", "il_vehicle_missing_info")
    base_price = price if price is not None else Decimal(0)
    state, basis = v.private_party_state(base_price, age if age is not None else 0, category)
    lines = [f"Illinois private party vehicle use tax (Form RUT-50, {v.table_year()}): state tax {_fmt(state)} ({basis})."]
    total = state
    if jur:
        local_cat = category if category != "exempt" else "exempt"
        locals_ = v.private_party_local(jur, age if age is not None else 0, local_cat) if category != "exempt" else []
        names = {"chicago": "City of Chicago", "cook": "Cook County"}
        for j, amount in locals_:
            lines.append(f"{names[j]} private party vehicle tax: {_fmt(amount)}.")
            total += amount
        if locals_:
            lines.append(f"Total due on the RUT-50: {_fmt(total)}. (Chicago and Cook County amounts come from IDOR's RUT-6 "
                         f"guide, edition R-03/24; IDOR says local rates can change each January and July.)")
    elif rec is None:
        lines.append("If the vehicle will be titled at an address in the City of Chicago or Cook County, local private party "
                     "taxes are added (Chicago $80/$65/$50/$0 and Cook County $225/$175/$90 by vehicle age, per RUT-6); tell "
                     "me where it will be titled and I'll add them.")
    else:
        lines.append(f"{_place_label(rec)} has no local private party vehicle tax in IDOR's RUT-6 guide.")
    lines.append("No trade-in deduction is allowed on this tax. The form is due within 30 days of acquiring the vehicle and is "
                 "needed to get the title.")
    if note:
        lines.append(note)
    return _result(" ".join(lines), "il_vehicle_private_party", tax=float(total),
                   citation="IDOR RUT-5 (2026), RUT-50 instructions (R-04/26), RUT-6 (R-03/24)", source_url=_RUT5_URL)


def _retail(question, q, rec, note, as_of, price, trade, paid_tax, out_of_state):
    if price is None:
        return _review("To compute Illinois tax on a vehicle bought from a dealer I need the purchase price (and any trade-in "
                       "credit) and the city or county where it will be titled.", "il_vehicle_missing_info")
    if rec is None:
        return _review("A vehicle is taxed at the rate for the place it will be titled or registered: 6.25% in most of the state, "
                       "7.25% in DuPage, Kane, Lake, McHenry and Will counties, 7.50% in Cook County outside Chicago, 8.75% for "
                       "an address in the City of Chicago, and 6.25% to 6.50% in Madison and St. Clair counties (rates since "
                       "August 1, 2026). Tell me the city or county and I'll compute it.", "il_vehicle_missing_place")
    rate = v.auto_rate(rec, as_of)
    if rate is None:
        return _review(f"IDOR's rate file for {_place_label(rec)} doesn't cover {s.long_date(as_of)}.", "il_vehicle_unsupported_date")
    lo, hi, kind = rate
    base_lo, tax_lo = v.dealer_tax(lo, price, trade)
    _b, tax_hi = v.dealer_tax(hi, price, trade)
    place = _place_label(rec)
    form = "RUT-25 (out-of-state retailer)" if out_of_state else "ST-556 (Illinois dealer)"
    head = f"Illinois tax on a {_fmt(price)} vehicle titled in {place}, as of {s.long_date(as_of)}, Form {form}: "
    parts = []
    if trade:
        parts.append(f"{_fmt(price)} price - {_fmt(trade)} trade-in credit = {_fmt(base_lo)} taxable")
    else:
        parts.append(f"{_fmt(base_lo)} taxable")
    parts.append(f"x {_rate_text(lo, hi)}")
    text = head + " ".join(parts)
    if lo == hi:
        text += f" = {_fmt(tax_lo)} (rounded to the whole dollar, as the return does)"
        due = tax_lo
    else:
        text += (f" = between {_fmt(tax_lo)} and {_fmt(tax_hi)} (6.25% outside the Metro East Transit District, 6.50% inside it; "
                 f"your township decides)")
        due = tax_hi
    if out_of_state and paid_tax:
        credit_lo = min(tax_lo, Decimal(str(paid_tax)))
        credit_hi = min(tax_hi, Decimal(str(paid_tax)))
        text += f". Credit for the {_fmt(paid_tax)} of sales tax you paid elsewhere (proof required)"
        if lo == hi:
            text += f": {_fmt(tax_lo - credit_lo)} is due"
            due = tax_lo - credit_lo
        else:
            text += f": {_fmt(tax_lo - credit_lo)} to {_fmt(tax_hi - credit_hi)} is due"
            due = tax_hi - credit_hi
    text += "."
    if kind == "City of Chicago":
        text += " For an address in the City of Chicago the rate includes the 1.25% Chicago Home Rule Use Tax."
    text += (" Manufacturer rebates don't reduce the taxable price, and trade-in credit applies only to the amount shown on "
             "the dealer's bill of sale. In the Metro East Transit District part of St. Clair County a fee of up to $20 may "
             "also apply.") if (rec["county"] or "").upper() in v.METRO_EAST else (
        " Manufacturer rebates don't reduce the taxable price, and trade-in credit applies only to the amount shown on "
        "the dealer's bill of sale.")
    text += (" The dealer files Form ST-556 within 20 days of delivery." if not out_of_state else
             " You file Form RUT-25 within 30 days of bringing the vehicle into Illinois.")
    if note:
        text += " " + note
    return _result(text, "il_vehicle_dealer_tax", tax=float(due),
                   citation="IDOR county/municipality auto rates; ST-556 (R-12/24) and RUT-25 (R-07/26) instructions; FY 2026-34",
                   source_url=_RUT25_URL if out_of_state else _ST556_URL)


def _moving_in(question, q, rec, note, as_of, price, paid_tax, months):
    if price is None or months is None:
        return _review("For a vehicle you bought and used outside Illinois before titling it here I need the purchase price, how "
                       "many months you used it outside Illinois, and where it will be titled. An individual who was an "
                       "out-of-state resident and used it outside Illinois for 3 months or more owes no Illinois use tax "
                       "(you still file Form RUT-25).", "il_vehicle_missing_info")
    if rec is None:
        return _review("I also need the city or county where the vehicle will be titled, because the rate depends on it.",
                       "il_vehicle_missing_place")
    rate = v.auto_rate(rec, as_of)
    if rate is None:
        return _review(f"IDOR's rate file for {_place_label(rec)} doesn't cover {s.long_date(as_of)}.", "il_vehicle_unsupported_date")
    lo, hi, _k = rate
    if lo != hi:
        return _review("Your township in that county decides whether the Metro East rate applies, so I can't give one figure; "
                       "tell me the township or use IDOR's rate finder.", "il_vehicle_unresolved_district")
    r = v.moving_in_tax(lo, price, months, paid_tax or 0)
    if r["exempt"]:
        text = (f"No Illinois use tax: an individual who was an out-of-state resident and used the vehicle outside Illinois for "
                f"{months} months (3 or more) is exempt (RUT-25 Step 4, line f). You still file Form RUT-25 within 30 days of "
                f"bringing it into Illinois and surrender the out-of-state title. The exemption doesn't apply to businesses "
                f"relocating, or to military members whose home of record is Illinois.")
        return _result(text, "il_vehicle_moving_in", tax=0.0, amount=0.0, citation="IDOR RUT-25 instructions (R-07/26)",
                       source_url=_RUT25_URL)
    text = (f"Illinois use tax on a {_fmt(price)} vehicle bought and used outside Illinois for {months} month(s) before titling "
            f"it in {_place_label(rec)}: depreciation of 1.67% per whole month = {_fmt(r['depreciation'])}, so "
            f"{_fmt(r['base'])} is taxable at {s.pct(lo)} = {_fmt(r['tax'])}")
    if paid_tax:
        text += f", less {_fmt(r['credit'])} credit for sales tax paid elsewhere (proof required), so {_fmt(r['due'])} is due"
    text += ". File Form RUT-25 within 30 days of bringing the vehicle into Illinois. This is for an individual; businesses and military members with an Illinois home of record are treated differently."
    if note:
        text += " " + note
    return _result(text, "il_vehicle_moving_in", tax=float(r["due"]), citation="IDOR RUT-25 instructions (R-07/26)",
                   source_url=_RUT25_URL)


def answer_vehicle(question, q, locations, as_of):
    if _LEASE.search(q):
        return _review("Vehicle leases are reported on different returns (ST-556-LSE for Illinois lessors, RUT-25-LSE for "
                       "out-of-state lessors) and I haven't loaded those rules, so I can't compute tax on a lease.",
                       "il_vehicle_lease")
    if _OTHER_CRAFT.search(q) and not _VEHICLE.search(q):
        return _review("Boats, aircraft, trailers and similar titled items are taxed under other rules (for example Form RUT-75 "
                       "for watercraft and aircraft) that I haven't loaded. I can help with cars, trucks, vans, motorcycles and "
                       "ATVs.", "il_vehicle_unsupported")
    masked = re.sub(r"(?<![$\d,.])\b(?:19[89]\d|20[0-2]\d)\b(?![,\d])", "YEAR", question)
    amounts = ie._amounts(masked)
    roles, unassigned = ie._roles(masked, _ROLES)
    price = roles.get("price")
    if price is None and len(unassigned) == 1 and not roles.get("trade") and not roles.get("paid_tax"):
        price = unassigned[0]
    price = None if price is None else Decimal(str(price))
    trade = Decimal(str(roles.get("trade", 0)))
    paid_tax = roles.get("paid_tax", 0)
    title_year = as_of.year
    model_year = _model_year(q, amounts, title_year)
    age = None if model_year is None else v.vehicle_age(title_year, model_year)
    rec, note, early = _place(question, locations)
    if early:
        return early
    motorcycle = bool(_MOTORCYCLE.search(q))
    family = bool(_FAMILY.search(q)) and not _NOT_FAMILY.search(q)
    estate = bool(re.search(r"inherit|estate", q)) and not re.search(r"surviving spouse", q)

    if _MOVING.search(q) and (_months(q) is not None or "illinois" in q):
        return _moving_in(question, q, rec, note, as_of, price, paid_tax, _months(q))

    private = bool(_PRIVATE.search(q)) or family or estate
    dealer = bool(_DEALER.search(q)) or bool(_OUT_OF_STATE.search(q))
    out_of_state = bool(_OUT_OF_STATE.search(q)) or bool(
        [n for n in ie._named_states(q) if n != "illinois"] and re.search(r"dealer|bought|purchased", q))
    if private and dealer and not (family or estate):
        return _review("Your question mentions both a dealer and a private seller, and they are taxed differently (a private "
                       "party purchase pays a flat tax from a chart on Form RUT-50; a dealer sale pays a percentage of the "
                       "price). Which was it?", "il_vehicle_ambiguous_source")
    if private:
        return _private_party(question, q, rec, note, as_of, price, age, family, estate, motorcycle)
    if dealer or out_of_state:
        return _retail(question, q, rec, note, as_of, price, trade, paid_tax, out_of_state)
    return _review("Illinois taxes a vehicle differently depending on who sold it. From a private person (including a gift or "
                   "inheritance) you pay a flat private party tax from a chart (2026: for example $290 on a 3-year-old vehicle "
                   "under $15,000, $850 on one worth $15,000-$19,999) plus local amounts in Chicago and Cook County. From a "
                   "dealer you pay a percentage of the price (less any trade-in) at the rate for where it will be titled "
                   "(6.25% to 8.75%). Which applies to you, and where will it be titled?", "il_vehicle_ask_source")
