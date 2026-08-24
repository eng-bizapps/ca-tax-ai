"""Adversarial item sweep for the Ring 2 income-tax domain (Phase 3/4).

Mirrors item_sweep.py's proven pattern (cached, resumable, mandatory
regression gate after any change to income_brackets.py/engine.py's income
path) but for the heterogeneous income-domain content: bracket computations
(hand-verified against FTB's own worked example, one case crossing the $1M
Behavioral Health Services Tax surtax threshold per the plan's explicit
Phase 3 verification requirement), standard-deduction lookups, structured
topic verdicts, deliberate-defer cases (complexity disqualifiers), and
CROSS-DOMAIN safety (a sales-tax question must never be answered by the
income domain, and vice versa -- the plan's Phase 4 requirement, started
here rather than deferred further since the risk is concrete and testable
now).

Usage:
  python income_item_sweep.py run
  python income_item_sweep.py report
  python income_item_sweep.py reset
"""
import json
import os
import sys

import engine

CACHE = os.path.join(os.path.dirname(__file__), "income_item_sweep_results.json")
TOL = 0.02   # float rounding tolerance for dollar comparisons

# Each item: (question, expected dict). expected keys are checked only if present.
#   status: 'answered' | 'informational' | 'needs_review'
#   domain: 'income' | 'sales'
#   category, taxable, tax (within TOL)
ITEMS = [
    # --- bracket computation (hand-verified) ---
    # $125,000 MFJ: dedu $11,412 -> taxable $113,588 (Sched Y $82,904-$115,084
    # band: $2,044.02 + 6% x ($113,588-$82,904) = $3,885.06)
    ("what is my california tax bracket if I make $125,000 filing married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3885.06}),
    # $60,000 HOH: dedu $11,412 -> taxable $48,588 (Sched Z $22,173-$52,530
    # band: $221.73 + 2% x ($48,588-$22,173) = $750.03)
    ("how much tax do I owe on $60,000 as head of household",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 750.03}),
    # $30,000 single: dedu $5,706 -> taxable $24,294 (Sched X $11,079-$26,264
    # band: $110.79 + 2% x ($24,294-$11,079) = $375.09)
    ("how much california tax do I owe on $30,000 single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 375.09}),
    # $1,500,000 single: dedu $5,706 -> taxable $1,494,294, CROSSES the $1M
    # surtax threshold. Bracket: $72,219.84 + 12.3% x ($1,494,294-$742,953) =
    # $164,634.78; surtax: 1% x ($1,494,294-$1,000,000) = $4,942.94.
    # Total = $169,577.72.
    ("how much california tax do I owe on $1,500,000 single filing",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 169577.72}),

    # --- standard deduction (real structured lookup, not LLM paraphrase) ---
    ("what is the california standard deduction",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),
    ("what is the standard deduction for single filers",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),
    ("what is the standard deduction for married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_standard_deduction"}),

    # --- CalEITC (verified 658-row 2025 table, see load_income_content.py) ---
    # $9,975, 2 children: table row 9,951-10,000, column 2 = $3,288 --
    # spot-checked directly against the visually-read PDF page during
    # extraction (independent of the extraction script's own validation).
    ("what is my CalEITC if I make $9,975 with 2 qualifying children",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 3288.00}),
    # $100, 0 children: table row 51-100, column 0 = $5
    ("what is my california earned income tax credit if I earn $100 with no children",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 5.00}),
    # missing children count -> specific clarifying message, not generic defer
    ("what is my caleitc if I make $10,000",
     {"status": "needs_review", "domain": "income"}),

    # --- CalEITC investment-income disqualification (FTB 3514 Step 2) --
    # verified against the actual 2025 FTB 3514 Booklet: the 2025 limit is
    # $4,814 -- NOT the federal EITC's ~$11,950 figure (a prior memory note
    # had guessed the federal number by analogy; checking the actual CA
    # form caught the real, much lower CA-specific figure). $9,975/2
    # children under the limit -> same $3,288 as the plain CalEITC case
    # above (investment income only gates eligibility, doesn't change the
    # credit amount); both orderings tested identical.
    ("what is my CalEITC if I make $9,975 with 2 qualifying children and $1,000 in investment income",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 3288.00}),
    ("what is my CalEITC if I make $1,000 in investment income and $9,975 with 2 qualifying children",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 3288.00}),
    # OVER the $4,814 limit -> disqualified entirely, $0, regardless of
    # earned income or child count.
    ("what is my CalEITC if I make $9,975 with 2 qualifying children and $5,000 in investment income",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 0.00}),
    # missing children count -> specific clarifying message, same pattern
    ("what is my CalEITC if I make $9,975 with $1,000 in investment income",
     {"status": "needs_review", "domain": "income"}),

    # --- Young Child Tax Credit (exact FTB Form 3514 Part VII arithmetic) ---
    # below the $27,425 phase-out threshold -> flat $1,189
    ("what is my young child tax credit if I earn $20,000",
     {"status": "answered", "domain": "income", "category": "young_child_tax_credit", "tax": 1189.00}),
    # near the top of the phase-out range ($32,900, same ceiling as CalEITC):
    # excess=5475, step1=54.75, reduction=54.75*21.71=1188.68 (FTB's own
    # 2-step rounding), credit=1189-1188.68=0.32 -> between $0 and $1 -> $1
    ("what is my yctc if I make $32,900",
     {"status": "answered", "domain": "income", "category": "young_child_tax_credit", "tax": 1.00}),

    # --- Foster Youth Tax Credit (FTB 2025 Form 3514, Step 10 / Part IX) --
    # reclassified from "Bucket 1 -- structurally impossible" once it was
    # clear the barrier was missing facts, not multi-turn intake. SAME
    # phase-out formula/numbers as YCTC ($1,189 max, $27,425 threshold,
    # $21.71 per $100) verified against Part IX's own Line 34/36-39
    # arithmetic -- but gated on TWO extra facts (age 18-25, foster care
    # at 13+) PLUS a real CalEITC-eligibility check (via lookup_eitc_table,
    # not a bare claim). $9,975/2 children under the $27,425 threshold ->
    # full $1,189 (below threshold, no phase-out).
    ("what is my foster youth tax credit if I am 20 years old, was in foster care at age 15, and made $9,975 with 2 qualifying children",
     {"status": "answered", "domain": "income", "category": "foster_youth_tax_credit", "tax": 1189.00}),
    # non-numeric "since age 13" phrasing, reordered clauses (income+children
    # stated BEFORE age/foster-care) -- both orderings must agree.
    ("what is my fytc if I am 22, in foster care since age 13, and made $100 with no children",
     {"status": "answered", "domain": "income", "category": "foster_youth_tax_credit", "tax": 1189.00}),
    ("what is my fytc if I made $9,975 with 2 children and am 20 years old and was in foster care at age 15",
     {"status": "answered", "domain": "income", "category": "foster_youth_tax_credit", "tax": 1189.00}),
    # a stated age OUTSIDE 18-25 (once foster-care-at-13+ already checks
    # out) is a clean, definitive "no" -- not a generic defer.
    ("what is my fytc if I am 30 years old, was in foster care at age 15, and made $9,975 with 2 children",
     {"status": "answered", "domain": "income", "category": "foster_youth_tax_credit", "tax": 0.00}),
    # income too high to be CalEITC-eligible at all -> FYTC denied on the
    # FIRST gate (Step 10's own requirement), not the phase-out arithmetic.
    # Also the case that caught the sharpest bug this pass: three
    # clustered numbers (age 20, foster-care-age 15, income $50,000) --
    # the shared distance-based _amount_near picked the WRONG number
    # (age, not income) for the keyword "made", and even a from-scratch
    # nearest-number fix hit an exact tie between the two candidate ages;
    # only clause-splitting (isolating "foster care at age 15" as its own
    # clause) resolved it reliably.
    ("what is my fytc if I am 20, was in foster care at age 15, and made $50,000 with no children",
     {"status": "answered", "domain": "income", "category": "foster_youth_tax_credit", "tax": 0.00}),
    # missing age/foster-care facts entirely -> specific checklist, not a
    # generic defer.
    ("what is my foster youth tax credit if I made $9,975 with 2 children",
     {"status": "needs_review", "domain": "income"}),
    # age stated but foster-care-at-13+ fact missing -> still incomplete
    # (a bare "was a foster youth" alone doesn't establish the age-13 cutoff).
    ("what is my fytc if I am 20 and made $9,975 with 2 children",
     {"status": "needs_review", "domain": "income"}),

    # --- Senior Head of Household Credit (Code 163) -- min(2% of AGI,
    # $1,860), with the $98,652 AGI ceiling as a SEPARATE eligibility
    # gate verified against the 2025 Form 540 instructions' own worksheet
    # (not just the credit page's looser "income" wording). $40,000 AGI
    # -> 2%*40000=$800 (under the cap).
    ("what is my senior head of household credit if I am 67, qualified for head of household last year, my qualifying person died this year, and my AGI is $40,000",
     {"status": "answered", "domain": "income", "category": "senior_hoh_credit", "tax": 800.00}),
    # $95,000 AGI: 2%*95000=$1,900 (over the $1,860 cap) but STILL under
    # the $98,652 eligibility ceiling -> capped at exactly $1,860, not
    # disqualified (the sharpest wrinkle this credit has).
    ("what is my senior head of household credit if I am 70, qualified for head of household 2 years ago, my qualifying person died last year, and my AGI is $95,000",
     {"status": "answered", "domain": "income", "category": "senior_hoh_credit", "tax": 1860.00}),
    # $100,000 AGI -- AT/ABOVE the $98,652 ceiling -> disqualified
    # entirely (not just capped).
    ("what is my senior head of household credit if I am 70, qualified for head of household 2 years ago, my qualifying person died last year, and my AGI is $100,000",
     {"status": "answered", "domain": "income", "category": "senior_hoh_credit", "tax": 0.00}),
    # explicit under-65 age (other facts check out) -> clean "no", not a
    # generic defer.
    ("what is my senior hoh credit if I am 60, qualified for head of household last year, my qualifying person died this year, and my AGI is $40,000",
     {"status": "answered", "domain": "income", "category": "senior_hoh_credit", "tax": 0.00}),
    # missing the "qualified for HOH before"/"qualifying person died"
    # facts -> specific checklist, not a generic defer.
    ("what is my senior head of household credit if I am 67 and my AGI is $40,000",
     {"status": "needs_review", "domain": "income"}),

    # --- Joint Custody Head of Household Credit (Code 170) / Credit for
    # Dependent Parent (Code 173) -- SHARE the same 30%-of-tax-liability-
    # capped-at-$610 formula (verified against the 2025 Form 540
    # instructions' shared worksheet), different eligibility checklists.
    # Also the pass that caught a real bug in the MOST shared function in
    # the income domain: income_brackets.detect_filing_status's plain
    # "married" substring check ALSO matched inside "unmarried", and
    # combined with "joint custody" (containing "joint"), was wrongly
    # detected as MFJ filing status -- fixed with a \bmarried\b word-
    # boundary check that benefits every income compute path, not just
    # this one. ALSO required reordering _answer_income() -- these 3
    # credits' own names contain "head of household" (read as a genuine
    # filing-status statement) and their natural "my tax liability is $X"
    # phrasing collides with income_brackets.COMPUTE_TRIGGERS, so they're
    # now checked BEFORE the generic wage-only bracket path rather than
    # after every other credit.
    ("I have joint custody of my daughter, pay more than half her expenses, was unmarried, she lived with me 180 days, and my tax liability is $2,000 -- what is my joint custody head of household credit?",
     {"status": "answered", "domain": "income", "category": "joint_custody_hoh_credit", "tax": 600.00}),
    # married but lived apart from spouse ALL YEAR is the alternative
    # marital-status branch (not just "unmarried") -- and a high enough
    # tax liability to hit the $610 cap.
    ("I have joint custody of my son, pay more than half his expenses, was married but lived apart from my spouse all year, he lived with me 200 days, and my tax liability is $5,000 -- what is my joint custody hoh credit?",
     {"status": "answered", "domain": "income", "category": "joint_custody_hoh_credit", "tax": 610.00}),
    # residency days OUTSIDE 146-219 (too few -- child lived with them
    # LESS than the required range) -> clean "no", not a generic defer.
    ("I have joint custody of my daughter, pay more than half her expenses, was unmarried, she lived with me 100 days, and my tax liability is $2,000 -- what is my joint custody head of household credit?",
     {"status": "answered", "domain": "income", "category": "joint_custody_hoh_credit", "tax": 0.00}),
    # missing facts (no residency days, no marital status stated) ->
    # specific checklist.
    ("what is my joint custody head of household credit if I have joint custody of my son and pay more than half his expenses",
     {"status": "needs_review", "domain": "income"}),
    ("what is my dependent parent credit if I am married filing separately, my spouse was not a member of my household for the last six months, I paid more than half my mothers household expenses, and my tax liability is $2,000",
     {"status": "answered", "domain": "income", "category": "dependent_parent_credit", "tax": 600.00}),
    # wrong filing status stated (MFJ, not MFS) -> not this credit's
    # shape at all -> specific checklist.
    ("what is my dependent parent credit if I am married filing jointly and my tax liability is $2,000",
     {"status": "needs_review", "domain": "income"}),

    # --- Nonrefundable Renter's Credit (flat amount, hard income ceiling) ---
    ("what is my renters credit if I make $40,000 single",
     {"status": "answered", "domain": "income", "category": "renters_credit", "tax": 60.00}),
    ("what is my renters credit if I make $40,000 filing married filing jointly",
     {"status": "answered", "domain": "income", "category": "renters_credit", "tax": 120.00}),
    # missing filing status -> specific clarifying message, not generic defer
    ("what is my renters credit if I make $40,000",
     {"status": "needs_review", "domain": "income"}),

    # --- structured conformity-topic verdicts ---
    ("is unemployment compensation taxable in california",
     {"status": "answered", "domain": "income", "category": "unemployment_compensation", "taxable": False}),
    ("is social security income taxable in california",
     {"status": "answered", "domain": "income", "category": "social_security_income", "taxable": False}),
    ("is california paid family leave taxable",
     {"status": "answered", "domain": "income", "category": "paid_family_leave", "taxable": False}),
    ("is an inheritance taxable in california",
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),

    # --- second conformity batch (verified against FTB pages fetched via
    # browser this pass; income_route_eval.py confirmed the gambling/lottery
    # same-domain collision pair resolves correctly at every threshold) ---
    # status flipped answered -> conditional (2026-08-10): collision_audit.py
    # --domain=income flagged gambling_winnings/california_lottery_winnings
    # as a live HIGH-risk undisclosed collision (0.008 apart, opposite
    # verdicts) -- confirmed as a REAL bug: before this fix, a question
    # naming the CA Lottery specifically was confidently answered TAXABLE
    # with no mention of the CA-specific lottery exclusion, because
    # _income_topic_answer never called _find_branches at all. Now it does
    # (see engine._income_topic_answer/_income_branch_info), and these two
    # topics correctly disclose each other as alternates -- same accepted
    # "working as intended" pattern as sales tax's own MEDIUM-bucket close
    # pairs, not a regression.
    ("are gambling winnings taxable in california",
     {"status": "conditional", "domain": "income", "category": "gambling_winnings", "taxable": True}),
    ("are california lottery winnings taxable",
     {"status": "conditional", "domain": "income", "category": "california_lottery_winnings", "taxable": False}),
    # status flipped answered -> conditional (2026-08-10), same branch-
    # disclosure fix: "US government bond interest" (exempt) vs "out-of-
    # state MUNICIPAL bond interest" (taxable) is a genuinely easy real-world
    # mix-up -- both read as "bonds from somewhere other than California" to
    # a layperson, but CA taxes them oppositely. Disclosing the distinction
    # here is a valuable catch, not noise.
    ("is interest from us treasury bonds taxable in california",
     {"status": "conditional", "domain": "income", "category": "us_government_bond_interest", "taxable": False}),
    ("is interest from an out of state municipal bond taxable in california",
     {"status": "answered", "domain": "income", "category": "out_of_state_municipal_bond_interest", "taxable": True}),
    ("are hsa contributions taxable in california",
     {"status": "answered", "domain": "income", "category": "hsa_contributions_and_earnings", "taxable": True}),

    # --- Head of Household eligibility: DELIBERATELY informational-only
    # (no taxable key asserted -- it's None, same as ca_standard_deduction's
    # pattern above). Confirms the compute path (which needs a dollar
    # amount) and this pure-eligibility topic never collide.
    ("can I file as head of household in california",
     {"status": "answered", "domain": "income", "category": "head_of_household_eligibility"}),
    ("am I eligible for head of household filing status",
     {"status": "answered", "domain": "income", "category": "head_of_household_eligibility"}),

    # --- Head of Household DETERMINATION (not just the criteria) -- a
    # genuine yes/no verdict when ALL required facts are stated in one
    # question. Reclassified from "Bucket 1 -- structurally impossible"
    # after realizing the real barrier was missing facts, not multi-turn
    # intake -- the same "trust the input" principle as every other
    # compute path. Verified against FTB's actual 2025 Form 3532
    # Instructions before building; deliberately narrow (unmarried the
    # entire year, taxpayer's OWN child, not a qualifying relative, no
    # "considered unmarried" separated-spouse complexity).
    ("I was unmarried all year, paid more than half the cost of keeping up my home, and my 10 year old son lived with me all year -- do I qualify for head of household?",
     {"status": "answered", "domain": "income", "category": "head_of_household_determination", "taxable": True}),
    # a full-time student age 19-23 still passes the age test.
    ("I was unmarried all year, paid more than half the cost of keeping up my home, and my 20 year old daughter who is a full-time student lived with me all year -- am I eligible for head of household?",
     {"status": "answered", "domain": "income", "category": "head_of_household_determination", "taxable": True}),
    # a clean, simple negative: married all year is a hard gate,
    # independent of every other fact.
    ("I was married all year, do I qualify for head of household?",
     {"status": "answered", "domain": "income", "category": "head_of_household_determination", "taxable": False}),
    # age 25, not a full-time student -- an explicit age-test failure is
    # its own clean "no" once everything else already checks out (not a
    # generic defer).
    ("I was unmarried all year, paid more than half the cost of keeping up my home, and my 25 year old son lived with me all year -- do I qualify for head of household?",
     {"status": "answered", "domain": "income", "category": "head_of_household_determination", "taxable": False}),
    # a stated age WITHOUT "years old" phrasing ("he is 16") -- found
    # missing via testing, the age regex originally required "N years
    # old"/"yo" explicitly.
    ("my son lived with me all year, I was unmarried all year, and I paid more than half the cost of keeping up my home. He is 16. Do I qualify for head of household?",
     {"status": "answered", "domain": "income", "category": "head_of_household_determination", "taxable": True}),
    # negation-aware full-time-student check -- "NOT a full-time student"
    # contains the literal substring "full-time student", which a naive
    # check would have wrongly counted as satisfying the rescue.
    ("I was unmarried all year, my son is 22 and NOT a full-time student, he lived with me all year, and I paid more than half the cost of keeping up my home -- do I qualify for head of household?",
     {"status": "answered", "domain": "income", "category": "head_of_household_determination", "taxable": False}),
    # RDP-only phrasing (no literal "unmarried") -- a real, plausible way
    # to state the marital-status fact in California specifically.
    ("I was not in a registered domestic partnership all year, paid more than half the cost of keeping up my home, and my daughter, 14 years old, lived with me for 200 days -- do I qualify for head of household?",
     {"status": "answered", "domain": "income", "category": "head_of_household_determination", "taxable": True}),
    # a qualifying-person who is a RELATIVE (not the taxpayer's own
    # child) -- real complexity (gross-income ceiling, community
    # property) this v1 deliberately defers rather than guesses.
    ("I was unmarried all year and my nephew lived with me, do I qualify for head of household?",
     {"status": "needs_review"}),
    # a married-but-separated taxpayer -- the "considered unmarried"
    # sub-test, real complexity this v1 deliberately defers.
    ("I was separated from my spouse and my son lived with me, do I qualify for head of household?",
     {"status": "needs_review"}),
    # a partial checklist (some facts stated, not all) gets a SPECIFIC
    # checklist message, not a generic defer -- same pattern as every
    # other missing-fact clarifying message in this project.
    ("I was unmarried all year and my 15 year old son lived with me all year, do I qualify for head of household?",
     {"status": "needs_review", "domain": "income"}),
    # precision guard: a BARE "am I eligible"/"can I file as HOH" question
    # with ZERO personal facts must keep falling through to the existing
    # informational topic (already tested above), NOT get intercepted by
    # the new checklist-incomplete message -- caught as a real regression
    # while building this feature (every vague HOH question was
    # downgraded from "answered" informational to "needs_review" before
    # the fix requiring at least one stated personal fact first).
    ("what are the requirements to file head of household",
     {"status": "answered", "domain": "income", "category": "head_of_household_eligibility"}),

    # --- workers' comp / SDI: sourced via the actual statute (R&TC 17131,
    # 17083 at leginfo.legislature.ca.gov) after ftb.ca.gov had no dedicated
    # adjustment page for either -- both excluded from FEDERAL AGI already,
    # so there's never a Schedule CA line for them the way unemployment/PFL have.
    ("is workers compensation taxable in california",
     {"status": "answered", "domain": "income", "category": "workers_compensation", "taxable": False}),
    ("is california state disability insurance taxable",
     {"status": "answered", "domain": "income", "category": "state_disability_insurance", "taxable": False}),
    # (the existing "how much tax do I owe on $60,000 as head of household"
    # bracket-compute case above already proves the compute path and this
    # eligibility topic don't collide -- amount-bearing questions never
    # reach topic routing at all, see _answer_income's call order.)

    # --- cross-domain override: found via direct testing, sales & income
    # both fired confidently on "gift" (sales: promotional_gifts -- the
    # GIVER's use-tax liability, wrong for this phrasing; income: correctly
    # nontaxable). Must resolve to income for receiver-phrased questions,
    # and must NOT hijack genuinely giver-phrased sales questions.
    ("do I have to pay tax on a gift I received",
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),
    ("I got a gift from my parents, is that taxable",
     {"status": "answered", "domain": "income", "category": "gifts_and_inheritance", "taxable": False}),
    ("I am giving away merchandise as a gift, do I owe tax",
     {"status": "answered", "domain": "sales", "category": "promotional_gifts", "taxable": True}),

    # --- self-employment (sole-proprietor Schedule C) compute path ---
    # was a "deliberate defer" case until this pass; now genuinely computed.
    # Hand-verified: $100k net profit x 0.9235 = $92,350 SE earnings; SE tax
    # = $92,350 x 0.153 = $14,129.55; half-deduction = $7,064.77(5); AGI =
    # $92,935.23; taxable income (minus $11,412 MFJ std deduction) =
    # $81,523.23; MFJ bracket (52528-82904, base 828.98, 4%): 828.98 + 0.04
    # x (81523.23-52528) = $1,988.79.
    ("how much california tax do I owe on $100,000 self-employed married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax",
      "tax": 1988.79}),
    ("what is my self employment tax bracket if I make $50,000 freelancing single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax"}),
    # missing filing status -> specific clarifying message, same pattern
    ("how much tax do I owe on $50,000 self-employed",
     {"status": "needs_review", "domain": "income"}),

    # --- self-employment complexity still correctly defers (never guess
    # past a fact pattern this simple sole-proprietor path doesn't cover) ---
    ("how much tax do I owe on $100,000 self-employed with rental income single",
     {"status": "needs_review"}),
    # was a deliberate defer until the mixed wages+SE compute path landed
    # this pass; now genuinely computed (hand-verified: $100k SE net profit
    # + $30k wages MFJ -> SE tax $14,129.55 -> half-deduction $7,064.77(5)
    # -> AGI $122,935.23 -> taxable (minus $11,412 MFJ std deduction)
    # $111,523.23; Sched Y 82904-115084, base 2044.02, 6%:
    # 2044.02+0.06*(111523.23-82904) = $3,761.17).
    ("how much tax do I owe on $100,000 self-employed and $30,000 in wages married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax",
      "tax": 3761.17}),
    # CORRECTED 2026-08-15: this was a stale expectation, not a genuine
    # defer -- entity-level tax has no personal filing status at all, so
    # a well-formed "how much tax does my S-corp owe" question shouldn't
    # be blocked by an irrelevant trailing word like "single". Found via
    # the Income Coverage Blueprint Phase 2b routing change, which forced
    # a full cache reset (the normal incremental "run" never re-verifies
    # already-cached entries, so this had been silently passing against
    # an outdated cached value for who knows how long). Verified
    # independently: entity_tax.detect_entity_type correctly reads this
    # as ('s_corp', False) [not financial, not first-year], and $800
    # base annual tax + 1.5% x $100,000 = $2,300 matches the already-
    # documented S-corp formula exactly -- not a new bug, a correct
    # answer the old cache just never re-checked.
    ("how much tax do I owe as an s-corp making $100,000 single",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 2300.0}),

    # --- mixed wages + self-employment: the first multi-amount compute
    # path (engine._amount_near, distance-based keyword tagging so the
    # wage figure and SE figure are never confused regardless of order in
    # the sentence -- verified directly by testing both orderings gave
    # identical results before trusting it). $50k wages + $30k net SE
    # profit MFJ: SE tax on $30k = $30,000*0.9235*0.153 = $4,238.865 ->
    # half-deduction ~$2,119.43; AGI = 80000-2119.43=77880.57ish; taxable
    # (minus $11,412 MFJ std deduction) ~$66,468.57; MFJ bracket
    # (52528-82904, base 828.98, 4%): 828.98+0.04*(66468.57-52528) =
    # $1,386.60 (hand-verified, matched by the engine exactly).
    ("how much tax do I owe on $50,000 in wages and $30,000 in self-employment income married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 1386.60}),
    ("how much tax do I owe on $30,000 in self-employment income and $50,000 in wages married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 1386.60}),
    # missing filing status -> specific clarifying message, same pattern
    ("how much tax do I owe on $50,000 in wages and $30,000 self-employed",
     {"status": "needs_review", "domain": "income"}),
    # complexity still correctly defers even in the mixed path
    ("how much tax do I owe on $50,000 in wages and $30,000 self-employed with rental income single",
     {"status": "needs_review"}),
    # a real bug found and fixed while building this: "freelancing"/
    # "contracting" (gerund forms) didn't match "freelance"/"contractor"
    # as substrings, so these silently fell through to the WAGE-ONLY path
    # and dropped the self-employment income entirely (a real understated-
    # tax bug, not a safe defer) before the fix.
    ("how much tax do I owe on $50,000 freelancing single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 994.39}),
    ("how much tax do I owe on $50,000 contracting single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 994.39}),

    # --- investment income (capital gains, dividends, interest) computed
    # as ordinary income -- verified against FTB Schedule CA (540) Line 7a:
    # "California taxes long and short term capital gains as regular
    # income. No special rate...exists." Hand-verified: $50k single, $5,706
    # std deduction -> $44,294 taxable (Sched X 41452-57542, base 1022.01,
    # 6%): 1022.01 + 0.06*(44294-41452) = $1,192.53. Same figure for all
    # three income-type phrasings since the math is identical by design.
    ("how much tax do I owe on $50,000 in capital gains single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1192.53}),
    ("how much tax do I owe on $50,000 in dividends single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1192.53}),
    ("how much tax do I owe on $50,000 in interest income single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1192.53}),
    # landmine guard: capital gain + a home-sale word must NOT go through
    # the simple compute path -- Section 121's $250k/$500k primary-
    # residence exclusion is a completely different calculation.
    ("how much tax do I owe on $50,000 capital gain from selling my house single",
     {"domain": "income"}),
    # capital LOSS and stock/RSU still correctly defer (real complexity
    # this simple path deliberately does not attempt).
    ("how much tax do I owe on $50,000 in capital losses single",
     {"status": "needs_review"}),
    ("how much tax do I owe on $50,000 in stock single",
     {"status": "needs_review"}),

    # --- bonus and pension income: verified via FTB Schedule CA (540) --
    # bonus is confirmed just ordinary wage income (the 10.23% supplemental
    # rate is a WITHHOLDING mechanic, not the actual liability); pensions
    # are "generally no adjustment" (Line 5a/5b) with a narrow Tier 2
    # railroad-retirement exception disclosed in the answer text.
    # $60k single bonus: taxable $54,294 (Sched X 41452-57542, base
    # 1022.01, 6%): 1022.01+0.06*(54294-41452)=$1,792.53.
    ("how much tax do I owe on $60,000 including a bonus single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1792.53}),
    # $40k single pension: taxable $34,294 (Sched X 26264-41452, base
    # 414.49, 4%): 414.49+0.04*(34294-26264)=$735.69.
    ("how much tax do I owe on $40,000 in pension income single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 735.69}),

    # --- alimony: a genuine trap caught by reading the actual current FTB
    # text (not secondary sources) -- California conformed to the federal
    # TCJA alimony rule for agreements executed after 12/31/2025, but NOT
    # for the 2019-2025 "gap" window, and NOT before 2019 either (matches
    # federal there too, for a different reason). Genuinely date-dependent,
    # not a single boolean -- taxable=None, informational-style like HOH.
    ("is alimony taxable in california",
     {"status": "answered", "domain": "income", "category": "alimony_spousal_support"}),
    ("is spousal support taxable in california",
     {"status": "answered", "domain": "income", "category": "alimony_spousal_support"}),

    # --- itemized deductions: "trust the input" (like SE net profit) --
    # verified against FTB's 2025 Schedule CA (540) Line 29/30 instructions
    # (greater-of comparison; MFS special rule; AGI-limitation threshold).
    # $80k wages single, $12,000 itemized (> $5,706 standard deduction) ->
    # taxable $68,000 (Sched X 57542-72731, base 2469.19, 8%... actually
    # verified directly against the engine + compute_ca_tax: $2,824.05).
    # Uses _amount_near like the mixed wage+SE path -- both orderings tested
    # to confirm the itemized figure and income figure are never swapped.
    ("how much california tax do I owe on $80,000 in wages with $12,000 in itemized deductions filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2824.05}),
    ("how much california tax do I owe on $12,000 in itemized deductions and $80,000 in wages filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2824.05}),
    # itemized total BELOW the standard deduction ($3,000 < $5,706) -- must
    # fall back to the standard deduction (the "greater of" rule), not the
    # smaller itemized figure: taxable $80,000-$5,706=$74,294 -> $3,347.98.
    ("how much california tax do I owe on $80,000 in wages with $3,000 in itemized deductions filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),
    # missing filing status -> specific clarifying message, same pattern
    ("how much california tax do I owe on $80,000 in wages with $12,000 in itemized deductions",
     {"status": "needs_review", "domain": "income"}),
    # married/RDP filing separately: CA requires both spouses to itemize (or
    # both take the standard deduction) -- can't know the other spouse's
    # choice, so this defers with a specific explanation instead of guessing.
    ("how much california tax do I owe on $80,000 in wages with $12,000 in itemized deductions married filing separately",
     {"status": "needs_review", "domain": "income"}),
    # above the 2025 AGI limitation threshold ($252,203 single) -- Tier 2
    # (2026-08-11, same session): the Line 29 phase-out worksheet is now
    # IMPLEMENTED (was: silently deferred). Verified by hand: excess AGI
    # cap = (600000-252203)*0.06 = 20867.82, 80%-of-itemized cap =
    # 50000*0.8 = 40000 -> the smaller (excess-AGI cap) binds -> reduced
    # itemized = 50000-20867.82 = 29132.18 -> taxable = 600000-29132.18 =
    # 570867.82 -> tax = 52774.21 (matches the live engine exactly).
    ("how much california tax do I owe on $600,000 in wages with $50,000 in itemized deductions filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 52774.21}),
    # a much higher AGI where the OTHER side of the min() binds instead:
    # 80% of itemized (10000*0.8=8000) is smaller than 6% of excess AGI
    # ((2000000-252203)*0.06=104867.82) -> reduction=8000 (capped, not the
    # excess-AGI figure) -> reduced itemized=2000 -> below the $5,706
    # standard deduction, so the STANDARD deduction is used instead
    # (greater-of rule) -> taxable=2000000-5706=1994294.
    ("how much california tax do I owe on $2,000,000 in wages with $10,000 in itemized deductions filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket"}),
    # SALT automation: $30,000 itemized total, $12,000 of which is stated
    # state income tax -- California disallows that portion entirely
    # (Schedule CA Line 5a), so the CA-usable itemized total is $18,000
    # (still > the $5,706 standard deduction) -> taxable=100000-18000=82000
    # -> tax=4064.64 (hand-verified against the live engine).
    ("how much california tax do I owe on $100,000 in wages with $30,000 in itemized deductions, of which $12,000 was state income tax, filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 4064.64}),
    # SALT amount large enough to push the CA-usable itemized total BELOW
    # the standard deduction -- must fall back to the standard deduction,
    # not a negative/near-zero itemized figure: $20,000 itemized - $18,000
    # SALT = $2,000 CA-usable, less than $5,706 standard -> standard wins
    # -> taxable=80000-5706=74294.
    ("how much california tax do I owe on $80,000 in wages with $20,000 in itemized deductions, of which $18,000 was state income tax, filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),
    # mortgage interest addback (Schedule CA Line 8): $30,000 itemized +
    # $8,000 disallowed-federally-but-CA-allowed mortgage interest =
    # $38,000 CA-usable -> taxable=100000-38000=62000 -> tax=2344.05
    # (hand-verified against the live engine).
    ("how much tax do I owe on $100,000 in wages with $30,000 in itemized deductions, of which $8,000 in mortgage interest was limited by the federal cap, filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2344.05}),
    # SALT + mortgage addback together (4 clustered dollar amounts, each
    # with its own distinct anchor phrase): $30,000 itemized - $12,000 SALT
    # + $8,000 mortgage addback = $26,000 CA-usable -> taxable=74000 ->
    # tax=3320.64.
    ("how much tax do I owe on $100,000 in wages with $30,000 in itemized deductions, $12,000 of which was state income tax, and $8,000 in mortgage interest was disallowed, filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3320.64}),
    # misc itemized 2% floor reinstatement (Schedule CA Lines 19-22):
    # $30,000 itemized + $5,000 tax-prep-fee-style misc expenses, floor =
    # 2% of $100,000 AGI = $2,000, reinstated = $5,000-$2,000 = $3,000 ->
    # $33,000 CA-usable -> taxable=100000-33000=67000 -> tax=2744.05
    # (verified via direct function call, no Gemini needed for the math).
    ("how much tax do I owe on $100,000 in wages with $30,000 in itemized deductions and $5,000 in tax preparation fees filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2744.05}),
    # all 5 clustered figures together (income, itemized, SALT, mortgage
    # addback, misc expenses): itemized=30000-12000(SALT)+8000(mortgage)=
    # 26000, misc floor=2%*100000=2000, reinstated=5000-2000=3000,
    # final=29000 -> taxable=71000 -> tax=3064.05.
    ("how much tax do I owe on $100,000 in wages with $30,000 in itemized deductions, $12,000 of which was state income tax, $8,000 in mortgage interest was disallowed, and $5,000 in unreimbursed employee expenses, filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3064.05}),
    # charitable contribution AGI cap (Schedule CA Lines 11-12): $60,000
    # itemized incl. $55,000 charitable, CA cap = 50% of $100,000 AGI =
    # $50,000, disallowed = $5,000 -> itemized=55000 -> taxable=45000 ->
    # tax=1234.89 (verified via direct function call).
    ("how much tax do I owe on $100,000 in wages with $60,000 in itemized deductions and $55,000 in charitable contributions filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1234.89}),
    # charitable contribution well under the cap -- no adjustment, same as
    # the plain-itemized baseline with these numbers: taxable=70000 ->
    # tax=2984.05.
    ("how much tax do I owe on $100,000 in wages with $30,000 in itemized deductions and $5,000 in charitable contributions filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2984.05}),
    # SALT cap addback (Schedule CA Line 5e), Tier 2's 7th and final item:
    # $30,000 itemized + $8,000 that was cut off by the federal $40k/$20k
    # SALT cap, which California doesn't conform to -> $38,000 CA-usable
    # -> taxable=100000-38000=62000 -> tax=2344.05 (same shape/numbers as
    # the mortgage-addback-only case, since both are a simple +$8,000 to
    # the itemized total -- verified via direct function call).
    ("how much tax do I owe on $100,000 in wages with $30,000 in itemized deductions and $8,000 that was over the federal salt limit filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2344.05}),
    # SALT (income tax) + SALT cap addback together: $30,000 - $12,000
    # (income tax, Line 5a) + $8,000 (cap addback, Line 5e) = $26,000 ->
    # taxable=74000 -> tax=3320.64.
    ("how much tax do I owe on $100,000 in wages with $30,000 in itemized deductions, $12,000 of which was state income tax, and $8,000 that was over the federal salt limit, filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3320.64}),
    # Casualty/theft loss (Schedule CA Part II Line 15), the 8th and final
    # itemized-deduction figure, closing out the completeness ledger --
    # verified against FTB's Line 15 text: "Under federal law, the
    # personal casualty and theft loss deduction is suspended, with
    # exception for personal casualty gains... Federal law allows a
    # deduction for personal casualty and theft loss incurred in a
    # federally declared disaster. California law does not conform.
    # California allows personal casualty and theft loss and disaster
    # loss deductions." A two-fact design (deliberate middle ground, not
    # blind trust-the-input, not a full per-event rebuild): the stated
    # casualty_loss_amount is trusted as ALREADY net of the federal
    # $100-per-event floor and insurance reimbursement (same trust
    # boundary as charitable_amount/capital_loss elsewhere), but the
    # 10%-of-AGI floor (IRS Pub. 547's "10% Rule") is COMPUTED here
    # rather than trusted, since it's the one piece a taxpayer's
    # self-reported "final number" is most likely to get wrong.
    #
    # Basic: $100,000 wages, $20,000 other itemized, $25,000 casualty
    # loss, single -> 10% AGI floor = $10,000 -> $15,000 deductible ->
    # itemized total $35,000 -> taxable $65,000 -> $2,584.05.
    ("how much california tax do I owe on $100,000 in wages with $20,000 in itemized deductions and a $25,000 casualty loss single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2584.05}),
    # same figures, casualty loss stated first -- order independence.
    ("how much california tax do I owe on a $25,000 casualty loss, $20,000 in itemized deductions, and $100,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2584.05}),
    # BELOW the 10%-AGI floor ($5,000 loss < $10,000 floor at $100,000
    # AGI): no deduction applies, itemized total stays $20,000 -> taxable
    # $80,000 -> $3,878.64.
    ("how much california tax do I owe on $100,000 in wages with $20,000 in itemized deductions and a $5,000 casualty loss single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3878.64}),
    # alternate trigger phrasing ("theft loss" instead of "casualty
    # loss") -- same figures as the basic case -> $2,584.05.
    ("how much california tax do I owe on $100,000 in wages with $20,000 in itemized deductions and a $25,000 theft loss single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2584.05}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $100,000 in wages with $20,000 in itemized deductions and a $25,000 casualty loss",
     {"status": "needs_review", "domain": "income"}),
    # combined with a THIRD optional itemized figure (charitable
    # contributions, under California's 50%-of-AGI cap so no
    # disallowance) -- exercises 3 simultaneous tagged figures + income
    # without collision, same result as the basic case since the
    # charitable amount is fully allowed -> $2,584.05.
    ("how much california tax do I owe on $100,000 in wages with $20,000 in itemized deductions, a $25,000 casualty loss, and $10,000 in charitable contributions single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2584.05}),
    # MFJ coverage: $150,000 wages, $30,000 other itemized, $30,000
    # casualty loss -> 10% AGI floor $15,000 -> $15,000 deductible ->
    # itemized total $45,000 -> taxable $105,000 -> $4,368.10.
    ("how much california tax do I owe on $150,000 in wages with $30,000 in itemized deductions and a $30,000 casualty loss married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 4368.10}),
    # a third, unrelated dollar amount makes the question ambiguous (which
    # figure is income?) -- correctly defers rather than guessing.
    ("how much california tax do I owe on $80,000 in wages with $12,000 in itemized deductions and $5,000 in bonus filing single",
     {"status": "needs_review"}),
    # itemizing + self-employment together is still out of scope (itemize
    # stays in SE_COMPLEXITY_EXCLUDE) -- correctly defers.
    ("how much california tax do I owe on $80,000 self-employed with $12,000 in itemized deductions filing single",
     {"status": "needs_review"}),

    # --- capital losses: annual $3,000/$1,500-MFS offset limit, same
    # conformity pattern as itemized deductions -- verified against FTB's
    # 2025 Instructions for California Schedule D (540), Line 9. Unlike
    # itemized deductions, MFS is a normal case here (just a smaller
    # limit), not excluded. $80k wages single, $10,000 capital loss (>
    # $3,000 limit) -> deductible $3,000 -> AGI $77,000 -> taxable
    # $71,294 (Sched X 57542-72731, 8%... verified directly against the
    # engine + compute_ca_tax: $3,087.57).
    ("how much california tax do I owe on $80,000 in wages with $10,000 in capital losses filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3087.57}),
    ("how much california tax do I owe on $10,000 in capital losses and $80,000 in wages filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3087.57}),
    # loss BELOW the annual limit ($1,000 < $3,000) -- fully deductible,
    # no carryover: AGI $79,000 -> taxable $73,294 -> $3,254.98.
    ("how much california tax do I owe on $80,000 in wages with $1,000 in capital losses filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3254.98}),
    # MFS uses the HALVED $1,500 limit (not $3,000): AGI $78,500 -> taxable
    # $72,794 -> $3,208.48.
    ("how much california tax do I owe on $80,000 in wages with $10,000 in capital losses married filing separately",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3208.48}),
    # missing filing status -> specific clarifying message, same pattern
    ("how much california tax do I owe on $80,000 in wages with $10,000 in capital losses",
     {"status": "needs_review", "domain": "income"}),
    # netting a gain against a loss in one question is out of scope -- defers
    ("how much california tax do I owe on $80,000 in wages with $10,000 in capital losses and $5,000 in capital gains filing single",
     {"status": "needs_review"}),
    # (a bare capital loss with no other income to offset isn't a
    # meaningful compute case -- already covered by the pre-existing
    # "$50,000 in capital losses single" defer case in the investment-
    # income section above, still correctly needs_review.)
    # capital loss + self-employment together is still out of scope --
    # correctly defers.
    ("how much california tax do I owe on $80,000 self-employed with $10,000 in capital losses filing single",
     {"status": "needs_review"}),

    # --- excess business loss limitation (Ring 3 extension, IRC 461(l) /
    # FTB Form 3461) -- verified against the 2025 Form 3461 instructions:
    # CA runs its OWN continuous limitation (does not conform to the
    # current federal version), 2025 threshold $313,000 (single/MFS/HOH),
    # $626,000 (MFJ/RDP joint). All tax figures below verified directly
    # against the live engine (compute_excess_business_loss_ca_tax), same
    # practice as every other compute path in this file.
    #
    # MFJ, $800,000 wages, $700,000 business loss (EXCEEDS $626,000
    # threshold): allowed loss capped at $626,000, $74,000 excess carries
    # forward (not applied) -> AGI $174,000 -> taxable $162,588 -> $7,997.96.
    ("how much california tax do I owe on $800,000 in wages with $700,000 in business loss married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.96}),
    # same figures, loss stated before income -- order independence, same
    # pattern as the capital-loss reordering test.
    ("how much california tax do I owe on $700,000 in business loss and $800,000 in wages married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.96}),
    # QSS THRESHOLD DETERMINATION, locked in behaviorally: Form 3461's
    # definitions section doesn't name "Qualifying Surviving Spouse/RDP"
    # explicitly (only single/HOH/MFS and MFJ) -- resolved from this
    # codebase's own precedent that QSS always pairs with MFJ at every
    # other filing-status dollar threshold (standard deduction, Schedule Y
    # bracket table, itemized AGI phase-out). This is the exact question
    # that was being fact-checked via a risky direct-PDF browser navigation
    # when the prior Claude Desktop session crashed (see project memory) --
    # resolved instead via in-codebase precedent, no PDF fetch needed. Same
    # dollar figures as the MFJ case immediately above -> IDENTICAL result
    # confirms the $626,000 threshold applies to QSS too.
    ("how much california tax do I owe on $800,000 in wages with $700,000 in business loss qualifying surviving spouse",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.96}),
    # HOH shares the $313,000 threshold with single/MFS (not the higher
    # MFJ/QSS figure): $500,000 wages, $400,000 loss (EXCEEDS $313,000) ->
    # allowed loss $313,000, $87,000 excess carries forward -> AGI
    # $187,000 -> taxable $175,588 -> $10,740.06.
    ("how much california tax do I owe on $500,000 in wages with $400,000 in business loss head of household",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 10740.06}),
    # MFS also uses the $313,000 threshold (same as single/HOH, NOT a
    # halved figure the way the capital-loss annual limit works) -- same
    # $500,000/$400,000 facts as the HOH case above, different (smaller)
    # standard deduction -> taxable $181,294 -> $13,298.98.
    ("how much california tax do I owe on $500,000 in wages with $400,000 in business loss married filing separately",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 13298.98}),
    # UNDER the threshold ($100,000 loss < $313,000 single threshold) --
    # the limitation does NOT apply at all, full loss is deductible, no
    # carryforward: $200,000 wages - $100,000 loss = AGI $100,000 -> minus
    # $5,706 std deduction -> taxable $94,294 -> $5,207.98.
    ("how much california tax do I owe on $200,000 in wages with $100,000 in business loss filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 5207.98}),
    # loss exactly AT the threshold ($313,000) -- boundary case, still
    # fully deductible (excess is 0, not a rounding-off-by-one): $400,000
    # wages - $313,000 loss = AGI $87,000 -> taxable $81,294 -> $3,998.98.
    ("how much california tax do I owe on $400,000 in wages with $313,000 in business loss filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3998.98}),
    # missing filing status -> specific clarifying message, same pattern
    # as every other compute path.
    ("how much california tax do I owe on $200,000 in wages with $400,000 in business loss",
     {"status": "needs_review", "domain": "income"}),
    # business loss + capital loss together is out of scope (two different
    # loss mechanisms with different limits/carryforward rules) --
    # correctly defers rather than picking one arbitrarily.
    ("how much california tax do I owe on $200,000 in wages with $400,000 in business loss and $10,000 in capital losses filing single",
     {"status": "needs_review"}),
    # SELF-EMPLOYMENT COLLISION GUARD: before this feature, "self-employed"
    # + a stated dollar figure would have been intercepted by the
    # self-employment path and treated as POSITIVE net profit even though
    # the question describes a LOSS (compute_se_tax has no way to know the
    # figure was meant as negative) -- the same "wrong number silently
    # used" bug class as the contracted/salaried stemming gaps found
    # earlier this project. Added "business loss" etc. to
    # SE_COMPLEXITY_EXCLUDE/K1_COMPLEXITY_EXCLUDE alongside this feature so
    # the self-employment/K-1 paths correctly step aside instead. This
    # exact phrasing doesn't cleanly match the excess-business-loss path
    # either (no separately-stated "other income" figure, self-employment
    # IS the business loss here) -- correctly defers rather than guessing
    # which path should own it.
    ("how much california tax do I owe on $700,000 self-employed with a $700,000 business loss filing single",
     {"status": "needs_review"}),

    # --- CA NOL (net operating loss) carryover deduction suspension (Ring
    # 3 extension, R&TC 17276.24 / FTB Form 3805V) -- verified against the
    # 2025 Form 3805V instructions: suspended for TY2024-2026 when net
    # business income AND modified AGI are both >=$1,000,000 (an AND test
    # for suspension = an OR test for the exemption); no percentage cap
    # when not suspended (unlike federal law's 80%-of-income cap), just
    # capped at Modified Taxable Income (MTI). This path collapses "net
    # business income" and "modified AGI" into ONE stated business-income
    # figure under a disclosed sole-income-source assumption. All tax
    # figures verified directly against the live engine.
    #
    # SUSPENDED: $1,200,000 business income (>=$1,000,000 threshold),
    # $200,000 NOL carryover -> ENTIRE carryover disallowed this year
    # (deduction $0, full $200,000 preserved) -> taxable income is just
    # business income minus std deduction: $1,194,294 -- crosses the $1M
    # Behavioral Health Services Tax surtax threshold too -> $129,677.72.
    ("how much california tax do I owe on $1,200,000 in business income with a $200,000 nol carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 129677.72}),
    # exact $1,000,000 BOUNDARY -- FTB's "or more" means suspended AT the
    # threshold, not just strictly above it: $1,000,000 business income,
    # $50,000 NOL carryover -> still suspended -> taxable $994,294 ->
    # $103,134.78.
    ("how much california tax do I owe on $1,000,000 in business income with a $50,000 nol carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 103134.78}),
    # NOT suspended, carryover fully usable (under MTI): $500,000 business
    # income (<$1M), $100,000 NOL carryover -> MTI = $494,294, full
    # $100,000 deductible -> taxable $394,294 -> $33,336.13.
    ("how much california tax do I owe on $500,000 in business income with a $100,000 nol carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 33336.13}),
    # NOT suspended, but carryover EXCEEDS MTI -- capped at MTI (not the
    # full stated carryover), remainder still carries forward: $50,000
    # business income, MTI = $44,294, $60,000 NOL carryover -> only
    # $44,294 deductible, $15,706 remains carrying forward -> taxable
    # income drops to exactly $0 -> tax $0.00.
    ("how much california tax do I owe on $50,000 in business income with a $60,000 nol carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 0.00}),
    # MFJ coverage, not suspended: $700,000 business income, $150,000 NOL
    # carryover -> MTI = $688,588, full $150,000 deductible -> taxable
    # $538,588 -> $42,965.96.
    ("how much california tax do I owe on $700,000 in business income with a $150,000 nol carryover married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 42965.96}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $500,000 in business income with a $100,000 nol carryover",
     {"status": "needs_review", "domain": "income"}),
    # wage income mixed in -- real complexity this MVP doesn't attempt
    # (the sole-income-source assumption behind collapsing "net business
    # income" and "modified AGI" into one figure would no longer hold) --
    # correctly defers.
    ("how much california tax do I owe on $60,000 in wages and $500,000 in business income with a $100,000 nol carryover single",
     {"status": "needs_review"}),
    # SELF-EMPLOYMENT COLLISION, TWO amounts stated: before the
    # SE_COMPLEXITY_EXCLUDE fix, "self-employed" + a business-income
    # figure would have been intercepted by the self-employment path,
    # which would compute SE tax on the profit while silently IGNORING
    # the NOL carryover the question explicitly asked about -- a
    # confidently-computed answer omitting a deduction the user asked
    # for. Now the self-employment path correctly steps aside and the
    # dedicated NOL path picks it up instead, giving the SAME correct
    # (suspended) answer as the plain $1,200,000/$200,000-carryover case
    # above but with a different (smaller) carryover amount that doesn't
    # change the outcome (still suspended either way): tax $129,677.72.
    ("how much california tax do I owe on $1,200,000 self-employed with a $50,000 nol carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 129677.72}),
    # SELF-EMPLOYMENT COLLISION, ONE amount only (genuinely ambiguous --
    # no separately-stated NOL carryover dollar figure to extract):
    # correctly defers rather than guessing which figure means what.
    ("how much california tax do I owe if I am self-employed and made $1,200,000, considering my nol carryover single",
     {"status": "needs_review"}),

    # --- NOL carryover for a WAGE-ONLY filer with NO current-year
    # business income (Schedule CA (540) Line 8a "wages/other income"
    # population -- previously left deferred in schedule_ca_inventory.py
    # as "real new scope," but found tractable via a closer look at the
    # SAME suspension rule already verified above: the suspension test is
    # an AND condition (net business income >= $1M AND modified AGI
    # >= $1M) -- a taxpayer with $0 current business income (a closed
    # prior business) can NEVER satisfy the business-income leg, so
    # suspension is structurally impossible for this population at ANY
    # wage level. Requires an EXPLICIT closed-business confirmation,
    # never assumed from silence.
    #
    # Basic: $80,000 wages, $20,000 NOL carryover, closed business.
    # MTI = 80000-5706=74294, deduction=min(20000,74294)=20000, taxable
    # =54294 -> $1,792.53.
    ("how much California tax do I owe with an NOL carryover of $20,000, my closed business, if my wages are $80,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1792.53}),
    # order independence.
    ("single, my wages are $80,000, my closed business, how much california tax do i owe with an nol carryover of $20,000?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1792.53}),
    # HIGH INCOME still never suspended -- confirms this isn't just true
    # near the threshold; $2,000,000 wages, $20,000 carryover -> fully
    # deductible regardless -> $233,417.72 (includes the BHS surtax).
    ("how much California tax do I owe with an NOL carryover of $20,000, my closed business, if my wages are $2,000,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 233417.72}),
    # missing filing status -> specific clarifying message.
    ("how much California tax do I owe with an NOL carryover of $20,000, my closed business, if my wages are $80,000?",
     {"status": "needs_review", "domain": "income"}),
    # AMBIGUOUS: NOL vocabulary present but neither a closed-business
    # confirmation nor an ongoing-business signal -- routes to a
    # dedicated clarifying question. This exact phrasing exposed a real
    # bug live: detect_compute_signal's COMPLEXITY_EXCLUDE doesn't
    # recognize NOL/carryover vocabulary at all, so without an explicit
    # NOL-term exclusion added to detect_compute_signal, the plain wage-
    # compute path would race ahead and answer with the FIRST dollar
    # figure in the question (the NOL amount, not wages) instead of
    # deferring -- a silently WRONG answer, not just a missing one.
    ("how much California tax do I owe with an NOL carryover of $20,000 if my wages are $80,000, single?",
     {"status": "needs_review", "domain": "income"}),
    # ONGOING BUSINESS signal ("self-employed") -- correctly excluded,
    # since business income could genuinely be nonzero for this
    # population and the real suspension-test complexity still applies.
    ("how much California tax do I owe with an NOL carryover of $20,000, I'm self-employed, if my wages are $80,000, single?",
     {"status": "informational", "domain": "income"}),

    # --- cannabis 280E business-expense decoupling (Ring 3 extension,
    # R&TC Section 17209) -- verified against the statute and the 2025
    # Schedule CA (540) instructions' Line 3 cannabis paragraph: LICENSED
    # (MAUCRSA/DCC) commercial cannabis businesses may deduct ordinary
    # business expenses federal IRC 280E disallows, restored as a
    # subtraction against California income. Reuses
    # compute_self_employment_ca_tax's cannabis_280e_expenses parameter
    # (a self-employment computation with one extra CA-specific fact, not
    # a new mechanism). All tax figures verified directly against the
    # live engine.
    #
    # LICENSED, restoration applies: $500,000 federal net profit, $150,000
    # in disallowed 280E expenses restored -> SE tax $35,227.15 (half
    # deductible $17,613.58, computed on the FULL federal figure --
    # unaffected by CA's own conformity choice), AGI = 500,000 -
    # 17,613.58 - 150,000 = 332,386.42 -> taxable $326,680.42 ->
    # $26,819.92.
    ("how much california tax do I owe on $500,000 net profit from a licensed cannabis business with $150,000 in disallowed 280e expenses single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 26819.92}),
    # MFJ coverage, different figures: $300,000 net profit, $80,000
    # disallowed expenses restored -> $10,886.97.
    ("how much california tax do I owe on $300,000 net profit from a licensed cannabis business with $80,000 in disallowed 280e expenses married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 10886.97}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $500,000 net profit from a licensed cannabis business with $150,000 in disallowed 280e expenses",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COLLISION, TWO amounts stated: before the
    # SE_COMPLEXITY_EXCLUDE fix, "self-employed" + a licensed-cannabis
    # net-profit figure would have been intercepted by the plain
    # self-employment path, computing SE tax on the FEDERAL (280E-
    # inflated) net profit while silently IGNORING the CA-specific
    # restoration the question explicitly stated -- understating the
    # deduction, overstating the tax owed. Now the self-employment path
    # correctly steps aside and the dedicated cannabis-280E path answers
    # instead, giving the SAME result as the plain $500,000/$150,000 case
    # above: $26,819.92.
    ("how much california tax do I owe if I am self-employed running a licensed cannabis business with $500,000 net profit and $150,000 in disallowed 280e expenses single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 26819.92}),
    # SELF-EMPLOYMENT COLLISION, ONE amount only (genuinely ambiguous --
    # no separately-stated 280E restoration figure to extract): correctly
    # defers rather than guessing which figure means what.
    ("how much california tax do I owe if I am self-employed running a licensed cannabis business that made $500,000 single",
     {"status": "needs_review"}),
    # UNLICENSED cannabis business -- deliberately NOT a cannabis-280E
    # trigger (no CANNABIS_LICENSE_TERMS match), so this correctly falls
    # through to the PLAIN self-employment path unmodified -- federal
    # 280E fully applies for California too when unlicensed, so no
    # restoration is the CORRECT answer here, not a gap. $50,000 net
    # profit, single -- matches the plain SE $50k/single figure exactly
    # (same math as any ordinary sole-proprietor question).
    ("how much tax do I owe on $50,000 self-employed single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 994.39}),

    # --- employer fringe-benefit expense restoration (Schedule CA Line 3,
    # Part I Section B "Business Income or (Loss)") -- verified against
    # the 2025 FTB Schedule CA (540) instructions directly: TCJA
    # disallows/limits employer deductions for entertainment, employee
    # parking/transit, and on-premises meals; California does not
    # conform (confirmed still true post-SB-711 -- a specific decoupling,
    # not a byproduct of the old conformity date, same pattern as QSBS
    # and cannabis 280E). Reuses compute_self_employment_ca_tax's math
    # unchanged via a new fringe_benefit_restoration parameter -- same
    # shape as cannabis 280E, not a new compute path. Scope-gated to
    # taxpayers who are themselves EMPLOYERS (these are benefits paid TO
    # employees). All tax figures verified directly against the live
    # engine; no new bugs found this time (applied the collision-guard
    # and phantom-amount lessons from the four prior Line-7a-family
    # features up front).
    #
    # Basic restoration: $500,000 net profit, $50,000 disallowed fringe-
    # benefit expenses restored, single -> AGI $432,386.42 -> taxable
    # $426,680.42 -> $36,671.94.
    ("how much california tax do I owe on $500,000 self-employed net profit with $50,000 in disallowed employee fringe benefit expenses single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 36671.94}),
    # MFJ coverage, using the "employee parking" specific trigger term
    # rather than the generic "fringe benefit expenses" phrase: $300,000
    # net profit, $30,000 restored -> $15,536.97.
    ("how much california tax do I owe on $300,000 self-employed net profit with $30,000 in disallowed employee parking expenses married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 15536.97}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $500,000 self-employed net profit with $50,000 in disallowed employee fringe benefit expenses",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COLLISION, TWO amounts stated: before the
    # SE_COMPLEXITY_EXCLUDE fix, this would have been intercepted by the
    # plain self-employment path, computing SE tax on the federal (TCJA-
    # limited) net profit while silently ignoring the stated restoration.
    # Now correctly redirects to this dedicated path, same result as the
    # plain restoration case above with identical figures: $36,671.94.
    ("how much california tax do I owe on $500,000 self-employed with $50,000 in disallowed employee fringe benefit expenses single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 36671.94}),
    # SELF-EMPLOYMENT COLLISION, ONE amount only (genuinely ambiguous --
    # no separately-stated restoration figure to extract): correctly
    # defers rather than guessing which figure means what.
    ("how much california tax do I owe if I am self-employed with disallowed employee fringe benefit expenses and made $500,000 single",
     {"status": "needs_review"}),

    # --- traditional IRA deduction pass-through (Ring 3 extension,
    # Schedule CA (540) Part I Section C Line 20) -- verified against the
    # 2025 vs 2024 FTB Schedule CA (540) instructions diffed year-over-
    # year: SB 711 (Conformity Act of 2025) moved California's general
    # IRC conformity date from 1/1/2015 to 1/1/2025, which repealed the
    # two previously-live Line 20 divergence triggers (age-70.5 addback,
    # catch-up-contribution-indexing addback) for TY2025 -- so for TY2025
    # the stated federal IRA deduction is allowed for California
    # UNCHANGED. All tax figures verified directly against the live
    # engine. Also confirmed via the full engine.answer() pipeline (not
    # just the income-domain function directly) that this feature has NO
    # sales-tax routing collision, unlike cannabis 280E.
    #
    # Basic pass-through: $80,000 wages, $6,000 IRA deduction, single ->
    # AGI $74,000 -> taxable $68,294 -> $2,847.57.
    ("how much california tax do I owe on $80,000 in wages with a $6,000 IRA deduction single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2847.57}),
    # same figures, IRA deduction stated before income -- order
    # independence, same pattern as capital-loss/EBL/NOL reordering tests.
    ("how much california tax do I owe on a $6,000 IRA deduction and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2847.57}),
    # MFJ coverage: $120,000 wages, $12,000 IRA deduction -> AGI
    # $108,000 -> taxable $96,588 -> $2,865.06.
    ("how much california tax do I owe on $120,000 in wages with a $12,000 IRA deduction married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2865.06}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with a $6,000 IRA deduction",
     {"status": "needs_review", "domain": "income"}),
    # ROTH IRA REDIRECT: Roth contributions are never deductible --
    # "roth ira contribution" also contains "ira contribution" as a
    # substring, so this specifically tests that the Roth check
    # intercepts BEFORE the main IRA-deduction path could wrongly try to
    # compute a deduction for something that was never eligible.
    ("how much california tax do I owe on $80,000 in wages with a $6,000 roth ira contribution single",
     {"status": "answered", "domain": "income", "category": "roth_ira_not_deductible"}),
    # SELF-EMPLOYMENT COLLISION: an IRA deduction is a general above-the-
    # line adjustment that can accompany ANY income type, including
    # self-employment -- before the SE_COMPLEXITY_EXCLUDE fix, this would
    # have been intercepted by the plain self-employment path, silently
    # ignoring the stated IRA deduction. Now correctly defers (the
    # self-employment/worker-classification mismatch is the one
    # confirmed remaining CA/federal divergence trigger for this line,
    # genuinely out of scope for this single-question model).
    ("how much california tax do I owe on $80,000 self-employed with a $6,000 IRA deduction single",
     {"status": "needs_review"}),

    # --- QSBS (Qualified Small Business Stock, IRC 1202/1045) full
    # addback -- verified against the 2025 FTB Instructions for
    # California Schedule D (540): "California does not conform to the
    # qualified small business stock deferral and gain exclusion under
    # IRC Sections 1045 and 1202. Enter the entire gain realized in
    # column (e)." A COMPLETE non-conformity (100%, not partial), NOT
    # affected by SB 711 (specific R&TC 18152.5 decoupling, not a general
    # conformity-date item -- confirmed since CA still doesn't conform to
    # OBBBA's 2025 QSBS expansion either). Two stated figures required
    # (federal taxable gain + the amount excluded/deferred), never one --
    # deliberately doesn't guess which a single stated figure means. All
    # tax figures verified directly against the live engine, including a
    # phantom-amount fix (literal "1202"/"1045" in the question parses as
    # a bare number via the shared amount regex, same collision class as
    # cannabis 280E's phantom $280) and a full-pipeline check confirming
    # no sales-tax routing collision.
    #
    # Basic addback: $100,000 federal taxable gain, $400,000 excluded
    # under Section 1202, single -> CA gain $500,000 -> taxable $494,294
    # -> $44,121.37.
    ("how much california tax do I owe on $100,000 federal taxable gain from qsbs stock with $400,000 excluded under section 1202 single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 44121.37}),
    # same figures, order reversed -- order independence.
    ("how much california tax do I owe on $400,000 excluded under section 1202 and $100,000 federal taxable gain from qsbs stock single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 44121.37}),
    # MFJ coverage: $200,000 federal taxable gain, $600,000 excluded,
    # married filing jointly -> CA gain $800,000 -> taxable $788,588 ->
    # $66,672.26.
    ("how much california tax do I owe on $200,000 federal taxable gain from qualified small business stock with $600,000 excluded under section 1202 married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 66672.26}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $100,000 federal taxable gain from qsbs stock with $400,000 excluded under section 1202",
     {"status": "needs_review", "domain": "income"}),
    # AMBIGUOUS: only one dollar figure stated, no separate excluded
    # amount -- correctly defers rather than guessing whether the stated
    # figure is pre- or post-exclusion.
    ("how much california tax do I owe on $500,000 in qsbs gain single",
     {"status": "needs_review"}),
    # PLAIN CAPITAL-GAINS PATH COLLISION GUARD: before the QSBS guard was
    # added to detect_compute_signal, a stated "capital gains" figure
    # alongside a QSBS mention would have been silently answered by the
    # generic wage/capital-gains path, treating the stated figure as
    # already-CA-taxable -- WRONG if it's actually the post-exclusion
    # federal figure. Now correctly defers, mirroring the existing
    # home-sale (Section 121) carve-out's exact pattern.
    ("how much california tax do I owe on $500,000 in capital gains from qualified small business stock single",
     {"status": "needs_review"}),

    # --- HSA-held investment sale gain addback -- verified against the
    # 2025 FTB Instructions for Schedule CA (540): "the California basis
    # of the assets listed [below] may be different from the federal
    # basis... Gain or loss from the sale of investments inside an HSA."
    # California doesn't recognize HSAs as tax-favored at all, so a
    # realized gain is CA-taxable the year it occurs with NO federal
    # counterpart (federally it's invisible inside the tax-advantaged
    # wrapper) -- unlike QSBS, only ONE adjustment figure needed, no
    # offsetting amount to add back. Losses are explicitly out of scope
    # (ordinary capital-loss $3,000/$1,500 annual-limit mechanics apply
    # instead, nothing HSA-specific about that case). All tax figures
    # verified directly against the live engine, including confirming
    # this does NOT collide with the existing hsa_contributions_and_
    # earnings informational topic (that topic only fires as a fallback,
    # after every compute path -- including this one -- has had its
    # chance) and does NOT defer on self-employment mentions the way IRA
    # deduction intentionally does (HSA gain taxability is orthogonal to
    # how the other income was earned -- a narrower, purpose-built
    # HSA_GAIN_COMPLEXITY_EXCLUDE is used instead of the shared
    # COMPLEXITY_EXCLUDE, confirmed via a self-employment case that now
    # correctly answers rather than defers).
    #
    # Basic addback: $80,000 wages, $5,000 HSA investment gain, single ->
    # AGI $85,000 -> taxable $79,294 -> $3,812.98.
    ("how much california tax do I owe on $80,000 in wages with a $5,000 hsa investment gain single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3812.98}),
    # same figures, HSA gain stated before income -- order independence.
    ("how much california tax do I owe on a $5,000 hsa investment gain and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3812.98}),
    # MFJ coverage: $120,000 wages, $10,000 HSA investment gain -> AGI
    # $130,000 -> taxable $118,588 -> $4,255.14.
    ("how much california tax do I owe on $120,000 in wages with a $10,000 hsa investment gain married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 4255.14}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with a $5,000 hsa investment gain",
     {"status": "needs_review", "domain": "income"}),
    # LOSS phrasing -- deliberately NOT this feature's scope (ordinary
    # capital-loss mechanics apply instead, nothing HSA-specific about a
    # loss). Also had to add HSA_LOSS_TERMS to the same three exclude
    # sets as HSA_INVESTMENT_GAIN_TERMS -- without that, the plain wage
    # path doesn't recognize "hsa investment loss" as excluded (it
    # doesn't literally say "capital loss") and silently drops the
    # stated loss figure, computing on wages alone -- found live via this
    # exact regression case. Once no compute path claims the question, it
    # correctly falls through to the PRE-EXISTING hsa_contributions_and_
    # earnings informational topic (built in an earlier project phase) --
    # a safe, non-dangerous "yes, taxable" answer with no claimed dollar
    # figure, not a hard defer.
    ("how much california tax do I owe on $80,000 in wages with a $5,000 hsa investment loss single",
     {"status": "answered", "domain": "income", "category": "hsa_contributions_and_earnings"}),
    # SELF-EMPLOYMENT COMBINATION: unlike IRA deduction, HSA gain
    # taxability has nothing to do with how the OTHER income was earned
    # -- correctly ANSWERED (not deferred) here, same figures/result as
    # the plain wage case above, confirming the SE path steps aside
    # (SE_COMPLEXITY_EXCLUDE guard) while this path still computes.
    ("how much california tax do I owe on $80,000 self-employed with a $5,000 hsa investment gain single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3812.98}),

    # --- K-1 pass-through CAPITAL GAIN (Schedule CA Line 7a / Schedule D
    # Line 2, distinct from the existing ORDINARY K-1 income feature on
    # Line 5) -- verified against the 2025 FTB Instructions for
    # California Schedule D (540): "Combine gain(s) and loss(es) from all
    # California Schedule(s) K-1... Enter the net loss on line 2, column
    # (d), or the net gain on line 2, column (e)." Reuses
    # compute_k1_ca_tax's math unchanged (CA taxes capital gains as
    # ordinary income, no special rate) but with the correct Schedule D
    # citation instead of Schedule CA Line 5. Needed its OWN trigger
    # rather than widening K1_TRIGGERS, since K1_COMPLEXITY_EXCLUDE
    # deliberately excludes "capital gain" from the ordinary K-1 path --
    # confirmed "$50,000 in K-1 income" (existing) and "$50,000 in K-1
    # capital gains" (new) produce the IDENTICAL tax figure, just
    # different citations, since the underlying math is the same. Also
    # had to insert this BEFORE the existing K1 fallback catch-all in the
    # dispatcher (not in this session's usual later spot), since
    # "k-1 capital gain" contains the bare "k-1" substring the fallback
    # matches on. GAINS only -- losses use the standard capital-loss
    # annual-limit mechanic instead, explicitly excluded here.
    #
    # Basic: $50,000 K-1 capital gain, single -> taxable $44,294 ->
    # $1,192.53 (same figure as the existing ordinary-K-1-income test
    # case with identical inputs, confirming the shared math).
    ("how much california tax do I owe on $50,000 in K-1 capital gains single",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_capital_gain_tax", "tax": 1192.53}),
    # MFJ coverage: $80,000 K-1 capital gain -> taxable $68,588 ->
    # $1,471.38.
    ("how much california tax do I owe on $80,000 in K-1 capital gains married filing jointly",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_capital_gain_tax", "tax": 1471.38}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $50,000 in K-1 capital gains",
     {"status": "needs_review", "domain": "income"}),
    # LOSS phrasing -- deliberately NOT this feature's scope (ordinary
    # capital-loss annual-limit mechanics apply instead) -- correctly
    # falls through to the generic K1 fallback message rather than
    # mishandling it as a gain.
    ("how much california tax do I owe on $50,000 in K-1 capital losses single",
     {"status": "needs_review"}),
    # EXISTING ordinary K-1 income path, sanity check unaffected by this
    # feature -- same figures/result as before this session.
    ("how much california tax do I owe on $50,000 in K-1 income single",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_income_tax", "tax": 1192.53}),

    # --- Capital loss CARRYOVER from a prior year (Schedule CA Line 7a /
    # Schedule D (540) Line 6) -- verified against the 2025 FTB
    # Instructions for California Schedule D (540): resident-all-prior-
    # years case uses the carryover as-is; a nonresident year anywhere in
    # the carryover's history requires FTB's own recalculation, out of
    # scope here. MATH IS IDENTICAL to the existing current-year capital-
    # loss feature (same $3,000/$1,500-MFS annual limit) -- the real gap
    # was DISCLOSURE: before this feature, "capital loss carryover"
    # phrasing already computed the right NUMBER via the generic capital-
    # loss path (since "capital loss" is a substring), but the generic
    # path's own answer text wrongly claimed "assumes... no capital loss
    # carryover from a prior year" -- contradicting what the question
    # said. This dedicated path (checked BEFORE the generic one, same
    # ordering fix as K-1 capital gain) gives the ACCURATE resident-all-
    # prior-years disclosure instead.
    #
    # Basic, over the annual limit: $80,000 wages, $10,000 carryover,
    # single -> same $3,087.57 as the existing current-year-loss test
    # case with identical inputs, confirming the shared math -- only the
    # citation/disclosure text differs (verified directly, not asserted
    # here since the sweep only checks the fields below).
    ("how much california tax do I owe on $80,000 in wages with a $10,000 capital loss carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3087.57}),
    # MFJ, UNDER the annual limit: $120,000 wages, $2,000 carryover ->
    # fully deductible, no further carryforward -> AGI $118,000 -> taxable
    # $106,588 -> $3,465.06.
    ("how much california tax do I owe on $120,000 in wages with a $2,000 capital loss carryover married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3465.06}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with a $10,000 capital loss carryover",
     {"status": "needs_review", "domain": "income"}),
    # NONRESIDENT HISTORY -- FTB requires recalculating the carryover as
    # if CA-resident throughout if any prior year was spent as a
    # nonresident; this single-question model can't perform that
    # recalculation, so it correctly defers rather than silently ignoring
    # the caveat.
    ("how much california tax do I owe on $80,000 in wages with a $10,000 capital loss carryover single, I was a nonresident of california two years ago",
     {"status": "needs_review"}),
    # (The existing generic current-year capital-loss path's own sanity
    # coverage -- same $80,000/$10,000/single question, no "carryover"
    # wording -- is already tested earlier in this file; not duplicated
    # here. Confirmed directly against the live engine that it still
    # returns its own Line 9 citation and current-year-loss disclosure,
    # unaffected by this feature.)

    # --- CA non-conformity to IRC 469(c)(7), the "real estate
    # professional" exception (Schedule CA Line 5 / FTB Form 3801) --
    # verified against the 2025 FTB Instructions for Form 3801: "For
    # California purposes, all rental activities are passive activities."
    # A real estate professional's rental loss is fully deductible
    # (nonpassive) federally, but stays capped by CA's ordinary $25,000
    # active-participation allowance with its $100,000-$150,000 MAGI
    # phase-out -- CONFIRMED IDENTICAL formula/thresholds to federal Form
    # 8582 (not a new CA-specific calculation, just a refusal to exempt
    # the taxpayer from the standard one). All tax figures verified
    # directly against the live engine, including a routing-collision fix
    # (fiduciary_tax.detect_fiduciary_type deliberately matches bare
    # "estate" as a low-risk substring, which "real ESTATE professional"
    # also triggers -- had to move this feature's dispatcher checks
    # BEFORE the fiduciary trust/estate checks) and a second self-
    # exclusion bug (my own "real estate professional" trigger phrase
    # contains "estate", which COMPLEXITY_EXCLUDE also uses -- fixed by
    # subtracting it from this feature's own base exclude set, same bug
    # class as cannabis 280E's self-exclusion, different word this time).
    #
    # UNDER the $100,000 MAGI threshold: $80,000 other income, $15,000
    # rental loss, single -> full allowance ($25,000) covers the full
    # loss -> AGI $65,000 -> taxable $59,294 -> $2,127.57.
    ("how much california tax do I owe if I am a real estate professional with $80,000 in other income and a $15,000 rental loss single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2127.57}),
    # IN the phase-out range: $120,000 MAGI -> allowance = $25,000 -
    # 0.5*($120,000-$100,000) = $15,000 -> only $15,000 of the $20,000
    # loss allowed, $5,000 added back -> AGI $105,000 -> taxable $99,294
    # -> $5,672.98.
    ("how much california tax do I owe if I am a real estate professional with $120,000 in other income and a $20,000 rental loss single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 5672.98}),
    # OVER $150,000 MAGI: allowance fully phased to $0 -> entire $30,000
    # loss added back for CA even though fully deductible federally ->
    # taxable income equals other income minus std deduction -> $14,507.98.
    ("how much california tax do I owe if I am a real estate professional with $200,000 in other income and a $30,000 rental loss single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 14507.98}),
    # MFJ coverage, under threshold: same $80,000/$15,000 shape, full
    # allowance applies -> taxable $53,588 -> $871.38.
    ("how much california tax do I owe if I am a real estate professional with $80,000 in other income and a $15,000 rental loss married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 871.38}),
    # MFS, lived apart all year -- HALVED thresholds ($12,500 allowance,
    # $50,000-$75,000 phase-out): $60,000 MAGI is in the halved phase-out
    # range -> allowance = $12,500 - 0.5*($60,000-$50,000) = $7,500 ->
    # $7,500 of $15,000 loss allowed -> taxable $46,794 -> $1,342.53.
    ("how much california tax do I owe if I am a real estate professional with $60,000 in other income and a $15,000 rental loss married filing separately, I lived apart from my spouse all year",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1342.53}),
    # MFS, did NOT live apart -- ZERO allowance regardless of MAGI (IRC
    # 469(i)(5)): entire $15,000 loss added back -> taxable $54,294 ->
    # $1,792.53.
    ("how much california tax do I owe if I am a real estate professional with $60,000 in other income and a $15,000 rental loss married filing separately, we lived together all year",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1792.53}),
    # MFS missing the lived-apart/lived-together fact -- material to
    # which allowance applies ($12,500 vs $0), so this gets a SPECIFIC
    # clarifying message rather than a generic defer.
    ("how much california tax do I owe if I am a real estate professional with $60,000 in other income and a $15,000 rental loss married filing separately",
     {"status": "needs_review", "domain": "income"}),
    # missing filing status entirely -> specific clarifying message.
    ("how much california tax do I owe if I am a real estate professional with $80,000 in other income and a $15,000 rental loss",
     {"status": "needs_review", "domain": "income"}),

    # --- Federal foreign earned income/housing exclusion addback
    # (Schedule CA Line 8d, Form 2555) -- verified against the 2025 FTB
    # Instructions for Schedule CA (540): "Enter in column C, as a
    # positive number, the amount excluded from federal income on
    # federal Schedule 1 (Form 1040), line 8d." A flat, unconditional
    # restatement -- California doesn't conform to IRC 911 at all for
    # this resident-population form, no partial-addback or residency-
    # history complexity (the ledger's original "needs residency-history
    # facts" note was stale, same pattern as Line 8a's NOL addback note).
    # SIMPLER than QSBS -- no offsetting federal figure, just one addback
    # figure, mirroring HSA investment gain's shape exactly. Also hit and
    # fixed the SAME phantom-amount bug class as cannabis 280E/QSBS
    # (literal "2555" in "Form 2555" parses as a bare number) and the
    # SAME over-broad-exclude-set bug as HSA's first attempt (initially
    # copied IRA deduction's "defer on self-employment" pattern, which
    # doesn't apply here since the addback is unconditional regardless
    # of income source) -- caught before the regression sweep this time.
    #
    # Basic addback: $80,000 wages, $50,000 excluded under Form 2555,
    # single -> AGI $130,000 -> taxable $124,294 -> $7,997.98.
    ("how much california tax do I owe on $80,000 in wages with $50,000 excluded under form 2555 single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.98}),
    # same figures, excluded amount stated before income -- order
    # independence.
    ("how much california tax do I owe on $50,000 excluded under form 2555 and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.98}),
    # MFJ coverage: $120,000 wages, $60,000 excluded -> AGI $180,000 ->
    # taxable $168,588 -> $8,555.96.
    ("how much california tax do I owe on $120,000 in wages with $60,000 excluded under form 2555 married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 8555.96}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with $50,000 excluded under form 2555",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COMBINATION: unlike IRA deduction, this addback is
    # unconditional regardless of how the OTHER income was earned --
    # correctly ANSWERED (not deferred), same result as the plain wage
    # case above, confirming the SE path steps aside while this path
    # still computes.
    ("how much california tax do I owe on $80,000 self-employed with $50,000 excluded under form 2555 single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.98}),

    # --- IRC 951(a) Subpart F income inclusion subtraction (Schedule CA
    # Line 8n) -- verified against the 2025 FTB Instructions for Schedule
    # CA (540): "Under federal law, if you are a U.S. shareholder of a
    # CFC, you must include IRC Section 951(a) amount in your income.
    # California law does not conform. If you included the amount as
    # income for federal purposes on line 8n, column A, enter the amount
    # on line 8n, column B." A flat, unconditional subtraction -- no
    # worksheet, no cap. Unlike the FEIE ADDBACK above, this fully
    # CANCELS: federal AGI = other_income + inclusion_amount, and the CA
    # subtraction removes it in full, so CA AGI reduces back down to
    # exactly other_income -- the tax comes out identical to a plain
    # other-income-only question, which is the correct outcome (CA taxes
    # CFC earnings only on actual distribution, never on this deemed
    # inclusion). Also hit and proactively guarded against the SAME
    # phantom-amount bug class as cannabis 280E/QSBS/Form 2555 (literal
    # "951" in "951(a)" parses as a bare number) before running this
    # sweep.
    #
    # Basic subtraction: $80,000 wages, $50,000 Subpart F inclusion,
    # single -> federal AGI $130,000 -> CA AGI $80,000 (inclusion fully
    # cancels) -> taxable $74,294 -> $3,347.98 (same as a plain $80,000-
    # wages-only question -- confirms the wash is correct, not a bug).
    ("how much california tax do I owe on $80,000 in wages with a $50,000 subpart f income inclusion single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),
    # same figures, inclusion amount stated before income -- order
    # independence.
    ("how much california tax do I owe on a $50,000 subpart f inclusion and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),
    # alternate trigger phrasing ("irc section 951(a)" instead of "subpart
    # f") -- must route to the SAME Subpart F path, not GILTI (951A is a
    # different, textually-distinct IRC section reference).
    ("how much california tax do I owe on $80,000 in wages with a $50,000 irc section 951(a) inclusion single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),
    # MFJ coverage: $120,000 wages, $50,000 inclusion -> federal AGI
    # $170,000 -> CA AGI $120,000 -> taxable $108,588 -> $3,585.06.
    ("how much california tax do I owe on $120,000 in wages with a $50,000 subpart f income inclusion married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3585.06}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with a $50,000 subpart f income inclusion",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COMBINATION: unconditional regardless of how the
    # OTHER income was earned, same "correctly ANSWERED, not deferred"
    # precedent as FEIE/HSA gain above.
    ("how much california tax do I owe on $80,000 self-employed with a $50,000 subpart f income inclusion single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),

    # --- IRC 951A(a) GILTI inclusion subtraction (Schedule CA Line 8o) --
    # same non-conformity/cancellation mechanic as Subpart F above,
    # TCJA-era instead of pre-existing law: FTB's Line 8o instruction
    # states verbatim "California law does not conform" for GILTI,
    # confirmed separately in the instructions' own TCJA "What's New"
    # bullet list. IRC Section 250's 50% GILTI deduction is a non-issue
    # here -- it's only available to C corps or individuals with an IRC
    # 962 election, so an ordinary individual's federal Schedule 1 line
    # 8o already reports the GROSS inclusion with nothing netted out,
    # meaning the flat full-amount subtraction is correct as-is for the
    # standard case.
    #
    # Basic subtraction: $80,000 wages, $50,000 GILTI inclusion, single ->
    # same wash as Subpart F -> $3,347.98.
    ("how much california tax do I owe on $80,000 in wages with $50,000 in gilti single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),
    # order independence.
    ("how much california tax do I owe on $50,000 in gilti and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),
    # alternate trigger phrasing ("form 8992", GILTI's own computation
    # form) -- also exercises the Form-8992 phantom-amount guard (literal
    # "8992" would otherwise parse as a bare number) and MFJ coverage:
    # $120,000 wages, $50,000 GILTI -> federal AGI $170,000 -> CA AGI
    # $120,000 -> taxable $108,588 -> $3,585.06.
    ("how much california tax do I owe on $120,000 in wages and $50,000 gilti reported on form 8992 married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3585.06}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with $50,000 in gilti",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COMBINATION: same unconditional precedent as above.
    ("how much california tax do I owe on $80,000 self-employed with $50,000 in gilti single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),

    # --- Excess business loss CARRYOVER absorption (Schedule CA Line 8z)
    # -- verified against the 2025 FTB Instructions for Schedule CA
    # (540): "If in the current year, the taxpayer has enough business
    # income to fully offset all of the excess business loss carryover
    # from prior year, then the carryover balance is applied to offset
    # the business income." Cross-referenced against Line 8p's own text
    # ("any disallowed loss will be treated as a carryover excess
    # business loss INSTEAD OF an NOL carryover") -- unlike NOL
    # carryforward, there's no MTI/suspension multi-year recomputation
    # here, so this is a genuine "trust the stated carryover balance"
    # extension of the already-built Line 8p threshold formula, not new
    # historical-tracking complexity. Two cases modeled: FULL ABSORPTION
    # (this year's business income >= carryover, flat uncapped
    # subtraction) and THIS-YEAR LOSS (combines with the carryover and
    # reapplies the SAME Line 8p threshold formula to the combined
    # figure). The one case NOT modeled -- business income positive but
    # LESS than the carryover (partial absorption) -- routes to a
    # dedicated needs_review message rather than guessing at FTB's
    # unverified Form 3461 PDF worksheet.
    #
    # Dispatcher-ordering note: "excess business loss carryover" contains
    # "excess business loss" as a substring, so this feature's checks had
    # to be inserted BEFORE the pre-existing Line 8p checks (same "move
    # the check earlier" pattern as K-1 capital gain/real-estate-
    # professional) -- without that ordering, Line 8p's own detector
    # would swallow carryover-phrased questions first.
    #
    # Full absorption: $80,000 wages, $600,000 business income this year,
    # $400,000 carryover, single -> AGI $280,000 (600k income - 400k
    # carryover, absorbed in full, uncapped) -> taxable $274,294 ->
    # $21,947.98.
    ("how much california tax do I owe on $80,000 in wages, $600,000 in business income this year, and a $400,000 excess business loss carryover from prior years, single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 21947.98}),
    # same figures, carryover stated first -- order independence.
    ("how much california tax do I owe on a $400,000 excess business loss carryover from prior years, $600,000 in business income this year, and $80,000 in wages, single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 21947.98}),
    # THIS-YEAR LOSS branch: $80,000 wages, $100,000 business loss this
    # year, $400,000 carryover, single -> combined loss $500,000, capped
    # at the $313,000 threshold -> allowed loss $313,000, new carryover
    # $187,000 -> AGI floored at $0 (80,000-313,000) -> taxable $0 ->
    # $0.00.
    ("how much california tax do I owe on $80,000 in wages, a $100,000 business loss this year, and a $400,000 excess business loss carryover from prior years, single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 0.0}),
    # PARTIAL ABSORPTION (business income positive but less than the
    # carryover) -> the one deliberately-unmodeled case, specific
    # needs_review message (not a generic defer, not a guessed number).
    ("how much california tax do I owe on $80,000 in wages, $200,000 in business income this year, and a $400,000 excess business loss carryover from prior years, single",
     {"status": "needs_review", "domain": "income"}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages, $600,000 in business income this year, and a $400,000 excess business loss carryover from prior years",
     {"status": "needs_review", "domain": "income"}),
    # MFJ coverage: $120,000 wages, $700,000 business income this year,
    # $500,000 carryover -> AGI $320,000 -> taxable $308,588 ->
    # $21,575.96.
    ("how much california tax do I owe on $120,000 in wages, $700,000 in business income this year, and a $500,000 excess business loss carryover from prior years, married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 21575.96}),
    # NON-REGRESSION CHECK: a plain excess-business-loss question with NO
    # "carryover" wording must still route to the PRE-EXISTING Line 8p
    # feature unaffected by this dispatcher reordering. $80,000 wages,
    # $700,000 business loss, single -> $313,000 allowed, AGI floored at
    # $0 -> $0.00 (same math as Line 8p's own existing test coverage).
    ("how much california tax do I owe on $80,000 in wages with a $700,000 excess business loss single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 0.0}),

    # --- Phase 2a LLM extraction FALLBACK, promoted to live 2026-08-23
    # with explicit user sign-off (scoped to "fallback only" -- regex
    # stays primary and decides the answer whenever it succeeds; the LLM
    # is called ONLY when regex returns None entirely). Backed by a
    # same-day adversarial test: regex fails outright on phrasing a
    # fixed keyword-anchor approach can't generalize past (reordered
    # facts, an anchor-phrase-to-value disconnect), while the LLM
    # extracted every fact correctly in both cases (hand-verified
    # against the question text before promotion).
    #
    # REORDERED FACTS: other-income stated first, then business result,
    # then carryover, in one flowing sentence with non-adjacent
    # connectors -- regex returns None (used to `needs_review`); the LLM
    # fallback now answers. $80,000 other income, $25,000 business loss
    # this year, $10,000 carryover -> combined loss $35,000 (under the
    # $313k single threshold, fully deductible) -> AGI $45,000, taxable
    # $39,294 -> $935.69.
    ("Single, my other income is $80,000. Even though my business actually lost $25,000 this year, I'm carrying an excess business loss carryover of $10,000 from before that too. How much tax do I owe?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 935.69}),
    # ANCHOR-PHRASE-TO-VALUE DISCONNECT: the trigger phrase "excess
    # business loss carryover" appears with no nearby dollar figure at
    # all; the actual carryover amount is stated much later as "prior
    # carryover balance," different wording than any EBL_CARRYOVER_TERMS
    # anchor -- regex returns None (used to fall through to a bare
    # `informational` non-answer); the LLM fallback now answers. $95,000
    # wages, $40,000 business loss this year, $15,000 carryover ->
    # combined loss $55,000 (fully deductible) -> AGI $55,000, taxable
    # $49,294 -> $735.69.
    ("How much California tax do I owe with an excess business loss carryover? I make $95,000 in wages, and after my business's $40,000 loss this year, I'm told my prior carryover balance is $15,000, filing single.",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 735.69}),

    # --- Foreign income of nonresident aliens -- worldwide-income
    # true-up (Schedule CA Line 8z) -- verified against the 2025 FTB
    # Instructions for Schedule CA (540): "Foreign income of nonresident
    # aliens -- Adjust federal income to reflect worldwide income
    # computed under California law. Enter losses from foreign sources
    # on line 8z, column B. Enter foreign source income on line 8z,
    # column C." A flat, unconditional, two-directional restatement.
    # "Nonresident alien" here is the FEDERAL tax-status term (Form
    # 1040-NR filer), confirmed by its other uses on the same
    # resident-only instructions page -- the population is a full-year
    # CA resident who is also a federal nonresident alien. Requires an
    # explicit self-identification phrase to trigger, since "foreign
    # income" alone is too generic.
    #
    # Addition case: $80,000 wages, $50,000 foreign source income, single
    # -> AGI $130,000 -> taxable $124,294 -> $7,997.98 (same wash shape
    # as the FEIE addback above, opposite legal direction -- FEIE adds
    # back an exclusion federal already gave; this adds back income
    # federal never taxed at all for an NRA filer).
    ("how much california tax do I owe on $80,000 in wages with $50,000 in foreign source income as a nonresident alien single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.98}),
    # same figures, foreign income stated first -- order independence.
    ("how much california tax do I owe on $50,000 in foreign source income as a nonresident alien and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.98}),
    # Subtraction case (LOSS from foreign sources): $80,000 wages,
    # $30,000 foreign source loss, single -> AGI $50,000 -> taxable
    # $44,294 -> $1,192.53.
    ("how much california tax do I owe on $80,000 in wages with a $30,000 foreign source loss as a nonresident alien single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1192.53}),
    # alternate self-ID phrasing ("form 1040-nr" instead of "nonresident
    # alien") -- also exercises the "1040" phantom-amount guard (literal
    # "1040" in "1040-nr" would otherwise parse as a bare number). Same
    # figures as the basic addition case -> $7,997.98.
    ("how much california tax do I owe on $80,000 in wages with $50,000 in foreign income, I file form 1040-nr, single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.98}),
    # MFJ coverage: $120,000 wages, $60,000 foreign source income ->
    # AGI $180,000 -> taxable $168,588 -> $8,555.96.
    ("how much california tax do I owe on $120,000 in wages with $60,000 in foreign source income as a nonresident alien married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 8555.96}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with $50,000 in foreign source income as a nonresident alien",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COMBINATION: unconditional regardless of how the
    # OTHER income was earned, same precedent as FEIE/Subpart F/GILTI.
    ("how much california tax do I owe on $80,000 self-employed with $50,000 in foreign source income as a nonresident alien single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 7997.98}),

    # --- California disaster loss carryover deduction (Schedule CA Line
    # 9b1, FTB Form 3805V) -- verified against the 2025 FTB Instructions
    # for Schedule CA (540): "If you have a California disaster loss
    # carryover deduction and there is income in the current taxable
    # year, enter the total amount of disaster loss carryover deduction
    # from your 2025 form FTB 3805V, Part III, line 2, column (f)..." A
    # flat "copy this cell from your own 3805V" pass-through -- pulls
    # from the SAME Part III cell as Line 9b2's NOL carryover (already
    # built). SIMPLER than NOL carryover, not harder: disaster loss
    # carryovers are explicitly EXEMPT from the 2024-2027 $1M NOL
    # suspension rule regardless of income, so there's no suspended
    # branch here -- deduction = min(carryover, MTI), remainder carries
    # forward. Also broader population than NOL (applies against ANY
    # income, not just business income) -- confirmed via FTB's own line
    # text ("there is income," not "business income"). Original ledger
    # note ("requires declared-disaster-county + loss facts") was
    # stale, same pattern as Line 8a/8d -- the per-item casualty-loss
    # facts (FMV, insurance, floors) were already baked in when the loss
    # ORIGINATED at the separate, still-deferred Part II Line 15; this
    # feature only absorbs the already-computed leftover balance.
    #
    # Full absorption: $80,000 wages, $50,000 carryover, single -> MTI
    # $74,294 -> full $50,000 deductible -> taxable $24,294 -> $375.09.
    ("how much california tax do I owe on $80,000 in wages with a $50,000 disaster loss carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 375.09}),
    # same figures, carryover stated first -- order independence.
    ("how much california tax do I owe on a $50,000 disaster loss carryover and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 375.09}),
    # PARTIAL deduction: carryover ($90,000) exceeds MTI ($74,294) -->
    # deduction capped at MTI, taxable income floored at $0 -> $0.00,
    # remainder ($15,706) continues to carry forward.
    ("how much california tax do I owe on $80,000 in wages with a $90,000 disaster loss carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 0.0}),
    # NOT SUSPENDED even at very high income ($2,000,000 -- well above
    # the $1M NOL-suspension threshold): unlike NOL carryover, disaster
    # loss carryover is exempt from suspension entirely, so the full
    # $50,000 remains fully deductible -> taxable $1,944,294 ->
    # $229,427.72 (includes the $1M+ Behavioral Health Services surtax).
    ("how much california tax do I owe on $2,000,000 in wages with a $50,000 disaster loss carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 229427.72}),
    # MFJ coverage: $120,000 wages, $60,000 carryover -> MTI $108,588 ->
    # full $60,000 deductible -> taxable $48,588 -> $750.18.
    ("how much california tax do I owe on $120,000 in wages with a $60,000 disaster loss carryover married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 750.18}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with a $50,000 disaster loss carryover",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COMBINATION: unconditional regardless of how the
    # OTHER income was earned, same precedent as NOL/FEIE/Subpart F.
    ("how much california tax do I owe on $80,000 self-employed with a $50,000 disaster loss carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 375.09}),
    # NON-REGRESSION CHECK: a plain NOL-carryover question (bare "nol
    # carryover" wording, no "disaster") must still route to the
    # PRE-EXISTING Line 9b2 NOL feature unaffected by this new dispatcher
    # insertion. $80,000 business income, $50,000 NOL carryover, single
    # -> same figures as the disaster-loss case above (coincidentally
    # identical tax, $375.09, since neither is suspended/capped here) --
    # confirms NOL_COMPLEXITY_EXCLUDE's pre-existing "disaster loss"
    # exclusion correctly keeps the two features from colliding in
    # either direction.
    ("how much california tax do I owe on $80,000 in business income with a $50,000 nol carryover single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 375.09}),

    # --- Foreign housing DEDUCTION (Schedule CA Line 24j, Form 2555) --
    # distinct from the foreign earned income/housing EXCLUSION (Line
    # 8d, already built above) -- IRC 911(c)'s housing DEDUCTION is the
    # self-employed counterpart, claimed as an above-the-line federal
    # deduction rather than an exclusion. Verified against the 2025 FTB
    # Instructions for Schedule CA (540): "j. Housing deduction from
    # federal Form 2555 -- If you claimed the foreign housing deduction
    # for federal purposes, enter the amount from column A in column
    # B." Same non-conformity, same NET ADDBACK direction as Line 8d
    # (tracing Section C's column arithmetic through Form 540's own
    # Line 26/27 chain confirms a larger column-B entry here REDUCES
    # the subtraction at the bottom of the form, i.e. RAISES CA taxable
    # income -- the ledger's "subtraction" tag reflects the column
    # label, not the AGI direction). Extends
    # compute_foreign_earned_income_ca_tax with an optional
    # housing_deduction_amount parameter rather than a separate
    # function, since the two commonly co-occur for self-employed
    # expats (911(c) housing deduction available IN ADDITION TO the
    # 911(a) earned-income exclusion).
    #
    # TWO REAL BUGS FOUND AND FIXED before locking in these values:
    # (1) a naive "remove the housing anchor, then search the remainder
    # for the exclusion anchor" approach still double-counted a single
    # housing-deduction figure as BOTH an exclusion and a deduction for
    # "...a $30,000 form 2555 housing deduction..." phrasing, because
    # the bare "form 2555" substring (shared between both term sets)
    # still found the one REMAINING amount within its proximity window
    # even though it was semantically unrelated -- fixed by only
    # searching for an exclusion amount when an EXCLUSION-SPECIFIC term
    # is present (not just bare "form 2555" already claimed by a
    # housing phrase). (2) a genuine sales-domain cross-routing
    # collision -- same class as cannabis 280E/military retirement --
    # where a "both exclusion and housing deduction stated together"
    # question was confidently misrouted to the sales domain for one
    # capitalization but not another (an embedding-space quirk); fixed
    # with an early-intercept guard in _answer() mirroring the existing
    # cannabis-280E/military-retirement precedent.
    #
    # Housing-deduction-only: $80,000 wages, $30,000 housing deduction,
    # single -> AGI $110,000 -> taxable $104,294 -> $6,137.98.
    ("how much california tax do I owe on $80,000 in wages with a $30,000 foreign housing deduction single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 6137.98}),
    # same figures, housing deduction stated first -- order independence.
    ("how much california tax do I owe on a $30,000 foreign housing deduction and $80,000 in wages single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 6137.98}),
    # BOTH exclusion AND housing deduction stated together (the modal
    # self-employed-expat case): $80,000 wages, $50,000 excluded under
    # Form 2555, $30,000 foreign housing deduction, single -> AGI
    # $160,000 -> taxable $154,294 -> $10,787.98. Also the case that
    # exercised the sales-domain routing-collision fix above.
    ("how much california tax do I owe on $80,000 in wages with $50,000 excluded under form 2555 and a $30,000 foreign housing deduction single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 10787.98}),
    # AMBIGUOUS PHRASING regression guard: "form 2555 housing deduction"
    # contains the bare "form 2555" substring shared with the exclusion
    # term set -- must be treated as housing-deduction-ONLY (not
    # double-counted), same result as the housing-only case above.
    ("how much california tax do I owe on $80,000 in wages with a $30,000 form 2555 housing deduction single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 6137.98}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with a $30,000 foreign housing deduction",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COMBINATION: unconditional regardless of how the
    # OTHER income was earned, same precedent as the exclusion/FEIE.
    ("how much california tax do I owe on $80,000 self-employed with a $30,000 foreign housing deduction single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 6137.98}),
    # MFJ coverage: $120,000 wages, $40,000 housing deduction -> AGI
    # $160,000 -> taxable $148,588 -> $6,695.96.
    ("how much california tax do I owe on $120,000 in wages with a $40,000 foreign housing deduction married filing jointly",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 6695.96}),

    # --- Personal/Blind/Senior/Dependent Exemption Credits (Form 540
    # Lines 7-10, Line 32 AGI Limitation Worksheet) -- Income Coverage
    # Blueprint Phase 3's highest-frequency finding: every CA resident
    # filer receives at least the Personal Exemption Credit, a real gap
    # income_nonresident.py's own docstring had already flagged as
    # known-and-disclosed. Dollar figures verified directly against the
    # actual 2025 Form 540 PDF (not secondary aggregators) -- $153/unit
    # for personal/blind/senior (2 units for MFJ/QSS by default, 1
    # otherwise, per Line 7's own "enter 1 or 2" mechanic), $475/
    # dependent. This is a TAX CREDIT (subtracted from computed tax),
    # not a deduction from taxable income -- structurally different
    # from every itemized/standard-deduction feature already built.
    # Built as its OWN standalone opt-in path (not integrated into
    # compute_ca_tax itself), a deliberate scope decision so the ~300
    # already-verified expected values for every OTHER feature in this
    # file stay unaffected -- see income_brackets.py's module note.
    #
    # Basic: $80,000 wages, single, no dependents -> bracket tax $3,347.98
    # (unaffected baseline) minus $153 personal credit -> $3,194.98.
    ("how much california tax do I owe on $80,000 in wages, single, with my exemption credit?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3194.98}),
    # with 2 dependents: $153 personal + 2x$475=$950 dependent -> $1,103
    # total credit -> $3,347.98-$1,103 = $2,244.98.
    ("how much california tax do I owe on $80,000 in wages, single, with my exemption credit and 2 dependents?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2244.98}),
    # SAME figures, dependent count stated BEFORE the income figure --
    # order independence. This specifically exercises the extraction
    # fix: "2" (the dependent count) sits before "$80,000" in the
    # string, so a naive first-amount-wins extraction would have
    # wrongly treated 2 as the income figure.
    ("how much california tax do I owe on $80,000 in wages, with 2 dependents, single, and my exemption credit?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2244.98}),
    # MFJ coverage: 2 personal units by default -> $306 credit.
    # $150,000 wages MFJ -> bracket tax $5,855.14 (std ded $11,412 ->
    # taxable $138,588) minus $306 -> $5,549.14.
    ("how much california tax do I owe on $150,000 in wages, married filing jointly, with my personal exemption credit?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 5549.14}),
    # PHASE-OUT case: $300,000 wages single is above the $252,203
    # threshold -> 20 steps of $2,500 (rounded up) x $6/unit = $120
    # reduction -> personal credit floored at $153-$120=$33 -> bracket
    # tax $23,807.98 minus $33 -> $23,774.98.
    ("how much california tax do I owe on $300,000 in wages, single, with my exemption credit?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 23774.98}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do I owe on $80,000 in wages with my exemption credit?",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COLLISION GUARD: unlike the itemized-deduction-
    # style optional add-ons, this feature mirrors the PLAIN wage-only
    # path's scope exactly and must DEFER on self-employment (reusing
    # COMPLEXITY_EXCLUDE, minus "dependent" only) -- the pre-existing
    # self-employment feature answers normally instead (same disclosed-
    # simplification precedent as every other feature this session:
    # answers without the exemption credit, a small safe-direction
    # overestimate, rather than a wrong computation).
    ("how much california tax do I owe on $80,000 self-employed with my exemption credit, single?",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 2875.42}),

    # --- Estimated Use Tax Lookup Table (Form 540 Line 91) -- Income
    # Coverage Blueprint Phase 3's second-priority finding, and genuinely
    # simpler than the exemption credit above: no filing status at all,
    # just a flat California-AGI-band lookup. Table verified directly
    # against the actual 2025 Form 540 Booklet PDF (all 14 flat-dollar
    # bands plus the >$199,999 -> AGI x 0.0001 formula band). Only
    # covers individual non-business items purchased for LESS than
    # $1,000 each -- anything at/above that (or business purchases)
    # needs the separate Use Tax Worksheet instead, deliberately not
    # modeled, routed to a specific clarifying message rather than
    # silently misapplied.
    #
    # Basic: $80,000 CA AGI -> $8 (the $80,000-$89,999 band).
    ("how much use tax do I owe if my California AGI is $80,000?",
     {"status": "answered", "domain": "income", "category": "estimated_use_tax", "tax": 8.0}),
    # lower band: $25,000 -> $2.
    ("what is my estimated use tax if my California AGI is $25,000?",
     {"status": "answered", "domain": "income", "category": "estimated_use_tax", "tax": 2.0}),
    # top formula band: $300,000 -> 300000 x 0.0001 = $30.
    ("how much use tax do I owe if my California AGI is $300,000?",
     {"status": "answered", "domain": "income", "category": "estimated_use_tax", "tax": 30.0}),
    # same figure, AGI stated before the trigger phrase -- order independence.
    ("my California AGI is $80,000, how much use tax do I owe?",
     {"status": "answered", "domain": "income", "category": "estimated_use_tax", "tax": 8.0}),
    # OVER-CAP case: a specific stated item price ($2,000) isn't caught
    # by phrase-matching alone (no "over $1,000"-style wording) -- this
    # exercises the "two or more dollar figures is ambiguous, don't
    # guess which one is AGI" fix found live during this build.
    ("how much use tax do I owe if my California AGI is $80,000 and I bought a $2,000 TV out of state?",
     {"status": "needs_review", "domain": "income"}),
    # OVER-CAP case: explicit business-purchase phrasing (phrase-matched
    # directly, no second dollar figure needed).
    ("how much use tax do I owe if my California AGI is $80,000 for a business purchase?",
     {"status": "needs_review", "domain": "income"}),
    # PURE INFORMATIONAL (zero dollar figures) must NOT be swept into
    # the ambiguous-defer path -- falls through to the pre-existing
    # sales-side informational answer unaffected, same behavior as
    # before this feature existed.
    ("what is use tax?",
     {"status": "answered", "domain": "sales", "category": "use_tax_on_property_exempt_from_sales_tax_but_used_in_ca"}),

    # --- Other State Tax Credit (Schedule S (540), credit code 187) --
    # Income Coverage Blueprint Phase 3's third build, and the most
    # complex extraction this session: 4 dollar figures (income,
    # double-taxed income, other-state AGI, other-state tax paid) plus
    # filing status. Verified directly against the actual 2025
    # Schedule S PDF -- TWO independent prorations (not one, as an
    # earlier broad survey pass assumed), credit = lesser of the two.
    # CA side: bracket_tax x min(1.0, double_taxed_income/CA_AGI).
    # Other side: other_state_tax_paid x min(1.0, double_taxed_income/other_state_AGI).
    #
    # Basic case, single, other-state side binding: $150,000 CA income,
    # $60,000 double-taxed, $65,000 other-state AGI, $4,000 other-state
    # tax paid -> CA ratio 60000/150000=0.4, CA side $9,857.98*0.4=
    # $3,943.19; other ratio 60000/65000=0.9231 (capped<1.0), other side
    # $4,000*0.9231=$3,692.31 -> credit=min=$3,692.31 -> total tax
    # $9,857.98-$3,692.31=$6,165.67.
    ("how much California tax do I owe with the other state tax credit, if my California income is $150,000, my double-taxed income is $60,000, my other state AGI is $65,000, and my tax paid to the other state is $4,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 6165.67}),
    # SAME figures, fully reordered (anchors stated before the income
    # figure and the trigger question) -- order independence. Also
    # exercises the forward-only extraction fix found live during this
    # build: an earlier undirected-nearest-distance version picked a
    # PRECEDING amount over the one each anchor phrase actually
    # described, since "double-taxed income" sat character-wise closer
    # to the preceding $150,000 than to the $60,000 it was describing.
    ("my tax paid to the other state is $4,000, my other state AGI is $65,000, my double-taxed income is $60,000, how much California tax do I owe with the other state tax credit on $150,000 in California income, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 6165.67}),
    # MFJ coverage, CA side binding instead of the other-state side
    # (same figures as the basic case, different filing status changes
    # which side wins the min()): CA ratio still 0.4, but MFJ's own
    # bracket tax is lower -> CA side ($2,342.06) < other side
    # ($3,692.31) -> credit=$2,342.06 -> total tax $5,855.14-$2,342.06=$3,513.08.
    ("how much California tax do I owe with the other state tax credit, if my California income is $150,000, my double-taxed income is $60,000, my other state AGI is $65,000, and my tax paid to the other state is $4,000, married filing jointly?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3513.08}),
    # DUPLICATE-VALUE COLLISION AUDIT (found live building the
    # Underpayment penalty, then audited back across every "N anchors +
    # 1 remainder" multi-figure feature this session): double-taxed
    # income and other-state AGI are BOTH stated as $60,000 -- filtering
    # an already-matched amount out of the shared list BY VALUE (the
    # original pattern) would strip both occurrences or the wrong one;
    # fixed via _amount_near_filtered_span/_amount_after_filtered_span +
    # _remove_amount_span, which remove by character position instead.
    # ca_side=9857.98*min(1,60000/150000)=3943.19; other_side=4000*
    # min(1,60000/60000)=4000; credit=min(3943.19,4000)=3943.19; tax=
    # 9857.98-3943.19=5914.79.
    ("how much California tax do I owe with the other state tax credit, if my California income is $150,000, my double-taxed income is $60,000, my other state AGI is $60,000, and my tax paid to the other state is $4,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 5914.79}),
    # missing filing status -> specific clarifying message.
    ("how much California tax do I owe with the other state tax credit, if my California income is $150,000, my double-taxed income is $60,000, my other state AGI is $65,000, and my tax paid to the other state is $4,000?",
     {"status": "needs_review", "domain": "income"}),

    # --- Pass-Through Entity (PTE) Elective Tax Credit (FTB 3804-CR,
    # credit code 242) -- Income Coverage Blueprint Phase 3's fourth
    # build. A broad survey pass sketched this as a pure single-number
    # pass-through ("trust the K-1 figure"); a dedicated verification
    # pass found it's the THIRD claim from that same survey to be wrong
    # or incomplete once independently checked -- FTB 3804-CR is a real
    # (if small) worksheet: K-1 credit + optional prior-year carryover,
    # capped at CURRENT-YEAR CA tax liability (nonrefundable), excess
    # carries forward up to 5 years. Built as the CURRENT-YEAR-
    # absorption-only slice, same established pattern as the NOL/EBL/
    # disaster-loss/capital-loss carryovers -- carryforward disclosed,
    # not tracked. "CA tax liability" is computed from stated income via
    # the existing bracket engine, not asked as a separate stated fact
    # -- the 9.3% rate itself is entirely entity-side, confirmed from
    # FTB's own instructions, so the taxpayer's only real input is the
    # already-computed dollar credit off their K-1.
    #
    # Basic: $150,000 CA income single (bracket tax $9,857.98), $5,000
    # PTE credit, fully usable -> $9,857.98-$5,000=$4,857.98.
    ("how much California tax do I owe with a $5,000 PTE credit, if my California income is $150,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 4857.98}),
    # order independence.
    ("I have a $5,000 PTE credit, how much California tax do I owe on $150,000 in California income, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 4857.98}),
    # CREDIT EXCEEDS CURRENT-YEAR TAX LIABILITY: $30,000 CA income single
    # (bracket tax only $375.09) with a $5,000 PTE credit -> capped at
    # $375.09 used, tax floored at $0.00, $4,624.91 disclosed as
    # carrying forward (not tracked).
    ("how much California tax do I owe with a $5,000 PTE credit, if my California income is $30,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 0.0}),
    # WITH PRIOR-YEAR CARRYOVER: $5,000 K-1 credit + $2,000 carryover =
    # $7,000 total available, fully usable against $9,857.98 bracket tax
    # -> $9,857.98-$7,000=$2,857.98. Also exercises the "PTE credit
    # carryover contains PTE credit as a literal substring" extraction
    # case, correctly disambiguated by removing the carryover-claimed
    # amount before searching for the K-1 credit anchor.
    ("how much California tax do I owe with a $5,000 PTE credit and a $2,000 PTE credit carryover from a prior year, if my California income is $150,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 2857.98}),
    # DUPLICATE-VALUE COLLISION AUDIT: carryover and K1 credit both
    # stated as $5,000 -- see the Other State Tax Credit's own
    # duplicate-value test above for the general bug this audits.
    # total_available=10000, credit_used=min(10000,9857.98)=9857.98,
    # tax=max(0,9857.98-9857.98)=0.0.
    ("how much California tax do I owe with a $5,000 PTE credit and a $5,000 PTE credit carryover from a prior year, if my California income is $150,000, single?",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 0.0}),
    # missing filing status -> specific clarifying message.
    ("how much California tax do I owe with a $5,000 PTE credit, if my California income is $150,000?",
     {"status": "needs_review", "domain": "income"}),
    # SELF-EMPLOYMENT COLLISION GUARD: mirrors the exemption credit/OSTC
    # scope exactly (full COMPLEXITY_EXCLUDE) -- the pre-existing
    # self-employment feature answers normally instead, same disclosed-
    # simplification precedent as those two.
    ("how much California tax do I owe on $80,000 self-employed with a $5,000 PTE credit, single?",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 2875.42}),

    # --- Late-filing / late-payment penalties (Form 540 Line 112, R&TC
    # 19131/19132) -- Income Coverage Blueprint Phase 3's fifth build,
    # and architecturally different from every feature above: no filing
    # status, no bracket/income computation at all -- a flat percentage
    # of a stated unpaid balance. A dedicated verification pass found
    # this is genuinely more complex than the broad survey's sketch --
    # missing the payment penalty's own cap/40-month ceiling and
    # entirely missing the REQUIRED offset between the two penalties
    # (late-payment is reduced dollar-for-dollar by late-filing for the
    # same period) -- the FOURTH claim from that same survey to be
    # wrong or incomplete once independently checked.
    #
    # 3 months late, $5,000 balance: late-filing 5%x3=15%->$750;
    # late-payment 5%+0.5%x3=6.5%->$325 (less than filing, so offset
    # zeroes the assessed amount) -> total $750.
    ("what is my late filing penalty if I owe $5,000 and filed 3 months late?",
     {"status": "answered", "domain": "income", "category": "late_filing_payment_penalty", "tax": 750.0}),
    # order independence.
    ("I filed 3 months late, what is my late filing penalty if I owe $5,000?",
     {"status": "answered", "domain": "income", "category": "late_filing_payment_penalty", "tax": 750.0}),
    # PAYMENT-PENALTY-BINDING case (the one branch not exercised by the
    # basic case above): at exactly 1 month late, the payment penalty's
    # 5% flat start (5.5% total) exceeds the filing penalty's 5% -- the
    # offset assesses the $25 excess ON TOP of the $250 filing penalty,
    # rather than zeroing out. Total $250+$25=$275.
    ("what is my late payment penalty if I owe $5,000 and paid 1 month late?",
     {"status": "answered", "domain": "income", "category": "late_filing_payment_penalty", "tax": 275.0}),
    # both penalties at their 25% caps (40+ months late): late-filing
    # caps at 5 months (25%), late-payment caps at 40 months (25%) --
    # both equal, offset zeroes out, total = 25% of balance = $1,250.
    ("what is my late payment penalty if I owe $5,000 and paid 40 months late?",
     {"status": "answered", "domain": "income", "category": "late_filing_payment_penalty", "tax": 1250.0}),
    # FRACTIONAL MONTHS round UP to a full month ("month or fraction
    # thereof") -- 2.5 months late computes identically to 3 months late.
    ("what is my late filing penalty if I owe $5,000 and filed 2.5 months late?",
     {"status": "answered", "domain": "income", "category": "late_filing_payment_penalty", "tax": 750.0}),
    # REASONABLE-CAUSE REDIRECT: a case-by-case FTB determination, not
    # computed here -- dedicated informational message, no dollar figure.
    ("what is my late filing penalty if I have reasonable cause and owe $5,000, filed 3 months late?",
     {"status": "answered", "domain": "income", "category": "late_penalty_reasonable_cause"}),

    # --- California additional tax on early retirement distributions
    # (FTB 3805P Part I, R&TC 17085) -- Income Coverage Blueprint Phase
    # 3's sixth build. A dedicated verification pass confirmed the 2.5%
    # rate (not folklore) but found real complexity the survey's sketch
    # didn't capture: FTB's own text confirms California's exception
    # list does NOT fully match federal's (2 confirmed divergent
    # codes), a 25-code year-versioned exception table, a 6% override
    # for early-SIMPLE-IRA distributions, and different rates entirely
    # for other account types (12.5% Archer MSA, 50% Medicare Advantage
    # MSA). Scoped to the common no-exception Part-I case only --
    # exception-flavored language and non-Part-I account types both
    # route to dedicated clarifying messages rather than a guess.
    #
    # Basic: $20,000 taxable early distribution x 2.5% = $500.
    ("what is my early distribution tax if I took a $20,000 taxable early distribution?",
     {"status": "answered", "domain": "income", "category": "early_distribution_tax", "tax": 500.0}),
    # order independence.
    ("I took a taxable early distribution of $20,000, what is my early distribution tax?",
     {"status": "answered", "domain": "income", "category": "early_distribution_tax", "tax": 500.0}),
    # SIMPLE IRA within first 2 years: 6% override -> $20,000 x 6% = $1,200.
    ("what is my early distribution tax if I took a $20,000 taxable early distribution from my SIMPLE IRA within the first 2 years?",
     {"status": "answered", "domain": "income", "category": "early_distribution_tax", "tax": 1200.0}),
    # EXCEPTION MENTIONED: California's exception list is confirmed to
    # diverge from federal's -- deliberately deferred, not guessed.
    ("what is my early distribution tax if I took a $20,000 taxable early distribution, but I have a disability exception?",
     {"status": "needs_review", "domain": "income"}),
    # NON-PART-I ACCOUNT TYPE (Archer MSA uses a different 12.5% rate) --
    # deliberately deferred, not silently misapplied at 2.5%.
    ("what is my early distribution tax if I took a $20,000 non-qualified Archer MSA distribution?",
     {"status": "needs_review", "domain": "income"}),

    # --- Child and Dependent Care Expenses Credit (FTB 3506, credit code
    # 232) -- Income Coverage Blueprint Phase 3's seventh build. Broad
    # survey framed this as a simple "percentage of federal credit"
    # lookup; dedicated research confirmed Form 3506 is actually a full
    # parallel worksheet keyed on FEDERAL AGI (not CA AGI) that never
    # literally reads a federal credit dollar amount as input. The
    # "federal credit x FTB percentage" shortcut IS mathematically valid,
    # but only for the common case: full-year CA resident, all care
    # provided in California, no employer dependent-care benefits.
    # Scoped to that case; nonresident/part-year/out-of-state-care/
    # employer-benefits language routes to a dedicated clarifying message.
    #
    # Basic: federal AGI $50,000 falls in the 43% bracket ($40k-$70k).
    ("what is my child and dependent care credit if my federal credit is $1,000 and my federal AGI is $50,000?",
     {"status": "answered", "domain": "income", "category": "cdc_credit", "credit": 430.0}),
    # order independence: federal AGI stated before federal credit.
    ("my federal AGI is $50,000, what is my child and dependent care credit if my federal credit is $1,000?",
     {"status": "answered", "domain": "income", "category": "cdc_credit", "credit": 430.0}),
    # lowest bracket: federal AGI <= $40,000 -> 50%.
    ("what is my child and dependent care credit if my federal credit is $1,000 and my federal AGI is $30,000?",
     {"status": "answered", "domain": "income", "category": "cdc_credit", "credit": 500.0}),
    # DISQUALIFIED: federal AGI over $100,000 is a hard cutoff, not a
    # gradually-reduced percentage.
    ("what is my child and dependent care credit if my federal credit is $1,000 and my federal AGI is $150,000?",
     {"status": "answered", "domain": "income", "category": "cdc_credit", "credit": None}),
    # OUT OF SCOPE: nonresident care sourcing genuinely differs from the
    # federal calculation -- deliberately deferred, not guessed.
    ("what is my child and dependent care credit if my federal credit is $1,000 and my federal AGI is $50,000, and I am a nonresident?",
     {"status": "needs_review", "domain": "income"}),
    # PHANTOM-AMOUNT GUARD: "form 3506" must not be misparsed as a $3,506
    # dollar figure by the shared amount-extraction regex.
    ("what is my child and dependent care credit under form 3506 if my federal credit is $1,000 and my federal AGI is $50,000?",
     {"status": "answered", "domain": "income", "category": "cdc_credit", "credit": 430.0}),
    # DUPLICATE-VALUE COLLISION AUDIT: federal credit and federal AGI
    # both stated as $40,000 -- see the Other State Tax Credit's own
    # duplicate-value test for the general bug this audits. $40,000 AGI
    # falls in the <=$40k bracket -> 50% -> credit=40000*0.50=20000.
    ("what is my child and dependent care credit if my federal credit is $40,000 and my federal AGI is $40,000?",
     {"status": "answered", "domain": "income", "category": "cdc_credit", "credit": 20000.0}),

    # --- Child Adoption Costs Credit (Form 540 Credit Chart code 197) --
    # Income Coverage Blueprint Phase 3's eighth build. Broad survey/
    # ledger note had the core formula right (50% of costs, $2,500/child
    # cap, CA-public-agency-custody restriction) but was missing the
    # real nonrefundable-capped-at-CA-tax-liability-with-carryover
    # mechanic (same shape as the PTE credit, unlike the CDC credit's
    # simpler standalone formula) plus a second eligibility gate (child
    # must also be a US citizen/legal resident, assumed satisfied here
    # rather than elicited as its own fact).
    #
    # Basic: $60,000 CA income single (std deduction $5,706, taxable
    # $54,294, bracket tax $1,792.53) with a $3,000 adoption cost ($1,500
    # credit, fully absorbed) -> $1,792.53-$1,500=$292.53. Phrasing
    # deliberately avoids restating the $3,000 figure a second time (a
    # decorative "with a $3,000 adoption credit" lead-in, present in an
    # earlier version of this test, accidentally created a genuine
    # 3-figure question once duplicate-value removal became position-
    # based instead of value-based -- see the duplicate-value-collision
    # audit note elsewhere in this file).
    ("how much California tax do I owe with my child adoption credit if I paid $3,000 in adoption costs, adopted through the county foster care system, my income is $60,000, filing single?",
     {"status": "answered", "domain": "income", "category": "adoption_credit", "tax": 292.53}),
    # order independence.
    ("my income is $60,000, filing single, i adopted through the county foster care system, how much california tax do i owe with my child adoption credit if i paid $3,000 in adoption costs?",
     {"status": "answered", "domain": "income", "category": "adoption_credit", "tax": 292.53}),
    # CREDIT EXCEEDS CURRENT-YEAR TAX LIABILITY: $8,000 costs -> $2,500
    # credit (capped at the per-child max), but $15,000 CA income single
    # only has $92.94 of bracket tax -> credit capped at $92.94 used,
    # tax floored at $0.00, $2,407.06 disclosed as carrying forward.
    ("how much california tax do i owe with my child adoption credit if i paid $8,000 in adoption costs, adopted through the county foster care system, my income is $15,000, filing single?",
     {"status": "answered", "domain": "income", "category": "adoption_credit", "tax": 0.0}),
    # DUPLICATE-VALUE COLLISION AUDIT: qualifying costs and income both
    # stated as $60,000 -- see the Other State Tax Credit's own
    # duplicate-value test for the general bug this audits.
    # credit_available=min(60000*0.5,2500)=2500 (capped), bracket_tax at
    # $60k single = $1,792.53, credit_used=min(2500,1792.53)=1792.53,
    # tax=max(0,1792.53-1792.53)=0.0.
    ("how much California tax do I owe with my child adoption credit if I paid $60,000 in adoption costs, adopted through the county foster care system, my income is $60,000, filing single?",
     {"status": "answered", "domain": "income", "category": "adoption_credit", "tax": 0.0}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do i owe with my child adoption credit if i paid $3,000 in adoption costs, adopted through the county foster care system, my income is $60,000?",
     {"status": "needs_review", "domain": "income"}),
    # OUT OF SCOPE: private adoption -- FTB's own text confirms this
    # credit does not apply outside CA-public-agency custody.
    ("how much california tax do i owe with my child adoption credit if i paid $3,000 in adoption costs, this was a private adoption, my income is $60,000, filing single?",
     {"status": "needs_review", "domain": "income"}),
    # OUT OF SCOPE: international adoption.
    ("how much california tax do i owe with my child adoption credit if i paid $3,000 in international adoption costs, my income is $60,000, filing single?",
     {"status": "needs_review", "domain": "income"}),
    # AMBIGUOUS ELIGIBILITY: adoption-credit vocabulary present but
    # neither a public-agency signal nor an out-of-scope term -- routes
    # to a dedicated eligibility question rather than guessing.
    ("how much california tax do i owe with my child adoption credit if i paid $3,000 in adoption costs, my income is $60,000, filing single?",
     {"status": "needs_review", "domain": "income"}),

    # --- College Access Tax Credit (Form 540 Credit Chart code 235, FTB
    # 3592) -- Income Coverage Blueprint Phase 3's ninth build. Ledger
    # note's core claim (50% of contribution, for 2025) checked out true,
    # but the rate is year-keyed (not a permanent constant) and the
    # credit is nonrefundable with a SIX-year carryover (not five, unlike
    # the PTE credit) capped at CA tax liability -- same cap-at-liability
    # pattern as PTE/adoption credit, not a pure pass-through.
    #
    # Basic: $150,000 CA income single (bracket tax $9,857.98), $5,000
    # contribution -> $2,500 credit, fully usable -> $9,857.98-$2,500=$7,357.98.
    ("how much California tax do I owe with a $5,000 college access tax credit contribution, if my California income is $150,000, single?",
     {"status": "answered", "domain": "income", "category": "catc_credit", "tax": 7357.98}),
    # order independence.
    ("my california income is $150,000, single, how much california tax do i owe with a $5,000 college access tax credit contribution?",
     {"status": "answered", "domain": "income", "category": "catc_credit", "tax": 7357.98}),
    # CREDIT EXCEEDS CURRENT-YEAR TAX LIABILITY: $30,000 CA income single
    # (bracket tax only $375.09) with a $5,000 contribution ($2,500
    # credit) -> capped at $375.09 used, tax floored at $0.00, remaining
    # $2,124.91 disclosed as carrying forward (not tracked).
    ("how much california tax do i owe with a $5,000 college access tax credit contribution, if my california income is $30,000, single?",
     {"status": "answered", "domain": "income", "category": "catc_credit", "tax": 0.0}),
    # DUPLICATE-VALUE COLLISION AUDIT: contribution and income both
    # stated as $150,000 -- see the Other State Tax Credit's own
    # duplicate-value test for the general bug this audits.
    # credit_available=150000*0.5=75000, bracket_tax=$9,857.98,
    # credit_used=min(75000,9857.98)=9857.98, tax=max(0,9857.98-9857.98)=0.0.
    ("how much California tax do I owe with a $150,000 college access tax credit contribution, if my California income is $150,000, single?",
     {"status": "answered", "domain": "income", "category": "catc_credit", "tax": 0.0}),
    # missing filing status -> specific clarifying message.
    ("how much california tax do i owe with a $5,000 college access tax credit contribution, if my california income is $150,000?",
     {"status": "needs_review", "domain": "income"}),
    # PHANTOM-AMOUNT GUARD: "form 3592" must not be misparsed as a $3,592
    # dollar figure by the shared amount-extraction regex.
    ("how much california tax do i owe with a $5,000 college access tax credit (form 3592) contribution, if my california income is $150,000, single?",
     {"status": "answered", "domain": "income", "category": "catc_credit", "tax": 7357.98}),

    # --- Individual Shared Responsibility (ISR) Penalty (Form 540 Line
    # 92, FTB 3853) -- Income Coverage Blueprint Phase 3's tenth build,
    # and the FIRST item this session where the ledger's own "too
    # complex, same class as AMT" verdict was found WRONG (not just
    # incomplete) -- a dedicated research pass found the full formula
    # published as a self-contained, linear worksheet in FTB's own text,
    # with a genuinely tractable common-case slice: uninsured the entire
    # year, no exemption claimed, nobody turning 18 during the year.
    #
    # Basic: MFJ, 2 adults, 0 children, $100,000 household income.
    # Filing threshold (0 dependents) = $36,711 -> household income
    # exceeds it. Flat = min(950*2 + 475*0, 2850) = $1,900. Pct income =
    # 2.5% * (100000-36711) = $1,582.23. Base = max(1900, 1582.23) =
    # $1,900. Avg premium cap (2 people) = 377*12*2 = $9,048. Penalty =
    # min(1900, 9048) = $1,900.
    ("what is my individual shared responsibility penalty if I have 2 adults and 0 children in my household, uninsured all year, my household income is $100,000, married filing jointly?",
     {"status": "answered", "domain": "income", "category": "isr_penalty", "tax": 1900.0}),
    # order independence.
    ("married filing jointly, my household income is $100,000, uninsured all year, what is my individual shared responsibility penalty with 2 adults and 0 children in my household?",
     {"status": "answered", "domain": "income", "category": "isr_penalty", "tax": 1900.0}),
    # EXEMPT BELOW FILING THRESHOLD: MFJ, 2 adults, 2 children (dependent
    # bucket "2 or more") -> threshold $64,419; $50,000 income is at or
    # below it -> the ENTIRE penalty is $0, not just reduced.
    ("what is my individual shared responsibility penalty if I have 2 adults and 2 children in my household, uninsured all year, my household income is $50,000, married filing jointly?",
     {"status": "answered", "domain": "income", "category": "isr_penalty", "tax": 0.0}),
    # single, "no children" phrasing (word-form zero, not a digit).
    # Threshold (0 dep) = $18,353; flat = $950; pct = 2.5%*(40000-18353)
    # = $541.18; base = max(950, 541.18) = $950; premium cap (1 person)
    # = $4,524 -> penalty = min(950, 4524) = $950.
    ("what is my individual shared responsibility penalty if I have 1 adult and no children in my household, uninsured all year, my household income is $40,000, single?",
     {"status": "answered", "domain": "income", "category": "isr_penalty", "tax": 950.0}),
    # missing filing status -> specific clarifying message.
    ("what is my individual shared responsibility penalty if I have 2 adults and 0 children in my household, uninsured all year, my household income is $100,000?",
     {"status": "needs_review", "domain": "income"}),
    # OUT OF SCOPE: hardship exemption mentioned -- case-by-case FTB
    # determination, not computed.
    ("what is my individual shared responsibility penalty if I have 2 adults and 0 children in my household, uninsured all year, my household income is $100,000, married filing jointly, but I have a hardship exemption?",
     {"status": "needs_review", "domain": "income"}),
    # AMBIGUOUS COVERAGE: ISR-penalty vocabulary present but no full-year
    # confirmation and no exclusion term -- routes to a dedicated
    # clarifying question rather than assuming full-year uninsured.
    ("what is my individual shared responsibility penalty if I have 2 adults and 0 children in my household, my household income is $100,000, married filing jointly?",
     {"status": "needs_review", "domain": "income"}),

    # --- California AMT "screen" (Schedule P (540), Form 540 Line 61) --
    # Income Coverage Blueprint Phase 3's eleventh build. NOT a general
    # AMT computation (the ~11-preference-category general case correctly
    # stays deferred) -- a scoped "does AMT apply to you at all" check
    # for the narrow population already this codebase's baseline case:
    # standard deduction, wage-only income, zero preference items. For
    # that population, AMTI collapses to CA AGI (no addback modeling
    # needed) and California's flat 7.0% TMT rate + large exemption mean
    # TMT never exceeds regular tax at any realistic income level --
    # verified via dedicated research checking multiple income points
    # from the exemption threshold through $2M+ across the bracket
    # engine's own already-verified data. Built as a REAL formula
    # computation (not a hard-coded "always $0"), so it self-verifies.
    #
    # $100,000 single: AMTI=$100,000, exemption=$92,749 (no phase-out yet),
    # TMT=7%*(100000-92749)=$507.57, regular tax=$5,207.98 -> TMT well
    # below regular tax -> $0 AMT owed.
    ("do I owe california amt if my income is $100,000, single?",
     {"status": "answered", "domain": "income", "category": "amt_screen", "tax": 0.0}),
    # order independence.
    ("single, my income is $100,000, do i owe california amt?",
     {"status": "answered", "domain": "income", "category": "amt_screen", "tax": 0.0}),
    # HIGH INCOME still $0 -- confirms this isn't just true near the
    # exemption threshold; $2,000,000 single: TMT=$140,000, regular tax
    # (incl. Behavioral Health Services surtax)=$236,077.72 -> still $0.
    ("do i owe california amt if my income is $2,000,000, single?",
     {"status": "answered", "domain": "income", "category": "amt_screen", "tax": 0.0}),
    # missing filing status -> specific clarifying message.
    ("do i owe california amt if my income is $100,000?",
     {"status": "needs_review", "domain": "income"}),
    # OUT OF SCOPE: incentive stock options mentioned -- genuinely needs
    # the full ~11-category AMTI build, not this scoped screen.
    ("do i owe california amt if my income is $100,000, single, and i exercised incentive stock options?",
     {"status": "needs_review", "domain": "income"}),

    # --- Underpayment of Estimated Tax Penalty, SHORT METHOD ONLY (Form
    # 540 Line 113, FTB Form 5805 Side 2 Part II) -- Income Coverage
    # Blueprint Phase 3's twelfth build, a THIRD consecutive case of a
    # "too complex" ledger verdict found overly conservative -- but a
    # split finding: the REGULAR method's per-diem/changing-rate
    # mechanism correctly stays deferred (same disqualifying complexity
    # already excluded from the late-payment penalty's interest), but
    # FTB's SHORT METHOD collapses to one flat annual constant
    # (.05028767 for 2025) for the common "withholding-only, no
    # estimated payments made" population. Two genuine extraction bugs
    # found live: (1) the undirected _amount_near_filtered picked a
    # PRECEDING figure over the correct one (fixed with the established
    # _amount_after_filtered forward-only pattern from OSTC); (2) two
    # DIFFERENT stated facts sharing the same dollar value (e.g. prior-
    # year tax and withholding both $15,000) broke value-based removal
    # from the amounts list -- fixed with a NEW position-aware variant,
    # _amount_after_filtered_span, that removes the exact matched tuple
    # by its own character span rather than by value or list order.
    #
    # Basic: $150,000 CA income single (current-year tax $9,857.98),
    # $5,000 prior-year tax, $100,000 prior-year AGI (under the $150k
    # threshold -> 100% test), $2,000 withholding. Required annual
    # payment = min(90%*9857.98=8872.18, 100%*5000=5000) = $5,000.
    # Underpayment = 5000-2000=$3,000. Penalty = 3000*.05028767=$150.86.
    ("do I owe underpayment penalty if my income is $150,000, filing single, my prior year tax was $5,000, my prior year agi was $100,000, and my california withholding was $2,000?",
     {"status": "answered", "domain": "income", "category": "underpayment_penalty", "tax": 150.86}),
    # order independence.
    ("filing single, my california withholding was $2,000, my prior year agi was $100,000, my prior year tax was $5,000, do I owe underpayment penalty if my income is $150,000?",
     {"status": "answered", "domain": "income", "category": "underpayment_penalty", "tax": 150.86}),
    # SAFE HARBOR MET (100% test, prior AGI under threshold): $300,000
    # income single (current-year tax $23,807.98), $15,000 prior-year
    # tax, $80,000 prior-year AGI (under threshold -> 100%), $15,000
    # withholding. Required payment = min(21427.18, 15000) = $15,000,
    # exactly met by withholding -> $0.
    ("do I owe underpayment penalty if my income is $300,000, filing single, my prior year tax was $15,000, my prior year agi was $80,000, and my california withholding was $15,000?",
     {"status": "answered", "domain": "income", "category": "underpayment_penalty", "tax": 0.0}),
    # 110% TEST BINDING (prior AGI OVER the $150k threshold): same
    # figures as above except prior-year AGI $200,000 -> 110% test ->
    # required payment = min(21427.18, 16500) = $16,500. Underpayment =
    # 16500-15000=$1,500. Penalty = 1500*.05028767=$75.43. Deliberately
    # differs from the case above ONLY by the prior-year-AGI threshold
    # test -- the regression signal that would catch the threshold
    # check silently not being applied.
    ("do I owe underpayment penalty if my income is $300,000, filing single, my prior year tax was $15,000, my prior year agi was $200,000, and my california withholding was $15,000?",
     {"status": "answered", "domain": "income", "category": "underpayment_penalty", "tax": 75.43}),
    # FORCED 90%-ONLY (current-year AGI >= $1,000,000): the lesser-of
    # test is bypassed entirely regardless of how favorable the prior-
    # year test would otherwise be.
    ("do I owe underpayment penalty if my income is $1,500,000, filing single, my prior year tax was $5,000, my prior year agi was $50,000, and my california withholding was $10,000?",
     {"status": "answered", "domain": "income", "category": "underpayment_penalty", "tax": 7172.02}),
    # DE MINIMIS SAFE HARBOR, duplicate-value extraction case: prior-year
    # tax AND withholding are both stated as $300 -- the exact figure
    # collision that exposed the position-aware-removal bug.
    ("do I owe underpayment penalty if my income is $20,000, filing single, my prior year tax was $300, my prior year agi was $15,000, and my california withholding was $300?",
     {"status": "answered", "domain": "income", "category": "underpayment_penalty", "tax": 0.0}),
    # missing filing status -> specific clarifying message.
    ("do I owe underpayment penalty if my income is $150,000, my prior year tax was $5,000, my prior year agi was $100,000, and my california withholding was $2,000?",
     {"status": "needs_review", "domain": "income"}),
    # OUT OF SCOPE: estimated payments mentioned -- eligibility for the
    # short method then depends on exact payment dates, a timing
    # question this slice deliberately avoids.
    ("do I owe underpayment penalty if my income is $150,000, filing single, my prior year tax was $5,000, my prior year agi was $100,000, my california withholding was $2,000, and I also made estimated payments?",
     {"status": "needs_review", "domain": "income"}),

    # --- deliberate defers: complexity disqualifiers (never guess) ---
    ("what is my tax bracket if I make $80,000",   # no filing status given
     {"status": "needs_review"}),
    # found via real usage: a clear compute-shaped question missing only
    # filing status must get a SPECIFIC clarifying message (domain=income),
    # not the generic sales-side needs_review text (domain=sales) -- domain
    # is the regression signal that would catch this falling through again
    ("how much tax to pay for income of 100000 in california",
     {"status": "needs_review", "domain": "income"}),
    # was a deliberate defer until the investment-income compute path
    # landed this pass; now genuinely computed (verified by hand: $200k
    # single, $5,706 std deduction -> $194,294 taxable; Sched X 72724-
    # 371479, base 3201.97, 9.3%: 3201.97 + 0.093*(194294-72724) = $14,507.98).
    ("how much tax do I owe on $200,000 in capital gains single filer",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket",
      "tax": 14507.98}),

    # --- ADVERSARIAL HUNT ROUND 1 (2026-08-08): systematic audit of every
    # trigger/exclude term set for the same "no stemming" gap class that
    # caused the freelancing/contracting bug -- this time checked
    # proactively rather than found by accident, then verified empirically
    # against the live engine (never assumed from static reasoning alone).
    # Found 8 real term-matching gaps + 2 filing-status detection gaps, all
    # fixed in income_brackets.py/income_credits.py before being locked in
    # here. Two of the gaps (contracted, salaried) were the DANGEROUS class
    # -- silently dropping income or computing on the wrong dollar figure,
    # not just failing to compute -- confirmed by hand before fixing.
    #
    # "contracted" (past tense) didn't match "contractor"/"contracting" as
    # a substring -- before the fix, this silently DROPPED the $30,000 and
    # computed on $50,000 salary alone. Now correctly mixed-wage-SE
    # (matches the already-verified $50k+$30k MFJ figure exactly).
    ("how much california tax do I owe on $50,000 in salary and $30,000 contracted married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 1386.60}),
    # bare "contractor" (no "independent" prefix) wasn't in SE_TRIGGERS at
    # all -- would have safely deferred (not dangerous) but missed a
    # legitimate compute opportunity. $50k single -> matches the existing
    # freelancing/contracting-single figure exactly.
    ("how much tax do I owe on $50,000 as a contractor single",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 994.39}),
    # "salaried" didn't match wage/salary/w-2/w2 -- before the fix this let
    # the SE-ONLY path fire and compute using the FIRST dollar figure in
    # the sentence ($50,000, the salary) as if it were the SOLE
    # self-employment net profit, a garbled wrong answer using the wrong
    # number entirely. Now correctly mixed-wage-SE.
    ("how much tax do I owe on $50,000 salaried plus $30,000 freelancing married filing jointly",
     {"status": "answered", "domain": "income", "category": "self_employment_income_tax", "tax": 1386.60}),
    # "itemizing" (gerund) didn't match itemize/itemized -- before the fix,
    # stating an intent to itemize with NO dollar amount silently computed
    # using the STANDARD deduction instead, contradicting what the user
    # said. Now correctly defers (can't compute without the itemized total).
    ("I made $80,000 and I am itemizing this year, how much california tax do I owe filing single",
     {"status": "needs_review"}),
    # "renting"/"rented" didn't match "rental" -- before the fix this
    # SILENTLY DROPPED the $20,000 rental income and computed on the
    # $80,000 wage figure alone (the same dangerous class as the
    # freelancing/contracting bug). Now correctly defers (rental income
    # stays out of scope per the earlier CA-depreciation-conformity
    # research).
    ("how much california tax do I owe on $80,000 in wages and $20,000 from renting out my property filing single",
     {"status": "needs_review"}),
    ("how much california tax do I owe on $80,000 in wages and $15,000 that was rented out last year single",
     {"status": "needs_review"}),
    # "gambled"/"betting" didn't match "gambling" -- before the fix these
    # were silently computed via the plain wage-only path (numerically
    # coincidentally correct, since CA has no special rate for gambling
    # winnings either, but mislabeled and bypassing the deliberate
    # gambling exclude policy). Now correctly routes to the structured
    # gambling_winnings topic verdict instead.
    ("how much california tax do I owe on $60,000 including money I gambled and won single",
     {"status": "answered", "domain": "income", "category": "gambling_winnings", "taxable": True}),
    # status flipped answered -> conditional (2026-08-10): same branch-
    # disclosure fix as the gambling/lottery cases above -- "winnings from
    # betting" shares the word "winnings" with california_lottery_winnings'
    # own text closely enough to disclose the CA-lottery exception, a
    # genuinely useful catch (the asker never said which kind of winnings).
    ("how much california tax do I owe on $60,000 in winnings from betting single",
     {"status": "conditional", "domain": "income", "category": "gambling_winnings", "taxable": True}),
    # CREDIT_COMPLEXITY_EXCLUDE had NO freelance/gig-work terms at all --
    # before the fix, CalEITC computed on self-employment income as if it
    # were simple wage-only earned income, skipping FTB 3514's required
    # self-employment worksheet entirely (a real, undisclosed assumption
    # gap). Now correctly defers to the informational tier.
    ("what is my CalEITC if I freelance and made $9,975 with 2 qualifying children",
     {"status": "informational", "domain": "income"}),
    # rental income isn't earned income for CalEITC purposes at all --
    # before the fix this computed a specific dollar credit on rental
    # income as if it were earned income, a confidently wrong amount.
    ("what is my CalEITC if I make $9,975 from renting out property with 2 qualifying children",
     {"status": "informational", "domain": "income"}),
    # filing-status abbreviations (MFJ/MFS) previously required the word
    # "married" to ALSO be spelled out, even though the abbreviation
    # already encodes it -- "filing MFS" alone fell through to a generic
    # missing-filing-status defer despite the user having stated one.
    ("how much tax do I owe on $60,000 filing MFS",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 1792.53}),
    ("how much tax do I owe on $60,000 filing MFJ",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 750.18}),
    # hyphenated "head-of-household" didn't match the spaced phrase --
    # same missing-filing-status false defer despite a stated status.
    ("how much tax do I owe on $60,000 as head-of-household",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 750.03}),
    #
    # --- boundary-value checks (no bugs found -- confirms existing logic
    # already matches FTB's exact wording at the edges) ---
    # CalEITC investment income exactly AT $4,814 must NOT disqualify (FTB
    # says "more than $4,814"); $0.01 over must disqualify.
    ("what is my CalEITC if I make $9,975 with 2 qualifying children and $4,814 in investment income",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 3288.00}),
    ("what is my CalEITC if I make $9,975 with 2 qualifying children and $4,814.01 in investment income",
     {"status": "answered", "domain": "income", "category": "caleitc", "tax": 0.00}),
    # capital loss exactly AT the $3,000/$1,500-MFS limit is fully
    # deductible (matches the already-verified over-limit figures exactly,
    # since only the first $3,000/$1,500 is ever deductible either way).
    ("how much california tax do I owe on $80,000 in wages with $3,000 in capital losses filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3087.57}),
    ("how much california tax do I owe on $80,000 in wages with $1,500 in capital losses married filing separately",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3208.48}),

    # --- Schedule CA Tier 1 conformity expansion (2026-08-11, same session,
    # user said "yes start it" to the scoping plan) -- 5 new topics/rules,
    # verified against FTB's 2025 Schedule CA (540) instructions. See
    # schedule_ca_inventory.py for the full ~90-item research inventory and
    # which tier each belongs to. ---
    ("do I have to pay california tax on my state income tax refund",
     {"status": "answered", "domain": "income", "category": "state_tax_refund", "taxable": False}),
    ("is cancellation of my mortgage debt on my house taxable in california",
     {"status": "answered", "domain": "income", "category": "mortgage_forgiveness_debt_relief", "taxable": True}),
    ("can I deduct classroom supplies I bought as a teacher on my california taxes",
     {"status": "answered", "domain": "income", "category": "educator_expenses", "taxable": True}),
    ("I received a wildfire disaster settlement payment, is that taxable in california",
     {"status": "answered", "domain": "income", "category": "wildfire_disaster_settlement_exclusion", "taxable": False}),
    ("I got money from the kincade fire pge settlement, is that taxable in california",
     {"status": "answered", "domain": "income", "category": "wildfire_disaster_settlement_exclusion", "taxable": False}),
    # Military retirement -- an AGI eligibility cliff, not a flat exclusion:
    # under the $125k/$250k limit -> excluded; over it -> fully taxable.
    ("my AGI is $100,000, filing single, is my military retirement pay taxable in California",
     {"status": "answered", "domain": "income", "category": "military_retirement_exclusion", "taxable": False}),
    ("my AGI is $150,000, filing single, is my military retirement pay taxable in California",
     {"status": "answered", "domain": "income", "category": "military_retirement_exclusion", "taxable": True}),
    ("my AGI is $260,000, married filing jointly, is my DoD survivor benefit plan annuity taxable in California",
     {"status": "answered", "domain": "income", "category": "military_retirement_exclusion", "taxable": True}),
    # bare question, no AGI stated -- falls through to the informational
    # topic (taxable=None, conditional prose), not a guessed verdict.
    ("is my military retirement taxable in California",
     {"status": "answered", "domain": "income", "category": "military_retirement_exclusion", "taxable": None}),

    # --- Ring 3, Phases 1-2: nonresident tax (Form 540NR), the first
    # genuinely new compute engine built this session (2026-08-11) --
    # unlike Schedule CA Tier 1/2, this reuses income_brackets.
    # compute_ca_tax() for the "tax on total income" step but is a
    # different apportionment mechanism, for a population never handled
    # before. Verified against FTB's 2025 Form 540NR booklet + Schedule CA
    # (540NR) instructions directly. Phase 1 scope: full-year nonresident,
    # wage-only, 100%-or-0% CA-source only. Phase 2 (added 2026-08-13)
    # extends the same formula/compute function to any stated partial
    # CA-source dollar figure -- see income_nonresident.py's docstring. ---
    # 100% CA-source: a nonresident who worked entirely in CA owes
    # IDENTICAL tax to a resident with the same wages (hand-verified
    # algebraic identity, and confirmed live: matches the plain resident
    # $80k/single case exactly, tax=3347.98).
    ("I am a nonresident of California who worked entirely in California, how much CA tax do I owe on $80,000 in wages filing single",
     {"status": "answered", "domain": "income", "category": "nonresident_wage_tax", "tax": 3347.98}),
    # 0% CA-source: no CA tax owed on wages earned entirely outside CA.
    ("I am a nonresident of California and did not work in California at all, how much CA tax do I owe on $80,000 in wages filing single",
     {"status": "answered", "domain": "income", "category": "nonresident_wage_tax", "tax": 0.0}),
    # Phase 2: a stated PARTIAL CA-source dollar figure. Hand-derived:
    # $100k total wages, single standard deduction $5,706 -> total taxable
    # $94,294 -> tax_on_total $5,207.98 -> effective rate 5.5231...% ->
    # $30k CA-source, prorated deduction $1,711.80 -> CA taxable $28,288.20
    # -> ca_tax = $1,562.39 (verified via direct compute-function call too).
    ("I am a nonresident of California. I earned $100,000 in wages filing single, $30,000 of which was earned working in California. How much CA tax do I owe?",
     {"status": "answered", "domain": "income", "category": "nonresident_wage_tax", "tax": 1562.39}),
    # Phase 2, different anchor phrasing for the same mechanic.
    ("I am a nonresident of California. $45,000 in california-source wages, out of $90,000 total wages, filing single. How much CA tax do I owe?",
     {"status": "answered", "domain": "income", "category": "nonresident_wage_tax", "tax": 2138.99}),
    # Phase 2 safety: a nonsensical split (stated CA-source exceeds total
    # wages) must defer, not silently clamp or crash.
    ("I am a nonresident of California. I earned $50,000 in wages filing single, $80,000 of which was earned working in California. How much CA tax do I owe?",
     {"status": "needs_review", "domain": "income"}),
    # nonresident signal present but CA-source fraction/amount ambiguous --
    # correctly defers with a specific clarifying message rather than
    # guessing.
    ("I am a nonresident of California, how much CA tax do I owe on $80,000 in wages filing single",
     {"status": "needs_review", "domain": "income"}),
    # missing filing status -- correctly defers (mirrors every other
    # compute path's missing-filing-status pattern).
    ("I am a nonresident of California who worked entirely in California, how much CA tax do I owe on $80,000 in wages",
     {"status": "needs_review", "domain": "income"}),
    # regression check: an ordinary resident wage question (no nonresident
    # signal at all) must be completely unaffected -- same $80k/single
    # case, same answer as the plain wage-only path always gave.
    ("how much california tax do I owe on $80,000 in wages filing single",
     {"status": "answered", "domain": "income", "category": "ca_income_tax_bracket", "tax": 3347.98}),

    # --- Ring 3, Phase 3 (added 2026-08-13, same session): part-year
    # residents. Verified against FTB Pub 1100 -- SAME formula as the
    # full-year-nonresident case above (confirmed: Form 540NR/Schedule CA
    # (540NR) are shared by both populations), only the MEANING of the
    # stated CA-source figure changes (resident-period income in full,
    # plus only the CA-source share of nonresident-period income). Reuses
    # income_nonresident.compute_nonresident_wage_tax completely unchanged
    # -- zero new math, only new detection/wording. No ALL/NONE phrase
    # shortcut for this population (doesn't reduce cleanly, see
    # income_nonresident.py docstring), so a stated dollar figure is
    # always required. ---
    # stated CA-source figure: hand-derived via direct compute_nonresident_
    # wage_tax(conn, 90000, 60000, 'single') call = 2851.99.
    ("I was a part-year resident of California. I earned $90,000 in wages for the year, $60,000 of which was California-source. How much CA tax do I owe filing single?",
     {"status": "answered", "domain": "income", "category": "part_year_resident_wage_tax", "tax": 2851.99}),
    # missing CA-source figure (only total wages stated) -- defers with the
    # part-year-specific clarifying message, not the full-year one.
    ("I was a part-year resident of California filing single. I earned $90,000 in wages for the year. How much CA tax do I owe?",
     {"status": "needs_review", "domain": "income"}),
    # invalid split (CA-source exceeds total wages) -- must defer via the
    # fallback catch-all, not fall through to the generic RESIDENT bracket
    # path (the exact bug class the Phase 2 fallback fix was built for).
    ("I was a part-year resident of California filing single. I earned $50,000 in wages for the year, $80,000 of which was California-source. How much CA tax do I owe?",
     {"status": "needs_review", "domain": "income"}),
    # ALL/NONE phrase shortcut deliberately NOT honored for part-year
    # residents -- correctly defers rather than misapplying the full-year
    # nonresident shortcut.
    ("I was a part-year resident of California filing single. I worked entirely in California. How much CA tax do I owe on $80,000 in wages?",
     {"status": "needs_review", "domain": "income"}),
    # missing filing status -- correctly defers (mirrors every other
    # compute path's missing-filing-status pattern).
    ("I was a part-year resident of California. I earned $90,000 in wages for the year, $60,000 of which was California-source. How much CA tax do I owe?",
     {"status": "needs_review", "domain": "income"}),

    # --- Ring 3, business entities Phase A (added 2026-08-13, same
    # session): ENTITY-LEVEL California annual/minimum tax for S-corps,
    # LLCs, and partnerships -- the first feature that taxes the ENTITY
    # itself rather than an individual's personal return. All figures
    # hand-verified against FTB's business-entity pages -- see
    # load_entity_tax_data.py. Phase B (K-1 pass-through to the owner's
    # personal return) is a deliberately separate, not-yet-built feature.
    # ---
    # LLC: $800 annual tax + tiered fee. $300k -> $900 fee tier -> $1,700 total.
    ("how much franchise tax does my LLC owe on $300,000 in California income",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 1700.0}),
    # S-corp: $800 + 1.5% of net income. $200k net -> $3,000 income tax + $800 = $3,800.
    ("how much tax does my S-corp owe on $200,000 in net California income",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 3800.0}),
    # financial S-corp: 3.5% rate, not 1.5%. $200k net -> $7,000 + $800 = $7,800.
    ("how much tax does my financial S corporation owe on $200,000 in net California income",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 7800.0}),
    # LP: flat $800 regardless of income, no income figure needed at all.
    ("how much annual tax does my limited partnership owe",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 800.0}),
    # LLP: same flat $800 as LP.
    ("what tax does my limited liability partnership owe",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 800.0}),
    # general partnership: $0, the one entity type that owes nothing.
    ("how much tax does my general partnership owe",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 0.0}),
    # S-corp first-year waiver: $800 floor waived (permanent 2020+ rule),
    # but the 1.5% income tax itself still applies -- $200k net -> $3,000
    # only, not $3,800.
    ("how much tax does my newly formed S-corp owe on $200,000 in net California income this first year",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 3000.0}),
    # LLC first-year: NO waiver (AB 85 expired for 2024+) -- must still be
    # the full $800 + fee, not silently waived like the S-corp case above.
    ("how much franchise tax does my newly formed LLC owe on $300,000 in California income this first year",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 1700.0}),
    # LLC missing income figure -- defers, doesn't guess a fee tier.
    ("how much franchise tax does my LLC owe",
     {"status": "needs_review", "domain": "income"}),
    # ambiguous bare "partnership" -- general owes $0, LP/LLP owe $800, so
    # this must not guess between them.
    ("how much tax does my partnership owe",
     {"status": "needs_review", "domain": "income"}),

    # C-corp (added 2026-08-13, same session, "knock out C-corp first"):
    # bare "corporation" defaults to C-corp (S-corp is the marked/elected
    # case, already checked first). $200k net -> 8.84% = $17,680 + $800 =
    # $18,480.
    ("how much tax does my corporation owe on $200,000 in net California income",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 18480.0}),
    # financial corporation: 10.84% rate, not 8.84%. $200k net -> $21,680 + $800 = $22,480.
    ("how much tax does my financial corporation owe on $200,000 in net California income",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 22480.0}),
    # C-corp first-year: waiver applies here too (re-verified this session
    # as a GENERAL corporation rule, not S-corp-specific) -- $800 floor
    # waived, 8.84% income tax still applies -> $17,680 only, not $18,480.
    ("how much tax does my newly formed corporation owe on $200,000 in net California income this first year",
     {"status": "answered", "domain": "income", "category": "entity_annual_tax", "tax": 17680.0}),

    # --- Ring 3, business entities Phase B (added 2026-08-13, same
    # session): K-1 pass-through income to the INDIVIDUAL owner's personal
    # return. Confirmed via FTB research: K-1 income flows through the
    # EXACT SAME standard-deduction/bracket engine already built for
    # wages (Schedule CA (540) Line 5, no special rate) -- zero new tax
    # math, only new detection/wording. K-1-only scope (no wage mixing
    # yet). ---
    # K-1-only: $80,000 K-1 income, single -- must match the plain resident
    # $80k/single wage-tax figure exactly, since the underlying math is
    # identical (verified via direct compute_k1_ca_tax/compute_ca_tax
    # cross-check before this case was added).
    ("I received a K-1 from my partnership showing $80,000 in income, filing single. How much tax do I owe?",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_income_tax", "tax": 3347.98}),
    # THE CRITICAL COLLISION CASE: mentions BOTH an entity type ("S-corp")
    # and K-1 language -- must be answered as the INDIVIDUAL's personal
    # tax on $50,000 of pass-through income (category
    # k1_pass_through_income_tax), NEVER as what the S-CORP ENTITY itself
    # owes (which would incorrectly compute $800+1.5%*50000=$1,550 under
    # category entity_annual_tax) -- this is the exact bug class the
    # K1_EXCLUDE_TERMS defense-in-depth fix in entity_tax.py + this path's
    # early placement in _answer_income() were built to prevent.
    ("I received a K-1 from my S-corp showing $50,000 in income, filing single. How much tax do I owe?",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_income_tax", "tax": 1192.53}),
    # missing filing status -- correctly defers with the K-1-specific message.
    ("I received a K-1 from my LLC showing $80,000 in income. How much tax do I owe?",
     {"status": "needs_review", "domain": "income"}),
    # missing dollar amount -- correctly defers via the fallback catch-all,
    # not via entity_tax (confirmed entity_tax never fires on K-1 language).
    ("I received a K-1 from my S-corp. How much tax do I owe filing single?",
     {"status": "needs_review", "domain": "income"}),
    # mixed wage + K-1 income -- genuinely more complex than this pass's
    # scope, correctly defers rather than guessing which figure is which.
    ("I have $50,000 in wages and received a K-1 showing $30,000 in income, filing single. How much tax do I owe?",
     {"status": "needs_review", "domain": "income"}),

    # --- Trust/estate income Phase B (added 2026-08-13, same session):
    # trust/estate K-1s extend the SAME K-1 path above -- confirmed via
    # FTB research that trust/estate K-1 income lands on the identical
    # Schedule CA (540) Line 5, so no new compute engine was needed, just
    # broader trigger vocabulary + a tax-exempt-interest disclosure. ---
    # trust K-1: must match the business K-1 $80k/single figure exactly,
    # since the underlying math is identical.
    ("I received a K-1 from my trust showing $80,000 in income, filing single. How much tax do I owe?",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_income_tax", "tax": 3347.98}),
    # estate K-1.
    ("I received a K-1 from an estate showing $50,000 in income, filing single. How much tax do I owe?",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_income_tax", "tax": 1192.53}),
    # GRANTOR trust -- must redirect (income taxed directly to the grantor
    # via FTB's simplified reporting, not a real K-1), never compute a
    # number under this path.
    ("I received a K-1 from my grantor trust showing $80,000 in income, filing single. How much tax do I owe?",
     {"status": "needs_review", "domain": "income"}),

    # --- Trust/estate Phase A (added 2026-08-13, same session):
    # FIDUCIARY-level tax on RETAINED (undistributed) trust/estate income
    # -- what the trust/estate ITSELF owes, genuinely different from
    # Phase B's beneficiary K-1 tax. Confirmed via FTB research: reuses
    # income_brackets.compute_ca_tax's Schedule X bracket step UNCHANGED
    # (California's 541 rate schedule is numerically identical to
    # individual Single/MFS), only new reference data is the small
    # exemption credit ($1 trust / $10 estate / $144 qualified disability
    # trust), subtracted as a CREDIT after the bracket computation. ---
    # trust: hand-verified via direct compute_fiduciary_tax(conn, 50000,
    # 'trust') call = 1533.89 (tax_before_credit 1534.89 minus $1 credit).
    ("How much tax does my trust owe on $50,000 of retained income? All trustees are California residents.",
     {"status": "answered", "domain": "income", "category": "fiduciary_tax", "tax": 1533.89}),
    # estate: same $50k retained income, $10 credit instead of $1 ->
    # 1524.89, confirming the two entity types differ by exactly $9.
    ("How much tax does my estate owe on $50,000 of retained income? All trustees are California residents.",
     {"status": "answered", "domain": "income", "category": "fiduciary_tax", "tax": 1524.89}),
    # full distribution shortcut: $0 fiduciary tax, no residency assertion
    # or amount needed at all -- the distribution deduction offsets all
    # taxable income, beneficiary is taxed instead via K-1 (Phase B).
    ("My trust distributed all of its income to beneficiaries, does it owe any California tax?",
     {"status": "answered", "domain": "income", "category": "fiduciary_tax", "tax": 0.0}),
    # GRANTOR trust -- must redirect here too (reuses the same
    # GRANTOR_TRUST_TERMS constant as Phase B's K-1 redirect), never
    # compute a fiduciary-level number for a disregarded entity.
    ("How much tax does my grantor trust owe on $50,000 of retained income? All trustees are California residents.",
     {"status": "needs_review", "domain": "income"}),
    # missing CA-residency bail-out assertion -- correctly defers rather
    # than assuming the trust qualifies.
    ("How much tax does my trust owe on $50,000 of retained income?",
     {"status": "needs_review", "domain": "income"}),
    # missing retained-income amount -- correctly defers.
    ("How much tax does my trust owe? All trustees are California residents.",
     {"status": "needs_review", "domain": "income"}),
    # K-1 defense-in-depth regression: a K-1 question mentioning a trust
    # must NEVER be answered by the fiduciary-tax path (it would compute
    # what the TRUST owes, not what the BENEFICIARY owes) -- confirmed
    # this still routes to k1_pass_through_income_tax, not fiduciary_tax.
    ("I received a K-1 from my trust showing $50,000 in income, how much tax do I owe filing single?",
     {"status": "answered", "domain": "income", "category": "k1_pass_through_income_tax", "tax": 1192.53}),

    # --- genuinely out of scope (neither domain covers it) ---
    ("what is the property tax rate in los angeles", {"status": "needs_review"}),
    ("how do I register my car with the dmv", {"status": "needs_review"}),

    # --- cross-domain safety: sales-tax questions must NOT be captured by income ---
    ("is furniture taxable in california",
     {"status": "answered", "domain": "sales", "taxable": True}),
    ("is cannabis taxable in california", {"domain": "sales"}),
    ("is bread taxable in california", {"domain": "sales", "taxable": False}),
]


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def _check(result, expected):
    for k, want in expected.items():
        got = result.get(k)
        if k == "tax":
            if got is None or abs(float(got) - want) > TOL:
                return False, k
        elif got != want:
            return False, k
    return True, None


def run():
    cache = _load()
    graded = 0
    for q, exp in ITEMS:
        if q in cache:
            continue
        try:
            r = engine.answer(q, compose=False, source="income_item_sweep")
        except Exception as e:
            print(f"STOP after {graded}: {str(e)[:120]}")
            break
        ok, fail_key = _check(r, exp)
        cache[q] = {
            "expected": exp, "ok": ok, "fail_key": fail_key,
            "status": r.get("status"), "domain": r.get("domain"),
            "category": r.get("category"), "taxable": r.get("taxable"), "tax": r.get("tax"),
        }
        graded += 1
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] {q[:70]:70} -> status={r.get('status')} domain={r.get('domain')} "
              f"category={r.get('category')} taxable={r.get('taxable')} tax={r.get('tax')}")
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
    print(f"\n===== INCOME ITEM SWEEP ({len(cache)} items) =====")
    print(f"correct : {len(ok)}")
    print(f"WRONG   : {len(bad)}")
    if bad:
        print("\n--- WRONG (fix these) ---")
        for q, v in bad:
            print(f"  {q}")
            print(f"    expected={v['expected']}  got status={v['status']} domain={v['domain']} "
                  f"category={v['category']} taxable={v['taxable']} tax={v['tax']} "
                  f"(mismatch on: {v['fail_key']})")


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
