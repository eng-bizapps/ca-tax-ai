"""Completeness ledger for the CA property tax (county-assessor/BOE) domain
-- Ring 4's own inventory, mirroring form540_inventory.py's/schedule_ca_
inventory.py's structure exactly (tax_year, part, section, line_ref,
item_label, adjustment_type, frequency, citation, status, topic_key,
notes), but a GENUINELY NEW table (property_tax_inventory, via property_
schema.py) rather than a reuse of schedule_ca_inventory -- that table lives
in the income database, and form540_inventory.py's cross-part reuse trick
only works within one physical database. Ring 4 is a separate database by
design (see property_db.py).

Seeded from two primary-source research passes: one mapping the exact
existing architecture (to scope what a 3rd domain needs), one verifying
California property tax's actual mechanics directly against BOE
Publication 29/800-10, BOE's official Prop 19 page, Cal. Const. Art. XIII
A, and Rev. & Tax. Code (via leginfo.legislature.ca.gov) -- not secondary
tax-prep sources.

ONE CORRECTION FOUND AND FIXED BEFORE SHIPPING, worth remembering: the
initial Prop 19 base-year-value-transfer design omitted the 100%/105%/110%
timing-based comparison threshold entirely (computing as if every
replacement were purchased BEFORE the original home's sale). Verified via
BOE's own worked example (original FCV $400,000, FBYV $100,000, replacement
purchased within 1 year at FCV $600,000 -> the correct comparison uses
105% of $400,000 = $420,000, not $400,000 itself -> new taxable value
$280,000, not the $300,000 the initial formula produced) that this timing
adjustment is CURRENT law, not old superseded Prop 60/90 law -- independently
cross-checked via web search before trusting a single source. Fixed in
property_tax.compute_prop19_base_year_transfer before any regression case
was locked in.

status values -- same meanings as schedule_ca_inventory.py/form540_inventory.py:
  built                 -- feature exists and is wired into engine.py
  deferred_new_engine   -- tractable in principle, real population, but
                           genuinely needs more than a stated fact (a
                           per-parcel lookup, multi-year history, an
                           eligibility checklist) OR just hasn't been
                           built yet
  not_applicable        -- no viable path to build at all (no statewide
                           data source exists, not just "not built yet")

Usage:
  python property_tax_inventory.py load     # upsert the full inventory
  python property_tax_inventory.py status   # counts by status
  python property_tax_inventory.py list [status]   # list items, optionally filtered
"""
import sys

import property_db as db

TAX_YEAR = 2026  # lien year -- see property_tax.DEFAULT_LIEN_YEAR's own note

