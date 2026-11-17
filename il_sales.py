"""Illinois sales and use tax lookups and computations, from the Illinois sales-tax database
(il_sales_schema.py). Pure functions over a connection; chat parsing lives in il_sales_engine.py.

Rates come from IDOR's county/municipality machine-readable file: for each location a current
rate period and the prior period, each with its own dates. 'Receipts' rates are the Retailers'
Occupation Tax charged by a retailer selling at that location; 'purchases' rates are use tax.
A location whose address flag is set has business-district or address-level rates the file
cannot give, so no rate is reported for it (never the file's placeholder zeros).
"""
import datetime
import re
from decimal import Decimal, ROUND_HALF_UP

import il_sales_db

# test hook: the sweeps pin the date so expected values don't drift when IDOR's January/July files change
AS_OF_OVERRIDE = None


def today():
    return AS_OF_OVERRIDE or datetime.date.today()


def money(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pct(rate):
    """0.105 -> '10.50%' (up to three decimals, at least two)."""
    p = (Decimal(str(rate)) * 100).normalize()
    text = f"{p:f}"
    if "." not in text:
        text += ".00"
    elif len(text.split(".")[1]) < 2:
        text += "0"
    return text + "%"


def proper(name):
    """'DUPAGE' -> 'DuPage', 'MCHENRY' -> 'McHenry' ... (title case with Illinois' odd capitals)."""
    fixed = {"DUPAGE": "DuPage", "DEKALB": "DeKalb", "LASALLE": "LaSalle", "JO DAVIESS": "Jo Daviess"}
    if name.upper() in fixed:
        return fixed[name.upper()]
    t = name.title()
    return re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), t)


