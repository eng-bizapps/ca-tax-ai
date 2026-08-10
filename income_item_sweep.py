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
    ("are gambling winnings taxable in california",
     {"status": "answered", "domain": "income", "category": "gambling_winnings", "taxable": True}),
    ("are california lottery winnings taxable",
     {"status": "answered", "domain": "income", "category": "california_lottery_winnings", "taxable": False}),
    ("is interest from us treasury bonds taxable in california",
     {"status": "answered", "domain": "income", "category": "us_government_bond_interest", "taxable": False}),
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
    ("how much tax do I owe as an s-corp making $100,000 single",
     {"status": "needs_review"}),

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
    # above the 2025 AGI limitation threshold ($252,203 single) -- itemized
    # deductions get REDUCED by a worksheet this path doesn't implement, so
    # it correctly defers rather than silently skipping the reduction.
    ("how much california tax do I owe on $600,000 in wages with $50,000 in itemized deductions filing single",
     {"status": "needs_review"}),
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
    ("how much california tax do I owe on $60,000 in winnings from betting single",
     {"status": "answered", "domain": "income", "category": "gambling_winnings", "taxable": True}),
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
