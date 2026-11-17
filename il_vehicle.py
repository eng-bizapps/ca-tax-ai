"""Illinois vehicle tax computations (Illinois Department of Revenue): private party vehicle use tax
(Form RUT-50), tax on vehicles bought from a dealer (ST-556) or an unregistered out-of-state retailer
(RUT-25), and moving into Illinois with a vehicle. Pure functions; chat parsing is in il_vehicle_engine.py.

Sources (all in il_sales_data/): RUT-5 2026 chart, RUT-50 / RUT-25 / ST-556 instructions, RUT-6, FY 2026-34.
Rates for retail purchases are IDOR's auto rate columns in the county/municipality file: the vehicle is taxed
where it will be titled or registered.
"""
from decimal import Decimal, ROUND_HALF_UP

import il_sales as s

_CACHE = {}
SIX_COUNTIES = ("COOK", "DUPAGE", "KANE", "LAKE", "MCHENRY", "WILL")
METRO_EAST = ("MADISON", "ST. CLAIR")


def _load():
    if _CACHE:
        return _CACHE
    with s.connect() as conn:
        _CACHE["a"] = [(r[0], r[1], r[2], Decimal(r[3])) for r in conn.execute(
            "SELECT age_min, age_max, tax_year, tax FROM il_vehicle_pp_table_a ORDER BY age_min").fetchall()]
        _CACHE["b"] = [(Decimal(r[0]), None if r[1] is None else Decimal(r[1]), r[2], Decimal(r[3])) for r in conn.execute(
            "SELECT price_min, price_max, tax_year, tax FROM il_vehicle_pp_table_b ORDER BY price_min").fetchall()]
        _CACHE["local"] = [(r[0], r[1], r[2], r[3], Decimal(r[4])) for r in conn.execute(
            "SELECT jurisdiction, category, age_min, age_max, tax FROM il_vehicle_pp_local").fetchall()]
    return _CACHE


def clear_cache():
    _CACHE.clear()


def dollars(x):
    """The returns round to the whole dollar: drop under 50 cents, raise 50 cents or more."""
    return Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def param(key, as_of=None):
    return s.param(None, key, as_of)


def table_year():
    return _load()["a"][0][2]


# ---------------------------------------------------------------- private party (RUT-50)
def vehicle_age(title_year, model_year):
    """RUT-50 age table: year of the title application minus the vehicle's year (a newer model year counts as 0)."""
    return max(0, title_year - model_year)


def private_party_state(price, age, category="standard"):
    """State private party vehicle use tax. category: standard | exception | motorcycle | exempt.
    Returns (tax, basis text)."""
    price = Decimal(str(price))
    if category == "exempt":
        return Decimal(0), "an exemption applies, so the tax is $0"
    if category == "motorcycle":
        return param("veh_pp_motorcycle_tax"), "motorcycles, ATVs and similar vehicles pay a flat amount"
    if category == "exception":
        return param("veh_pp_exception_tax"), "the family / estate / reorganization exception is a flat amount"
    if price < param("veh_pp_table_threshold"):
        for lo, hi, _yr, tax in _load()["a"]:
            if age >= lo and (hi is None or age <= hi):
                return tax, f"Table A (value under $15,000), vehicle age {age}"
    for lo, hi, _yr, tax in _load()["b"]:
        if price >= lo and (hi is None or price <= hi):
            return tax, "Table B (value of $15,000 or more)"
    raise ValueError("no private party table row matched")


def private_party_local(jurisdictions, age, category="standard"):
    """Chicago / Cook County private party amounts (RUT-6). Returns [(jurisdiction, tax)]."""
    out = []
    for j in jurisdictions:
        for juris, cat, lo, hi, tax in _load()["local"]:
            if juris == j and cat == category and age >= lo and (hi is None or age <= hi):
                out.append((j, tax))
                break
    return out


# ---------------------------------------------------------------- retail purchase (ST-556 / RUT-25)
def auto_rate(rec, as_of):
    """(low, high, note) vehicle tax rate for the place a vehicle is titled. In the six Chicago-area counties IDOR's file
    carries a high rate that applies only to vehicles titled in the City of Chicago; Madison and St. Clair counties have
    a Metro East Transit District rate that applies only inside the district (a range is returned)."""
    per = s.period(rec, as_of)
    if per is None:
        return None
    lo, hi = per["auto"]
    if rec["name"].upper() == "CHICAGO":
        return hi, hi, "City of Chicago"
    if rec["county"] and rec["county"].upper() in METRO_EAST:
        return lo, hi, "Metro East"
    return lo, lo, None


def dealer_tax(rate, price, trade_in=0):
    base = max(Decimal(0), Decimal(str(price)) - Decimal(str(trade_in)))
    return base, dollars(base * Decimal(str(rate)))


def moving_in_tax(rate, price, months_out_of_state, tax_paid_elsewhere=0, individual=True):
    """RUT-25 for a vehicle bought and used outside Illinois before it is titled here. An out-of-state resident individual
    who used it outside Illinois for the exempt period or longer owes nothing; otherwise the price is depreciated by
    1.67% per whole month of out-of-state use. Returns a dict."""
    price = Decimal(str(price))
    exempt_months = int(param("veh_nonresident_exempt_months"))
    if individual and months_out_of_state >= exempt_months:
        return {"exempt": True, "depreciation": Decimal(0), "base": price, "tax": Decimal(0), "credit": Decimal(0), "due": Decimal(0)}
    dep = dollars(price * param("veh_depreciation_per_month") * months_out_of_state)
    base = price - dep
    tax = dollars(base * Decimal(str(rate)))
    credit = min(tax, Decimal(str(tax_paid_elsewhere)))
    return {"exempt": False, "depreciation": dep, "base": base, "tax": tax, "credit": credit, "due": tax - credit}
