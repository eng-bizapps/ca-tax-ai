"""Completeness ledger for Form 540 itself and California personal income
tax CREDITS -- the two areas the original Schedule CA (540) sweep never
touched (that ledger is scoped to Schedule CA's own adjustment lines only,
see schedule_ca_inventory.py). Built as Phase 3 ("Ledger Expansion") of the
Income Coverage Blueprint, seeded from a primary-source research pass
against the actual 2025 Form 540 PDF, the 2025 Form 540 Booklet, Schedule P
(540) instructions, and FTB's own Credit Chart -- same "verified truth
lives in code" precedent as schedule_ca_inventory.py, local_rates.py,
fees.py.

REUSES THE SAME schedule_ca_inventory TABLE (part='540', section='lines'
for Form 540's own lines, section='credits' for the credit-chart items) --
deliberately, to avoid a new migration; the underlying schema (tax_year,
part, section, line_ref, item_label, adjustment_type, frequency, citation,
status, topic_key, notes) is already generic enough to hold this too.

ONE CORRECTION APPLIED BEFORE TRUSTING THE SOURCE RESEARCH: the original
Phase 3 research pass claimed the Behavioral Health Services Tax (Line 62)
was unbuilt, based on a grep for the literal strings "mental_health"/
"behavioral_health" -- but the codebase implements it under a DIFFERENT
name (compute_ca_tax's own "surtax"/"mhs_surtax" fields, income_brackets.py
~line 3150-3188), and it has been visibly present in this session's own
tax computations all along. Marked "built" below, not "deferred_new_engine"
as the raw research claimed -- a reminder that a subagent's "zero grep
matches" is only as good as the search terms it guessed, not proof of
absence. Also corrected: income_credits.py's own module docstring falsely
claimed the Foster Youth Tax Credit "is NOT implemented" (stale -- it's
built and wired into engine.py); FYTC is marked "built" below.

status values -- same meanings as schedule_ca_inventory.py:
  built                 -- feature exists and is wired into engine.py
  deferred_new_engine   -- tractable in principle, real population, but
                           genuinely needs more than a stated fact (multi-
                           year data, an unconfirmed rate, a multi-input
                           worksheet) OR just hasn't been built yet
  not_applicable        -- narrow/one-time/business-entity-oriented/
                           repealed-carryover-only population, not worth
                           an individual build

Usage:
  python form540_inventory.py load     # upsert the full inventory
  python form540_inventory.py status   # counts by status
  python form540_inventory.py list [status]   # list items, optionally filtered
"""
import sys

import income_db as db

TAX_YEAR = 2025

