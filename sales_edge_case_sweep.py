"""Adversarial hunt round on the SALES side (2026-08-08), mirroring the
income-side hunt's method: systematically audit every term-based
include/exclude list (DISAMBIG in engine.py, the fee REGIMES in fees.py)
for the same "missing inflected form" gap class that caused real bugs on
the income side, verify each candidate empirically before trusting it,
then lock the confirmed bugs in as a permanent regression suite -- neither
item_sweep.py (fixed "Is X taxable?" template, boolean-only) nor
cross_domain_sweep.py (domain-routing focus) can express these cases:
fee-presence assertions, or free-form sentences testing a specific
DISAMBIG entry's phrasing sensitivity.

FINDINGS THIS ROUND:
  fees.py's REGIMES lists were missing plural forms in several places
  (battery fee's vehicle-context words, eWaste's device words, cannabis
  excise's dispensary/cannabinoid words) -- unlike DISAMBIG/embedding
  routing, a missed fee is a silently INCOMPLETE answer (a real fee never
  surfacing), not a wrong verdict, but still worth closing since the
  compose layer states a specific total that would be understated.
  Fixed by adding the missing plurals directly (fees.py uses the same
  tokenized-word-set matching as DISAMBIG, not substring `in` checks, so
  the fix is "enumerate the missing word," not a stemming rewrite).

  One genuine embedding-routing miss (not a term-list gap): "I sold my
  airplanes privately" (plural, adverb "privately", no other qualifying
  word) landed on a semantically unrelated rule (export delivery to a US
  government agency, taxable=False) while the singular form already
  correctly found aircraft_retail_sale (taxable=True) unaided. Fixed with
  a narrowly-scoped new DISAMBIG entry (see engine.DISAMBIG) requiring the
  literal adverb "privately" specifically, so it can never shadow the
  already-correct, MORE SPECIFIC "...in a private sale" phrasing (which
  uses the adjective "private" + noun "sale", not the adverb).

  Also verified (no bug, confirms existing safety nets work): the
  private-party occasional-sale DISAMBIG entry's exclude list is missing
  "airplanes"/"wholesalers"/"manufacturers" (plural), but
  engine._verify_disambig_hit's embedding double-check already catches
  and corrects any wrong occasional_sale hit these gaps could otherwise
  cause -- confirmed by testing the exact scenario and seeing the correct
  taxable answer despite the exclude-list gap. Locked in below as a
  regression guard on that safety net specifically, not just luck.

Usage:
  python sales_edge_case_sweep.py run
  python sales_edge_case_sweep.py report
  python sales_edge_case_sweep.py reset
"""
import json
import os
import sys

import engine

CACHE = os.path.join(os.path.dirname(__file__), "sales_edge_case_sweep_results.json")

