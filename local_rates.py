"""Local (city/county) combined sales-tax rates.

Source of truth: the California Open Data Portal dataset that powers CDTFA's own
"Find Your Tax Rate" application (CDTFA SalesandUseTaxRates Public). Each row is a
jurisdiction (an incorporated city, or a county's unincorporated area) with its
FULL COMBINED rate -- state base + county/city Bradley-Burns + district taxes all
included. Because the dataset already gives the combined rate, we never do
"7.25% + district" arithmetic (which would risk double-counting the 1.25% local
share); we use the jurisdiction's rate directly.

Granularity is city/county. Sub-city district pockets that only a full street
address resolves are NOT captured here -- for that, CDTFA's address API is needed.

Usage:
  python local_rates.py fetch     # download the dataset -> local_rates_raw.json
  python local_rates.py load      # create table + load + verify
  python local_rates.py verify    # re-run verification checks
  python local_rates.py resolve "San Francisco"   # test the resolver
"""
import json
import os
import re
import sys
from datetime import datetime

import db

RESOURCE_ID = "71cfc67f-af35-4edb-beac-224ad0d8e6ed"
RAW = os.path.join(os.path.dirname(__file__), "local_rates_raw.json")
BASE_RATE = 0.0725          # CA statewide minimum combined rate
RATE_CEILING = 0.1125       # current statewide maximum (guards against parse errors)

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_rates (
    jurisdiction TEXT PRIMARY KEY,
    county       TEXT NOT NULL,
    city         TEXT,
    city_proper  TEXT,
    rate         NUMERIC NOT NULL,
    start_date   DATE,
    as_of        DATE
);
"""


def fetch():
    import requests
    recs, off = [], 0
    while True:
        u = (f"https://data.ca.gov/api/3/action/datastore_search"
             f"?resource_id={RESOURCE_ID}&limit=1000&offset={off}")
        j = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).json()
        if not j.get("success"):
            raise SystemExit("data.ca.gov API returned success=false")
        batch = j["result"]["records"]
        recs += batch
        if len(batch) < 1000:
            break
        off += 1000
    json.dump(recs, open(RAW, "w", encoding="utf-8"))
    print(f"fetched {len(recs)} rate records -> {RAW}")


def _date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def load():
    if not os.path.exists(RAW):
        raise SystemExit(f"{RAW} missing -- run: python local_rates.py fetch")
    recs = json.load(open(RAW, encoding="utf-8"))
    conn = db.get_conn()
    conn.execute(SCHEMA)
    conn.execute("TRUNCATE local_rates")           # full refresh from the snapshot
    loaded = 0
    for r in recs:
        rate = float(r["RATE"])
        conn.execute(
            "INSERT INTO local_rates (jurisdiction, county, city, city_proper, rate, start_date, as_of) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (jurisdiction) DO UPDATE SET "
            "county=EXCLUDED.county, city=EXCLUDED.city, city_proper=EXCLUDED.city_proper, "
            "rate=EXCLUDED.rate, start_date=EXCLUDED.start_date, as_of=EXCLUDED.as_of",
            (r["JURIS_NAME"].strip().upper(), r["County_name"].strip().upper(),
             r["City_name"].strip().upper(), r["City_Name_Proper"].strip(),
             rate, _date(r.get("START_DATE")), _date(r.get("DateStamp"))),
        )
        loaded += 1
    print(f"loaded {loaded} jurisdictions")
    conn.close()
    verify()


def verify():
    conn = db.get_conn()
    n = conn.execute("SELECT count(*) FROM local_rates").fetchone()[0]
    counties = conn.execute("SELECT count(distinct county) FROM local_rates").fetchone()[0]
    lo, hi = conn.execute("SELECT min(rate), max(rate) FROM local_rates").fetchone()
    bad = conn.execute("SELECT jurisdiction, rate FROM local_rates "
                       "WHERE rate < %s OR rate > %s", (BASE_RATE, RATE_CEILING)).fetchall()
    # every county must have an unincorporated-area row, except consolidated
    # city-counties (San Francisco) which have no unincorporated territory.
    missing_uninc = conn.execute(
        "SELECT count(distinct county) FROM local_rates c "
        "WHERE c.county <> 'SAN FRANCISCO' AND NOT EXISTS "
        "(SELECT 1 FROM local_rates u WHERE u.county=c.county AND u.city='UNINCORPORATED')"
    ).fetchone()[0]
    # cross-check known published values (as of 2026 snapshot)
    checks = {"ALAMEDA": 0.1075, "BERKELEY": 0.1025}
    xchk = []
    for city, exp in checks.items():
        row = conn.execute("SELECT rate FROM local_rates WHERE jurisdiction=%s", (city,)).fetchone()
        got = float(row[0]) if row else None
        xchk.append((city, exp, got, "OK" if got == exp else "MISMATCH"))
    print(f"local_rates: {n} rows, {counties} counties, rate range {lo}-{hi}")
    print(f"  rows out of [{BASE_RATE}, {RATE_CEILING}] range: {len(bad)}  {bad[:5]}")
    print(f"  counties missing an unincorporated row: {missing_uninc}")
    for c, exp, got, st in xchk:
        print(f"  cross-check {c}: expected {exp} got {got} -> {st}")
    conn.close()
    ok = (n > 0 and len(bad) == 0 and missing_uninc == 0
          and all(x[3] == "OK" for x in xchk))
    print("VERIFY:", "PASS" if ok else "FAIL")
    return ok


# Unincorporated CA communities (census-designated places, no city government
# of their own -- fall under their COUNTY's unincorporated rate) that local_rates
# has no direct entry for, since it's keyed by incorporated city / county only.
# Sourced from the cannabis-city-tax research (2026-07-10), where each county
# attribution was independently confirmed from the county's own ordinance/code
# while researching that place's cannabis business tax -- not a guess. Kept
# deliberately small and confirmed-only rather than a general CA gazetteer
# (thousands of unincorporated places exist; guessing county attribution risks
# a wrong local rate, which is worse than "not found").
UNINCORPORATED_PLACES = {
    "MARINA DEL REY": "LOS ANGELES",
    "FELTON": "SANTA CRUZ",
    "BOULDER CREEK": "SANTA CRUZ",
    "SOQUEL": "SANTA CRUZ",
    "GUERNEVILLE": "SONOMA",
    "CASTROVILLE": "MONTEREY",
    "CARMEL": "MONTEREY",             # unincorporated Carmel Rancho/Mid-Valley area,
                                       # distinct from the incorporated Carmel-by-the-Sea
    "DIAMOND SPRINGS": "EL DORADO",
    "SHINGLE SPRINGS": "EL DORADO",
    "EL SOBRANTE": "CONTRA COSTA",
    "GUALALA": "MENDOCINO",
    "MENDOCINO": "MENDOCINO",         # the town, distinct from the county of the same name
    "MCKINLEYVILLE": "HUMBOLDT",
    "SAN ANDREAS": "CALAVERAS",
}
# proper display casing where str.title() gets it wrong (internal capitals,
# lowercase "del"/"de"/"la" particles conventionally kept lowercase in CA place names)
_PLACE_DISPLAY = {"MCKINLEYVILLE": "McKinleyville", "MARINA DEL REY": "Marina del Rey"}


# ---- resolution ----------------------------------------------------------

def resolve(conn, location: str):
    """Resolve a city or county name to its combined rate.

    Returns dict(rate, jurisdiction, county, label, level, as_of) or None.
    County input resolves to that county's UNINCORPORATED-area rate. A known
    unincorporated PLACE name (see UNINCORPORATED_PLACES) also resolves to its
    parent county's unincorporated rate, labeled with the place name so it's
    clear which specific community matched.
    """
    if not location:
        return None
    norm = re.sub(r"\s+", " ", location.strip().upper())
    norm = re.sub(r"^CITY OF ", "", norm)
    county_mode = norm.endswith(" COUNTY")
    if county_mode:
        norm = norm[:-len(" COUNTY")].strip()

    if not county_mode:
        row = conn.execute(
            "SELECT rate, jurisdiction, county, city_proper, as_of FROM local_rates "
            "WHERE city = %s AND city <> 'UNINCORPORATED'", (norm,)).fetchone()
        if row:
            return {"rate": float(row[0]), "jurisdiction": row[1], "county": row[2],
                    "label": f"{row[3]}, {row[2].title()} County", "level": "city",
                    "as_of": row[4]}
        place_county = UNINCORPORATED_PLACES.get(norm)
        if place_county:
            row = conn.execute(
                "SELECT rate, county, as_of FROM local_rates "
                "WHERE county = %s AND city = 'UNINCORPORATED'", (place_county,)).fetchone()
            if row:
                display = _PLACE_DISPLAY.get(norm, norm.title())
                # jurisdiction is the PLACE name itself (matching cannabis_local_tax's
                # keying convention for these same unincorporated communities), NOT the
                # county's raw jurisdiction string -- so a downstream lookup for a local
                # cannabis/other place-specific tax can actually find it
                return {"rate": float(row[0]), "jurisdiction": norm, "county": row[1],
                        "label": f"{display} (unincorporated {row[1].title()} County)",
                        "level": "place", "as_of": row[2]}
    # county / unincorporated
    row = conn.execute(
        "SELECT rate, jurisdiction, county, as_of FROM local_rates "
        "WHERE county = %s AND city = 'UNINCORPORATED'", (norm,)).fetchone()
    if row:
        return {"rate": float(row[0]), "jurisdiction": row[1], "county": row[2],
                "label": f"unincorporated {row[2].title()} County", "level": "county",
                "as_of": row[3]}
    # consolidated city-county (e.g. San Francisco): one jurisdiction, no unincorporated area
    rows = conn.execute(
        "SELECT rate, jurisdiction, county, as_of FROM local_rates WHERE county = %s", (norm,)).fetchall()
    if len(rows) == 1:
        r = rows[0]
        return {"rate": float(r[0]), "jurisdiction": r[1], "county": r[2],
                "label": f"{r[2].title()} (consolidated city-county)", "level": "county",
                "as_of": r[3]}
    return None


def detect(conn, question: str):
    """Conservative location detection from free text. Only fires on a clear cue
    ('... County' or 'in/at/to/for <known city>') to avoid false positives like
    the cities named Commerce, Industry, or Orange. Returns a location string or None."""
    q = question.strip()
    m = re.search(r"\b([A-Za-z][A-Za-z .'-]+?)\s+county\b", q, re.I)
    if m and resolve(conn, m.group(1) + " county"):
        return m.group(1) + " county"
    m = re.search(r"\b(?:in|at|to|for|near|within)\s+([A-Za-z][A-Za-z .'-]+?)(?:[?.,;]|\s+(?:ca|california)\b|$)", q, re.I)
    if m and resolve(conn, m.group(1)):
        return m.group(1)
    return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "fetch":
        fetch()
    elif cmd == "load":
        load()
    elif cmd == "verify":
        verify()
    elif cmd == "resolve":
        conn = db.get_conn()
        print(resolve(conn, " ".join(sys.argv[2:])))
        conn.close()
    else:
        print(__doc__)