# (part, section, line_ref, item_label, adjustment_type, frequency, citation, status, topic_key, notes)
ITEMS = [
    # ============ FORM 540's OWN LINES (not Schedule CA) ============
    ("540", "lines", "7-11", "Personal/Blind/Senior/Dependent Exemption Credits + Line 32 AGI Limitation Worksheet",
     "credit", "common", "FTB 2025 Form 540, Side 1 Lines 7-9, Side 2 Line 10, Line 32 AGI Limitation Worksheet",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15. Highest-frequency Phase 3 finding: every CA resident filer gets at "
     "least the Personal Exemption Credit ($153, doubled to $306 for MFJ/QSS via Line 7's own "
     "'enter 1 or 2' mechanic -- not a separately-set figure), plus $475/dependent, phased out "
     "$6/unit per $2,500 ($1,250 MFS) of AGI over $252,203 (single/MFS) / $504,411 (MFJ/QSS) / "
     "$378,310 (HOH). Dollar figures verified directly against the actual 2025 Form 540 PDF and "
     "booklet worksheet (downloaded and read directly, not a secondary aggregator) after the "
     "Behavioral Health Services Tax miss (below) raised the bar for what 'verified' means. "
     "Built as its OWN standalone opt-in feature (compute_exemption_credit_ca_tax in "
     "income_brackets.py), NOT integrated into compute_ca_tax itself -- a deliberate scope "
     "decision so the ~300 already-verified regression expectations for every other feature "
     "stay unaffected; see that function's module note. PARTIAL: Blind and Senior exemption "
     "units (same $153 rate, 0/1/2 each) are NOT modeled yet -- disclosed in the answer text, "
     "not silently dropped. Trigger vocabulary deliberately requires explicit 'exemption "
     "credit' phrasing rather than bare 'N dependents' (which is heavily overloaded elsewhere "
     "in this codebase for HOH/credit features) -- broadening that is a real, separate future "
     "extension, not attempted this build."),
    ("540", "lines", "12-19", "Taxable income assembly (state wages, federal AGI, standard/itemized deduction, CA AGI)",
     "mechanical", "common", "FTB 2025 Form 540 Instructions, Lines 12-19",
     "built", "ca_income_tax_bracket",
     "Mechanical once Schedule CA (already fully inventoried) and the standard/itemized "
     "deduction (already built, standard_deduction/compute_itemized_ca_tax) exist -- this IS "
     "what compute_ca_tax's callers already assemble, not a new feature. RDP pro-forma-federal-"
     "return recompute (a sub-case of Line 13) and the CA Standard Deduction Worksheet for "
     "Dependents (a sub-case of Line 18, min(max(earned_income,$1,350), filing-status cap)) "
     "are both genuinely narrow slices left unbuilt -- not_applicable-adjacent, not worth "
     "their own rows here."),
    ("540", "lines", "31", "Tax (bracket/rate schedule) + Schedule G-1/FTB 5870A lump-sum/trust-accumulation",
     "mechanical", "common", "FTB 2025 Form 540 Instructions, Line 31",
     "built", "ca_income_tax_bracket",
     "Bracket math already built (compute_ca_tax). Schedule G-1 (pre-1/2/1936-born lump-sum "
     "distributions) and FTB 5870A (trust accumulation distributions) are both extremely "
     "narrow/aging populations -- not_applicable-adjacent, not built."),
    ("540", "lines", "31", "FTB 3800 kiddie tax on unearned income (child's tax computed at parent's rate)",
     "both", "narrow", "FTB 2025 Instructions for Form FTB 3800",
     "built", None,
     "Built via income_brackets.compute_kiddie_tax_ca_tax / engine._income_kiddie_tax_answer. "
     "Re-examined 2026-08-28 at the user's request via a 'one-shot template' reframing: the "
     "original deferral ('needs the PARENT's marginal-rate/taxable-income figure -- a second "
     "return's data') correctly identified the blocker but wrongly concluded it needed cross-"
     "question persistent memory (Phase 1, not started) to solve -- this codebase's existing "
     "multi-fact extraction (already proven on ~25 other features) handles asking for BOTH the "
     "child's and parent's figures in ONE question fine. Verified against FTB's 2025 "
     "Instructions for Form FTB 3800 directly (not assumed from general 'kiddie tax' knowledge, "
     "given this session's 8-for-9 track record of shallow notes needing correction): California "
     "kept the ORIGINAL 'parent's marginal rate' method, not the TCJA 2018-2019 trust-rate "
     "method the federal government briefly used then reverted via the SECURE Act -- CA Form "
     "3800 explicitly cross-references federal Form 8615's structure, confirmed line-for-line "
     "against every line the CA instructions text spells out (1, 2, 6, 7, 9, 10, 15, 17, 18). "
     "$2,700 threshold confirmed directly from FTB's own text. Scoped to a single child (no "
     "multi-child Line 7/12 combination), standard deduction only, no earned income for the "
     "child (child's AGI = child's unearned income), child's own filing status defaults to "
     "single (disclosed) -- all deliberate, disclosed limitations, not oversights. Two real bugs "
     "found live: (1) 'form 3800' itself is a phantom-digit collision (the form number is a bare "
     "4-digit sequence the shared amount regex misparses as a dollar figure -- the same class "
     "found 10+ times this session, missed on the first pass despite citing the pattern in this "
     "feature's own module docstring, caught only once extraction was tested); (2) a bare "
     "'earned income' out-of-scope term is a literal substring of 'UNearned income' -- this "
     "feature's own core vocabulary was being wrongly rejected as out-of-scope until fixed to "
     "compound phrases only ('has earned income', 'child's earned income'). A third issue -- the "
     "edge-aware _amount_near_anchor_edge helper (built for the NOL-mixed feature's own "
     "bidirectional phrasing) picked the WRONG figure here, since this feature's phrasing is "
     "genuinely anchor-then-value only -- was fixed by switching to forward-only "
     "_amount_after_filtered_span instead, which sidesteps the whole 'nearby wrong neighbor' bug "
     "class by construction rather than tuning a distance metric."),
    ("540", "lines", "61", "Alternative Minimum Tax (Schedule P (540)) -- GENERAL case",
     "both", "moderate", "FTB 2025 Schedule P (540) Instructions",
     "deferred_new_engine", None,
     "Genuinely needs a new engine: full AMTI computation across ~11 preference-item "
     "categories (ISO exercises, passive-activity/depreciation preferences, etc.) plus a "
     "3-tier exemption phase-out (verified worksheet: exemption zeroes out entirely above "
     "$718,804/$958,413/$479,188 AMTI for 2025). Real, moderate population -- not narrow -- "
     "but a genuinely bigger build than a single stated fact, same complexity class as "
     "business entities/trusts. Anyone who itemizes, exercised ISOs, has passive-activity/"
     "depreciation adjustments, holds private activity bonds, or has other preference items "
     "still routes to a dedicated out-of-scope redirect (see the narrow 'AMT screen' row "
     "below, which IS built, for the population this general case's own deferral does NOT "
     "block). Not started. "
     "UPDATE 2026-09-01: a Phase-1 COMPOSABLE AGGREGATOR was built on the 'AMT screen' row "
     "below (not this row) -- it lets 5 of the facts this general case would otherwise need "
     "combine in one question (ISO, property tax, mortgage interest, misc. itemized, K-1 (541) "
     "passthrough), but does NOT close this row: NOL still can't compose with the others "
     "(different income shape), and the true remaining categories (depreciation, passive "
     "activity, investment interest, the ~10 narrow-industry/technical-classification 13-series "
     "items, Line 17's small-business exclusion test, Line 20's AMT-NOL-carryover recompute) are "
     "all independently re-confirmed genuinely out of reach for a chat interface, per two "
     "dedicated research passes against FTB's actual 2025 Schedule P (540) text. ALSO "
     "INVESTIGATED AND EXPLICITLY NOT BUILT: Schedule P Line 18 (itemized-deduction-limitation "
     "addback) -- a naive 'addback' reading of FTB's own text turns out to likely have the WRONG "
     "SIGN once checked against this codebase's own AMTI arithmetic (taxable_income already has "
     "the phase-out reduction baked in, so reversing it for AMT should SUBTRACT the reduction, "
     "not add it) -- needs a dedicated primary-source verification pass, not a guess, before "
     "ever building it. Still deferred_new_engine; a full ~11-category build remains a "
     "genuinely bigger undertaking than any further narrowing pass."),
    ("540", "lines", "61", "Alternative Minimum Tax -- NARROW screen (standard deduction, "
     "wage-only, zero preference items)",
     "both", "common", "2025 Schedule P (540), Side 1 Line 1, Side 2 Lines 22-24; R&TC Section 17062",
     "built", None,
     "Built via income_brackets.compute_amt_screen_ca_tax / engine._income_amt_screen_answer -- "
     "the FIRST Phase 3 item this session where a dedicated research pass found the ledger's "
     "OWN 'too complex' verdict wrong for a real (if narrow) slice, not just incomplete. For a "
     "standard-deduction, wage-only filer with zero preference items, FTB's own Schedule P (540) "
     "Line 1 text ('If you did not itemize deductions, enter your standard deduction... and go "
     "to line 6') means every preference/adjustment line is $0, so AMTI collapses to CA AGI -- "
     "no preference-item modeling needed. CA's AMT rate is a flat 7.0% (NOT federal's 26%/28% "
     "two-tier structure -- a real correction worth remembering for any future AMT work), and "
     "the exemption size relative to CA's own bracket rates means TMT never exceeds regular tax "
     "for this population at any realistic income (verified via dedicated research across "
     "multiple income points from the exemption threshold through $2M+, using this codebase's "
     "own already-verified bracket data) -- confirmed live at $2,000,000 single, still $0. Built "
     "as a REAL formula computation (regular tax via the existing bracket engine vs. TMT), not a "
     "hard-coded 'always zero' assumption, so it self-verifies rather than trusting the "
     "empirical claim blindly. Itemizing/ISO/passive-activity/depreciation/private-activity-"
     "bond/NOL language routes to a dedicated out-of-scope redirect to the (still deferred) "
     "general case above. Zero extraction bugs found live. "
     "EXTENDED 2026-08-28 with an ISO-exercise addback (income_brackets.compute_amt_iso_ca_tax / "
     "engine._income_amt_iso_answer), the single most common real-world reason an ordinary "
     "(non-itemizing) taxpayer actually hits AMT post-TCJA. Verified against FTB's 2025 "
     "Schedule P (540) Part I Line 10 instructions directly: the ISO 'bargain element' (FMV at "
     "exercise minus amount paid) creates NO regular-tax income at exercise, only an AMTI "
     "addback -- confirmed CA conforms via IRC 55-59 'as of January 1, 2015,' and IRC 56(b)(3) "
     "(the ISO AMT preference) is a long-stable pre-2015 provision, so no conformity-date "
     "divergence risk. Critical carve-out also from the same source and modeled explicitly: "
     "exercised-and-sold-in-the-same-year means NO adjustment applies at all -- routes to its "
     "own dedicated redirect rather than guessing either direction. Deliberately requires "
     "literal ISO language (word-boundary 'iso' or 'incentive stock option'), NOT a bare "
     "'exercised stock options' mention, since that's genuinely ambiguous with a non-qualified "
     "stock option (NSO -- ordinary income for both regular tax and AMT, no special preference "
     "treatment); California Qualified Stock Options (CQSOs, a narrower CA-specific provision) "
     "are also explicitly out of scope, not conflated with ISOs. One bug found live: bare "
     "'stock' is already in the shared COMPLEXITY_EXCLUDE set (for K-1/QSBS questions), which "
     "self-excluded this feature's own natural vocabulary ('stock options') -- fixed with the "
     "same 'subtract the trigger term back out' pattern used elsewhere this session. "
     "EXTENDED AGAIN 2026-08-28 with an ITEMIZER extension (income_brackets.compute_amt_"
     "itemized_ca_tax / engine._income_amt_itemized_answer), covering an itemizer with NO other "
     "AMT preference items. Verified against FTB's 2025 Schedule P (540) Part I Line 3 "
     "instructions directly: PROPERTY tax (personal property + real estate) is fully disallowed "
     "for AMT though still allowed for regular tax -- a real correction from an initial "
     "hypothesis that this would mirror CA's OWN regular-tax 'SALT addback' (state/local INCOME "
     "tax, a DIFFERENT category already disallowed for regular tax too, see compute_itemized_ca_"
     "tax's pre-existing salt_amount parameter). Reuses compute_itemized_ca_tax unchanged for "
     "the regular-tax figure; AMTI = that function's own taxable_income plus the stated "
     "property-tax addback. Declines (falls through) if the stated itemized amount doesn't "
     "actually exceed the standard deduction -- Schedule P Line 1's own instruction means no "
     "property-tax addback applies to a filer who didn't really itemize. Deliberately does NOT "
     "compose with compute_itemized_ca_tax's 6 other optional adjustments (SALT removal, "
     "mortgage-interest addback, misc itemized, charitable cap, SALT-cap addback, casualty "
     "loss) in the same question -- kept to exactly 3 required dollar figures. Two bugs found "
     "live: (1) an inconsistency from copy-pasting a different feature's pattern -- this "
     "extension initially required a COMPUTE_TRIGGERS phrase, unlike the base screen and ISO "
     "extension (neither requires one, since AMT + itemizing + property-tax vocabulary together "
     "is already unambiguous), silently rejecting the feature's own natural phrasing until "
     "removed for consistency; (2) a genuinely NEW bug in the shared _amount_near_anchor_edge "
     "helper itself (built earlier this session for NOL-mixed, also used by kiddie tax) -- "
     "searching a keyword SET with variants of different lengths ('itemized deduction' vs "
     "'itemized deductions') can make a shorter variant's own edge distance tie EXACTLY between "
     "a genuinely-following amount and an unrelated preceding one, with the preceding amount "
     "silently winning the tie. Fixed AT THE SHARED HELPER this time (not worked around "
     "locally), given its small 3-caller blast radius -- re-verified NOL-mixed's and kiddie "
     "tax's own existing regression values unchanged after the fix. "
     "EXTENDED AGAIN 2026-09-01 with a MORTGAGE-INTEREST extension (income_brackets.compute_"
     "amt_mortgage_ca_tax / engine._income_amt_mortgage_answer), Schedule P (540) Part I Line 4 "
     "-- home mortgage interest NOT used to buy, build, or improve the home (a use-of-proceeds "
     "test, unrelated to loan size; FTB's own example: a home-equity loan used to buy a ski "
     "boat goes on Line 4, the same loan used to install a pool does NOT, since a home "
     "improvement IS acquisition debt). Deliberately does NOT reuse compute_itemized_ca_tax's "
     "existing mortgage_interest_addback parameter's TERM SET for this feature's own trigger -- "
     "verified against that parameter's own docstring that it bundles TWO different federal-"
     "nonconformity sub-rules into one trusted figure: (1) interest on genuine acquisition debt "
     "between the federal $750k cap and CA's pre-TCJA $1M cap (still acquisition debt -- Line 4 "
     "does NOT disallow this), and (2) interest on debt not used to buy/build/improve the home "
     "(Line 4 DOES disallow this). Reusing the whole stated figure would have overstated AMTI "
     "for anyone whose federal disallowance was purely the cap-size difference -- this extension "
     "asks for the NARROWER, Line-4-specific figure via its own dedicated vocabulary instead, "
     "though the underlying MECHANIC (pass it into compute_itemized_ca_tax's existing "
     "mortgage_interest_addback parameter unchanged for the regular-tax leg, since CA doesn't "
     "conform to federal's suspension either way, then add it back a second time for AMTI) is "
     "the same. One bug found live: the qualifying detection phrase ('...that was not used to "
     "buy, build, or improve...') sits too far (30+ chars) from its own dollar figure in natural "
     "phrasing for _amount_near_anchor_edge's window, while sitting numerically CLOSER to an "
     "unrelated itemized-total figure in an earlier clause -- extracting itemized total first "
     "(mirroring the sibling property-tax extension's order) let the wrong match win. Fixed with "
     "a separate, SHORTER extraction-only anchor ('mortgage interest', income_brackets.AMT_"
     "MORTGAGE_INTEREST_ANCHOR_TERMS) used only for amount extraction, distinct from the longer "
     "detection-only qualifying phrase, and by extracting mortgage interest FIRST. "
     "EXTENDED AGAIN 2026-09-01 with an NOL extension (income_brackets.compute_amt_nol_ca_tax / "
     "compute_amt_nol_wages_ca_tax / compute_amt_nol_mixed_ca_tax, engine._income_amt_nol_*_"
     "answer), Schedule P (540) Part I Line 16 -- a straight add-back of whatever REGULAR-tax "
     "NOL deduction was already claimed this year (confirmed against FTB's own 2025 instruction "
     "text: 'NOL deductions from Schedule CA (540)... enter as a positive amount'), genuinely "
     "simpler than and NOT to be confused with Line 20 (the separate AMT NOL deduction, needing "
     "its own multi-year AMT-basis carryover recompute -- stays out of scope). Wraps ALL 3 "
     "existing, already-shipped (non-AMT) NOL population variants -- business-income-only, "
     "wage-only/closed-business, mixed wages+ongoing-business -- via one shared AMTI/exemption/"
     "TMT helper (_amt_nol_addback), since amti = taxable_income + nol_deduction is identical "
     "across all three and a no-op in the suspended branch. A real trap avoided by design, not "
     "found live: AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS already contains 'net operating loss' and "
     "the shared _amt_screen_has_preference_exclusion helper does an unconditional bare-NOL "
     "regex check -- this feature's own vocabulary IS 'net operating loss'/NOL, so its 3 base-"
     "signal gates independently reimplement the AMT-vocabulary + population-specific-signal "
     "check rather than reusing that shared helper (same self-exclusion trap the ISO extension "
     "had to avoid for bare 'stock'). One dispatcher-ordering bug found live: the wages-only "
     "population's own ambiguity fallback (fires when neither closed- nor ongoing-business "
     "language is stated) doesn't require ongoing-business EXCLUSION vocabulary, only the "
     "ABSENCE of closed-business language -- so a natural mixed-population question ('$X wages, "
     "$Y business income...') also satisfied it, and since it was checked BEFORE the mixed "
     "variant's own compute path, it silently shadowed the mixed path entirely. Fixed by "
     "reordering to check mixed BEFORE the wages-only ambiguous fallback, matching the ordering "
     "the sibling (non-AMT) NOL family already uses for the identical reason. All 5 regression "
     "sweeps green after both extensions (income_item_sweep 454/454 via a full reset+run, "
     "item_sweep 130/130, cross_domain_sweep 16/16, sales_edge_case_sweep 16/16, "
     "district_rate_sweep 8/8), confirming the AMT_ITEMIZED_OTHER_ADJUSTMENT_EXCLUDE_TERMS "
     "expansion (needed so the property-tax extension correctly declines on the new mortgage "
     "vocabulary too) and both new dispatcher-block insertions didn't regress any of the 3 "
     "pre-existing AMT slices or anything else. "
     "EXTENDED AGAIN 2026-09-01, Phase 1 of a GENERAL-CASE aggregator (income_brackets.compute_"
     "amt_general_ca_tax, engine._income_amt_general_answer) -- an ADDITIVE new feature, not a "
     "rewrite: the 4 already-derived facts (ISO bargain element, property tax, non-acquisition "
     "mortgage interest) now COMPOSE freely in one question instead of mutually excluding, plus "
     "2 new trust-the-figure facts (Schedule P Line 5 misc. itemized addback -- auto-derived from "
     "compute_itemized_ca_tax's existing misc_reinstated output, no new question needed; Line 12 "
     "K-1 (541) beneficiary passthrough, a pure external-document figure, same 'trust an already-"
     "computed input' precedent as the shipped PTE credit/OSTC). NOL composition and Schedule P "
     "Line 18 (itemized-deduction-limitation addback) deliberately NOT attempted -- Line 18 "
     "specifically was investigated and DROPPED after independent verification found the initial "
     "design likely had the wrong SIGN (this codebase's AMTI model already has the phased-out "
     "itemized deduction baked into taxable_income, so reversing the phase-out for AMT purposes "
     "should SUBTRACT the reduction, not add it as a naive addback reading suggested) -- flagged "
     "on the GENERAL CASE row below for a dedicated future verification pass, not silently "
     "dropped. Two real dispatcher-collision bugs found live and fixed: (1) the shared preference-"
     "exclude set used by every other AMT slice (AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS) contains "
     "ISO's OWN vocabulary (to make the base screen defer to the ISO extension) -- reusing it "
     "unchanged for this aggregator would have made it wrongly decline on ISO, one of its own "
     "composable facts; fixed with its own named exclude constant mirroring AMT_ISO_OTHER_"
     "PREFERENCE_EXCLUDE_TERMS's construction. (2) The K-1(541) fact collided with the "
     "UNRELATED, pre-existing K-1-only-income feature TWICE: detect_k1_signal (fixed via a "
     "K1_COMPLEXITY_EXCLUDE addition) and, found live AFTER that fix, a completely separate "
     "catch-all _income_k1_fallback_answer that intercepted on bare K1_TRIGGERS presence with "
     "NO exclusion logic at all -- fixed by adding an AMT_SCREEN_TERMS carve-out there too. Every "
     "hand-computed test scenario independently re-verified live against the actual code (not "
     "hand-calculated) before locking in, including 2 composed-fact scenarios that were "
     "impossible to answer at all before this build. 4 of 5 regression sweeps green (item_sweep "
     "130/130, cross_domain_sweep 16/16, sales_edge_case_sweep 16/16, district_rate_sweep 8/8); "
     "income_item_sweep's full reset+run hit the documented per-day Gemini embed-quota limit at "
     "344/461 graded (zero failures on everything graded) -- the 7 new general-case cases weren't "
     "reached before quota exhaustion, but were independently verified live via direct engine."
     "answer() calls with exact matches to hand-computed values; re-running income_item_sweep.py "
     "run (not reset) once quota recovers will pick up exactly where it left off."),
    ("540", "lines", "62", "Behavioral Health Services Tax (formerly Mental Health Services Tax)",
     "addition", "moderate", "FTB 2025 Form 540 Instructions, Line 62",
     "built", "ca_income_tax_bracket",
     "ALREADY BUILT, long before this ledger existed -- confirmed 2026-08-15 after a Phase 3 "
     "research pass incorrectly claimed this was unbuilt (grepped for literal 'mental_health'/"
     "'behavioral_health', which don't appear -- the codebase implements it as compute_ca_tax's "
     "own 'surtax'/'mhs_surtax' fields, income_brackets.py ~line 3150-3188, and it has been "
     "visible in this session's own tax computations throughout, e.g. every feature that "
     "crosses $1M taxable income). Flat 1% x (taxable income - $1,000,000), computed as its own "
     "step alongside every bracket computation this whole codebase already does."),
    ("540", "lines", "63a", "Additional 2.5% CA tax on early retirement-plan distributions (FTB 3805P)",
     "addition", "moderate", "FTB Form 3805P Instructions, Part I; R&TC Section 17085",
     "built", "early_distribution_tax",
     "Built 2026-08-15: compute_early_distribution_tax in income_brackets.py. The 2.5% rate is "
     "CONFIRMED, not folklore (R&TC 17085(c)(1): '2 1/2 percent'; FTB 3805P Line 4: 'Multiply "
     "line 3 by 2 1/2% (.025)'). But the deep-dive found real complexity the original ledger "
     "note's flat-rate sketch didn't capture -- the FIFTH claim from the broad Phase 3 survey "
     "to be incomplete once independently checked: FTB's own text states 'California does not "
     "conform to all of the federal exceptions' (2 confirmed divergent codes: federal "
     "phased-retirement-annuitant and auto-enrollment-permissible-withdrawal exceptions are "
     "explicitly 'Not applicable' for CA), a 25-code YEAR-VERSIONED exception table (not "
     "static -- SB 711's 2025 conformity jump added several new codes), a 6% override for "
     "early-SIMPLE-IRA distributions, and ENTIRELY DIFFERENT rates for other account types "
     "(12.5% Archer MSA under R&TC 17215, 50% Medicare Advantage MSA) -- none of which the "
     "original note anticipated. Scoped to the common no-exception Part-I case only -- "
     "exception-flavored language (disability, death, QDRO, medical, first-home, education, "
     "SEPP, birth/adoption, disaster, military, terminal illness, domestic abuse, etc.) and "
     "non-Part-I account types (Archer MSA, Medicare Advantage MSA, Coverdell, ABLE, HSA) both "
     "route to dedicated clarifying messages rather than a guessed computation -- given FTB's "
     "own confirmed 2-code divergence from federal, guessing on a specific exception is a real "
     "risk of a confidently WRONG (understated) answer, not just an incomplete one."),
    ("540", "lines", "63b-d", "IRC 409A NQDC tax / IRC 453A interest / business credit recapture",
     "addition", "narrow", "FTB 2025 Form 540 Instructions, Line 63",
     "not_applicable", None,
     "409A: narrow (executives with failed deferred-comp elections). 453A: needs multi-year "
     "installment-sale tracking, same class as already-deferred Schedule CA items. Recapture: "
     "business-credit population, narrow for individuals."),
    ("540", "lines", "71-73", "Withholding / estimated payments / Form 592-B/593 nonresident withholding",
     "mechanical", "common", "FTB 2025 Form 540 Instructions, Lines 71-73",
     "not_applicable", None, "Pure trust-the-input pass-throughs, no adjustment logic to build."),
    ("540", "lines", "74", "Refundable Program 4.0 Motion Picture Credit",
     "credit", "narrow", "FTB 2025 Form 540 Instructions, Line 74",
     "not_applicable", None, "Film-industry-specific, requires an irrevocable election and CFC certification."),
    ("540", "lines", "78", "Claim of Right repayment (IRC 1341)",
     "both", "narrow", "FTB 2025 Form 540 Instructions, Line 78",
     "not_applicable", None,
     "Same item already inventoried on the Schedule CA ledger (Part II Line 16) -- needs a "
     "verified PRIOR-YEAR return fact, multi-year, consistent reasoning."),
    ("540", "lines", "91", "Use tax on out-of-state/online purchases",
     "addition", "moderate", "FTB 2025 Form 540 Instructions, Line 91; Estimated Use Tax Lookup Table",
     "built", "estimated_use_tax",
     "Built 2026-08-15: compute_estimated_use_tax in income_brackets.py, using the Estimated "
     "Use Tax Lookup Table path (the simpler of the two FTB-sanctioned methods, and the one "
     "most filers actually use) -- a flat California-AGI-band lookup, all 14 dollar bands plus "
     "the >$199,999 -> AGI x 0.0001 formula band verified directly against the actual "
     "downloaded 2025 Form 540 Booklet PDF. Uses CA AGI (Line 17), not federal AGI, confirmed "
     "from FTB's own instruction text. Scope cap (individual non-business items under $1,000 "
     "each) enforced explicitly, not silently ignored -- a stated item price at/above that "
     "threshold, or any business-purchase mention, routes to a dedicated clarifying message "
     "instead of a guessed number. The OTHER path (exact price x district rate via "
     "district_rates.py/local_rates.py, for $1,000+ items) is NOT built -- left for a future "
     "extension, disclosed in the clarifying message rather than silently unsupported. No "
     "filing status needed at all (the table is AGI-only), a genuinely simpler shape than "
     "every other feature built this session."),
    ("540", "lines", "92", "Individual Shared Responsibility Penalty (FTB 3853)",
     "addition", "moderate", "FTB 3853 (2025) Instructions, Individual Shared Responsibility Penalty "
     "Worksheet, Steps 1-5; R&TC Section 61050",
     "built", None,
     "Built via income_brackets.compute_isr_penalty / engine._income_isr_penalty_answer. This "
     "entry's OWN prior note ('same complexity class as AMT') was found WRONG, not just "
     "incomplete, by dedicated research -- the full formula is a self-contained linear 5-step "
     "worksheet published directly in FTB's own 2025 instructions with every dollar figure "
     "stated ($950/adult, $475/child, $2,850 flat cap, 2.5% of income over threshold, $377/month "
     "average-bronze-premium cap), not synthesized from scattered sections the way AMT's ~11 "
     "preference categories would require. Scoped to the tractable common case, same discipline "
     "as the early-distribution-tax build: uninsured the ENTIRE year, no coverage exemption "
     "claimed, nobody in the household turning 18 during the year. Requires filing status, "
     "adult/child household counts, and household income; the filing-threshold lookup table "
     "(new: income_brackets.ISR_FILING_THRESHOLD_AGI, from FTB's own 'Do I Have to File?' chart, "
     "cross-verified against the instructions' own worked example) assumes UNDER-65 -- a real, "
     "disclosed limitation, since 65+ uses a higher threshold not modeled here. Also disclosed, "
     "not modeled: a dependent's own income counting toward household income if that dependent "
     "independently has a filing requirement; CA tax-exempt interest addition; the 'unclaimed-"
     "but-claimable household member' edge case. Zero extraction bugs found live."),
    ("540", "lines", "110", "Voluntary contributions (18 named funds, incl. 2 new for 2025)",
     "subtraction", "common", "FTB 2025 Form 540 Booklet, Voluntary Contribution Fund Descriptions",
     "not_applicable", None, "User-stated donation amounts, no tax logic of any kind."),
    ("540", "lines", "112", "Late-filing / late-payment penalty",
     "addition", "moderate", "R&TC Sections 19131 (late filing) and 19132 (late payment)",
     "built", "late_filing_payment_penalty",
     "Built 2026-08-15: compute_late_penalties in income_brackets.py. CORRECTED the prior "
     "survey-pass sketch once independently verified -- the FOURTH claim from that same broad "
     "survey to be wrong or incomplete once checked (after the Behavioral Health Services Tax, "
     "OSTC's formula, and the PTE credit's 'pure pass-through' claim): the survey's sketch "
     "omitted the late-payment penalty's own 25% cap/40-month ceiling AND entirely missed the "
     "REQUIRED offset between the two penalties (R&TC 19132(b)) -- late-payment is reduced "
     "dollar-for-dollar by late-filing for the same period; skipping this would double-count "
     "whenever both apply, the common case. Architecturally different from every other feature "
     "built this session: no filing status needed at all (confirmed filing-status-agnostic "
     "directly from the statute), a flat percentage of a stated unpaid balance. Verified both "
     "branches of the offset logic live (the filing-penalty-binding case AND the rarer payment-"
     "penalty-binding case, which happens for very short delays since the payment penalty's 5% "
     "flat start exceeds one month of the filing penalty's 5%/month). DELIBERATELY OUT OF "
     "SCOPE, disclosed: the $135-minimum-penalty test (needs a second date input -- California's "
     "automatic extended due date, a separate clock from the 5%/month accrual's original-due-"
     "date anchor -- only binds for filers ~8+ months late, a narrow slice); interest "
     "(mandatory, separate, compounds daily at a semi-annually-changing rate, a genuine moving "
     "target); reasonable-cause abatement (case-by-case FTB determination, routed to a "
     "dedicated informational redirect rather than computed); the one-time Timeliness Penalty "
     "Abatement (R&TC 19132.5, tractable in principle but adds several more gating facts, left "
     "for a future extension). Assumes filing and full payment happened at the same time (one "
     "stated months-late figure drives both penalty accruals)."),
    ("540", "lines", "113", "Underpayment of Estimated Tax Penalty (FTB 5805) -- REGULAR method",
     "addition", "moderate", "2025 FTB Form 5805 Instructions, Worksheet II; R&TC Section 19136",
     "built", None,
     "Built via income_brackets.compute_underpayment_penalty_regular / "
     "engine._income_underpayment_regular_answer. Re-examined 2026-08-28 at the user's explicit "
     "request, the last remaining deferred_new_engine item on the whole income-tax ledger -- "
     "genuinely different in kind from every other 'too complex' reversal this session, not a "
     "missing-input problem but a whole new capability (this codebase had ZERO date-parsing/"
     "date-math infrastructure before this build). Covers the population the SHORT METHOD row "
     "below's own eligibility rule excludes: any LATE or partial estimated payment. "
     "Transcribed FTB's Worksheet II line-by-line (Part I's 4-column running ledger with "
     "carried-forward Line7/Line8/Line9 state; Part II's two-rate-period per-diem interest, "
     "8% through 6/30/25 then 7% through 4/15/26, the same mid-year change already verified for "
     "the short method) directly from the fetched instructions text, then independently cross-"
     "validated against federal Form 2210 Part III Section A (which CA's worksheet mirrors "
     "almost line-for-line) as a second source of confidence. Resolved the one genuinely "
     "underspecified part of the source text (Line 10/12's 'the date the amount on line 8 was "
     "paid,' which FTB's own worksheet assumes the taxpayer already knows by hand) mechanically: "
     "since Line8/Line9 are mutually exclusive per column and Line5 additively carries forward "
     "everything owed, the earliest LATER column with a Line9 overpayment is exactly the point "
     "an earlier shortfall gets cleared -- no ad hoc rule needed, hand-verified against multiple "
     "consecutive-underpayment chains. Uses that column's DUE DATE (not the exact payment date "
     "within its bucket) as the resolution date -- a conservative, disclosed choice, and also "
     "the necessary one, since multiple payments can land in the same due-date bucket once "
     "extracted, leaving no single well-defined 'the' payment date to point to. Extraction is "
     "date-BUCKETING, not quarter-labeling: users state each payment as '$AMOUNT on MM/DD/YYYY' "
     "and the engine buckets by actual payment date into the FTB due-date window (Form 2210's "
     "own convention), deliberately not asking users to self-label 'Q1/Q2/Q3/Q4' since a late "
     "payment made in August is FTB's own Q3, not the user's intuitive 'catching up Q1' -- "
     "removes a mislabeling risk rather than asking the user to get it right. Required refactoring "
     "compute_underpayment_penalty (the short method) to extract its shared required-annual-"
     "payment logic into compute_required_annual_payment -- verified behavior-preserving against "
     "every pre-existing short-method regression case. New engine.py date-parsing primitives "
     "(_dates/_mask_dates/_pair_amounts_with_dates), the first in this codebase -- required a "
     "proactive fix distinct from the usual phantom-DIGIT-vocabulary collision class found 10+ "
     "times this session: a literal date like '4/15/2025' gets misparsed by the shared _amounts() "
     "regex into phantom amounts 15.0/2025.0 regardless of trigger vocabulary, verified live, so "
     "dates must be masked out of the question text before dollar-amount extraction runs at all. "
     "Verified via 31 assertions in the dedicated underpayment_regular_method_test.py (pure-"
     "arithmetic Worksheet II unit tests, date/bucketing helper tests, and engine.answer() "
     "integration scenarios), all passing, plus one new income_item_sweep.py regression case. "
     "Deliberately not supported, disclosed not hidden: the annualized income installment "
     "method (Worksheet Part III, a separate and even bigger worksheet); fiscal-year filers; "
     "the Farmer/Fisherman exception (FTB 5805F); a withholding actual-date election (ratable "
     "1/4-per-quarter is used, the standard IRS/FTB default)."),
    ("540", "lines", "113", "Underpayment of Estimated Tax Penalty -- SHORT METHOD (withholding-"
     "only, no estimated payments made)",
     "addition", "common", "2025 FTB Form 5805 Instructions, Short Method (Form 5805 Side 2, "
     "Part II); R&TC Section 19136",
     "built", None,
     "Built via income_brackets.compute_underpayment_penalty_ca_tax / "
     "engine._income_underpayment_answer -- a THIRD consecutive case this session of a "
     "dedicated research pass finding the ledger's own 'too complex' verdict overly "
     "conservative, but a split finding: the REGULAR method (per-diem interest, changing "
     "rates) stays deferred (see row above), but FTB's SHORT METHOD collapses the whole "
     "underpayment calculation to ONE flat annual constant for 2025 (.05028767), eligible "
     "specifically for taxpayers who made no estimated tax payments -- withholding-only "
     "filers, a real and common population. Implements the full verified 5-step mechanic: de "
     "minimis safe harbor (<$500/$250 MFS balance due), zero-prior-year-liability safe "
     "harbor, required annual payment = lesser of 90% current-year tax or 100%/110% prior-"
     "year tax (110% if prior-year CA AGI exceeded $150k/$75k MFS, verified as a REQUIRED "
     "stated fact, not defaulted -- defaulting to the lower 100% test would risk "
     "understating a real penalty for a real population), forced 90%-only for current-year "
     "CA AGI >= $1M/$500k MFS. Any estimated tax payment mentioned routes to a dedicated "
     "out-of-scope redirect (short-method eligibility for that population depends on exact "
     "payment dates, reintroducing the excluded timing question), same for the separate "
     "Farmer/Fisherman exception (FTB 5805F). TWO genuine extraction bugs found live and "
     "fixed: (1) the undirected proximity helper picked a preceding figure over the correct "
     "one among 4 tightly-packed dollar figures (fixed with the established OSTC-derived "
     "forward-only pattern); (2) two DIFFERENT stated facts sharing the same dollar VALUE "
     "(e.g. prior-year tax and withholding both $15,000) broke value-based list removal -- "
     "fixed with a new position-aware extraction variant (_amount_after_filtered_span) that "
     "removes the exact matched tuple by its own character span, not by value or list order. "
     "Verified 6 scenarios live after the fix, including the exact 100%-vs-110%-threshold "
     "divergence and the duplicate-dollar-value collision that exposed the bugs."),

    # ============ CALIFORNIA PERSONAL INCOME TAX CREDITS ============
    ("540", "credits", "-", "California Earned Income Tax Credit (CalEITC)",
     "credit", "common", "FTB 2025 Form 540 Booklet, Credit Chart",
     "built", "caleitc", "Built earlier this session -- table lookup by (income, qualifying children), incl. investment-income disqualification sub-case."),
    ("540", "credits", "-", "Young Child Tax Credit (YCTC)",
     "credit", "common", "FTB 2025 Form 540 Booklet, Credit Chart",
     "built", "ycta", "Built earlier this session -- flat amount with a linear phase-out."),
    ("540", "credits", "-", "Foster Youth Tax Credit (FYTC)",
     "credit", "narrow", "FTB 2025 Form 540 Booklet, Credit Chart",
     "built", "fytc",
     "Built and wired into engine.py -- income_credits.py's own module docstring falsely "
     "claimed this was 'NOT implemented' (stale, corrected 2026-08-15). Eligibility (foster-"
     "youth status at/after age 13) handled as an explicit checklist question, not inferred."),
    ("540", "credits", "163", "Senior Head of Household Credit",
     "credit", "narrow", "FTB 2025 Form 540 Booklet, Credit Chart, code 163",
     "built", "senior_hoh_credit", "Built earlier this session."),
    ("540", "credits", "170", "Joint Custody Head of Household Credit",
     "credit", "narrow", "FTB 2025 Form 540 Booklet, Credit Chart, code 170",
     "built", "joint_custody_hoh_credit", "Built earlier this session."),
    ("540", "credits", "173", "Dependent Parent Credit",
     "credit", "narrow", "FTB 2025 Form 540 Booklet, Credit Chart, code 173",
     "built", "dependent_parent_credit", "Built earlier this session."),
    ("540", "credits", "-", "Nonrefundable Renter's Credit",
     "credit", "common", "FTB 2025 Form 540 Booklet, Credit Chart",
     "built", "renters_credit", "Built earlier this session."),
    ("540", "credits", "-", "Military Retirement Pay / DoD Survivor Benefit Plan exclusion",
     "subtraction", "narrow", "FTB 2025 Schedule CA (540) Instructions, Line 5a/5b",
     "built", "military_retirement_exclusion",
     "Built earlier this session -- technically a Schedule CA Line 5a/5b subtraction, not one "
     "of the Credit Chart's coded credits, but lives in income_credits.py per this codebase's "
     "existing organization."),
    ("540", "credits", "232", "Nonrefundable Child and Dependent Care Expenses Credit",
     "credit", "common", "FTB Form 3506 Instructions, Line 9; Schedule P (540)",
     "built", None,
     "Built via income_brackets.compute_cdc_credit / engine._income_cdc_credit_answer. "
     "Dedicated research corrected the survey's 'simple % of federal credit' framing: FTB "
     "Form 3506 is actually a full parallel worksheet keyed on FEDERAL AGI (not CA AGI) "
     "across two percentage charts, and never literally reads a federal credit dollar amount "
     "as input anywhere. The 'federal credit x FTB percentage' shortcut IS mathematically "
     "valid, but only for the common case (full-year CA resident, all care provided in "
     "California, no employer dependent-care benefits) -- scoped the build to that case. "
     "Federal AGI >$100,000 is a hard disqualifying cutoff (not a phaseout); <=$40k=50%, "
     "$40k-$70k=43%, $70k-$100k=34%. Nonrefundable, no carryover. Nonresident/part-year/"
     "out-of-state-care/employer-dependent-care-benefits language routes to a dedicated "
     "clarifying message rather than a guess. Zero extraction bugs found live -- cleanest "
     "build of this Phase 3 batch."),
    ("540", "credits", "187", "Other State Tax Credit (Schedule S)",
     "credit", "common", "FTB 2025 Schedule S (540) Instructions, Part I-II",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_other_state_tax_credit_ca_tax in income_brackets.py. CORRECTED "
     "the prior survey-pass shape once independently verified against the actual Schedule S "
     "PDF: it's TWO INDEPENDENT prorations (CA side: bracket_tax x min(1, double-taxed "
     "income/CA AGI); other-state side: other-state tax paid x min(1, double-taxed income/"
     "other-state AGI)), credit = the LESSER of the two, not one proration capped at raw "
     "other-state tax paid. 'CA tax liability' (Line 2) is NOT a separate stated fact -- "
     "computed from CA income via the existing bracket engine, same as every other feature, "
     "not asked from the taxpayer. The most complex extraction built this session (4 dollar "
     "figures + filing status) -- found and fixed a genuine NEW extraction bug live: with "
     "several dollar figures packed close together, the existing shared proximity helper "
     "(_amount_near_filtered) picked a PRECEDING amount over the one an anchor phrase actually "
     "described, since the preceding clause's connector was character-wise shorter. Fixed with "
     "a new forward-only variant (_amount_after_filtered in engine.py) scoped to this feature "
     "only, not applied to the shared helper 20+ other features rely on unchanged. NOT "
     "modeled, disclosed in the answer text: the anti-double-benefit rule (no credit if the "
     "other state gives ITS OWN residents a credit for CA tax paid) and the AMT-offset "
     "restriction (moot -- this system doesn't compute AMT at all)."),
    ("540", "credits", "197", "Child Adoption Costs Credit",
     "credit", "moderate", "2025 Form 540 Booklet, Credit for Child Adoption Costs Worksheet, Code 197",
     "built", None,
     "Built via income_brackets.compute_adoption_credit_ca_tax / engine._income_adoption_credit_answer. "
     "Dedicated research confirmed the core formula (50% of qualifying costs, capped $2,500/child, "
     "CA-public-agency-custody restriction) but corrected two gaps: no separate numbered FTB form "
     "exists (no 'FTB 3600' -- computed on an unnumbered worksheet in the Form 540 booklet itself), "
     "and this credit is genuinely nonrefundable-with-indefinite-carryover, capped at current-year "
     "CA tax liability -- so it's modeled with the SAME cap-at-liability/carryover-disclosed pattern "
     "as the PTE credit, not CDC credit's simpler standalone formula. Requires an explicit CA-public-"
     "agency/foster-care eligibility signal in the question (ambiguous phrasing routes to a dedicated "
     "clarifying question rather than assuming eligibility); private/international/out-of-state/"
     "stepparent adoption language routes to a dedicated disqualification message. NOT modeled, "
     "disclosed: the second eligibility gate (child must be a US citizen/legal resident, assumed "
     "satisfied); multi-year failed-adoption-attempt cost aggregation; the Schedule CA (540) Line 27 "
     "addback if the same costs were also itemized on federal Schedule A (already tracked as "
     "not_applicable in schedule_ca_inventory.py); per-child cap for multiple adoptions in one year "
     "(each needs its own separate computation); Schedule P credit-ordering against other credits. "
     "Zero extraction bugs found live."),
    ("540", "credits", "242", "Pass-Through Entity (PTE) Elective Tax Credit",
     "credit", "moderate", "FTB 3804-CR Instructions, Part I-II; R&TC Section 17052.10",
     "built", "ca_income_tax_bracket",
     "Built 2026-08-15: compute_pte_credit_ca_tax in income_brackets.py. CORRECTED the prior "
     "survey-pass claim ('pure single-number pass-through') once independently verified -- "
     "the THIRD claim from that same broad survey to be wrong or incomplete once checked "
     "(after the Behavioral Health Services Tax miss and the Other State Tax Credit's "
     "formula). FTB 3804-CR is a genuine small worksheet: K-1 credit + optional prior-year "
     "carryover, capped at CURRENT-YEAR CA tax liability (nonrefundable), excess carries "
     "forward up to 5 years. Built as the current-year-absorption-only slice, same "
     "established pattern as the NOL/EBL/disaster-loss/capital-loss carryovers -- "
     "carryforward disclosed in the answer text, not tracked. The 9.3% rate itself is "
     "confirmed 100% entity-side (the electing PTE computes it and reports the resulting "
     "dollar figure on the K-1); the taxpayer's stated figure is trusted as-is, same "
     "precedent as every other K-1-sourced item. NOT modeled, disclosed: Schedule P's "
     "credit-ordering against OTHER nonrefundable credits (e.g. Other State Tax Credit, "
     "built earlier this session -- the two features don't coordinate with each other, same "
     "'each standalone feature is independent' precedent as the exemption credit/OSTC); the "
     "AMT/TMT interaction (moot, this system doesn't compute AMT); the SMLLC-specific "
     "limitation; and the one-credit-per-couple rule."),
    ("540", "credits", "235", "College Access Tax Credit",
     "credit", "narrow", "FTB Form 3592 (2025) Instructions, Sections B-D; R&TC Section 17053.85",
     "built", None,
     "Built via income_brackets.compute_catc_credit_ca_tax / engine._income_catc_credit_answer. "
     "Dedicated research confirmed 50% is correct for TY2025 but is YEAR-KEYED (60% TY2014, 55% "
     "TY2015, 50% TY2016-TY2027) and the credit is far more than a self-computed pass-through: "
     "requires CEFA application/reservation/certification (contribution alone isn't automatically "
     "creditable), a $500M/year statewide first-come-first-served pool, nonrefundable with a SIX-"
     "year carryover (not five, unlike PTE credit), a 2028 sunset (guarded via CATC_SUNSET_TAX_YEAR), "
     "a separate $5,000,000 aggregate business-credit ceiling for TY2024-2026, and a Schedule CA "
     "(540) Line 11 Column B addback if also deducted federally (already tracked not_applicable in "
     "schedule_ca_inventory.py). Modeled: 50% of a stated contribution (treated as equivalent to "
     "the CEFA-certified amount for a COMPLETED contribution, since CEFA's process reserves/"
     "certifies BEFORE the contribution is made), capped at CA tax liability with the 6-year "
     "carryover disclosed not tracked -- same PTE/adoption-credit pattern. NOT modeled, disclosed: "
     "the $500M pool/allocation status itself, the $5M business-credit ceiling, the Line 11 addback, "
     "non-CA-resident eligibility (CEFA's own FAQ declines to answer this). Zero extraction bugs "
     "found live -- ninth Phase 3 item shipped, eighth of nine requiring a real correction to the "
     "original survey/ledger note's claim about HOW something works."),
    ("540", "credits", "233/237/239/245/246/247/248", "Business/committee-allocated/industry-specific credits (Competes, film production incl. Program 4.0, cannabis equity/high-road, disabled-access, agricultural-donation, homeless-hiring, low-income-housing, historic-rehab)",
     "credit", "narrow", "FTB 2025 Form 540 Booklet, Credit Chart",
     "not_applicable", None,
     "All committee-allocated, employer-side, or industry-certified -- narrow for individual "
     "filers, individual exposure (if any) only via an already-fixed K-1 pass-through number."),
    ("540", "credits", "183", "Research Credit (FTB 3523)",
     "credit", "narrow", "FTB 2025 Form 540 Booklet, Credit Chart, code 183",
     "not_applicable", None, "Needs multi-year gross-receipts history for the fixed-base-percentage computation, same historical-data problem as several deferred Schedule CA items."),
    ("540", "credits", "188", "Prior Year Alternative Minimum Tax Credit (FTB 3510)",
     "credit", "narrow", "FTB 2025 Form 540 Booklet, Credit Chart, code 188",
     "not_applicable", None, "Needs prior-year AMT liability + carryover history -- multi-year, same class as deferred Schedule CA basis items."),
    ("540", "credits", "repealed", "Repealed/carryover-only credits (28 codes, 160-241 range)",
     "credit", "one_time", "FTB 2025 Form 540 Booklet, Repealed Credits list",
     "not_applicable", None,
     "Agricultural Products, Solar (Commercial/Residential/Pump), Ridesharing (incl. employer "
     "variants/transit passes), Employer Childcare, Energy Conservation, Enhanced Oil Recovery, "
     "Enterprise Zone (Hiring/Sales-Use), Farmworker Housing, LAMBRA (Hiring/Sales-Use), Low-"
     "Emission Vehicles, Main Street Small Business (I/II), Manufacturing Enhancement Area "
     "Hiring, Orphan Drug, Political Contributions, Recycling Equipment, Residential Rental & "
     "Farm Sales, Salmon & Steelhead Habitat, Targeted Tax Area (Hiring/Sales-Use), Water "
     "Conservation, Young Infant, and similar -- all aging/closing carryover-only populations "
     "needing the taxpayer's own historical carryover ledger balance, a fact nobody states in "
     "a Q&A, identical reasoning already applied to legacy items on the Schedule CA ledger."),
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
        # Same orphan-pruning discipline as schedule_ca_inventory.py's load(),
        # scoped to part='540' only so it never touches Schedule CA's own rows.
        current_keys = {(line_ref, label) for _, _, line_ref, label, *_ in ITEMS}
        existing = conn.execute(
            "SELECT id, line_ref, item_label FROM schedule_ca_inventory WHERE tax_year=%s AND part=%s",
            (TAX_YEAR, "540")).fetchall()
        orphan_ids = [row_id for row_id, line_ref, label in existing
                      if (line_ref, label) not in current_keys]
        if orphan_ids:
            conn.execute(
                "DELETE FROM schedule_ca_inventory WHERE id = ANY(%s)", (orphan_ids,))
            print(f"pruned {len(orphan_ids)} orphaned row(s) (stale item_label from a rename/split)")
    print(f"loaded {len(ITEMS)} form540 inventory items")
    status_report()


def status_report():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT status, count(*) FROM schedule_ca_inventory WHERE tax_year=%s AND part=%s "
        "GROUP BY status ORDER BY count(*) DESC", (TAX_YEAR, "540")).fetchall()
    total = sum(r[1] for r in rows)
    print(f"\n=== FORM 540 + CREDITS {TAX_YEAR} INVENTORY ({total} items) ===")
    for status, n in rows:
        print(f"  {status:28} {n}")
    conn.close()


def list_items(status_filter=None):
    conn = db.get_conn()
    q = "SELECT section, line_ref, item_label, status, topic_key FROM schedule_ca_inventory WHERE tax_year=%s AND part=%s"
    params = [TAX_YEAR, "540"]
    if status_filter:
        q += " AND status=%s"
        params.append(status_filter)
    q += " ORDER BY section NULLS LAST, line_ref"
    rows = conn.execute(q, params).fetchall()
    for section, line_ref, label, status, topic_key in rows:
        loc = f"Form 540 ({section}) line {line_ref}"
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
