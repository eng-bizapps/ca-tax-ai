"""CDTFA Tax Rate API -- address-level (sub-city district) sales tax rates.

The one remaining item on this project's original backlog: local_rates.py
gives city/county granularity (a single combined rate per jurisdiction),
but some addresses sit in a SUB-CITY special tax district whose boundary
doesn't align with city limits -- only a full street-address lookup
resolves those. CDTFA's own "Find Your Tax Rate" tool
(https://maps.cdtfa.ca.gov/) is powered by a PUBLIC, no-auth REST API
(https://services.maps.cdtfa.ca.gov/) -- confirmed by fetching its docs
AND testing it live (curl) before writing any integration code, same
"verify against reality, not just documentation" discipline as
everywhere else in this project. Live testing caught something the docs
don't emphasize enough: a NONSENSE address does NOT return an error --
it returns a "successful" 200 response with a low-confidence, wide-
search-radius geocode (tested: "asdkfjhaskldfjh, nowhere, 00000" came
back as "Trl to Nowhere, San Diego, CA" at confidence="Medium",
calcMethod="Interpolation", a 200-unit buffer). A caller that trusts
every 200 response blindly would be confidently wrong on a bad or
ambiguous address. This module gates on confidence == "High" before
ever treating a result as precise enough to state as a real address-
level rate.

confidence == "High" alone is NOT sufficient, either -- a second live
finding, caught by this project's own adversarial sweep (not the docs):
"zzznotarealstreet Ave, Sacramento, 00000" (a real city/zip, garbage
street) came back confidence="High", calcMethod="Interpolation", but
silently SNAPPED to a real, unrelated street ("10th Ave, Sacramento, CA
95818") -- the API is confident about the coordinates it landed on, not
about whether that location has anything to do with the street name it
was asked about. A caller trusting confidence alone would state a
precise-sounding rate for a street the user never gave. Fixed with a
second, independent gate: `_street_name_similarity` compares the input
street name against the geocoded result's street name (difflib ratio on
the name portion, house number and generic suffix stripped) and rejects
anything below `_MIN_STREET_SIMILARITY` -- confirmed this cleanly
separates genuine matches (ratio 1.0 on "450 N St" and "2444 S Alameda
St", both exact) from the fabricated one (ratio ~0.31 on
"zzznotarealstreet ave" vs "10th ave").

This is a genuinely different kind of dependency than everything else in
this project: a LIVE external network call at query time (like the
Gemini calls already made for compose=True), not static reference data
loaded once. Every failure mode (timeout, network error, non-200,
malformed JSON, low-confidence geocode) degrades GRACEFULLY to the
existing city/county-level local_rates.py path in engine.py -- this
module never raises, and a caller that gets None back falls through
exactly as if this feature didn't exist. A near-boundary match (the API
itself returns MULTIPLE taxRateInfo entries when a location is close to
a tax-rate-area boundary -- verified live: 2444 S Alameda St, Los
Angeles landed 0.0975 in one area and 0.105 in an adjacent one, both
"Good"/"High" confidence) is NOT treated as a failure -- it's returned
with ambiguous=True and the alternate(s), so the caller can DISCLOSE the
uncertainty rather than silently picking one rate or silently falling
back to the coarser city rate and hiding that a more precise answer was
almost available.

Usage (manual testing only -- engine.py is the real caller):
  python district_rates.py "450 N St" "Sacramento" 95814
"""
import difflib
import re
import sys

import requests

API_BASE = "https://services.maps.cdtfa.ca.gov/api/taxrate/GetRateByAddress"
TIMEOUT = 5
CITATION = "CDTFA Tax Rate API (address-level lookup)"
SOURCE_URL = "https://maps.cdtfa.ca.gov/"
MIN_STREET_SIMILARITY = 0.6

# Street-suffix-anchored address pattern: NUMBER + STREET NAME ending in a
# recognized suffix, then a CITY, then a 5-digit ZIP -- ALL FOUR pieces
# required, since the API itself requires street+city+zip. This can never
# collide with a bare city mention (existing local_rates.detect, no street
# number/suffix) or a dollar price in the same question (no ZIP-shaped
# 5-digit trailer immediately after a city-shaped word).
_STREET_SUFFIXES = (
    r"st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|way|"
    r"ct|court|pl|place|pkwy|parkway|cir|circle|hwy|highway|ter|terrace"
)
_ADDRESS_RE = re.compile(
    rf"\b(\d{{1,6}}[a-z]?)\s+([a-z][\w.'-]*(?:\s+[a-z][\w.'-]*){{0,5}}?\s+"
    rf"(?:{_STREET_SUFFIXES}))\.?,?\s+"
    r"(?:in\s+)?([a-z][a-z .'-]+?),?\s*(?:ca|california)?,?\s*"
    r"(\d{5})\b",
    re.I,
)


def detect_address(question: str):
    """Returns (street, city, zip) iff the question contains a clear
    street-address pattern, else None."""
    m = _ADDRESS_RE.search(question)
    if not m:
        return None
    street = re.sub(r"\s+", " ", m.group(1) + " " + m.group(2)).strip()
    city = re.sub(r"\s+", " ", m.group(3)).strip()
    zip_code = m.group(4)
    return street, city, zip_code


def _street_name(s: str) -> str:
    """Strips a leading house number and lowercases, for comparing just
    the street name portion (not the number, which interpolation can
    legitimately round to the nearest known address)."""
    return re.sub(r"^\s*\d+[a-z]?\s*", "", s.strip().lower())


def _street_name_similarity(input_street: str, formatted_address: str) -> float:
    """Compares the requested street name against the geocoded result's
    street name (the text before the first comma in formattedAddress).
    Low similarity means the API snapped to a different street than the
    one asked about, even if it reports confidence="High" for the
    coordinates it landed on."""
    a = _street_name(input_street)
    b = _street_name((formatted_address or "").split(",")[0])
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def lookup_by_address(street: str, city: str, zip_code: str):
    """Calls the CDTFA Tax Rate API directly. Returns a dict on a
    confident match (single or ambiguous-but-high-confidence); None on
    ANY failure -- network error, timeout, non-200, malformed JSON, an
    "errors" response, or confidence below "High"."""
    params = {"address": street, "city": city, "zip": zip_code}
    try:
        resp = requests.get(API_BASE, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "ca-tax-real/1.0"})
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if "errors" in data or not data.get("taxRateInfo"):
        return None
    geocode = data.get("geocodeInfo") or {}
    if geocode.get("confidence") != "High":
        return None
    if _street_name_similarity(street, geocode.get("formattedAddress")) < MIN_STREET_SIMILARITY:
        return None
    rates = data["taxRateInfo"]
    primary = rates[0]
    return {
        "rate": float(primary["rate"]),
        "jurisdiction": primary["jurisdiction"],
        "city": primary["city"],
        "county": primary["county"],
        "formatted_address": geocode.get("formattedAddress"),
        "ambiguous": len(rates) > 1,
        "alternates": [{"rate": float(r["rate"]), "jurisdiction": r["jurisdiction"]}
                       for r in rates[1:]],
        "citation": CITATION,
        "source_url": SOURCE_URL,
    }


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        raise SystemExit(1)
    street, city, zip_code = sys.argv[1], sys.argv[2], sys.argv[3]
    print(lookup_by_address(street, city, zip_code))
