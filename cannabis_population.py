"""DCC-licensed cannabis retailer city population -- the denominator for scaling
city-by-city Cannabis Business Tax research (see cannabis_local.py).

Source of truth: the California Department of Cannabis Control's own live
license-search backend (reverse-engineered from search.cannabis.ca.gov's
/config.js -> CANNA_API base URL; no auth, no CKAN dataset exists for this).
Filters to licenseTypeId=1 (Commercial - Retailer, i.e. storefront) and
licenseStatus=Active -- delivery-only/non-storefront and inactive licenses are
excluded since they're not where a consumer walks in and pays local tax.

Raw premiseCity values from the API are per-address, not per-municipality --
several are Los Angeles NEIGHBORHOODS (Venice, Van Nuys, Studio City...) that
fall under the City of Los Angeles's own ordinance, not separate cities; and
"W Hollywood"/"West Hollywood" are spelling variants of ONE real, separately-
incorporated city. Only confident, well-established mappings are normalized
here -- anything uncertain is left as its own raw jurisdiction rather than
guessed.

Usage:
  python cannabis_population.py fetch    # pull the live DCC list -> dcc_retailers_raw.json (cache)
  python cannabis_population.py build    # normalize + load cannabis_city_population table
  python cannabis_population.py status   # pending vs researched counts
"""
import json
import os
import sys
import time

import requests

import db

CANNA_API = "https://as-dcc-pub-cann-w-p-002.azurewebsites.net"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
RAW = os.path.join(os.path.dirname(__file__), "dcc_retailers_raw.json")

# Confident LA-CITY neighborhoods (not separate municipalities) -- established
# LA neighborhood geography, not a guess.
LA_NEIGHBORHOODS = {
    "ARLETA", "CANOGA PARK", "HARBOR CITY", "HOLLYWOOD", "MISSION HILLS",
    "N HOLLYWOOD", "NORTH HOLLYWOOD", "NORTH HILLS", "PACOIMA", "RESEDA",
    "SHERMAN OAKS", "STUDIO CITY", "SUN VALLEY", "SUNLAND", "SYLMAR", "TUJUNGA",
    "VALLEY VLG", "VAN NUYS", "VENICE", "WILMINGTON", "WOODLAND HLS", "PLAYA DEL REY",
}
# A separately incorporated city (own government/ordinance), NOT an LA
# neighborhood -- these are two spellings of the same real city.
WEST_HOLLYWOOD_VARIANTS = {"W HOLLYWOOD", "WEST HOLLYWOOD"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cannabis_city_population (
    jurisdiction    TEXT PRIMARY KEY,
    retailer_count  INTEGER NOT NULL,
    is_normalized   BOOLEAN NOT NULL DEFAULT FALSE,
    research_status TEXT NOT NULL DEFAULT 'pending',
    source          TEXT NOT NULL DEFAULT 'DCC active storefront retailer license search',
    as_of           DATE NOT NULL
)
"""


def fetch():
    records, page = [], 1
    while True:
        r = requests.get(
            f"{CANNA_API}/licenses/filteredSearch"
            f"?licenseTypeId=1&licenseStatus=Active&pageSize=250&page={page}",
            headers=H, timeout=30)
        j = r.json()
        data = j["data"]
        if not data:
            break
        records.extend(data)
        if page >= j["metadata"]["totalPages"]:
            break
        page += 1
        time.sleep(0.15)
    json.dump(records, open(RAW, "w", encoding="utf-8"))
    print(f"fetched {len(records)} active storefront retailer licenses -> {RAW}")


def build():
    if not os.path.exists(RAW):
        raise SystemExit(f"{RAW} missing -- run: python cannabis_population.py fetch")
    records = json.load(open(RAW, encoding="utf-8"))
    counts = {}
    for rec in records:
        city = (rec.get("premiseCity") or "").strip().upper()
        if not city:
            continue
        if city in LA_NEIGHBORHOODS:
            city = "LOS ANGELES"
        elif city in WEST_HOLLYWOOD_VARIANTS:
            city = "WEST HOLLYWOOD"
        counts[city] = counts.get(city, 0) + 1

    conn = db.get_conn()
    conn.execute(SCHEMA)
    conn.execute("TRUNCATE cannabis_city_population")
    for city, n in counts.items():
        conn.execute(
            "INSERT INTO cannabis_city_population (jurisdiction, retailer_count, is_normalized, as_of) "
            "VALUES (%s,%s,%s, CURRENT_DATE)",
            (city, n, city in ("LOS ANGELES", "WEST HOLLYWOOD")))
    # carry forward research_status for cities already resolved in cannabis_local_tax
    n_marked = conn.execute(
        "UPDATE cannabis_city_population SET research_status='researched' "
        "WHERE jurisdiction IN (SELECT jurisdiction FROM cannabis_local_tax)").rowcount
    print(f"loaded {len(counts)} jurisdictions ({len(records)} raw records); "
          f"{n_marked} already researched (from cannabis_local_tax)")
    conn.close()
    status()


def status():
    conn = db.get_conn()
    tot = conn.execute("SELECT count(*) FROM cannabis_city_population").fetchone()[0]
    pending = conn.execute(
        "SELECT count(*) FROM cannabis_city_population WHERE research_status='pending'").fetchone()[0]
    print(f"cannabis_city_population: {tot} jurisdictions total, {pending} pending, {tot - pending} researched")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "fetch":
        fetch()
    elif cmd == "build":
        build()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