# (part, section, line_ref, item_label, adjustment_type, frequency, citation, status, topic_key, notes)
ITEMS = [
    ("core", None, "prop13-estimate", "Core Prop 13 base-year-value estimate (1% rate, 2%/yr inflation cap)",
     "both", "common", "Cal. Const. Art. XIII A Sec. 1(a), Sec. 2(b); Rev. & Tax. Code Sec. 51",
     "built", None,
     "Built via property_tax.compute_property_tax_estimate / engine._property_estimate_answer. "
     "Given purchase price + purchase year, compounds the factored base year value at the FULL "
     "2%/year cap (a deliberate OVERESTIMATE -- the real annual factor is the CA CPI change, "
     "usually less than 2%, same 'when simplifying, err toward overestimating' precedent as "
     "income_brackets.compute_itemized_deduction_phaseout) x the constitutional 1% rate. Does "
     "NOT model local ad-valorem bond add-ons or Prop 8 decline-in-value -- both disclosed in "
     "the answer text, not silently ignored."),
    ("core", None, "homeowners-exemption", "Homeowners' Exemption ($7,000 flat)",
     "subtraction", "common", "Cal. Const. Art. XIII Sec. 3(k); Rev. & Tax. Code Sec. 218",
     "built", None,
     "Modeled as a Python constant (property_tax.HOMEOWNERS_EXEMPTION_AMOUNT), not a table -- "
     "this figure is fixed directly in the CA Constitution and hasn't moved since Prop 13, unlike "
     "the annually-republished Disabled Veterans' Exemption below. If ever amended, promote to a "
     "tax_year-keyed table then, not now."),
    ("dv_exemption", None, "dv-exemption", "Disabled Veterans' Property Tax Exemption",
     "subtraction", "narrow", "Rev. & Tax. Code Sec. 205.5; BOE Letter To Assessors 2025/014",
     "built", None,
     "Built via property_tax.compute_disabled_veterans_exemption_ca / engine._property_dv_"
     "exemption_answer. Inflation-indexed, republished annually via BOE Letter To Assessors -- "
     "seeded with the 2 confirmed years (2025: $175,298 basic / $262,950 low-income / $78,718 "
     "income threshold; 2026: $180,671 / $271,009 / $81,131). Confirmed directly from BOE's own "
     "page that this exemption is MUTUALLY EXCLUSIVE with the Homeowners' Exemption (whichever is "
     "granted, the other is unavailable on the same property) -- DV always wins when eligible "
     "since it's always larger; compute_property_tax_with_dv_exemption does not accept a "
     "homeowners_exemption parameter at all as a result. When household income isn't stated, "
     "defaults to the basic (no-income-limit) tier and discloses that a higher tier may apply."),
    ("prop19", None, "prop19-transfer", "Prop 19 base-year-value transfer (age 55+/disabled/disaster)",
     "both", "moderate", "Rev. & Tax. Code Sec. 69.6; Cal. Const. Art. XIII A Sec. 2(a)",
     "built", None,
     "Built via property_tax.compute_prop19_base_year_transfer / engine._property_prop19_"
     "transfer_answer. Verified against BOE's own worked example exactly: new_taxable_value = "
     "original_byv + max(0, replacement_fcv - comparison_fcv), where comparison_fcv = "
     "original_fcv x (100%/105%/110%, depending on whether the replacement was purchased before "
     "the sale / within 1 year / within 2 years). A REAL BUG caught before shipping: the initial "
     "design omitted the 100/105/110% timing comparison entirely (always used 100%), which was "
     "independently found by reproducing BOE's own $280,000 worked-example answer and discovering "
     "the naive formula gave $300,000 instead -- traced to the missing timing adjustment, "
     "confirmed via a second independent source (web search) that this is CURRENT Prop 19 law, "
     "not superseded Prop 60/90 law, before fixing. Age-55/disabled claimants capped at 3 "
     "transfers statewide (Rev. & Tax. Code 69.6); disaster victims are NOT subject to this cap. "
     "Purchasing the replacement more than 2 years after the original sale is outright ineligible "
     "(not just a worse percentage) -- returns eligible=False, never silently computed as if "
     "allowed. Does NOT verify the original home's actual sale/destruction or same/different-"
     "county rules -- disclosed input assumptions, same 'trust the stated figures' precedent as "
     "every other compute function in this codebase."),
    ("excluded", None, "local-tra-rate", "Exact local ad-valorem add-on (voter-approved bonds by tax rate area)",
     "addition", "common", "BOE Pub. 29 p.6/p.16; LAO property tax primer",
     "deferred_new_engine", None,
     "Real, per-parcel data exists (each of 58 counties' own tax-rate-area rate tables), just "
     "not centrally ingested into this codebase yet -- genuinely buildable later, same complexity "
     "class as business-entity apportionment, NOT narrow enough to mark not_applicable. LAO's own "
     "figure: voter-approved debt rates average roughly one-tenth of 1% (~1.1% total ad-valorem) "
     "statewide, but real per-parcel variation exists across tens of thousands of TRAs -- the "
     "core estimate discloses this rather than guessing a single number."),
    ("excluded", None, "mello-roos", "Mello-Roos Community Facilities District special taxes",
     "addition", "moderate", "Mello-Roos Community Facilities Act of 1982, Gov. Code Sec. 53311 et seq.",
     "not_applicable", None,
     "No statewide registry exists at all -- unlike TRA rates (a real dataset just not yet "
     "ingested), there is no single source to ever ingest: each CFD sets its own Rate and Method "
     "of Apportionment (a flat per-parcel amount, per-square-foot charge, or other district-"
     "specific formula), and a taxpayer may not even know if their own parcel is inside a CFD. "
     "Structurally outside the 1% constitutional cap entirely, not a rate this assistant could "
     "approximate."),
    ("excluded", None, "prop8-decline", "Proposition 8 decline-in-value / lower-of-factored-or-market enrollment",
     "both", "moderate", "Cal. Const. Art. XIII A; BOE Pub. 800-10 p.1, p.3",
     "deferred_new_engine", None,
     "Multi-year, path-dependent state a single stated-facts question can't reconstruct -- same "
     "complexity class as AMT's multi-year-basis limitation. Correctly answering 'what is my "
     "assessed value today' for a property that has ever been under Prop 8 status requires "
     "either trusting a stated current assessed value (defeats the purpose of computing it) or "
     "tracking every intervening year's actual market-value enrollment, which almost no taxpayer "
     "reports accurately from memory."),
    ("excluded", None, "parent-child-exclusion", "Prop 19 parent-child/grandparent-grandchild exclusion eligibility",
     "both", "moderate", "Rev. & Tax. Code Sec. 63.2; BOE Pub. 29 p.10; BOE Prop 19 page",
     "deferred_new_engine", None,
     "Eligibility-CHECKLIST-shaped, not formula-shaped -- same class as HOH eligibility "
     "determination, not a simple stated fact. Post-2021 rules are genuinely narrow and easy to "
     "misapply: limited to a family home/family farm only, requires the transferee to move in "
     "and file for the homeowners'/disabled veterans' exemption within a year, grandparent-"
     "grandchild transfers only qualify if all intervening parents are deceased, and even a "
     "qualifying transfer carries a value cap (currently $1,044,586, adjusted every 2 years) "
     "above which the excess IS reassessed. A wrong 'yes it qualifies' assumption produces a "
     "wildly wrong number (full market-value reassessment) rather than a mildly-off one -- a bad "
     "category for 'trust and compute.'"),
    ("excluded", None, "supplemental-assessment", "Supplemental assessment on change of ownership/new construction",
     "both", "moderate", "Rev. & Tax. Code Sec. 75.11 et seq.; BOE Pub. 29 pp.11-12,16",
     "deferred_new_engine", None,
     "Tractable IF the applicable rate is known/assumed (same disclosed-approximation posture as "
     "the core estimate), but genuinely more complex than the base 3 slices: a BIMODAL fiscal-"
     "year proration rule (an event between Jan 1-May 31 triggers TWO supplemental assessments, "
     "one for the remainder of the current fiscal year and one for the entire next fiscal year; "
     "an event between Jun 1-Dec 31 triggers only ONE, prorated for the remainder of the current "
     "fiscal year), not a simple month-fraction. Left for a dedicated future slice rather than "
     "folded into v1's core estimate."),
]


