"""Adversarial item sweep: hunt unidentified routing/content gaps.

The failure class seen four times (bottled water, shipping ice, furniture,
toothpaste): an everyday item with no specific rule routes to a topically
adjacent rule with the WRONG verdict -> confidently wrong. Instead of waiting
for users to find the next one, brute-force probe ~90 common consumer items
whose correct verdict is high-confidence, and triage every mismatch.

Grades the PRIMARY verdict (branches noted). Results cached; re-run after fixes.

Usage:
  python item_sweep.py run [N]   # grade up to N new items (default: all)
  python item_sweep.py report
  python item_sweep.py reset
"""
import json
import os
import sys

import engine

CACHE = os.path.join(os.path.dirname(__file__), "item_sweep_results.json")

# (item, expected_taxable) -- only items with high-confidence CA verdicts.
# Ordinary tangible goods are taxable by default; False = a known exemption.
ITEMS = [
    # personal care / toiletries (taxable -- excluded from 'medicines' by Reg 1591)
    ("toothpaste", True), ("mouthwash", True), ("shampoo", True),
    ("deodorant", True), ("a bar of soap", True), ("lipstick", True),
    ("sunscreen", True), ("razor blades", True), ("perfume", True),
    ("adhesive bandages", True), ("vitamins", True), ("protein powder", True),
    ("reading glasses from a drugstore", True),
    # household consumables (taxable)
    ("toilet paper", True), ("paper towels", True), ("laundry detergent", True),
    ("dish soap", True), ("trash bags", True), ("light bulbs", True),
    ("AA batteries", True), ("scented candles", True),
    # electronics (taxable)
    ("a television", True), ("headphones", True), ("a phone charger", True),
    ("printer ink", True), ("a computer mouse", True), ("a video game console", True),
    ("a phone case", True),
    # clothing (taxable in CA -- no clothing exemption)
    ("a t-shirt", True), ("jeans", True), ("running shoes", True),
    ("a winter jacket", True), ("socks", True), ("a baseball cap", True),
    # home / furniture / kitchen (taxable)
    ("bath towels", True), ("bed sheets", True), ("a pillow", True),
    ("a mattress", True), ("an office desk", True), ("a table lamp", True),
    ("an area rug", True), ("dinner plates", True), ("a frying pan", True),
    ("a kitchen knife", True), ("a microwave oven", True), ("a coffee maker", True),
    ("a blender", True), ("a vacuum cleaner", True),
    # tools / auto / garden (taxable)
    ("a power drill", True), ("a hammer", True), ("house paint", True),
    ("a garden hose", True), ("a lawn mower", True), ("motor oil", True),
    ("a car battery", True), ("windshield wipers", True),
    # pets / sports / toys / misc (taxable)
    ("a dog leash", True), ("cat litter", True), ("a dog toy", True),
    ("a bicycle", True), ("a skateboard", True), ("a basketball", True),
    ("a camping tent", True), ("a sleeping bag", True), ("children's toys", True),
    ("a board game", True), ("a paperback book", True), ("a greeting card", True),
    ("gift wrap", True), ("a gold necklace", True), ("a wristwatch", True),
    ("sunglasses", True), ("a flower bouquet", True),
    # tobacco / alcohol / carbonated (taxable)
    ("cigarettes", True), ("beer", True), ("wine", True),
    ("a carbonated energy drink", True),
    # grocery staples (exempt food products)
    ("bread", False), ("eggs", False), ("cheese", False),
    ("coffee beans", False), ("tea bags", False), ("sugar", False),
    ("flour", False), ("cooking oil", False), ("ground black pepper", False),
    ("baby formula", False), ("jarred baby food", False),
    ("frozen vegetables", False), ("breakfast cereal", False),
    ("peanut butter", False), ("chewing gum", False), ("breath mints", False),
    # health exemptions (Reg 1591)
    ("prescription antibiotics", False), ("insulin", False),
    ("a hearing aid", False), ("a wheelchair", False), ("crutches", False),
    ("prescription eyeglasses", False),
    # statutory exemptions likely MISSING from the SUTR corpus (the hunt)
    ("diapers", False), ("tampons", False), ("menstrual pads", False),
    # services (not sales of tangible property)
    ("a haircut", False),
    # cannabis (adult-use retail is taxable -- NOT an exempt medicine)
    ("cannabis", True), ("marijuana", True), ("cannabis edibles", True),

    # --- batch 2: adversarial pairs targeting attractor risk near Phase 1-4 content ---
    # excise-adjacent retail sale -- confirm sales tax still applies even though
    # tobacco/alcohol/fuel EXCISE regs are now in the corpus as tier=administrative
    ("gasoline", True), ("diesel fuel", True),
    # digital vs tangible software -- Reg 1502, a genuine adjacent-pair attractor risk
    ("software downloaded online with no disc", False),
    ("software purchased on a CD", True),
    # food-producing vs ornamental seeds/plants -- Reg 1587 adjacent-pair
    ("vegetable seeds for a garden", False), ("rose bushes for landscaping", True),
    # occasional/private-party sale exemption
    ("furniture sold at a garage sale by a private individual", False),
    # office/hardware supplies not yet probed (taxable)
    ("printer paper", True), ("a stapler", True), ("a fire extinguisher", True),
    ("an extension cord", True), ("a filing cabinet", True),
    # clothing edge cases (taxable -- no CA clothing exemption)
    ("a swimsuit", True), ("a belt", True), ("a wedding dress", True),
    # grocery staple edge cases (exempt food products)
    ("canned soup", False), ("orange juice", False), ("granola bars", False),
    ("bottled olive oil", False),
    # OTC health vs true medicine near-neighbor (Reg 1591)
    ("a thermometer", True),
    # hearing aid via licensed dispenser -- exempt, distinct from the general
    # taxable auditory-device rule (a genuine branch-prone pair)
    ("a hearing aid from a licensed hearing aid dispenser", False),
    # repealed manufacturing-equipment exemption -- should default to ordinary taxable
    ("manufacturing equipment", True),

    # --- batch 3: found via query_log mining of real live traffic (2026-07-28) ---
    # both previously flagged as near-miss threshold gaps but never fixed;
    # confirmed still broken in live usage, fixed via new DISAMBIG entries.
    ("groceries", False), ("clothing", True),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def run(limit=999):
    cache = _load()
    graded = 0
    for item, exp in ITEMS:
        if item in cache or graded >= limit:
            continue
        q = f"Is {item} taxable in California?"
        try:
            r = engine.answer(q, compose=False, source="item_sweep")
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:120]}")
            break
        cache[item] = {
            "expected": exp, "status": r["status"], "key": r["category"],
            "taxable": r["taxable"],
            "branch_keys": [b["key"] for b in (r.get("branches") or [])],
        }
        graded += 1
        v = cache[item]
        flag = "OK " if (v["status"] != "needs_review" and v["taxable"] == exp) else \
               ("REV" if v["status"] == "needs_review" else "BAD")
        print(f"  [{flag}] {item:38} -> {v['key']} taxable={v['taxable']}")
        _save(cache)
    print(f"\ngraded {graded} new; cached {len(cache)}/{len(ITEMS)}")
    report()


