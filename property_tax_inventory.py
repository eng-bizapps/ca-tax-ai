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
     "The remaining EXACT per-parcel gap, now that a county-WIDE AVERAGE is built separately (see "
     "'local-override-rate' below) -- real per-parcel data exists (each of 58 counties' own "
     "differently-formatted tax-rate-area rate books/lookup tools, e.g. LA County's own online TRA "
     "lookup vs. Kern County's own PDF rate book), just not centrally ingested into this codebase, "
     "genuinely buildable later but a real per-county scraping/data-engineering project, not a "
     "single source to ingest once. A taxpayer also wouldn't typically know their own TRA number "
     "without looking it up, unlike stating their county."),
    ("local_override_rate", None, "local-override-rate",
     "County-average voter-approved local ad-valorem override rate (bonds), by county",
     "addition", "common", 'California State Controller\'s Office, "CA Property Tax Data" portal, '
     "FY2025-26 Allocations + Levies by county",
     "built", None,
     "Built via property_tax.compute_county_override_rate / compute_property_tax_estimate_with_"
     "county_rate, engine._property_estimate_with_county_answer / _property_local_rate_with_"
     "county_answer. Real per-COUNTY (not per-parcel-TRA) average override rate, derived from the "
     "SCO portal's own JSON API: override_rate = county Levies / (county Allocations / 0.01) -- "
     "Allocations is the 1% base (lets you back out total assessed value), Levies is the total "
     "voter-approved local override. 56 of 58 counties seeded; San Benito and Plumas deliberately "
     "EXCLUDED as confirmed SCO data errors (San Benito's SCO-implied 5.32% override rate vs. its "
     "own bond tax-rate statement's normal ~0.025% scale; Plumas's SCO-implied 0.996% vs. its own "
     "county resolution's countywide total of 0.042%, ~24x off) -- the portal's own glossary "
     "disclaims responsibility for county-submitted data accuracy. Lookup uses 'most recent "
     "tax_year <= requested', NOT an exact match like ca_disabled_veterans_exemption -- county bond "
     "resolutions are adopted Aug-Sept and SCO aggregates afterward, so this data structurally lags "
     "DEFAULT_LIEN_YEAR by about a fiscal year; an exact-match lookup would silently break every "
     "year the moment DEFAULT_LIEN_YEAR is bumped. Still a county-WIDE AVERAGE, not exact-per-"
     "parcel -- see 'local-tra-rate' above for that remaining gap."),
    ("tra_rate_pilot", None, "tra-rate-kern-pilot",
     "Exact per-TRA local ad-valorem rate, 37 counties (Kern pilot + 36 more; ~42,000 TRAs): "
     "Alameda, Butte, Colusa, Contra Costa, El Dorado, Fresno, Humboldt, Imperial, Kern, Kings, Lake, "
     "Marin, Mariposa, Mono, Napa, Nevada, Orange, Placer, Riverside, Sacramento, San Diego, San Joaquin, "
     "San Luis Obispo, San Mateo, Santa Barbara, Santa Clara, Santa Cruz, Shasta, Sierra, Solano, Sonoma, "
     "Stanislaus, Sutter, Tehama, Tulare, Ventura, Yolo. "
     "Generalized from the Kern pilot by extract_tra_rates.py / load_tra_rates.py (per-county vintage "
     "in each row's tax_year; property_tax.TRA_FORMATS declares each county's TRA number shape). "
     "Data-integrity notes (every county's printed total is reconciled against its own listed funds "
     "where the file allows; a TRA that does not reconcile is skipped, never guessed): Santa Cruz 69-230 "
     "skipped and three Santa Cruz TRAs that print a total without the 1% levy excluded; Sacramento "
     "54-188/54-195/54-196 and Marin 94-023/94-056/94-061 skipped (printed total disagrees with the "
     "listed funds); Fresno prints 16 TRAs with no rate (left out); Riverside's 214 TRAs that carry a "
     "land-only levy are left out rather than understated, while Santa Barbara's 42 land-only levies "
     "(0.04268%) are not added; Santa Clara adds its land-and-improvements State Water Project rate; "
     "Kings has genuine sub-1% rates inside Westlands (negative tax reduction); San Mateo composites are "
     "4-decimal; Mono's source is a scanned exhibit, hand-transcribed and layered across overlapping TRA "
     "groups (cited as such); Shasta, Nevada and the older-vintage counties each carry their own "
     "tax_year. Seven of the new files (Santa Clara, Sacramento, Butte, Kings, Yolo, Sutter, Santa "
     "Barbara) were recovered by the user as archived copies of the county-hosted originals because "
     "the live county sites reject automated access. The other 21 counties keep the county-average "
     "fallback: Merced (FY2024-25 book only) and Yuba (2024-25 only) are one-to-two years stale and "
     "deliberately not loaded; Lassen and Mendocino have a located source that still could not be "
     "downloaded; Amador's resolutions are scanned and give no TRA mapping, Monterey publishes "
     "district rates only; Los Angeles is bot-gated and San Bernardino has no public source; 12 had "
     "no public rate table found (Alpine, Calaveras, Del Norte, Glenn, Inyo, Madera, Modoc, Plumas, "
     "San Benito, Siskiyou, Trinity, Tuolumne); San Francisco levies one citywide rate and already has "
     "a near-exact county average. "
     "ORIGINAL KERN-PILOT NOTE FOLLOWS:",
     "addition", "narrow", "Kern County Auditor-Controller-County Clerk, Annual Property Tax Rate "
     "Book, FY2025-26",
     "built", None,
     "A NARROW, EXPLICITLY-SCOPED PILOT for exactly ONE county (Kern) -- NOT a general solution to "
     "'local-tra-rate' above, which stays deferred for the other 57 counties. Built after a live "
     "survey confirmed exact TRA data is a genuine 58-county data-engineering problem with no shared "
     "format (LA County's own tool is bot-gated; Orange and San Diego each use a different PDF layout "
     "than Kern's; Riverside appears to require a paid physical copy; San Bernardino has no public "
     "machine-readable source found at all) -- rather than attempt all 58, this proves the pattern is "
     "real and usable for one. extract_kern_tra_rates.py (own docstring has full methodology) "
     "extracts Kern's own published rate book PDF via pdfplumber: words clustered into rows, split at "
     "an empirically-derived column gutter (a naive 50%-width crop was tested and rejected -- it "
     "misattributes words across the true column boundary), then a block-parsing state machine "
     "(AREA CODE header -> district line-items -> TOTAL) that correctly handles a real layout quirk "
     "found live -- a block too long for one column continues into the next with its header REPEATED "
     "as a continuation marker, which an earlier naive version mistook for a new block, orphaning the "
     "real one. Validated against the full document: 2,455 ordinary TRAs, 0 unclosed blocks, 0 "
     "duplicate codes, rate range 1.03%-1.27% (all plausible). `000-`-prefixed codes (state-assessed "
     "utility/railroad/pipeline categories) excluded -- not ordinary real property. Built via "
     "property_tax.compute_tra_rate / compute_property_tax_estimate_with_tra_rate, engine._property_"
     "estimate_with_tra_answer / _property_tra_rate_answer -- both run BEFORE their county-average "
     "counterparts (more specific wins) and both require Kern County AND an explicit TRA-number "
     "anchor phrase ('TRA', 'tax rate area', 'area code') to fire, falling through cleanly to the "
     "county-average otherwise. Same 'most recent tax_year <= requested' lookup design as 'local-"
     "override-rate', same reason."),
    ("excluded", None, "mello-roos", "Mello-Roos Community Facilities District special taxes",
     "addition", "moderate", "Mello-Roos Community Facilities Act of 1982, Gov. Code Sec. 53311 et seq.",
     "not_applicable", None,
     "No statewide registry exists at all -- unlike TRA rates (a real dataset just not yet "
     "ingested), there is no single source to ever ingest: each CFD sets its own Rate and Method "
     "of Apportionment (a flat per-parcel amount, per-square-foot charge, or other district-"
     "specific formula), and a taxpayer may not even know if their own parcel is inside a CFD. "
     "Structurally outside the 1% constitutional cap entirely, not a rate this assistant could "
     "approximate."),
    ("prop8_decline", None, "prop8-decline-market-value",
     "Proposition 8 decline-in-value, ordinary market decline (lower-of-factored-or-market enrollment)",
     "both", "moderate", "Rev. & Tax. Code Sec. 51(a)(2), (e); Cal. Const. Art. XIII A",
     "built", None,
     "REVERSAL of this session's own prior conclusion, reached 3 separate times, that this needed "
     "the property's full multi-year assessment history -- that assumption was WRONG, caught only "
     "by reading R&TC Sec. 51 directly in full (not a secondary source). Sec. 51(a)(1)'s factored "
     "base year value (FBYV) ceiling compounds PURELY from the original base year value at up to "
     "2%/year, NEVER reset or path-dependent on any intervening year's actual enrolled value; Sec. "
     "51(e) confirms the assessor just re-compares current full cash value against that SAME "
     "independently-compounding ceiling every year until it's exceeded again. So a current-year "
     "determination needs only the original purchase price/year (already used by the core estimate) "
     "plus one more trusted stated fact: current market value -- assessed_value = min(FBYV, "
     "current_market_value). Built via property_tax.compute_property_tax_prop8_decline / "
     "engine._property_prop8_decline_composed_answer. Composes with local rates (2026-10-31): "
     "when the question names a county (and TRA) the assessed value is taxed at the exact-TRA rate, "
     "else the county-average rate, else the 1% base (disclosed in the answer). See 'prop8-decline-damage-destruction' below for the genuinely different, "
     "disaster/destruction case, now handled as a Sec. 170 reassessment."),
    ("prop8_decline", None, "prop8-decline-damage-destruction",
     "Property damaged/destroyed by disaster: Sec. 170 reassessment of taxable value (stated before/after values)",
     "both", "moderate", "Rev. & Tax. Code Sec. 170(b), (g), (h); Sec. 51(b), (c)",
     "built", None,
     "Built after reading Sec. 170 directly in full from leginfo (not a secondary source): the assessor "
     "reduces the roll value by the percentage loss in full cash value ((before - after) / before, "
     "only if the loss is >= $10,000), and that damaged value compounds at the Sec. 51(a) factor until "
     "restored (170(g)); partial restoration adds back (factored base year value before the calamity - "
     "damaged value) x percent complete; full restoration returns the taxable value to the factored "
     "base year value. property_tax.compute_disaster_relief_reassessment / engine._property_prop8_"
     "damage_destruction_answer, taking stated purchase price/year, damage month+year (Jul-Dec -> that "
     "year's lien date, Jan-Jun -> prior year's), value before/after (or the dollar damage), and an "
     "optional % rebuilt. BOE's disaster-relief page says every county now has a Sec. 170 ordinance, so "
     "the answer assumes one covering the event and a timely application. DISCLOSED / NOT MODELED: the "
     "assessor, not the taxpayer, sets the before/after values (the answer trusts stated figures); one "
     "blended percentage rather than separate land/improvement/personalty percentages; the damage-year "
     "Sec. 70(c) new-construction exclusion; the "
     "5-year (8-year for LA/Ventura 2025 fires) rebuild window; the 2% cap used in place of actual "
     "CPI. BUILT 2026-10-31: the 170(e) damage-year proration (monthly, calamity month on the reduced "
     "side; year-of-calamity target allowed) and the 170(h) completion assessments (two if completed Jan-May, "
     "one if Jun-Dec; value added = new taxable value - taxable value on that roll). 170(h) tax is shown as a "
     "full-year equivalent because proration of those bills could not be confirmed (leginfo was unreachable; "
     "read via a statute summary cross-checked to BOE). Local rates are composed (2026-10-31): exact-TRA rate if a TRA is stated, else county average, "
     "else the 1% base (disclosed). Missing facts defer to needs_review with an example of what to state. "
     "The no-ordinance Sec. 51(b) path (land/improvements valued separately, new base year value) is "
     "not separately modeled."),
    ("parent_child_exclusion", None, "parent-child-exclusion", "Prop 19 parent-child/grandparent-grandchild exclusion eligibility",
     "both", "moderate", "Rev. & Tax. Code Sec. 63.2; Cal. Const. Art. XIII A Sec. 2.1(c); "
     "BOE Letter To Assessors 2026/026",
     "built", None,
     "Built as an eligibility-CHECKLIST-shaped determination, same tri-state True/False/None "
     "pattern as income_eligibility.py's HOH determination -- property_eligibility.detect_"
     "parent_child_exclusion_qualifies / engine._property_parent_child_exclusion_answer (+ a "
     "composed variant, _property_parent_child_composed_answer, when a dollar amount is also "
     "computable via property_tax.compute_parent_child_exclusion_value). Models: family home "
     "(requires the transferee to move in AND file for the homeowners'/disabled veterans' "
     "exemption, both within 1 year, no exceptions) vs. family farm (no occupancy/filing "
     "requirement at all); child/stepchild/in-law/adopted/foster relationships, including the "
     "divorce-ends-a-step/in-law-link rule; grandparent-grandchild transfers, gated on the "
     "grandchild's grandparent's-own-child parent being deceased; and the value cap (currently "
     "$1,044,586, adjusted every 2 years -- property_tax.PARENT_CHILD_EXCLUSION_VALUE_CAP) above "
     "which the excess IS reassessed. Also models a narrow hard-decline gate: a transfer stated "
     "as more than 3 years old AND already resold to a third party is categorically ineligible "
     "for retroactive relief. DISCLOSED as assumed rather than independently verified: the "
     "transferor's own prior Homeowners'/DV-Exemption eligibility on the family home. DEFERRED "
     "(returns None, never guessed): the biological-child-given-up-for-adoption exception, "
     "multi-generational step/in-law nuances inside the grandparent deceased-gate beyond the "
     "single flat fact, and split-parcel dual-category verdicts. One research inconsistency "
     "found and resolved before shipping: BOE's own LTA 2026/026 Q50 worked example doesn't "
     "reconcile against the current $1,044,586 cap figure (its numbers only work with a $1M "
     "addend) -- not used as a regression fixture; see property_tax.compute_parent_child_"
     "exclusion_value's docstring and property_item_sweep.py for a fresh self-consistent example."),
    ("supplemental_assessment", None, "supplemental-assessment", "Supplemental assessment on change of ownership/new construction",
     "both", "moderate", "Rev. & Tax. Code Sec. 75.11, 75.41; BOE Pub. 29 pp.11-12,16",
     "built", None,
     "Built via property_tax.compute_supplemental_assessment / engine._property_supplemental_"
     "assessment_answer. Models the BIMODAL R&TC 75.11 proration rule (an event between Jan 1-"
     "May 31 triggers TWO supplemental assessments -- one prorated for the remainder of the "
     "current fiscal year, plus, for a FULL-interest transfer, one unprorated for the entire next "
     "fiscal year; an event between Jun 1-Dec 31 triggers only ONE, prorated for the remainder of "
     "the current fiscal year) using property_tax.SUPPLEMENTAL_PRORATION_FACTOR, a 12-entry table "
     "cross-confirmed against both R&TC 75.41(c)'s own table and BOE's separately published page. "
     "Correctly implements the R&TC 75.41(b) presumed-effective-date rounding rule (always the "
     "1st of the FOLLOWING calendar month) including its two rollover edge cases (a June event "
     "rolls into the next fiscal year at factor 1.00 but stays in the 'one supplemental' bucket; "
     "a December event rolls into the next CALENDAR year for the presumed date but stays in the "
     "SAME fiscal year as the event). Uses the same 1% PROP13_BASE_RATE as the core estimate -- "
     "no special supplemental rate exists -- and discloses the same local-TRA-rate-gap caveat. "
     "DEFERRED (disclosed, not guessed): the partial-interest transfer's second-supplemental "
     "formula, which needs facts (remainder/whole-property taxable values on the roll being "
     "prepared) this codebase's 'trust one question's stated figures' shape can't safely obtain -- "
     "the first supplemental is still computed in full even when the second is deferred. The "
     "interspousal-transfer and qualifying-parent-child-exclusion exemptions from any supplemental "
     "at all are disclosed in the answer text, not actively cross-checked against the parent-child "
     "feature's own verdict in this v1."),
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
