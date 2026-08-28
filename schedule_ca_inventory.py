"""Completeness ledger for Schedule CA (540) -- every CA/federal conformity
line item found via primary-source research (FTB's 2025 "Instructions for
Schedule CA (540), California Adjustments -- Residents", full ~90-item
inventory extracted by an agent that read the entire ~104K-character
instructions document, not sampled). Mirrors corpus_manifest's
pending->stored->ruled precedent -- exists specifically because the
original Phase 3 plan flagged that full conformity coverage "deserves its
own completeness ledger, not a bullet point" and then one was never built,
which is exactly why this session had to re-research the whole form from
scratch instead of picking up where a ledger would have left off.

This is HAND-RESEARCHED reference data living in code, not crawled --
same "verified truth lives in code" precedent as local_rates.py/fees.py.
citations are R&TC section numbers where the source instructions gave one
directly; where they didn't (most narrow/deferred items), citation is left
generic ("Schedule CA (540) instructions, line X") since a dedicated rule
was never going to be built for those anyway.

status values:
  built                     -- income_tax_topics row exists, topic_key set
  pending                   -- targeted THIS pass (Tier 1)
  deferred_itemized_engine  -- Tier 2: extends _income_itemized_answer,
                               not a new topic lookup -- own future phase
  deferred_new_engine       -- Tier 3: needs history/basis-tracking no
                               single Q&A fact can supply (IRA basis, CFC/
                               GILTI, NOL, excess business loss) -- same
                               complexity class as business entities/trusts
  not_applicable            -- Tier 4: narrow/one-time, covered by a
                               generic "other adjustments may apply, see
                               FTB Pub. 1001" disclaimer instead of a
                               dedicated rule -- not worth individual build

Usage:
  python schedule_ca_inventory.py load     # upsert the full inventory
  python schedule_ca_inventory.py status   # counts by status
  python schedule_ca_inventory.py list [status]   # list items, optionally filtered
"""
import sys

import income_db as db

TAX_YEAR = 2025