def long_date(d):
    return f"{d:%B} {d.day}, {d.year}"


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[.,'’]", " ", text.upper())).strip()


# ---------------------------------------------------------------- locations
_LOC_COLS = ("location_id, name, county, address_flag, effective, receipts_gm_high, receipts_dm_high, receipts_gm_low, "
             "receipts_dm_low, receipts_flag, purchases_gm_high, purchases_dm_high, purchases_gm_low, purchases_dm_low, "
             "purchases_flag, prior_effective, prior_end, prior_receipts_gm_high, prior_receipts_dm_high, "
             "prior_receipts_gm_low, prior_receipts_dm_low, prior_receipts_flag, prior_purchases_gm_high, "
             "prior_purchases_dm_high, prior_purchases_gm_low, prior_purchases_dm_low, prior_purchases_flag, "
             "auto_high, auto_low, auto_med_fee_pct, auto_max_med_fee, auto_flag, prior_auto_high, prior_auto_low, "
             "prior_auto_med_fee_pct, prior_auto_max_med_fee, prior_auto_flag")
_LOC_KEYS = [c.strip() for c in _LOC_COLS.split(",")]


_CACHE = {}


def clear_cache():
    """Call after reseeding the database so a running process re-reads it."""
    _CACHE.clear()


def _load():
    if _CACHE:
        return _CACHE
    with connect() as conn:
        _CACHE["locations"] = [dict(zip(_LOC_KEYS, r)) for r in
                               conn.execute(f"SELECT {_LOC_COLS} FROM il_sales_locations").fetchall()]
        _CACHE["grocery"] = {r[0]: r[1:] for r in conn.execute(
            "SELECT location_id, effective, grocery_high, grocery_low, flag, prior_effective, prior_end, "
            "prior_grocery_high, prior_grocery_low, prior_flag FROM il_grocery_locations").fetchall()}
        item_keys = ("item_id", "description", "taxable", "rate", "explanation", "references")
        _CACHE["items"] = {r[0]: dict(zip(item_keys, r)) for r in conn.execute(
            'SELECT item_id, description, taxable, rate, explanation, "references" FROM il_sales_matrix_items').fetchall()}
        aliases = {}
        for alias, item_id in conn.execute("SELECT alias, item_id FROM il_sales_item_aliases").fetchall():
            aliases.setdefault(alias, []).append(item_id)
        _CACHE["aliases"] = aliases
        _CACHE["params"] = conn.execute(
            "SELECT param_key, effective_from, value FROM il_sales_params ORDER BY effective_from").fetchall()
        row = conn.execute("SELECT retrieved FROM il_sales_sources WHERE source_key='locations'").fetchone()
        _CACHE["retrieved"] = row[0] if row else None
    return _CACHE


def retrieved():
    return _load()["retrieved"]


def all_locations(conn=None):
    return _load()["locations"]


_COMMON_WORDS = frozenset({"NORMAL", "MASON", "HOPE", "UNION", "MARSHALL", "WARREN", "FRANKLIN", "GRANT", "PARK", "ROSE",
                           "ELMWOOD", "ILLINOIS", "COUNTY", "CHERRY", "PALMER", "OAKLAND", "SPRING", "BROWN", "CLAY",
                           "MARION", "LEWIS", "NEWTON", "ATHENS", "GREENFIELD", "FAIRFIELD", "SUMMIT", "FOREST",
                           "CHESTER", "WESTERN", "EASTERN", "NORTH", "SOUTH", "CENTRAL", "FREEDOM", "LIBERTY",
                           "UNITY", "PROGRESS", "PLEASANT", "SPRINGFIELD"})


_US_STATES = frozenset({
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT", "DELAWARE", "FLORIDA", "GEORGIA",
    "HAWAII", "IDAHO", "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN",
    "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO",
    "NEW YORK", "NORTH CAROLINA", "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON", "PENNSYLVANIA", "RHODE ISLAND",
    "SOUTH CAROLINA", "SOUTH DAKOTA", "TENNESSEE", "TEXAS", "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON", "WEST VIRGINIA",
    "WISCONSIN", "WYOMING"})


def find_places(question, locations, loose=False):
    """Places named in the question: [(normalized_name, [location records])]. A name counts only with a cue before it
    (in/at/for/from/near/of/to, optionally 'the city of'), or followed by COUNTY / IL / ILLINOIS, so ordinary words
    that happen to be village names are not picked up."""
    q = " " + norm(question) + " "
    by_name = {}
    for rec in locations:
        if norm(rec["name"]) != "ILLINOIS":    # IDOR's statewide placeholder record, not a place
            by_name.setdefault(norm(rec["name"]), []).append(rec)
    found = []
    for name in sorted(by_name, key=len, reverse=True):
        for m in re.finditer(r"(?<![A-Z])" + re.escape(name) + r"(?![A-Z])", q):
            before = q[:m.start()].rstrip()
            after = q[m.end():].lstrip()
            cue = bool(re.search(r"\b(?:IN|AT|FOR|FROM|NEAR|OF|TO|WITHIN)(?: THE)?(?: (?:CITY|VILLAGE|TOWN))?(?: OF)?$",
                                 before)) or name.endswith(" COUNTY")
            if loose and len(name) >= 6 and name not in _COMMON_WORDS and " " not in name.strip():
                cue = True
            if name in _US_STATES:
                cue = False          # a state name is the state unless it is written 'Washington, IL' / 'Washington County'
            tail = bool(re.match(r"(?:COUNTY|IL|ILLINOIS)\b", after))
            if cue or tail:
                q = q[:m.start()] + " " * (m.end() - m.start()) + q[m.end():]
                found.append((name, by_name[name]))
                break
    return found


def county_hints(question, locations):
    q = " " + norm(question) + " "
    counties = {norm(r["county"]) for r in locations if r["county"]}
    return {c for c in counties if re.search(r"(?<![A-Z])" + re.escape(c) + r" COUNTY\b|COUNTY OF " + re.escape(c) + r"\b", q)}


def resolve(records, hints):
    """Pick one record for a place name. Returns (record or None, note, ambiguous_counties)."""
    if len(records) == 1:
        return records[0], None, None
    narrowed = [r for r in records if norm(r["county"]) in hints] if hints else []
    if len(narrowed) == 1:
        return narrowed[0], None, None
    pool = narrowed or records
    seats = [r for r in pool if re.search(r"-0001-", r["location_id"])]
    if len(seats) == 1:
        others = ", ".join(sorted(proper(r["county"]) for r in pool if r is not seats[0]))
        return seats[0], (f"I used {proper(seats[0]['name'])} in {proper(seats[0]['county'])} County (the county seat); "
                          f"IDOR also lists locations of that name in {others} County. Name the county if you meant one "
                          f"of those."), None
    return None, None, sorted({proper(r["county"]) for r in pool})


# ---------------------------------------------------------------- rate periods
def period(rec, as_of):
    """The rate period in force on as_of: ('current'|'prior', values) or None if outside both periods."""
    if rec["effective"] and as_of >= rec["effective"]:
        kind, p = "current", ""
    elif rec["prior_effective"] and rec["prior_end"] and rec["prior_effective"] <= as_of <= rec["prior_end"]:
        kind, p = "prior", "prior_"
    else:
        return None
    return {"kind": kind,
            "start": rec["effective"] if kind == "current" else rec["prior_effective"],
            "end": None if kind == "current" else rec["prior_end"],
            "gm": (rec[p + "receipts_gm_low"], rec[p + "receipts_gm_high"]),
            "dm": (rec[p + "receipts_dm_low"], rec[p + "receipts_dm_high"]),
            "receipts_flag": rec[p + "receipts_flag"],
            "use_gm": (rec[p + "purchases_gm_low"], rec[p + "purchases_gm_high"]),
            "use_dm": (rec[p + "purchases_dm_low"], rec[p + "purchases_dm_high"]),
            "use_flag": rec[p + "purchases_flag"],
            "auto": (rec[p + "auto_low"], rec[p + "auto_high"]), "auto_flag": rec[p + "auto_flag"],
            "auto_med_fee_pct": rec[p + "auto_med_fee_pct"], "auto_max_med_fee": rec[p + "auto_max_med_fee"]}


def prior_of(rec, as_of):
    """The period before the one in force, when it ended recently (for 'changed on' notes)."""
    cur = period(rec, as_of)
    if cur and cur["kind"] == "current" and rec["prior_end"]:
        return {"gm": rec["prior_receipts_gm_high"], "dm": rec["prior_receipts_dm_high"], "end": rec["prior_end"]}
    return None


def grocery(conn, location_id, as_of):
    """Local grocery-tax period for a location, or None if as_of is before the state grocery rate ended."""
    row = _load()["grocery"].get(location_id)
    if row is None:
        return None
    eff, high, low, flag, p_eff, p_end, p_high, p_low, p_flag = row
    if as_of >= eff:
        return {"kind": "current", "start": eff, "low": low, "high": high, "flag": flag}
    if p_eff and p_end and p_eff <= as_of <= p_end:
        return {"kind": "prior", "start": p_eff, "low": p_low, "high": p_high, "flag": p_flag}
    return None


def state_grocery_rate(conn, as_of):
    return _param_value("state_grocery_rate", as_of)


def _param_value(key, as_of):
    best = None
    for k, eff, value in _load()["params"]:       # ordered by effective_from ascending
        if k == key and eff <= as_of:
            best = Decimal(value)
    return best


def param(conn, key, as_of=None):
    value = _param_value(key, as_of or today())
    if value is None:
        raise KeyError(f"Illinois sales parameter {key} not loaded")
    return value


# ---------------------------------------------------------------- items
def match_items(conn, question):
    """Matrix rows named in the question: [(alias, item dict)]. Longest alias first; matched text is blanked so a short
    alias can't re-match inside a longer one."""
    q = " " + re.sub(r"\s+", " ", question.lower().replace("’", "'")) + " "
    by_alias = _load()["aliases"]
    hits = []
    for alias in sorted(by_alias, key=len, reverse=True):
        m = re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?:e?s)?(?![a-z0-9])", q)
        if m:
            q = q[:m.start()] + " " * (m.end() - m.start()) + q[m.end():]
            hits.append((alias, by_alias[alias]))
    out, seen = [], set()
    for alias, ids in hits:
        for item_id in ids:
            if item_id in seen:
                continue
            seen.add(item_id)
            out.append((alias, _load()["items"][item_id]))
    return out


def grocery_class(item):
    """Matrix rows whose explanation says the local grocery tax still applies to a state-exempt item."""
    return item["taxable"] == "exempt" and "grocery tax" in (item["explanation"] or "").lower()


def connect():
    return il_sales_db.get_conn()


class _NoConnection:
    """Stands in for a database connection: all reads come from the in-process cache."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def lazy_conn():
    return _NoConnection()