def report():
    cache = _load()
    if not cache:
        print("nothing graded yet")
        return
    ok, bad, rev = [], [], []
    for item, v in cache.items():
        if v["status"] == "needs_review":
            rev.append((item, v))
        elif v["taxable"] == v["expected"]:
            ok.append((item, v))
        else:
            bad.append((item, v))
    print(f"\n===== ITEM SWEEP ({len(cache)} items) =====")
    print(f"correct primary verdict : {len(ok)}")
    print(f"WRONG verdict (danger)  : {len(bad)}")
    print(f"needs_review (safe gap) : {len(rev)}")
    if bad:
        print("\n--- WRONG (confidently wrong -- fix these) ---")
        for item, v in bad:
            br = f"  branches={v['branch_keys']}" if v["branch_keys"] else ""
            print(f"  {item:32} exp={v['expected']!s:5} got={v['taxable']!s:5} via {v['key']}{br}")
    if rev:
        print("\n--- NEEDS REVIEW (honest, but coverage gaps) ---")
        for item, v in rev:
            print(f"  {item}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "run":
        run(int(sys.argv[2]) if len(sys.argv) > 2 else 999)
    elif cmd == "report":
        report()
    elif cmd == "reset":
        if os.path.exists(CACHE):
            os.remove(CACHE)
        print("cache cleared")
    else:
        print(__doc__)