# (part, section, line_ref, item_label, adjustment_type, frequency, citation, status, topic_key, notes)
ITEMS = [
    # ============ PART I, SECTION A -- Income (lines 1a-7a) ============
    ("I", "A", "1a/1h", "Native American earned income exemption (tribal land income)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec A line 1a/1h; requires FTB 3504",
     "not_applicable", None, "Requires tribal-membership + reservation-source facts; narrow population"),
    ("I", "A", "1a-1h", "Tax treaty income exclusion addback",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 1a-1h",
     "not_applicable", None, "Nonresident-alien treaty claimants only"),
    ("I", "A", "1a-1h", "Sick pay under FICA/Railroad Retirement Act",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec A line 1a/1h",
     "not_applicable", None, None),
    ("I", "A", "1a", "Employee vs. independent contractor reclassification (Prop 22)",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 1a",
     "not_applicable", None, "Worker-classification fact pattern, not a simple boolean"),
    ("I", "A", "1d", "IHSS supplementary payments (In-Home Supportive Services providers)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec A line 1d",
     "not_applicable", None, None),
    ("I", "A", "1h", "Ridesharing fringe benefit differences",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec A line 1h",
     "not_applicable", None, None),
    ("I", "A", "1h", "California Qualified Stock Option (CQSO) exclusion",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec A line 1h",
     "not_applicable", None, "Narrow eligibility: earned income <=$40k, option value <$100k, <=1000 shares"),
    ("I", "A", "1h", "Employer HSA contribution (W-2 box 12 code W)",
     "addition", "moderate", "Sched CA (540) Pt I Sec A line 1h",
     "built", "hsa_contributions_and_earnings", "Part of HSA non-conformity cluster, already covered"),
    ("I", "A", "1i", "Combat zone extended to Sinai Peninsula (nontaxable combat pay election)",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 1i",
     "not_applicable", None, None),
    ("I", "A", "2", "US savings bonds / Treasury bills-notes-bonds interest",
     "subtraction", "common", "Sched CA (540) Pt I Sec A line 2",
     "built", "us_government_bond_interest", None),
    ("I", "A", "2", "Non-CA state/municipal bond interest",
     "addition", "common", "Sched CA (540) Pt I Sec A line 2",
     "built", "out_of_state_municipal_bond_interest", None),
    ("I", "A", "2", "HSA investment interest/earnings (taxable for CA)",
     "addition", "moderate", "Sched CA (540) Pt I Sec A line 2",
     "built", "hsa_contributions_and_earnings", "Part of HSA non-conformity cluster, already covered"),
    ("I", "A", "2", "Parent/child interest income shift (FTB 3803)",
     "both", "narrow", "Sched CA (540) Pt I Sec A line 2",
     "not_applicable", None, None),
    ("I", "A", "3", "CFC dividends / RIC capital gains / pre-1987 S-corp distributions",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 3",
     "not_applicable", None,
     "Reclassified 2026-08-14 after research: bundling confirmed accurate (FTB 2025 "
     "instructions list all three verbatim as Column C additions), but each is a genuinely "
     "narrow population -- CFC dividends (requires owning a controlled foreign corporation), "
     "RIC undistributed capital gains (requires a Form 2439, rare even among mutual fund "
     "investors), pre-1987 S-corp distributions (aging/closing population, comparable to the "
     "1986-87 three-year-rule annuity election item). SB 711's conformity-date change does "
     "NOT affect any of the three (verified against R&TC 17024.5/17088's actual amended text "
     "-- these are specific-provision carve-outs, not general date-conformity items). Covered "
     "by the generic FTB Pub. 1001 disclaimer instead of a dedicated feature."),
    ("I", "A", "4a/4b", "IRA distribution basis/timing differences",
     "both", "moderate", "Sched CA (540) Pt I Sec A line 4a/4b; FTB Pub 1005",
     "built", None,
     "Built via income_brackets.compute_ira_distribution_basis_diff_ca_tax / "
     "engine._income_ira_distribution_basis_diff_answer -- a thin wrapper reusing "
     "compute_generic_basis_diff_ca_tax's exact math. This was the item the basis-difference "
     "batch was originally named for, but got built LAST (7th of 7) after 'trust the stated "
     "already-computed figure' was applied to Lines 5-6 (rental/farm depreciation) first while "
     "sweeping Schedule CA in line order. The original 'requires historical CA-vs-federal "
     "contribution basis' framing conflated deriving the divergence with simply asking for it: "
     "the taxpayer's own federal and California Form 1099-R-derived taxable-distribution figures "
     "are exactly the two already-computed numbers Line 4a/4b needs, same precedent as every "
     "other item this pass. Own anchor vocabulary ('federal/California taxable distribution', "
     "not 'capital gain', since a distribution isn't a gain). Own out-of-scope population: Roth "
     "conversions and early distributions each have their own distinct mechanic, already modeled "
     "elsewhere, so this feature explicitly redirects rather than silently mis-answering. Zero "
     "extraction bugs found live -- seventh and final basis-difference item this pass, benefiting "
     "from every lesson (detect_compute_signal exclusion, dispatcher ordering before the generic "
     "feature) applied proactively before verification rather than discovered by it."),
    ("I", "A", "5a/5b", "Tier 2 railroad retirement benefits",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec A line 5a/5b",
     "not_applicable", None, None),
    ("I", "A", "5a/5b", "1986-87 three-year-rule annuity election",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 5a/5b",
     "not_applicable", None, "Aging/closing population"),
    ("I", "A", "5a/5b", "Military retirement pay / DoD Survivor Benefit Plan exclusion",
     "subtraction", "moderate", "R&TC 17132.9, 17132.10; Sched CA (540) Pt I Sec A line 5a/5b",
     "built", "military_retirement_exclusion", "NEW for TY2025-2029; $20k cap per payment type, AGI cliff $125k single/$250k MFJ/surviving spouse"),
    ("I", "A", "6", "Social Security / Tier 1 railroad retirement benefits",
     "subtraction", "common", "Sched CA (540) Pt I Sec A line 6",
     "built", "social_security_income", None),
    # Line 7a's original single bundled entry was split 2026-08-14 after research
    # found NINE distinct FTB-confirmed divergence triggers, not four, with very
    # different tractability profiles -- see the per-item entries below.
    ("I", "A", "7a", "QSBS gain exclusion/deferral addback (IRC 1202/1045)",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 7a; Schedule D (540) Column (e); R&TC 18152.5",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-14: compute_qsbs_ca_tax in income_brackets.py. California does NOT "
     "conform to the federal QSBS exclusion/deferral AT ALL -- 100% addback, confirmed by "
     "Cutler v. FTB (2012) and R&TC 18152.5's repeal of CA's own QSBS provisions, unaffected "
     "by SB 711 (CA still doesn't conform to OBBBA's 2025 QSBS expansion either). Requires "
     "TWO stated figures (federal taxable gain + excluded amount) rather than guessing which "
     "a single figure means. Also closed a phantom-amount bug: literal '1202'/'1045' in "
     "question text parses as a bare number via the shared amount-extraction regex, same "
     "collision class as cannabis 280E's phantom $280 parse."),
    ("I", "A", "7a", "HSA-held investment sale gain addback",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 7a; Schedule D (540)",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-14: compute_hsa_investment_gain_ca_tax in income_brackets.py. CA doesn't "
     "recognize HSA tax-shelter status at all, so a realized investment-sale gain is fully "
     "CA-taxable the year it occurs with NO federal counterpart (unlike QSBS, only one "
     "adjustment figure needed). GAINS only -- losses are ordinary capital losses subject to "
     "the existing $3,000/$1,500-MFS annual limit, nothing HSA-specific about that case. "
     "Needed a narrower purpose-built exclude list (HSA_GAIN_COMPLEXITY_EXCLUDE) rather than "
     "the shared COMPLEXITY_EXCLUDE, since HSA gain taxability is orthogonal to how other "
     "income was earned (self-employment doesn't disqualify it, unlike IRA deduction). Falls "
     "through correctly to the pre-existing hsa_contributions_and_earnings informational "
     "topic when facts are missing or a loss is mentioned."),
    ("I", "A", "7a", "Pass-through capital gain from CA Schedule K-1 (100S/541/565/568)",
     "addition", "moderate", "Sched CA (540) Pt I Sec A line 7a; Schedule D (540) Line 2",
     "built", "k1_pass_through_capital_gain_tax",
     "Built 2026-08-14: reuses compute_k1_ca_tax's math unchanged (K-1 pass-through income "
     "already built on Line 5) with the correct Schedule D Line 2 citation instead of Schedule "
     "CA Line 5 -- CA taxes capital gains as ordinary income, no special rate, so the "
     "arithmetic is identical, only the citation differs. Needed its own detect function "
     "(K1_COMPLEXITY_EXCLUDE deliberately excludes 'capital gain' from the ordinary K-1 path) "
     "wired in BEFORE the existing K1 fallback catch-all, since 'k-1 capital gain' contains "
     "the bare 'k-1' substring the fallback matches on. GAINS only -- K-1 capital LOSSES use "
     "the standard $3,000/$1,500-MFS annual-limit mechanic instead, out of scope here."),
    ("I", "A", "7a", "CA capital loss carryover from prior year's Schedule D (540)",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec A line 7a; Schedule D (540) Line 6",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-14: resident-all-prior-years case, reusing compute_capital_loss_ca_tax's "
     "math UNCHANGED (a carryover loss faces the SAME $3,000/$1,500-MFS annual limit as any "
     "capital loss -- the real gap was disclosure, not arithmetic: the generic current-year "
     "path already computed the correct number for carryover-labeled questions since 'capital "
     "loss' is a substring, but its answer text wrongly claimed a current-year-loss assumption "
     "-- fixed with a dedicated path giving the accurate resident-all-prior-years disclosure, "
     "checked BEFORE the generic path in the dispatcher). Nonresident/part-year-resident "
     "history excluded -- FTB requires a year-by-year recalculation this model can't perform."),
    ("I", "A", "7a", "Generic CA/federal basis differences (pre-1987, depreciation, inherited property, credits)",
     "both", "moderate", "2025 Schedule CA (540) Instructions -- Part I, Section A, Line 7a; "
     "Schedule D (540) Instructions, Column (c)",
     "built", None,
     "Built via income_brackets.compute_generic_basis_diff_ca_tax / "
     "engine._income_generic_basis_diff_answer. Re-examined 2026-08-23: the original deferral "
     "reasoning (needs cumulative historical CA-vs-federal basis tracking nobody has on hand) "
     "is true if this feature tried to DERIVE the CA-adjusted gain from raw acquisition/"
     "depreciation history -- but Line 7a doesn't care HOW that figure was derived, only what "
     "it IS. Reuses the SAME 'trust the stated figure' precedent as every K-1/carryover/credit "
     "feature already built: asks for the taxpayer's own already-computed federal AND "
     "California capital gain figures directly. CA AGI only ever needs the CA gain (federal "
     "gain is used purely to compute/disclose the Line 7a adjustment amount, mirroring the "
     "form's own column structure). Scoped to GAINS only (a basis difference affecting a LOSS "
     "interacts with the separate capital-loss annual-limit mechanic, a genuinely different "
     "case). QSBS/K-1-capital-gain/home-sale/installment-sale language routes to a dedicated "
     "out-of-scope redirect (each has its own genuinely different mechanic). One real bug found "
     "live: detect_compute_signal's pre-existing 'capital gain' income-type label meant a "
     "'basis difference' question mentioning 'federal capital gain'/'California capital gain' "
     "would otherwise be silently answered by the plain wage-compute path using the FIRST "
     "dollar figure (often unrelated other income, not either gain) -- fixed with the same "
     "NOL-precedent exclusion pattern. A second bug (undirected proximity picking a PRECEDING "
     "amount over the one the anchor actually describes) was fixed by switching to the "
     "established forward-only extraction pattern before any regression values were locked in."),
    ("I", "A", "7a", "Installment sale gain (FTB 3805E) with CA/federal basis difference",
     "both", "narrow", "2025 Schedule CA (540) Instructions -- Part I, Section A, Line 7a; "
     "FTB Form 3805E Instructions",
     "built", None,
     "Built via income_brackets.compute_installment_sale_basis_diff_ca_tax / "
     "engine._income_installment_sale_basis_diff_answer -- a thin wrapper reusing "
     "compute_generic_basis_diff_ca_tax's exact math. Re-examined 2026-08-23: the 'multi-"
     "input form' concern only applies if this feature tried to compute the gross-profit-"
     "ratio-times-payments-received recognition math from scratch; it doesn't need to -- a "
     "taxpayer with an ongoing installment sale already has a federal Form 6252/3805E "
     "computing THIS YEAR's recognized gain, so this just asks for the already-computed "
     "federal AND California recognized-gain figures, same 'trust the stated figure' "
     "precedent as the generic basis-difference feature. Scoped to the current year's "
     "recognized gain only, not the full remaining installment schedule. One real dispatcher-"
     "ordering bug found live: a natural phrasing like 'installment sale with a basis "
     "difference' contains both a generic-feature trigger AND the generic feature's own "
     "out-of-scope exclusion term, so without checking this more specific feature FIRST, the "
     "generic feature's redirect would claim the question -- fixed by reordering, same 'move "
     "the more specific check earlier' pattern as K-1 capital gain/EBL carryover."),
    ("I", "A", "7a", "Gain on personal residence sale where CA/federal depreciation diverged",
     "both", "narrow", "2025 Schedule CA (540) Instructions -- Part I, Section A, Line 7a",
     "built", None,
     "Built via income_brackets.compute_home_sale_basis_diff_ca_tax / "
     "engine._income_home_sale_basis_diff_answer -- a thin wrapper reusing "
     "compute_generic_basis_diff_ca_tax's exact math. Re-examined 2026-08-23 with the same "
     "'trust the stated already-computed gain figures' reframing as the other basis-"
     "difference items. Scoped narrowly and deliberately: this population specifically means "
     "a residence with BUSINESS/RENTAL USE (home office, partial rental conversion) generating "
     "a depreciation history to diverge on -- trigger requires BOTH home/residence-sale "
     "language AND an explicit depreciation mention together (an ordinary personal-use-only "
     "home sale has no depreciation at all and correctly does not match). Assumes the stated "
     "federal/California gain figures are already NET of any IRC Section 121 exclusion "
     "($250k/$500k MFJ) -- CA generally conforms to Section 121 itself, so only the "
     "depreciation-driven basis/gain difference underneath it needs this feature. Zero "
     "extraction bugs found live -- benefited directly from the dispatcher-ordering lesson "
     "just learned building the installment-sale item (checked before the generic feature's "
     "own out-of-scope guard from the start, since home/residence-sale language is one of "
     "that guard's own exclusion terms)."),
    ("I", "A", "7a", "Kiddie-tax capital gain pass-through (FTB 3803, child's gain reported on parent's return)",
     "addition", "narrow", "Sched CA (540) Pt I Sec A line 7a",
     "not_applicable", None,
     "Single-fact tractable if built, but a narrow election most families don't use -- same "
     "reasoning that got the Line 3 items reclassified. Judgment call, noted 2026-08-14."),

    # ============ PART I, SECTION B -- Additional Income (lines 1-9a) ============
    ("I", "B", "1", "State tax refund",
     "subtraction", "common", "Sched CA (540) Pt I Sec B line 1",
     "built", "state_tax_refund", "Near-universal for prior-year itemizers; CA doesn't tax its own refund"),
    ("I", "B", "2a", "Alimony received (agreements executed 2018-12-31 through 2025-12-31)",
     "addition", "moderate", "Sched CA (540) Pt I Sec B line 2a",
     "built", "alimony_spousal_support", "Transitional population tied to the 2025-12-31 conformity cutoff"),
    ("I", "B", "3", "Other loan forgiveness addback for ineligible entities (CAA 2021)",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 3",
     "not_applicable", None, "Pandemic-era, aging out"),
    ("I", "B", "3", "PPP loan forgiveness addback for ineligible entities",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 3",
     "not_applicable", None, "Pandemic-era, aging out"),
    ("I", "B", "3", "Shuttered Venue Operator Grant addback for ineligible entities",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 3",
     "not_applicable", None, "Pandemic-era, aging out"),
    ("I", "B", "3", "Commercial cannabis activity business-expense deduction (IRC 280E)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 3",
     "built", "self_employment_income_tax",
     "Built 2026-08-14: cannabis_280e_expenses param on compute_self_employment_ca_tax in "
     "income_brackets.py (reuses the self-employment path, sole-proprietor/SMLLC scope per "
     "R&TC 17209; K-1 recipients need no adjustment, entity already absorbs it). Required an "
     "early-intercept guard in _answer() (mirrors the pre-existing military-retirement "
     "collision fix) since this project's own sales-tax cannabis_retail_adult_use rule was "
     "shadowing the income-domain answer for any cannabis-flavored question."),
    ("I", "B", "3", "Limitation on employer fringe benefit expense deduction",
     "both", "narrow", "Sched CA (540) Pt I Sec B line 3",
     "built", "self_employment_income_tax",
     "Built 2026-08-15: fringe_benefit_restoration param on compute_self_employment_ca_tax "
     "in income_brackets.py (same shape as cannabis 280E -- extends the existing SE path, "
     "not a new one). California doesn't conform to TCJA's IRC 274 limits on employer "
     "entertainment/employee-parking-transit/on-premises-meal deductions, confirmed still "
     "true post-SB-711 (specific decoupling, not a conformity-date byproduct). Scope-gated "
     "to Schedule-C filers who are themselves EMPLOYERS (benefits paid to staff, not "
     "themselves); K-1 recipients need no adjustment since the entity absorbs it first, "
     "same reasoning as cannabis 280E. No new bugs found -- collision-guard and phantom-"
     "amount lessons from the prior four Line-7a-family builds applied up front."),
    ("I", "B", "3", "Limitation on wagering losses",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 3",
     "not_applicable", None, "CA doesn't conform to federal cap on wagering-transaction expenses"),
    ("I", "B", "3", "Professional sports league penalty (owner-level)",
     "addition", "one_time", "Sched CA (540) Pt I Sec B line 3",
     "not_applicable", None, "Extremely narrow"),
    ("I", "B", "3", "Business expense disallowance -- Edge College/Key Worldwide ('Varsity Blues')",
     "addition", "one_time", "Sched CA (540) Pt I Sec B line 3",
     "not_applicable", None, "Specific fraud-scheme defendants only"),
    ("I", "B", "4", "Other gains/losses basis differences (Schedule D-1) -- GAINS only",
     "both", "narrow", "2025 Schedule CA (540) Instructions -- Part I, Section B, Line 4; "
     "Schedule D-1 Instructions (parallels federal Form 4797)",
     "built", None,
     "Built via income_brackets.compute_schedule_d1_basis_diff_ca_tax / "
     "engine._income_schedule_d1_basis_diff_answer -- a thin wrapper reusing "
     "compute_generic_basis_diff_ca_tax's exact math. Re-examined 2026-08-23: the 2026-08-15 "
     "research's root-cause finding (CA's standing bonus-depreciation non-conformity requiring "
     "cumulative CA-basis history to DERIVE) still holds, but this feature doesn't need to "
     "derive that history -- only accept the taxpayer's own already-computed federal and "
     "California Form 4797/Schedule D-1 gain figures directly, same 'trust the stated figure' "
     "precedent as the other basis-difference items. Scoped to GAINS only -- an ordinary "
     "Section 1231/1245/1250 LOSS is not subject to the capital-loss annual limit, a genuinely "
     "different mechanic not attempted here (see the separate not_applicable-adjacent note: "
     "this narrows the item's own frequency from its original 'narrow' rating). One real bug "
     "found live: this feature's own trigger vocabulary ('form 4797', 'section 1231/1245/1250') "
     "contains digits that parse as phantom dollar amounts via the shared extraction regex -- "
     "fixed with a local phantom-filter, same established pattern as 9+ prior collisions this "
     "session (280E, QSBS, Form 2555, CDC credit's '3506', etc.). Also proactively applied the "
     "dispatcher-ordering and detect_compute_signal exclusion fixes learned from the earlier "
     "basis-difference items, before testing rather than after."),
    ("I", "B", "5", "Rental RE/royalties/partnership/S-corp/trust depreciation differences (ordinary passive activities) -- INCOME only",
     "both", "moderate", "2025 Schedule CA (540) Instructions -- Part I, Section B, Line 5; "
     "FTB 3885A Instructions",
     "built", None,
     "Built via income_brackets.compute_rental_depreciation_basis_diff_ca_tax / "
     "engine._income_rental_depreciation_basis_diff_answer -- a thin wrapper reusing "
     "compute_generic_basis_diff_ca_tax's exact math. Re-examined 2026-08-23: the 2026-08-15 "
     "research's finding still holds (PAL mechanic confirmed identical CA/federal -- FTB 3801: "
     "'Generally, California law is the same as federal law concerning PAL limitations'; "
     "divergence comes purely from feeding CA-basis vs federal-basis income/loss into that "
     "otherwise-identical calculation) -- but this feature doesn't need to reproduce the PAL "
     "calculation or the depreciation history behind it, only accept the taxpayer's own "
     "already-computed federal and California NET RENTAL/ROYALTY INCOME figures directly. "
     "Scoped to INCOME (gains) only, a real and meaningful limitation for this line "
     "specifically -- rental activities commonly show a LOSS after depreciation, and a net "
     "loss here would need to interact with either the ALREADY-BUILT real-estate-professional "
     "allowance (see the separate 'IRC 469(c)(7)' row) or the unmodeled passive-activity-loss "
     "suspension, genuinely more than a sign flip. Zero extraction bugs found live -- "
     "proactively applied every lesson learned building the prior 4 basis-difference items "
     "(self-referential 'rental' exclusion, detect_compute_signal exclusion, phantom-digit "
     "guard for '3885', dispatcher ordering before the generic feature's own checks) before "
     "ever testing, and all of them were needed on the very first live run."),
    ("I", "B", "5b", "CA non-conformity to IRC 469(c)(7) real estate professional exception",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 5; FTB 3801 General Information",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_real_estate_pro_allowance/ca_tax in income_brackets.py. "
     "California refuses the federal IRC 469(c)(7) recharacterization -- 'For California "
     "purposes, all rental activities are passive activities' -- so a real estate "
     "professional's rental loss, fully deductible (nonpassive) federally, stays capped by "
     "CA's ordinary $25,000 active-participation allowance with its $100,000-$150,000 MAGI "
     "phase-out (IRC 469(i)) -- CONFIRMED IDENTICAL formula/thresholds to federal Form 8582 "
     "(FTB 3801 reuses the federal MAGI figure and federal Form 6198 at-risk mechanics "
     "directly, just with CA-basis inputs). MFS-lived-apart-all-year uses halved figures "
     "($12,500/$50k-$75k); MFS not living apart gets $0 allowance (IRC 469(i)(5)) -- deferred "
     "if lived-apart status isn't stated, not guessed. Narrow population (real estate "
     "professionals specifically) despite the parent row's 'moderate' frequency, which mixes "
     "in the much more common (and untractable) ordinary-landlord case."),
    ("I", "B", "6", "Farm income depreciation/passive-activity/NOL differences -- DEPRECIATION, INCOME only",
     "both", "narrow", "2025 Schedule CA (540) Instructions -- Part I, Section B, Line 6; "
     "FTB 3801/3885A Instructions",
     "built", None,
     "Built via income_brackets.compute_farm_depreciation_basis_diff_ca_tax / "
     "engine._income_farm_depreciation_basis_diff_answer -- a thin wrapper reusing "
     "compute_generic_basis_diff_ca_tax's exact math. Re-examined 2026-08-23: the 2026-08-15 "
     "research checked for a Line-5-style tractable SUB-POPULATION (a farm-specific carve-out "
     "analogous to the real-estate-professional exception) and correctly found none -- but "
     "that's a different question from whether the 'trust the stated already-computed figure' "
     "reframing (used across every basis-difference item this pass) applies, which it does: "
     "structurally this is Line 5's exact mechanic substituting Schedule F farm income for "
     "Schedule E rental income. Scoped to INCOME (gains) only, same discipline as rental "
     "depreciation. The research's OTHER two 2026-08-15 findings remain correct and UNCHANGED: "
     "PASSIVE-ACTIVITY has no CA divergence for farming at all (genuinely not tractable, not "
     "just deferred -- nothing to build), and 'NOLs' in this line's own framing sentence is "
     "boilerplate, not a distinct Line 6 mechanism (the real NOL addback lives at the separate "
     "Line 8a rows). Zero extraction bugs found live -- sixth and final basis-difference item "
     "this pass, benefiting from every lesson (self-referential exclusions, "
     "detect_compute_signal exclusion, phantom-digit guards, dispatcher ordering) applied "
     "proactively before testing."),
    ("I", "B", "7", "Unemployment compensation",
     "subtraction", "common", "Sched CA (540) Pt I Sec B line 7",
     "built", "unemployment_compensation", None),
    ("I", "B", "7", "Paid Family Leave / Family Temporary Disability Insurance",
     "subtraction", "common", "Sched CA (540) Pt I Sec B line 7",
     "built", "paid_family_leave", None),
    ("I", "B", "8a", "Federal net operating loss addback (sole-business-income population)",
     "addition", "moderate", "Sched CA (540) Pt I Sec B line 8a; FTB 3805V",
     "built", "ca_income_tax_bracket",
     "Researched 2026-08-15: previous note ('requires separate CA NOL computation') was stale "
     "-- that computation was built at Line 9b2 (compute_nol_ca_tax) this session. Confirmed "
     "the addback itself is a trivial dollar-for-dollar restatement (FTB: 'Enter the amount of "
     "the federal NOL included on line 8a, column A, as a positive number in column C') with "
     "no partial-addback or percentage scenarios. For the population 9b2 already serves (sole "
     "business income, no wages/other income), NO new code is needed: compute_nol_ca_tax "
     "computes taxable income directly from business income rather than through a federal-AGI "
     "intermediate, so it already implicitly performs the addback+recompute wash -- the "
     "existing feature's total-tax answer is already mathematically equivalent to running the "
     "real Line 8a -> Line 9b2 sequence. See the separate 'general wage+NOL population' row "
     "for the genuinely unbuilt slice."),
    ("I", "B", "8a-general", "Federal NOL addback for taxpayers with wages/other income AND "
     "current-year business income (mixed sources)",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 8a; FTB 3805V",
     "built", None,
     "Built via income_brackets.compute_nol_mixed_ca_tax / engine._income_nol_mixed_answer. "
     "Re-examined 2026-08-28 at the user's request, the last remaining deferred item after the "
     "basis-difference batch: the original 'needs the full suspension-test generalization' "
     "reasoning doesn't survive contact with the suspension test's own text once you stop "
     "trying to make ONE stated figure stand in for BOTH halves of it. Asked for directly, net "
     "business income is simply the stated business_income figure (no conflation with total "
     "income needed at all), and modified AGI is wages + business_income (a two-source sum, "
     "same disclosed 'no other income/adjustments' simplification the sibling wages-only row "
     "already carries, just widened by one explicit source). No new algorithm, no new FTB "
     "research -- the SAME suspension AND test already verified and cited for the sibling rows, "
     "applied to its two actual inputs instead of one approximated input. Requires an EXPLICIT "
     "ongoing-business signal (or a stated business-income figure, which is itself direct "
     "evidence of one) to distinguish from the wages-only-closed-business row, and explicit "
     "wage/other-income vocabulary to distinguish from the business-only row -- both already "
     "correctly step aside for this population unchanged. One extraction bug found live: the "
     "shared _amount_near_filtered_span helper's anchor-start-to-amount-midpoint distance "
     "metric picked the wrong figure on a reordered-facts phrasing (a long anchor phrase like "
     "'business income' can land numerically closer to an unrelated PRECEDING amount than to "
     "its own value right after it); fixed with a dedicated boundary-aware helper measuring "
     "from the anchor's nearest edge instead. Also found live: 's-corp'/'llc'/'partnership' as "
     "trigger vocabulary collided with the already-established entity_annual_tax feature ('I "
     "run an s-corp...' reads as an entity question, not a personal-income one) -- dropped from "
     "the trigger set, scope limited to unambiguous personal-income phrasing (self-employed, "
     "freelance, contractor, sole proprietor, K-1, Schedule C/E, or simply stating a business-"
     "income figure)."),
    ("I", "B", "8a-wages-only", "Federal NOL addback for WAGE-ONLY filers with NO current-year "
     "business income (closed prior business)",
     "addition", "moderate", "FTB 2025 Instructions for Form 3805V -- NOL Suspension / General "
     "Information",
     "built", None,
     "Built via income_brackets.compute_nol_wages_ca_tax / engine._income_nol_wages_answer. "
     "The original deferral reasoning ('would require generalizing the MTI/suspension test to a "
     "broader income base') turned out to have a tractable narrower slice: FTB's suspension "
     "test is an AND condition ('net business income >= $1,000,000 AND modified AGI >= "
     "$1,000,000') -- already verified/cited for compute_nol_ca_tax, no new research needed. A "
     "taxpayer whose business has CLOSED has $0 current-year business income, which can NEVER "
     "satisfy the '>= $1,000,000' business-income leg regardless of wage level -- suspension is "
     "therefore structurally impossible for this population at ANY income, not just an "
     "assumption. Requires an EXPLICIT closed-business confirmation (never inferred from "
     "silence, since guessing wrong here could understate a real suspension); any ongoing-"
     "business signal (self-employed, 1099, K-1, etc.) routes to the still-deferred general "
     "case above. Also fixed a real bug found while building this: detect_compute_signal's "
     "COMPLEXITY_EXCLUDE didn't recognize NOL/carryover vocabulary at all, so a question with "
     "NOL terms but no OTHER complexity term (the ambiguous closed-vs-ongoing-business case) "
     "would let the plain wage-compute path race ahead and silently answer with the WRONG "
     "(first-found) dollar figure instead of deferring -- fixed by adding an explicit NOL-term "
     "exclusion to detect_compute_signal, mirroring the existing QSBS/home-sale precedent for "
     "extending that function. Verified via a full income_item_sweep.py reset (388/388) plus "
     "all 4 other regression sweeps, all clean."),
    ("I", "B", "8b", "California Lottery winnings exclusion",
     "subtraction", "common", "Sched CA (540) Pt I Sec B line 8b",
     "built", "california_lottery_winnings", None),
    ("I", "B", "8b", "Gambling winnings (general, non-CA-lottery)",
     "addition", "common", "Sched CA (540) Pt I Sec B line 8b",
     "built", "gambling_winnings", None),
    ("I", "B", "8c", "Mortgage forgiveness debt relief (principal residence, post-2017)",
     "addition", "moderate", "Sched CA (540) Pt I Sec B line 8c",
     "built", "mortgage_forgiveness_debt_relief", "CA does NOT conform to the federal COD exclusion on a principal residence"),
    ("I", "B", "8c", "Employer student loan payment exclusion (CARES Act)",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 8c",
     "not_applicable", None, None),
    ("I", "B", "8d", "Federal foreign earned income/housing exclusion (Form 2555) addback",
     "addition", "moderate", "Sched CA (540) Pt I Sec B line 8d",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_foreign_earned_income_ca_tax in income_brackets.py. The old "
     "'needs residency-history facts' note was stale, same failure mode as Line 8a's NOL "
     "addback note -- Schedule CA (540) is titled 'California Adjustments -- Residents' and "
     "already presupposes full-year residency (part-year/nonresident lives on the separate "
     "540NR engine), so within that scope CA taxes ALL worldwide income unconditionally, no "
     "date-based or partial addback. FTB confirms: flat restatement of the federal-excluded "
     "amount, no worksheet. Simpler than QSBS -- one addback figure, no offsetting federal "
     "figure, mirrors HSA investment gain's shape. Two bugs caught proactively before the "
     "regression sweep (not found live, unlike most prior features): phantom-amount collision "
     "from literal '2555' in 'Form 2555' (same class as cannabis 280E/QSBS), and an initial "
     "over-broad COMPLEXITY_EXCLUDE-derived exclude set that would have wrongly deferred on "
     "self-employment mentions (same lesson as HSA's first attempt -- fixed with a narrower "
     "purpose-built exclude list before ever running the sweep)."),
    ("I", "B", "8d", "Combat zone extended to Sinai Peninsula (foreign earned income)",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 8d",
     "not_applicable", None, None),
    ("I", "B", "8e", "Archer MSA to HSA rollover",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 8e; FTB 3805P",
     "not_applicable", None, None),
    ("I", "B", "8f", "HSA distributions for unqualified medical expenses",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec B line 8f",
     "built", "hsa_contributions_and_earnings", "Part of HSA non-conformity cluster, already covered"),
    ("I", "B", "8n", "IRC 951(a) Subpart F income inclusion",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8n",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_subpart_f_ca_tax in income_brackets.py. FTB's own Line 8n "
     "text is a flat, unconditional 'California law does not conform... enter the amount on "
     "line 8n, column B' restatement -- no worksheet, no cap -- so the STATUS/COMPLEXITY-TIER "
     "was the stale part of this ledger note, not the population framing (unlike Line 8a/8d, "
     "where the NOTE itself was stale). Mechanically a full cancellation, not a net change: "
     "federal AGI = other_income + inclusion_amount; the CA subtraction removes the inclusion "
     "in full, so CA AGI reduces back to exactly other_income, matching CA's baseline of "
     "taxing CFC earnings only on actual distribution (corroborated by the Line 3 dividends "
     "instruction). Trusts the taxpayer's stated federal 951(a) inclusion amount as already "
     "correct -- does not independently verify >=10% CFC-shareholder status. 'CFC-owner "
     "population'/narrow frequency confirmed accurate, not stale."),
    ("I", "B", "8o", "IRC 951A(a) GILTI inclusion",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8o",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_gilti_ca_tax in income_brackets.py. Same flat non-conformity "
     "restatement and full-cancellation mechanic as Subpart F (Line 8n) above, TCJA-era "
     "(2017) instead of pre-existing law. IRC Section 250's 50% GILTI deduction is a "
     "non-issue for this single-fact model: it's only available to C corps or individuals "
     "with an IRC 962 election, so an ordinary individual's federal Schedule 1 line 8o "
     "already reports the GROSS inclusion with nothing netted out -- the flat full-amount "
     "subtraction is correct as-is for the standard (non-962-electing) case, which is out of "
     "scope but explicitly disclosed. 'CFC-owner population'/narrow frequency confirmed "
     "accurate, not stale."),
    ("I", "B", "8p", "Excess business loss limitation (FTB 3461)",
     "both", "moderate", "Sched CA (540) Pt I Sec B line 8p; FTB 3461",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-14: compute_excess_business_loss_ca_tax in income_brackets.py. "
     "Thresholds $313k (single/MFS/HOH) / $626k (MFJ/QSS) for 2025 -- QSS paired with MFJ "
     "per this codebase's own precedent (Form 3461 doesn't name QSS explicitly). Trusts a "
     "single stated aggregate business-loss figure (Form 3461 Parts I/II netting not "
     "derived from components); excess carryforward disclosed, not tracked into future years."),
    ("I", "B", "8z-wildfire-general", "Wildfire disaster settlement exclusion (general)",
     "subtraction", "moderate", "R&TC 17138.7; Sched CA (540) Pt I Sec B line 8z",
     "built", "wildfire_disaster_settlement_exclusion", "TY2021-2029; folded into the generic wildfire_disaster_settlement_exclusion topic"),
    ("I", "B", "8z-chiquita-canyon", "Chiquita Canyon elevated temperature landfill event exclusion",
     "subtraction", "narrow", "R&TC 17157.5; Sched CA (540) Pt I Sec B line 8z",
     "built", "wildfire_disaster_settlement_exclusion", "TY2024-2028; folded into the generic wildfire_disaster_settlement_exclusion topic"),
    ("I", "B", "8z-wildfire-mitigation", "Wildfire mitigation payment (CA Wildfire Mitigation Financial Assistance Program)",
     "subtraction", "narrow", "R&TC 17138.8; Sched CA (540) Pt I Sec B line 8z",
     "built", "wildfire_disaster_settlement_exclusion", "TY2024-2028; folded into the generic wildfire_disaster_settlement_exclusion topic"),
    ("I", "B", "8z-kincade", "Kincade Wildfire exclusion (2019, PG&E settlement)",
     "subtraction", "one_time", "R&TC 17139.2; Sched CA (540) Pt I Sec B line 8z",
     "built", "wildfire_disaster_settlement_exclusion", "TY2020-2027; folded into the generic wildfire_disaster_settlement_exclusion topic"),
    ("I", "B", "8z-zogg", "Zogg Wildfire exclusion (2020, PG&E settlement)",
     "subtraction", "one_time", "R&TC 17139.3; Sched CA (540) Pt I Sec B line 8z",
     "built", "wildfire_disaster_settlement_exclusion", "TY2020-2027; folded into the generic wildfire_disaster_settlement_exclusion topic"),
    ("I", "B", "8z-fire-victims-trust", "Fire Victims Trust exclusion (Camp Fire/PG&E bankruptcy trust)",
     "subtraction", "one_time", "R&TC 17138.5; Sched CA (540) Pt I Sec B line 8z",
     "built", "wildfire_disaster_settlement_exclusion", "Through TY2027; folded into the generic wildfire_disaster_settlement_exclusion topic"),
    ("I", "B", "8z-thomas-woolsey", "Thomas and Woolsey Wildfires exclusion (2017/2018, SCE settlement)",
     "subtraction", "one_time", "R&TC 17138.6; Sched CA (540) Pt I Sec B line 8z",
     "built", "wildfire_disaster_settlement_exclusion", "Through TY2026; folded into the generic wildfire_disaster_settlement_exclusion topic"),
    ("I", "B", "8z-wildfire-relief-federal", "Wildfire relief payment (Federal Disaster Tax Relief Act 2023)",
     "addition", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "OPPOSITE mechanic (CA does NOT conform to this federal exclusion) -- deliberately excluded from the generic wildfire-exclusion rule so it can't be confused with the 6 subtraction items above"),
    ("I", "B", "8z", "529-to-Roth-IRA rollover (CAA 2023/SECURE 2.0) addback",
     "addition", "moderate", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Plus 2.5% additional CA tax"),
    ("I", "B", "8z", "California HOPE for Children Trust Account Program",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Narrow, foster-youth population"),
    ("I", "B", "8z", "Interagency Council on Homelessness payment exclusion",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Discharge of community college student fees",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "TY2022-2026"),
    ("I", "B", "8z", "Guaranteed income pilot program payment exclusion",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "TY2022-2026"),
    ("I", "B", "8z", "COVID-19 supplemental paid sick leave relief grant (small business/nonprofit)",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Pandemic-era, aging out"),
    ("I", "B", "8z", "Turf replacement water conservation program rebate",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "TY2022-2026"),
    ("I", "B", "8z", "Excess business loss carryover from prior years",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec B line 8z; FTB 3461",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_ebl_carryover_ca_tax in income_brackets.py, extending the "
     "already-built Line 8p threshold formula rather than new historical-tracking complexity. "
     "FTB confirms California treats a disallowed excess business loss as a carryover excess "
     "business loss INSTEAD OF an NOL carryover -- no MTI/suspension-style multi-year "
     "recomputation to replicate (unlike Line 8a-general's still-deferred NOL carryforward). "
     "Two cases modeled: full absorption (this year's business income >= carryover, flat "
     "uncapped subtraction, FTB's own explicit words) and this-year-loss (combines with the "
     "carryover, reapplies the SAME Line 8p threshold formula). Partial absorption (business "
     "income positive but less than the carryover) deliberately left unmodeled -- FTB defers "
     "that case to an unverified Form 3461 PDF worksheet; routes to a specific needs_review "
     "message rather than guessing. Dispatcher-ordering fix required: 'excess business loss "
     "carryover' contains 'excess business loss' as a substring, so this had to be checked "
     "BEFORE the pre-existing Line 8p feature. 'Business-owner shape'/moderate frequency "
     "confirmed accurate; the deferred status was stale."),
    ("I", "B", "8z", "California Venues Grant (CalOSBA)",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Pandemic-era, aging out"),
    ("I", "B", "8z", "Small Business COVID-19 Relief Grant Program",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Pandemic-era, aging out"),
    ("I", "B", "8z", "Expanded use of 529 account funds (K-12 tuition, TCJA-era)",
     "addition", "moderate", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Parents' election to report child's interest/dividends (FTB 3803) difference",
     "both", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Reward from a crime hotline",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Beverage container recycling (CRV) income",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Usually immaterial dollar amounts"),
    ("I", "B", "8z", "Water/energy conservation appliance rebates",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Seismic improvement financial incentive (Earthquake Brace + Bolt etc.)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Original issue discount, debt instruments issued 1985-1986",
     "both", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Essentially obsolete population"),
    ("I", "B", "8z", "Foreign income of nonresident aliens",
     "both", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_nra_foreign_income_ca_tax in income_brackets.py. FTB's own "
     "Line 8z text is a flat, unconditional, two-directional worldwide-income true-up: "
     "'Adjust federal income to reflect worldwide income computed under California law. "
     "Enter losses from foreign sources on line 8z, column B. Enter foreign source income on "
     "line 8z, column C.' No worksheet, no cap. 'Nonresident alien' here is the FEDERAL "
     "tax-status term (Form 1040-NR filer), NOT a California-residency term -- confirmed by "
     "its other uses on the same resident-only instructions page -- so this is NOT the "
     "540NR/nonresident engine's job (confirmed via grep: income_nonresident.py has zero "
     "existing handling for this). The 'same class as Form 540NR work' framing was the stale "
     "part of this ledger note; 'NRA filer shape'/narrow frequency was accurate. Requires an "
     "explicit federal-NRA self-identification phrase to trigger (not just any 'foreign "
     "income' mention, which is far too generic on its own)."),
    ("I", "B", "8z", "Cost-share payments to forest landowners",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Coverdell ESA distribution difference",
     "both", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "Energy-efficiency retrofit grants to low-income individuals",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, None),
    ("I", "B", "8z", "CA National Guard Surviving Spouse & Children Relief Act death benefits",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Extremely narrow"),
    ("I", "B", "8z", "Ottoman Turkish Empire settlement payments",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec B line 8z",
     "not_applicable", None, "Historical, closing population"),
    ("I", "B", "9b1", "Disaster loss deduction (FTB 3805V)",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec B line 9b1; FTB 3805V",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_disaster_loss_carryover_ca_tax in income_brackets.py, mirroring "
     "compute_nol_ca_tax's shape MINUS the $1M suspension branch (disaster loss carryovers are "
     "explicitly exempt from the 2024-2027 NOL suspension rule per FTB's own suspension text). "
     "FTB's own Line 9b1 instruction is a flat 'copy this cell from your own 3805V, Part III "
     "line 2 column (f)' pass-through -- the SAME cell Line 9b2's NOL carryover pulls from, "
     "just a different loss type. The original ledger note ('requires declared-disaster-county "
     "+ loss facts') was stale, same pattern as Line 8a/8d -- the per-item casualty-loss facts "
     "(FMV, insurance, $100/10%-AGI floors) were already baked in when the loss ORIGINATED at "
     "the separate, still-deferred Part II Line 15 (Casualty/theft loss); this feature never "
     "touches that computation, only the already-limited leftover carryover balance in a later "
     "year. Broader population than NOL carryover -- applies against ANY income (wages "
     "included), not just business income, per FTB's own 'there is income' (not 'business "
     "income') wording."),
    ("I", "B", "9b2", "NOL deduction (FTB 3805V)",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec B line 9b2; FTB 3805V",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-14: compute_nol_ca_tax in income_brackets.py. TY2024-2026 suspension when "
     "net business income AND modified AGI are both >=$1M, collapsed to one stated business-"
     "income figure under a disclosed sole-income-source assumption. No % cap when not "
     "suspended (unlike federal), capped at Modified Taxable Income instead. Disaster-loss "
     "carveout and pre-2008 NOLs remain out of scope."),
    ("I", "B", "9b3", "NOL deduction -- Enterprise Zone/LAMBRA/Targeted Tax Area (FTB 3805Z/3807/3809)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec B line 9b3",
     "not_applicable", None, "Legacy economic-incentive-zone programs, largely phased out"),

    # ============ PART I, SECTION C -- Adjustments to Income (lines 11-25) ============
    ("I", "C", "11", "Educator expenses",
     "addition", "common", "Sched CA (540) Pt I Sec C line 11",
     "built", "educator_expenses", "CA does not conform; the full federal deduction amount is added back"),
    ("I", "C", "12", "Reservists/performing artists/fee-basis officials business expenses (Form 2106)",
     "both", "narrow", "Sched CA (540) Pt I Sec C line 12",
     "not_applicable", None, None),
    ("I", "C", "13", "HSA deduction",
     "subtraction", "moderate", "Sched CA (540) Pt I Sec C line 13",
     "built", "hsa_contributions_and_earnings", "Part of HSA non-conformity cluster, already covered"),
    ("I", "C", "14", "Moving expenses (FTB 3913)",
     "addition", "narrow", "Sched CA (540) Pt I Sec C line 14; FTB 3913",
     "not_applicable", None, "Military-adjacent population"),
    ("I", "C", "15", "Deductible part of self-employment tax (worker reclassification)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec C line 15",
     "not_applicable", None, None),
    ("I", "C", "17", "Self-employed health insurance deduction (worker reclassification)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec C line 17",
     "not_applicable", None, None),
    ("I", "C", "19a", "Alimony paid (agreements executed 2018-12-31 through 2025-12-31)",
     "addition", "moderate", "Sched CA (540) Pt I Sec C line 19a",
     "built", "alimony_spousal_support", "Transitional population tied to the 2025-12-31 conformity cutoff"),
    ("I", "C", "20", "IRA deduction (IRC 408 election)",
     "both", "narrow", "Sched CA (540) Pt I Sec C line 20",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-14: compute_ira_deduction_ca_tax in income_brackets.py, pass-through "
     "(trusts the stated federal deduction). For TY2025, SB 711's conformity-date change "
     "(1/1/2015 -> 1/1/2025) repealed both previously-live Line 20 divergence triggers "
     "(age-70.5 addback, catch-up-indexing addback), confirmed by diffing the 2024 vs 2025 "
     "FTB instructions -- so no CA adjustment applies for the common case. Self-employment/"
     "worker-classification income mismatch remains the one open trigger, deferred. FTB Pub. "
     "1005 (PDF-only) not independently verified. Roth IRA mentions get a dedicated "
     "not-deductible redirect rather than a silent wrong computation."),
    ("I", "C", "21", "Student loan interest deduction (military spouse/RDP domicile)",
     "addition", "narrow", "Sched CA (540) Pt I Sec C line 21",
     "not_applicable", None, None),
    ("I", "C", "24", "Personal property rental income deductible expenses",
     "both", "narrow", "Sched CA (540) Pt I Sec C line 24b",
     "not_applicable", None, None),
    ("I", "C", "24", "Reforestation amortization (non-CA qualified timber property)",
     "subtraction", "one_time", "Sched CA (540) Pt I Sec C line 24d",
     "not_applicable", None, "Very narrow"),
    ("I", "C", "24", "IRC 501(c)(18)(D) pension plan contributions",
     "both", "one_time", "Sched CA (540) Pt I Sec C line 24f",
     "not_applicable", None, "Rare pre-1959 plan type"),
    ("I", "C", "24", "Chaplain contributions to 403(b) plans",
     "both", "one_time", "Sched CA (540) Pt I Sec C line 24g",
     "not_applicable", None, None),
    ("I", "C", "24", "IRS whistleblower award attorney fees/court costs",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec C line 24i",
     "not_applicable", None, None),
    ("I", "C", "24", "Foreign housing deduction (Form 2555)",
     "subtraction", "narrow", "Sched CA (540) Pt I Sec C line 24j",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: extended compute_foreign_earned_income_ca_tax (Line 8d) with an "
     "optional housing_deduction_amount param rather than a new function, since the two "
     "commonly co-occur for self-employed expats (IRC 911(c) housing deduction available IN "
     "ADDITION TO the 911(a) earned-income exclusion). FTB's Line 24j text is a flat 'enter "
     "the amount from column A in column B' restatement, same non-conformity as Line 8d's "
     "exclusion -- no cap table, trust-the-input applies. NOTE: despite living in Section C's "
     "'column B' (labeled subtraction), tracing the arithmetic through Form 540's own Line "
     "26/27 chain confirms the NET effect on CA taxable income is an ADDBACK (same direction "
     "as Line 8d), not a literal subtraction -- Section C's column B/C encode subtraction/"
     "addition to the DEDUCTION total, opposite-signed from Section A/B's income-total "
     "convention; the ledger's 'subtraction' tag reflects the column label only. Two bugs "
     "found and fixed during verification: (1) a shared 'form 2555' substring between the "
     "exclusion and housing-deduction term sets could double-count a single housing figure as "
     "both an exclusion and a deduction (fixed: only search for an exclusion amount when an "
     "exclusion-specific term is present, not bare 'form 2555' already claimed by a housing "
     "phrase); (2) a sales-domain cross-routing collision for the 'both stated together' "
     "phrasing -- same class as cannabis 280E/military retirement -- fixed with an "
     "early-intercept guard in _answer()."),

    # ============ PART II -- Itemized Deduction Adjustments ============
    ("II", None, "1-4", "Self-employed health insurance moved into medical expenses (worker reclassification)",
     "addition", "narrow", "Sched CA (540) Pt II line 1-4",
     "not_applicable", None, None),
    ("II", None, "1-4", "HSA distributions for qualified medical expenses > 7.5% AGI",
     "addition", "narrow", "Sched CA (540) Pt II line 1-4",
     "built", "hsa_contributions_and_earnings", "Part of HSA non-conformity cluster, already covered"),
    ("II", None, "5a", "State and local tax (SALT) deduction disallowance",
     "subtraction", "common", "Sched CA (540) Pt II line 5a",
     "built", "ca_income_tax_bracket", "Tier 2 (2026-08-11): optional salt_amount param on compute_itemized_ca_tax, extracted via SALT_TERMS in _income_itemized_answer"),
    ("II", None, "5e", "SALT cap difference (federal $40k/$20k MFS 2025 OBBBA cap vs CA no cap)",
     "addition", "common", "Sched CA (540) Pt II line 5e",
     "built", "ca_income_tax_bracket", "Tier 2's 7th and final item (2026-08-11): re-derived the mechanic -- the form itself says add back 'the amount over the federal limit' directly, a statable fact (same trust-the-input precedent as mortgage_interest_addback), NOT something requiring a property-tax/income-tax allocation as originally assumed when this was first deferred. Proved algebraically that -salt_amount + max(0, total_paid-cap) matches the property-tax-based derivation exactly. New optional salt_cap_addback param on compute_itemized_ca_tax; verified via direct function calls without Gemini quota. TIER 2 IS NOW COMPLETE (7/7)."),
    ("II", None, "6", "Foreign income taxes / foreign property taxes / generation-skipping transfer tax",
     "both", "narrow", "Sched CA (540) Pt II line 6",
     "not_applicable", None, None),
    ("II", None, "8", "Mortgage acquisition debt cap difference ($1M/$500k CA vs $750k/$375k federal)",
     "addition", "moderate", "Sched CA (540) Pt II line 8",
     "built", "ca_income_tax_bracket", "Tier 2 continuation (2026-08-11): optional mortgage_interest_addback param on compute_itemized_ca_tax -- takes the DISALLOWED amount directly (trust-the-input, not derived from loan balance/origination date), covers both this and the home-equity item below via the identical add-back mechanic"),
    ("II", None, "8", "Home equity indebtedness interest (federal suspended, CA doesn't conform)",
     "addition", "moderate", "Sched CA (540) Pt II line 8",
     "built", "ca_income_tax_bracket", "Tier 2 continuation (2026-08-11): same mortgage_interest_addback mechanism as the acquisition-debt-cap item above -- both restore via the identical Line 8 column C add-back, so one fact covers both sub-rules"),
    ("II", None, "8", "Mortgage interest credit (Form 8396 MCC) addback",
     "addition", "narrow", "Sched CA (540) Pt II line 8",
     "not_applicable", None, None),
    ("II", None, "9", "Investment interest expense (FTB 3526)",
     "both", "narrow", "Sched CA (540) Pt II line 9; FTB 3526",
     "not_applicable", None, None),
    ("II", None, "11-12", "Charitable contribution AGI cap difference (CA 50% vs federal higher post-TCJA/OBBBA)",
     "subtraction", "moderate", "Sched CA (540) Pt II line 11-12",
     "built", "ca_income_tax_bracket", "Tier 2 continuation (2026-08-11): optional charitable_amount param on compute_itemized_ca_tax -- CA flatly caps at 50% of AGI regardless of federal's own cash/non-cash cap distinction, verified against primary source; 6th clustered figure, verified via direct function calls without Gemini quota. Excludes the narrower charitable conservation easement sub-limit (CA 30% vs federal 50%), which stays deferred/not_applicable"),
    ("II", None, "11-12", "College Access Tax Credit contribution addback",
     "subtraction", "narrow", "Sched CA (540) Pt II line 11-12",
     "not_applicable", None, None),
    ("II", None, "11-12", "Charitable conservation easement AGI cap (CA 30% vs federal 50%)",
     "subtraction", "narrow", "Sched CA (540) Pt II line 12",
     "not_applicable", None, None),
    ("II", None, "11-12", "Charitable contribution disallowance -- Edge College/Key Worldwide ('Varsity Blues')",
     "subtraction", "one_time", "Sched CA (540) Pt II line 11-12",
     "not_applicable", None, "Same narrow population as the Sec B line 3 item"),
    ("II", None, "13", "Charitable contribution carryover difference",
     "addition", "moderate", "Sched CA (540) Pt II line 13",
     "not_applicable", None, None),
    ("II", None, "13", "Conservation contribution carryover period (CA 5yr vs federal 15yr)",
     "subtraction", "narrow", "Sched CA (540) Pt II line 13",
     "not_applicable", None, None),
    ("II", None, "13", "Appreciated stock donated to private foundation pre-2002-01-01",
     "subtraction", "one_time", "Sched CA (540) Pt II line 13",
     "not_applicable", None, "Very narrow, aging population"),
    ("II", None, "15", "Casualty/theft loss (CA allows without federal 'declared disaster' restriction)",
     "both", "moderate", "Sched CA (540) Pt II line 15",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15 (last item in the completeness ledger): extended compute_itemized_ca_tax "
     "with an optional casualty_loss_amount param (8th and final optional itemized-deduction "
     "figure). FTB: 'California allows personal casualty and theft loss and disaster loss "
     "deductions' regardless of a federal disaster declaration -- federal law (post-TCJA) only "
     "allows the deduction for federally-declared-disaster losses. Two-fact design, a "
     "deliberate middle ground: IRS Pub. 547's THREE floors are the $100-per-event floor and "
     "insurance-reimbursement netting (both per-event data this system can't collect in one "
     "question -- pushed onto the taxpayer's stated pre-floor total, same trust boundary as "
     "charitable_amount/capital_loss) and the 10%-of-AGI floor (applied to the yearly TOTAL, "
     "not per-event -- COMPUTED here via compute_casualty_loss_floor rather than trusted, "
     "since it's the one piece a taxpayer's self-reported final number is most likely to get "
     "wrong). Requires itemizing, same as every other Part II adjustment -- no standalone "
     "trigger, wired into the existing _income_itemized_answer dispatcher alongside SALT/"
     "mortgage/misc/charitable/SALT-cap-addback. No sales-domain routing collision found "
     "(checked, unlike the Line 24j housing-deduction build immediately before this one). "
     "$100/10% floors confirmed current for TY2025 -- OBBBA's casualty-loss changes take "
     "effect TY2026 forward and leave these figures unchanged even then."),
    ("II", None, "16", "Unreimbursed impairment-related work expenses",
     "subtraction", "narrow", "Sched CA (540) Pt II line 16",
     "not_applicable", None, None),
    ("II", None, "16", "Federal employee temporary-duty travel expenses (prosecution duties)",
     "subtraction", "one_time", "Sched CA (540) Pt II line 16",
     "not_applicable", None, "Very narrow"),
    ("II", None, "16", "CA lottery gambling losses (not deductible for CA)",
     "addition", "common", "Sched CA (540) Pt II line 16",
     "built", "california_lottery_winnings", "Part of gambling/lottery cluster, already covered"),
    ("II", None, "16", "Federal estate tax on income in respect of a decedent (IRD)",
     "subtraction", "narrow", "Sched CA (540) Pt II line 16",
     "not_applicable", None, None),
    ("II", None, "16", "Claim-of-right repayment >$3,000 (incl. Social Security carve-out)",
     "both", "narrow", "Sched CA (540) Pt II line 16",
     "not_applicable", None, None),
    ("II", None, "19-22", "Misc. itemized deductions reinstatement (2% floor: job expenses, tax prep, other)",
     "subtraction", "moderate", "Sched CA (540) Pt II line 19-22",
     "built", "ca_income_tax_bracket", "Tier 2 continuation (2026-08-11): optional misc_itemized_expenses param on compute_itemized_ca_tax, applies the standard pre-TCJA IRC 67(a) 2%-of-AGI floor (max(0, expenses - 0.02*AGI)); 5th clustered figure, verified via direct function calls without needing Gemini quota"),
    ("II", None, "27", "Adoption expenses addback (if claiming CA adoption cost credit)",
     "addition", "narrow", "Sched CA (540) Pt II line 27",
     "not_applicable", None, None),
    ("II", None, "27", "Nontaxable-income-related expenses adjustment",
     "both", "narrow", "Sched CA (540) Pt II line 27",
     "not_applicable", None, None),
    ("II", None, "27", "State legislator travel expenses (overnight-away-from-residence)",
     "subtraction", "one_time", "Sched CA (540) Pt II line 27",
     "not_applicable", None, "Extremely narrow, legislators only"),
    ("II", None, "27", "Interest on utility-company loans for energy-efficient equipment",
     "addition", "narrow", "Sched CA (540) Pt II line 27",
     "not_applicable", None, "CA-only deduction, no federal equivalent"),
    ("II", None, "29", "CA itemized deduction phase-out (AGI-based haircut)",
     "worksheet", "moderate", "Sched CA (540) Pt II line 29",
     "built", "ca_income_tax_bracket", "Tier 2 (2026-08-11): compute_itemized_deduction_phaseout implements the verified 10-step worksheet (min of 80%-of-reducible / 6%-of-excess-AGI); simplification disclosed (treats full itemized total as reducible, can only overstate the reduction, never understate it)"),
]


def load():
    with db.get_conn() as conn:
        for part, section, line_ref, label, adj, freq, citation, status, topic_key, notes in ITEMS:
            conn.execute(
                "INSERT INTO schedule_ca_inventory "
                "(tax_year, part, section, line_ref, item_label, adjustment_type, "
                "frequency, citation, status, topic_key, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tax_year, line_ref, item_label) DO UPDATE SET "
                "adjustment_type=EXCLUDED.adjustment_type, frequency=EXCLUDED.frequency, "
                "citation=EXCLUDED.citation, status=EXCLUDED.status, "
                "topic_key=EXCLUDED.topic_key, notes=EXCLUDED.notes",
                (TAX_YEAR, part, section, line_ref, label, adj, freq, citation, status, topic_key, notes))
        # Prune orphaned rows: the upsert above is keyed on (tax_year,
        # line_ref, item_label), so renaming or splitting an item_label
        # (as happened twice during the 2026-08-14 Line 7a/3 research
        # passes) leaves the OLD label behind as a stale row with no
        # DELETE step to clean it up. Recompute the current valid set on
        # every load and remove anything in this tax_year not in it.
        # AND part IN ('I','II') matters as of 2026-08-15: form540_inventory.py
        # (Phase 3 of the Income Coverage Blueprint) reuses this SAME table for
        # Form 540's own lines/credits, tagged part='540' -- without this
        # filter, this module's own orphan-pruning/status/list queries would
        # silently pick up and mix in that other module's rows.
        current_keys = {(line_ref, label) for _, _, line_ref, label, *_ in ITEMS}
        existing = conn.execute(
            "SELECT id, line_ref, item_label FROM schedule_ca_inventory "
            "WHERE tax_year=%s AND part IN ('I','II')",
            (TAX_YEAR,)).fetchall()
        orphan_ids = [row_id for row_id, line_ref, label in existing
                      if (line_ref, label) not in current_keys]
        if orphan_ids:
            conn.execute(
                "DELETE FROM schedule_ca_inventory WHERE id = ANY(%s)", (orphan_ids,))
            print(f"pruned {len(orphan_ids)} orphaned row(s) (stale item_label from a rename/split)")
    print(f"loaded {len(ITEMS)} inventory items")
    status_report()


def status_report():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT status, count(*) FROM schedule_ca_inventory "
        "WHERE tax_year=%s AND part IN ('I','II') "
        "GROUP BY status ORDER BY count(*) DESC", (TAX_YEAR,)).fetchall()
    total = sum(r[1] for r in rows)
    print(f"\n=== SCHEDULE CA {TAX_YEAR} INVENTORY ({total} items) ===")
    for status, n in rows:
        print(f"  {status:28} {n}")
    conn.close()


def list_items(status_filter=None):
    conn = db.get_conn()
    q = ("SELECT part, section, line_ref, item_label, status, topic_key FROM schedule_ca_inventory "
         "WHERE tax_year=%s AND part IN ('I','II')")
    params = [TAX_YEAR]
    if status_filter:
        q += " AND status=%s"
        params.append(status_filter)
    q += " ORDER BY part, section NULLS LAST, line_ref"
    rows = conn.execute(q, params).fetchall()
    for part, section, line_ref, label, status, topic_key in rows:
        loc = f"Pt {part}" + (f" Sec {section}" if section else "") + f" line {line_ref}"
        tk = f" -> {topic_key}" if topic_key else ""
        print(f"  [{status:26}] {loc:28} {label}{tk}")
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