def load():
    with db.get_conn() as conn:
        for part, section, line_ref, label, adj, freq, citation, status, topic_key, notes in ITEMS:
            conn.execute(
                "INSERT INTO property_tax_inventory "
                "(tax_year, part, section, line_ref, item_label, adjustment_type, "
                "frequency, citation, status, topic_key, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tax_year, line_ref, item_label) DO UPDATE SET "
                "adjustment_type=EXCLUDED.adjustment_type, frequency=EXCLUDED.frequency, "
                "citation=EXCLUDED.citation, status=EXCLUDED.status, "
                "topic_key=EXCLUDED.topic_key, notes=EXCLUDED.notes",
                (TAX_YEAR, part, section, line_ref, label, adj, freq, citation, status, topic_key, notes))
        current_keys = {(line_ref, label) for _, _, line_ref, label, *_ in ITEMS}
        existing = conn.execute(
            "SELECT id, line_ref, item_label FROM property_tax_inventory WHERE tax_year=%s",
            (TAX_YEAR,)).fetchall()
        orphan_ids = [row_id for row_id, line_ref, label in existing
                      if (line_ref, label) not in current_keys]
        if orphan_ids:
            conn.execute("DELETE FROM property_tax_inventory WHERE id = ANY(%s)", (orphan_ids,))
            print(f"pruned {len(orphan_ids)} orphaned row(s) (stale item_label from a rename/split)")
    print(f"loaded {len(ITEMS)} property tax inventory items")
    status_report()


def status_report():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT status, count(*) FROM property_tax_inventory WHERE tax_year=%s "
        "GROUP BY status ORDER BY count(*) DESC", (TAX_YEAR,)).fetchall()
    total = sum(r[1] for r in rows)
    print(f"\n=== PROPERTY TAX {TAX_YEAR} INVENTORY ({total} items) ===")
    for status, n in rows:
        print(f"  {status:28} {n}")
    conn.close()


def list_items(status_filter=None):
    conn = db.get_conn()
    q = "SELECT part, line_ref, item_label, status, topic_key FROM property_tax_inventory WHERE tax_year=%s"
    params = [TAX_YEAR]
    if status_filter:
        q += " AND status=%s"
        params.append(status_filter)
    q += " ORDER BY part, line_ref"
    rows = conn.execute(q, params).fetchall()
    for part, line_ref, label, status, topic_key in rows:
        loc = f"property ({part}) {line_ref}"
        tk = f" -> {topic_key}" if topic_key else ""
        print(f"  [{status:26}] {loc:36} {label}{tk}")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load()
    elif cmd == "status":
        status_report()
    elif cmd == "list":
        list_items(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