# Each item: (question, expected). expected keys checked only if present;
# "fee_ids" checks the SET of fees.applicable() ids present in result["fees"]
# (order-independent, exact set match).
ITEMS = [
    # --- fee plural-form gaps (fees.py REGIMES) ---
    ("what is the fee on batteries for my trucks",
     {"status": "answered", "domain": "sales", "fee_ids": {"battery"}}),
    ("what is the fee on batteries for my vehicles",
     {"status": "answered", "domain": "sales", "fee_ids": {"battery"}}),
    ("what is the eWaste fee on computers",
     {"status": "answered", "domain": "sales", "fee_ids": {"ewaste"}}),
    ("what is the eWaste fee on screens",
     {"status": "answered", "domain": "sales", "fee_ids": {"ewaste"}}),
    ("what is the eWaste fee on my notebooks",
     {"status": "answered", "domain": "sales", "fee_ids": {"ewaste"}}),
    ("what is the eWaste fee on my displays",
     {"status": "answered", "domain": "sales", "fee_ids": {"ewaste"}}),
    ("what is the eWaste fee on my chromebooks",
     {"status": "answered", "domain": "sales", "fee_ids": {"ewaste"}}),
    ("how much tax do I pay at the local dispensaries",
     {"status": "answered", "domain": "sales", "fee_ids": {"cannabis_excise"}}),
    # false-positive guard still holds after widening the battery group
    # (laptop/phone/watch batteries must never trigger the LEAD-ACID fee,
    # even though "laptops" is a real eWaste-group word now shared between
    # the two regimes). No product rule matches this odd phrasing either
    # (no purchasable item named), so it correctly defers rather than
    # guessing -- a safe gap, not a wrong answer.
    ("what is the battery fee on batteries for my phones",
     {"status": "needs_review", "domain": "sales", "fee_ids": set()}),

    # --- DISAMBIG: airplane/aircraft "privately" routing miss ---
    # singular baseline (already correct before this pass, locked in now
    # as a regression guard since it shares the fix's territory).
    ("I sold my airplane privately, do I owe tax",
     {"status": "answered", "domain": "sales", "category": "aircraft_retail_sale", "taxable": True}),
    # the actual bug: plural landed on an unrelated export-exemption rule
    # before the fix.
    ("I sold my airplanes privately, do I owe tax",
     {"status": "answered", "domain": "sales", "category": "aircraft_retail_sale", "taxable": True}),
    # precision guard: the new DISAMBIG entry must NOT shadow the already-
    # correct, more specific rule for "in a private sale" phrasing (which
    # uses "private" the adjective + "sale" the noun, not "privately" the
    # adverb) -- both singular and plural.
    ("I sold my airplane in a private sale, do I owe tax",
     {"status": "answered", "domain": "sales", "category": "private_party_vessel_or_aircraft_sale", "taxable": True}),
    ("I sold my airplanes in a private sale, do I owe tax",
     {"status": "answered", "domain": "sales", "category": "private_party_vessel_or_aircraft_sale", "taxable": True}),
    # exclude guard: chartering an aircraft is a different taxable event
    # (a lease/service question, not a retail sale) and must not be swept
    # into the new "privately sold" entry just because "aircraft" and
    # "privately" both appear. CAUGHT WHILE BUILDING THIS SUITE: the first
    # version of the new DISAMBIG entry's exclude list had "charter" but
    # not "chartering" -- the EXACT SAME stemming-gap class as every other
    # bug this hunt found, this time in code written minutes earlier in
    # the same pass. Fixed by enumerating rent/rents/rented/renting,
    # lease/leases/leased/leasing, charter/charters/chartered/chartering
    # fully instead of just the bare root. Correctly defers now (no
    # specific chartering rule exists in the corpus) rather than being
    # force-routed to a retail-sale verdict that doesn't fit a lease/
    # service fact pattern.
    ("is chartering an aircraft privately taxable",
     {"status": "needs_review", "domain": "sales"}),

    # --- _verify_disambig_hit safety net: confirms the occasional-sale
    # exclude-list's plural gaps (airplanes/wholesalers/manufacturers, not
    # yet individually patched) don't matter in practice because the
    # embedding double-check already overrides a wrong occasional_sale hit
    # whenever a specific, well-grounded alternative exists. ---
    ("a private individual sold his airplanes, is that an occasional sale",
     {"status": "answered", "domain": "sales", "taxable": True}),
    ("private individuals sold to wholesalers, is that an occasional sale",
     {"status": "answered", "domain": "sales", "taxable": True}),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def _check(result, expected):
    for k, want in expected.items():
        if k == "fee_ids":
            got = {f["id"] for f in (result.get("fees") or [])}
            if got != want:
                return False, k
        elif result.get(k) != want:
            return False, k
    return True, None


def _json_safe(expected):
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in expected.items()}


def run():
    cache = _load()
    graded = 0
    for q, exp in ITEMS:
        if q in cache:
            continue
        try:
            r = engine.answer(q, compose=False, source="sales_edge_case_sweep")
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:120]}")
            break
        ok, fail_key = _check(r, exp)
        cache[q] = {
            "expected": _json_safe(exp), "ok": ok, "fail_key": fail_key,
            "status": r.get("status"), "domain": r.get("domain"),
            "category": r.get("category"), "taxable": r.get("taxable"),
            "fee_ids": [f["id"] for f in (r.get("fees") or [])],
        }
        graded += 1
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] {q[:65]:65} -> status={r.get('status')} category={r.get('category')} "
              f"taxable={r.get('taxable')} fees={cache[q]['fee_ids']}")
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
    print(f"\n===== SALES EDGE-CASE SWEEP ({len(cache)} items) =====")
    print(f"correct : {len(ok)}")
    print(f"WRONG   : {len(bad)}")
    if bad:
        print("\n--- WRONG (fix these) ---")
        for q, v in bad:
            print(f"  {q}")
            print(f"    expected={v['expected']}  got status={v['status']} category={v['category']} "
                  f"taxable={v['taxable']} fees={v['fee_ids']} (mismatch on: {v['fail_key']})")


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
