"""Adversarial + correctness sweep for the address-level district-rate
feature (district_rates.py + engine._effective_rate's address-first
branch) -- the one remaining item from this project's original backlog.

Genuinely different from every other sweep in this project: this is the
FIRST feature whose correctness depends on a LIVE, non-Gemini external
service (CDTFA's public Tax Rate API, https://services.maps.cdtfa.ca.gov/)
staying up and returning consistent data -- not just LLM routing/compute
logic. Follows the SAME "hit the live dependency for real, don't mock it"
precedent already established for the Gemini-dependent sweeps (income_
item_sweep.py, item_sweep.py) rather than introducing a new, inconsistent
testing philosophy for this one feature.

Verified via direct browser fetch + curl BEFORE building any integration
code: the API is public (no auth/key), returns clean JSON, and --
importantly, not obvious from the docs alone -- a NONSENSE address does
NOT error, it returns a 200 with a low-confidence, wide-search-radius
geocode. district_rates.py gates on confidence=="High" specifically
because of this; several cases below lock that gate in as a permanent
regression guard, not just a one-off finding.

Usage:
  python district_rate_sweep.py run
  python district_rate_sweep.py report
  python district_rate_sweep.py reset
"""
import json
import os
import sys

import engine

CACHE = os.path.join(os.path.dirname(__file__), "district_rate_sweep_results.json")
TOL = 0.02

# Each item: (question, expected). expected keys checked only if present.
# "rate" checked to within TOL; "rate_basis_contains" checks a substring
# is present in rate_basis (used to confirm WHICH code path answered --
# address-level vs city-level vs statewide -- without hardcoding the
# full disclosure sentence).
ITEMS = [
    # --- basic address-level lookup (single, unambiguous, high confidence) ---
    ("what is the tax on a $500 couch at 450 N St, Sacramento, 95814",
     {"status": "answered", "domain": "sales", "taxable": True, "rate": 0.0875,
      "tax": 43.75, "rate_basis_contains": "address-level"}),
    # same address, different phrasing (spelled-out "California", "in" before city)
    ("what is the tax on a $500 couch at 450 N St in Sacramento, California 95814",
     {"status": "answered", "domain": "sales", "taxable": True, "rate": 0.0875,
      "rate_basis_contains": "address-level"}),

    # --- near-boundary AMBIGUOUS case: two different tax-rate areas
    # legitimately apply. Locks in that this is DISCLOSED (both rates
    # named in rate_basis), not silently resolved to one -- the whole
    # point of address-level precision is undermined if a genuine
    # boundary case pretends to be certain. ---
    ("what is the tax on a $500 couch at 2444 S Alameda St, Los Angeles 90058",
     {"status": "answered", "domain": "sales", "taxable": True,
      "rate_basis_contains": "near a tax-rate-area boundary"}),

    # --- graceful degradation: a nonsense/ungeocodable address must NOT
    # be trusted just because the API returns 200 -- confidence gate
    # must reject it and fall through to city/county (Sacramento is
    # still named, so local_rates.resolve should still find a city rate)
    # or statewide base, never a fabricated "precise" address rate.
    # LOCKS IN a real bug found by this sweep: the API returned
    # confidence="High" for this exact garbage street name while
    # silently substituting a different, real street ("10th Ave") --
    # district_rates.MIN_STREET_SIMILARITY exists specifically to catch
    # this, so this case must land on the CITY rate (0.0875), not an
    # address-level rate. ---
    ("what is the tax on a $500 couch at zzznotarealstreet Ave, Sacramento, 00000",
     {"status": "answered", "domain": "sales", "taxable": True, "rate": 0.0875,
      "rate_basis_contains": "combined rate"}),

    # --- a non-CA address (detection regex matches the SHAPE, but CDTFA
    # has no CA tax-rate area for it) -- must degrade to statewide base,
    # not crash and not fabricate a rate. NOTE: phrasing avoids "$500 couch"
    # and the White House's real address -- both independently found (while
    # building this feature) to trip a PRE-EXISTING, unrelated embedding-
    # routing sensitivity (dollar amounts combined with address-shaped text
    # push route_dist over threshold; "1600 Pennsylvania Ave" specifically
    # also collides with a "furnished dwelling" rule via its real-world
    # "residence" association) that has nothing to do with district_rates.py
    # -- confirmed by testing the same non-CA-address degradation with
    # plain "is furniture taxable" phrasing, which routes cleanly. ---
    ("is furniture taxable at 123 Main St, Portland 97201",
     {"status": "answered", "domain": "sales", "taxable": True,
      "rate_basis_contains": "statewide base"}),

    # --- precision guards: questions that must NOT trigger the address
    # path at all (no false-positive network calls on ordinary
    # city-only or no-location questions). ---
    ("what is the tax on $100 of furniture in Sacramento",
     {"status": "answered", "domain": "sales", "taxable": True, "rate": 0.0875,
      "rate_basis_contains": "combined rate"}),
    ("is furniture taxable in california",
     {"status": "answered", "domain": "sales", "taxable": True,
      "rate_basis_contains": "statewide base"}),
    # my own expectation here was WRONG when first written: this question
    # has no address at all, so it's answerable purely on the existing
    # (unrelated) pet-food-taxability rule -- confirming detect_address()
    # correctly does NOT false-positive on a bare quantity+noun sentence.
    ("I have 5 St Bernards, is pet food taxable",
     {"status": "answered", "domain": "sales", "category": "companion_animal_feed",
      "taxable": True}),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def _check(result, expected):
    for k, want in expected.items():
        if k == "rate":
            got = result.get("rate")
            if got is None or abs(float(got) - want) > TOL:
                return False, k
        elif k == "rate_basis_contains":
            basis = result.get("rate_basis") or ""
            if want not in basis:
                return False, k
        elif result.get(k) != want:
            return False, k
    return True, None


def run():
    cache = _load()
    graded = 0
    for q, exp in ITEMS:
        if q in cache:
            continue
        try:
            r = engine.answer(q, compose=False, source="district_rate_sweep")
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:150]}")
            break
        ok, fail_key = _check(r, exp)
        cache[q] = {
            "expected": exp, "ok": ok, "fail_key": fail_key,
            "status": r.get("status"), "domain": r.get("domain"),
            "taxable": r.get("taxable"), "rate": r.get("rate"), "tax": r.get("tax"),
            "rate_basis": r.get("rate_basis"),
        }
        graded += 1
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] {q[:65]:65} -> rate={r.get('rate')} basis={str(r.get('rate_basis'))[:60]}")
        _save(cache)
    print(f"\ngraded {graded} new; cached {len(cache)}/{len(ITEMS)}")
    report()


def report():
    cache = _load()
    if not cache:
        print("nothing graded yet")
        return
    ok = [item for item in cache.items() if item[1]["ok"]]
    bad = [item for item in cache.items() if not item[1]["ok"]]
    print(f"\n===== DISTRICT RATE SWEEP ({len(cache)} items) =====")
    print(f"correct : {len(ok)}")
    print(f"WRONG   : {len(bad)}")
    if bad:
        print("\n--- WRONG (fix these) ---")
        for q, v in bad:
            print(f"  {q}")
            print(f"    expected={v['expected']}  got status={v['status']} rate={v['rate']} "
                  f"basis={v['rate_basis']} (mismatch on: {v['fail_key']})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "run":
        run()
    elif cmd == "report":
        report()
    elif cmd == "reset":
        if os.path.exists(CACHE):
            os.remove(CACHE)
        print("cache cleared")
    else:
        print(__doc__)
