"""Ring 2 / Phase 3 -- bounded first content slice for the income-tax domain.

ALL figures below are hand-verified against primary FTB sources fetched
directly this session (NOT LLM-drafted, status='verified'):
  - 2025 California Tax Rate Schedules (Schedule X/Y/Z):
    https://www.ftb.ca.gov/forms/2025/2025-540-tax-rate-schedules.pdf
    base_amount/rate/floor/ceiling copied VERBATIM from the "Enter on Form
    540, line 31" columns -- not re-derived by our own segment summation, so
    there is no independent rounding risk vs the official PDF. Cross-checked
    against FTB's own worked example (Chris & Pat Smith, MFJ, $125,000
    taxable income -> $4,768.10): our compute_ca_tax() must reproduce this
    exactly (see income_item_sweep.py).
  - Behavioral Health Services Tax (the current, 2025 name for what used to
    be called the Mental Health Services Tax 1% surtax -- renamed under
    Prop 1): https://www.ftb.ca.gov/forms/2025/2025-540-instructions.html,
    "Line 62": 1% of (taxable income - $1,000,000), computed the SAME way
    regardless of filing status (confirmed: the instructions reference only
    "Form 540, line 19", no filing-status branching) -- this is the concrete
    non-doubling-for-joint-filers fact the Phase-0 plan review flagged.
  - 2025 standard deduction: https://www.ftb.ca.gov/file/personal/deductions/index.html
    Single/MFS $5,706; MFJ/HOH/QSS $11,412 (note HOH shares the higher tier
    with MFJ, it is NOT a third distinct amount).
  - Conformity topics (unemployment, Social Security): both fetched directly
    from their FTB income-types pages -- both are wholly EXCLUDED from CA
    taxable income (subtraction adjustment on Schedule CA (540)), i.e.
    taxable=False categorically, not case-dependent.
  - CalEITC "2025 Earned Income Tax Credit Table" (FTB 3514 Booklet,
    https://www.ftb.ca.gov/forms/2025/2025-3514-booklet.pdf, pages 22-30):
    UNLIKE the tax brackets, this credit has NO published smooth formula --
    the table itself IS FTB's official source of truth. Extracted with
    pdfplumber (structured text extraction, not eyeballed from the rendered
    page images) into caleitc_table_2025.json: 658 rows ($1-$32,900 in
    exact $50 increments, 0 gaps, 0 duplicates, verified) x 4 qualifying-
    child columns. Spot-checked 3 rows against the visually-read PDF pages
    (exact match) and confirmed the table's own maximum credit ($3,756 for
    3+ children) matches FTB's independently-published headline number on
    their CalEITC overview page.
  - Young Child Tax Credit (YCTC) formula, from FTB Form 3514 Part VII: a
    FLAT $1,189 (2025) if CA earned income <= $27,425, phasing out above
    that at a rate of $21.71 per $100 of excess (computed via the form's
    OWN two-step rounding: divide excess by 100 and round to 2 decimals,
    THEN multiply by $21.71 and round to 2 decimals -- replicated exactly,
    not approximated as excess*0.2171 in one step, to match FTB's own
    arithmetic to the penny). Completely phases out at $32,900 (same
    ceiling as CalEITC itself).

DELIBERATE DESIGN CHOICE (documented per the Phase 2/3 plan's explicit ask):
we replicate FTB's RATE SCHEDULE formula for ALL income levels, not the
separate "Tax Table" FTB directs filers with taxable income <= $100,000 to
use instead. The Tax Table buckets income into ~$50 ranges and taxes the
BUCKET MIDPOINT, so it differs from the exact formula by at most a few
dollars for those filers. The Rate Schedule formula is the exact source of
truth the Tax Table is itself derived from (confirmed by reproducing FTB's
own >$100k worked example exactly) -- replicating the formula uniformly
keeps this system's tax math exact and auditable rather than approximated by
a lookup-bucket step whose own rounding behavior we would have to encode.

Usage:
  python load_income_content.py load             # insert all Phase 3 content (idempotent)
  python load_income_content.py load_eitc_table   # insert the 658-row CalEITC table (idempotent)
  python load_income_content.py embed             # embed income_tax_topics into income_rule_embeddings
  python load_income_content.py status
"""
import json
import os
import sys
import time

import google.generativeai as genai

import config
import income_db as db

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))

TAX_YEAR = 2025
RATE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-tax-rate-schedules.pdf"
RATE_CITATION = "FTB 2025 California Tax Rate Schedules"
INSTR_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-instructions.html"
DEDUCTION_URL = "https://www.ftb.ca.gov/file/personal/deductions/index.html"
EITC_TABLE_PATH = os.path.join(os.path.dirname(__file__), "caleitc_table_2025.json")
EITC_TABLE_CITATION = "FTB 2025 Earned Income Tax Credit Table (FTB 3514 Booklet)"
EITC_BOOKLET_URL = "https://www.ftb.ca.gov/forms/2025/2025-3514-booklet.html"
YCTC_CITATION = "FTB 2025 Form 3514, Part VII (Young Child Tax Credit)"
FYTC_CITATION = "FTB 2025 Form 3514, Step 10 / Part IX (Foster Youth Tax Credit)"
RENTERS_CREDIT_CITATION = "FTB Nonrefundable Renter's Credit"
RENTERS_CREDIT_URL = "https://www.ftb.ca.gov/file/personal/credits/nonrefundable-renters-credit.html"
# (max_amount, income_ceiling) by filing status -- flat amount below the
# ceiling, $0 above it (a hard cutoff, NOT a gradual phase-out like YCTC).
RENTERS_CREDIT = {
    "single": (60, 53994), "mfs": (60, 53994),
    "mfj": (120, 107987), "hoh": (120, 107987), "qss": (120, 107987),
}

# (floor, ceiling, base_amount, rate) -- verbatim from Schedule X/Y/Z.
SCHEDULE_X = [   # Single, or Married/RDP Filing Separately
    (0, 11079, 0.00, 0.01),
    (11079, 26264, 110.79, 0.02),
    (26264, 41452, 414.49, 0.04),
    (41452, 57542, 1022.01, 0.06),
    (57542, 72724, 1987.41, 0.08),
    (72724, 371479, 3201.97, 0.093),
    (371479, 445771, 30986.19, 0.103),
    (445771, 742953, 38638.27, 0.113),
    (742953, None, 72219.84, 0.123),
]
SCHEDULE_Y = [   # Married/RDP Filing Jointly, or Qualifying Surviving Spouse/RDP
    (0, 22158, 0.00, 0.01),
    (22158, 52528, 221.58, 0.02),
    (52528, 82904, 828.98, 0.04),
    (82904, 115084, 2044.02, 0.06),
    (115084, 145448, 3974.82, 0.08),
    (145448, 742958, 6403.94, 0.093),
    (742958, 891542, 61972.37, 0.103),
    (891542, 1485906, 77276.52, 0.113),
    (1485906, None, 144439.65, 0.123),
]
SCHEDULE_Z = [   # Head of Household
    (0, 22173, 0.00, 0.01),
    (22173, 52530, 221.73, 0.02),
    (52530, 67716, 828.87, 0.04),
    (67716, 83805, 1436.31, 0.06),
    (83805, 98990, 2401.65, 0.08),
    (98990, 505208, 3616.45, 0.093),
    (505208, 606251, 41394.72, 0.103),
    (606251, 1010417, 51802.15, 0.113),
    (1010417, None, 97472.91, 0.123),
]
# Schedule X covers single & MFS identically; Schedule Y covers MFJ & QSS
# identically; Schedule Z (HOH) is genuinely its own, distinct table -- NOT
# the same numbers as Schedule Y despite HOH sharing Y's standard-deduction
# tier. Storing all 5 filing statuses explicitly (rather than a schedule-group
# indirection) keeps every row directly traceable to its FTB citation.
SCHEDULES = {
    "single": SCHEDULE_X, "mfs": SCHEDULE_X,
    "mfj": SCHEDULE_Y, "qss": SCHEDULE_Y,
    "hoh": SCHEDULE_Z,
}

STANDARD_DEDUCTION = {
    "single": 5706, "mfs": 5706,
    "mfj": 11412, "hoh": 11412, "qss": 11412,
}

INCOME_TOPICS = [
    {
        "topic_key": "unemployment_compensation",
        "topic_label": "Unemployment compensation",
        "schedule_ca_line": "Pt I Sec B line 7",
        "taxable": False,
        "summary": ("Unemployment compensation is nontaxable for California state "
                     "purposes (though it IS federally taxable) -- make a subtraction "
                     "adjustment on the unemployment compensation line, column B, of "
                     "Schedule CA (540)."),
        "citation": "FTB Schedule CA (540) Instructions -- Unemployment",
        "source_url": "https://www.ftb.ca.gov/file/personal/income-types/unemployment.html",
    },
    {
        "topic_key": "social_security_income",
        "topic_label": "Social Security income",
        "schedule_ca_line": "Pt I Sec A line 6",
        "taxable": False,
        "summary": ("California excludes Social Security income (and equivalent "
                     "Railroad Retirement benefits) from state taxable income entirely "
                     "-- subtract any amount included in federal AGI on Schedule CA (540), "
                     "regardless of how much of it the IRS taxes federally."),
        "citation": "FTB Schedule CA (540) Instructions -- Social Security",
        "source_url": "https://www.ftb.ca.gov/file/personal/income-types/social-security.html",
    },
    {
        "topic_key": "paid_family_leave",
        "topic_label": "Paid Family Leave (PFL) benefits",
        "schedule_ca_line": "Pt I Sec B line 7",
        "taxable": False,
        "summary": ("California Paid Family Leave (PFL) benefits are nontaxable for "
                     "California state purposes (though included in federal AGI) -- "
                     "make a subtraction adjustment on the unemployment compensation "
                     "line, column B, of Schedule CA (540), the same mechanism used "
                     "for unemployment compensation."),
        "citation": "FTB Schedule CA (540) Instructions -- Paid Family Leave",
        "source_url": "https://www.ftb.ca.gov/file/personal/income-types/paid-family-leave.html",
    },
    {
        "topic_key": "gifts_and_inheritance",
        "topic_label": "Gifts and inheritance",
        "taxable": False,
        "summary": ("A gift or inheritance itself is not taxable income when you "
                     "receive it, for both federal and California purposes -- however, "
                     "if the gift or inheritance later PRODUCES income (e.g. interest "
                     "on inherited cash, dividends, rental income), that subsequently-"
                     "earned income IS taxable, even though the original gift/"
                     "inheritance is not."),
        "citation": "FTB Personal Income Types -- Gifts and Inheritance",
        "source_url": "https://www.ftb.ca.gov/file/personal/income-types/gifts-and-inheritance.html",
    },
    # --- second conformity batch (Phase 3 extend, verified against FTB pages
    # fetched directly this session via browser -- WebFetch was 403'd by
    # ftb.ca.gov, browser navigation was not) ---
    {
        "topic_key": "gambling_winnings",
        "topic_label": "Gambling and lottery winnings (general)",
        "schedule_ca_line": "Pt I Sec B line 8b",
        "taxable": True,
        "summary": ("All gambling winnings are taxable in California, including "
                     "winnings from raffles, horse races, casinos, and lotteries run "
                     "by OTHER states -- this matches federal treatment. The only "
                     "carve-out is winnings from the California Lottery itself "
                     "(SuperLotto, Powerball, Mega Millions played through the CA "
                     "Lottery), which California does not tax -- see the California "
                     "Lottery winnings topic for that narrower exception."),
        "citation": "FTB Personal Income Types -- Gambling",
        "source_url": "https://www.ftb.ca.gov/file/personal/income-types/gambling.html",
    },
    {
        "topic_key": "california_lottery_winnings",
        "topic_label": "California Lottery winnings",
        "schedule_ca_line": "Pt I Sec B line 8b",
        "taxable": False,
        "summary": ("California does not tax winnings from the California Lottery, "
                     "including SuperLotto, Powerball, and Mega Millions when played "
                     "through the CA Lottery -- even though these winnings ARE "
                     "federally taxable. This exclusion is specific to the California "
                     "Lottery: winnings from another state's lottery are NOT excluded "
                     "and remain taxable by California."),
        "citation": "FTB Personal Income Types -- Gambling (California Lottery)",
        "source_url": "https://www.ftb.ca.gov/file/personal/income-types/gambling.html",
    },
    {
        "topic_key": "us_government_bond_interest",
        "topic_label": "Interest on U.S. government obligations",
        "schedule_ca_line": "Pt I Sec A line 2",
        "taxable": False,
        "summary": ("Interest from U.S. Treasury bills, notes, and bonds, U.S. "
                     "savings bonds, and other direct obligations of the United "
                     "States government is excluded from California taxable income "
                     "-- even though it IS federally taxable -- via a subtraction "
                     "adjustment on Schedule CA (540). This does NOT extend to "
                     "Fannie Mae, Ginnie Mae, or Freddie Mac securities, which remain "
                     "fully taxable by California despite being government-related."),
        "citation": "FTB 2025 Instructions for Schedule CA (540) -- Line 2b, Taxable Interest",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
    {
        "topic_key": "out_of_state_municipal_bond_interest",
        "topic_label": "Interest on out-of-state municipal bonds",
        "schedule_ca_line": "Pt I Sec A line 2",
        "taxable": True,
        "summary": ("Unlike federal law, which exempts interest from municipal "
                     "bonds issued by ANY state, California taxes interest from "
                     "municipal bonds issued by states OTHER than California (and "
                     "their political subdivisions) -- add this interest back to "
                     "income on Schedule CA (540). Interest from California "
                     "municipal bonds themselves remains exempt from California tax."),
        "citation": "FTB 2025 Instructions for Schedule CA (540) -- Line 2a, Tax-Exempt Interest",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
    {
        "topic_key": "hsa_contributions_and_earnings",
        "topic_label": "Health Savings Account (HSA) contributions and earnings",
        "schedule_ca_line": "Pt I Sec A line 1h/2, Sec B line 8e/8f, Sec C line 13 (cluster)",
        "taxable": True,
        "summary": ("California does not conform to federal law on Health Savings "
                     "Accounts (HSAs): contributions are NOT deductible for "
                     "California purposes, and interest or other earnings inside the "
                     "HSA are taxable in the year earned rather than tax-deferred as "
                     "they are federally. Distributions used for qualified medical "
                     "expenses remain tax-free for California, matching federal "
                     "treatment."),
        "citation": "FTB 2025 Instructions for Form 3805P; FTB 2025 Instructions for Schedule CA (540)",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-3805p-instructions.html",
    },
    # --- Head of Household: DELIBERATELY informational-only (taxable=None,
    # not a yes/no verdict). Genuine HOH eligibility depends on facts a
    # single question can't establish (exactly who lived with you and for
    # how long, who paid what share of household costs) -- the same
    # multi-turn problem that had this deferred earlier in Phase 3. This
    # topic states the official criteria and explicitly disclaims
    # determining any reader's own eligibility, rather than guessing.
    {
        "topic_key": "head_of_household_eligibility",
        "topic_label": "Head of Household filing status eligibility",
        "taxable": None,
        "summary": ("To file as Head of Household (HOH) in California you must "
                     "meet ALL of the following as of December 31 of the tax year: "
                     "(1) you were unmarried, considered unmarried, or not in a "
                     "registered domestic partnership; (2) you have a qualifying "
                     "child or relative; (3) that qualifying person lived with you "
                     "for more than 183 days during the year; (4) you paid more "
                     "than half the cost of maintaining the home; and (5) you were "
                     "a U.S. citizen or legal resident for the entire year. This "
                     "states the official requirements only -- it does not "
                     "determine whether YOUR specific situation qualifies, since "
                     "that depends on facts this assistant can't verify from a "
                     "single question. You must also file FTB Form 3532 (Head of "
                     "Household Filing Status Schedule) with your return; see FTB "
                     "Publication 1540 for full details."),
        "citation": "FTB Filing Status -- Head of Household",
        "source_url": "https://www.ftb.ca.gov/file/personal/filing-status/head-of-household.html",
    },
    # --- workers' comp / SDI: the 2 topics deferred earlier this session for
    # lacking a clean citable page (ftb.ca.gov has no dedicated adjustment
    # page for either -- they're excluded from FEDERAL AGI in the first
    # place under IRC 104(a)(1), so there's never a Schedule CA adjustment
    # LINE for them the way there is for unemployment/PFL). Resolved by going
    # to the actual STATUTE instead of an FTB summary page -- verified
    # directly against leginfo.legislature.ca.gov, the official California
    # Legislative Information site (a stronger primary source than an FTB
    # webpage, not a weaker one).
    {
        "topic_key": "workers_compensation",
        "topic_label": "Workers' compensation benefits",
        "taxable": False,
        "summary": ("Workers' compensation benefits (temporary disability, permanent "
                     "disability, and death benefits paid for a work-related injury or "
                     "illness) are excluded from both federal and California taxable "
                     "income. California Revenue and Taxation Code Section 17131 "
                     "incorporates the federal exclusion for injury/sickness "
                     "compensation (Internal Revenue Code Section 104(a)(1)) -- "
                     "California simply follows federal treatment here, which is why "
                     "there is no separate Schedule CA adjustment line for it."),
        "citation": "California R&TC Section 17131 (incorporating IRC Section 104(a)(1))",
        "source_url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=17131.&lawCode=RTC",
    },
    {
        "topic_key": "state_disability_insurance",
        "topic_label": "California State Disability Insurance (SDI) benefits",
        "taxable": False,
        "summary": ("California State Disability Insurance (SDI) benefits are not "
                     "taxable for California purposes. Even in the one case where SDI "
                     "IS taxable federally -- when paid as a substitute for "
                     "unemployment insurance benefits, under Internal Revenue Code "
                     "Section 85 -- California does not tax it, because California "
                     "Revenue and Taxation Code Section 17083 rejects IRC Section 85 "
                     "entirely for California purposes (the same statute that makes "
                     "ordinary unemployment compensation nontaxable in California)."),
        "citation": "California R&TC Section 17083 (California does not adopt IRC Section 85)",
        "source_url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=17083.&lawCode=RTC",
    },
    # A genuine trap, caught by checking the ACTUAL current FTB text rather
    # than trusting secondary sources (which described only the ORIGINAL
    # TCJA non-conformity and missed a subsequent change): this is NOT a
    # simple boolean -- it depends on WHEN the divorce/separation agreement
    # was executed, across THREE windows, verified directly against FTB's
    # 2025 Schedule CA (540) instructions, Line 2a "Alimony Received":
    #   (1) On or before 12/31/2018: pre-TCJA: taxable to recipient /
    #       deductible to payer under BOTH federal and CA (they never
    #       diverged for these).
    #   (2) After 12/31/2018 through 12/31/2025 (the "gap" window):
    #       federal (TCJA) says NOT taxable to recipient / NOT deductible
    #       to payer -- but California does NOT conform and kept the OLD
    #       rule: STILL taxable to recipient / STILL deductible to payer,
    #       needing a California-specific Schedule CA addition/subtraction.
    #   (3) After 12/31/2025 (today's date is 2026-08-08 -- this window is
    #       NOW the current one for any new agreement): FTB's own text --
    #       "California treatment is the same as federal and no adjustment
    #       is needed" -- California has since conformed going forward.
    # taxable=None deliberately (like head_of_household_eligibility) -- a
    # single verdict would be actively wrong for 2 of the 3 windows;
    # the summary states all three explicitly rather than picking one.
    {
        "topic_key": "alimony_spousal_support",
        "topic_label": "Alimony / spousal support",
        "schedule_ca_line": "Pt I Sec B line 2a / Sec C line 19a",
        "taxable": None,
        "summary": ("Whether alimony (spousal support) is taxable in California depends "
                     "on WHEN the divorce or separation agreement was executed -- it is "
                     "not the same answer in every case. (1) Agreements executed on or "
                     "before December 31, 2018: taxable to the recipient and deductible "
                     "by the payer, matching federal law. (2) Agreements executed after "
                     "December 31, 2018 through December 31, 2025: California does NOT "
                     "conform to the federal TCJA change -- alimony is STILL taxable to "
                     "the recipient and STILL deductible by the payer for California "
                     "purposes, even though it is federally tax-free for this window (a "
                     "California-specific Schedule CA adjustment is required). (3) "
                     "Agreements executed after December 31, 2025: California has since "
                     "conformed to federal law -- alimony is NOT taxable to the recipient "
                     "and NOT deductible by the payer, same as federal, no adjustment "
                     "needed. Please specify when the agreement was executed for an exact "
                     "answer."),
        "citation": "FTB 2025 Instructions for Schedule CA (540) -- Line 2a, Alimony Received",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
    # --- Schedule CA Tier 1 conformity expansion (2026-08-11, same session,
    # user said "yes start it" to the scoping plan) -- 5 new topics chosen
    # from a full ~90-item inventory (see schedule_ca_inventory.py) for
    # being both common/high-value AND answerable as a single topic verdict
    # with no new compute engine, unlike the deferred IRA-basis/CFC-GILTI/
    # business-owner items. All figures verified directly against FTB's
    # 2025 Schedule CA (540) instructions (full ~104K-char document fetched
    # via browser and read in its entirety by a research agent, not
    # sampled/summarized secondhand).
    {
        "topic_key": "state_tax_refund",
        "topic_label": "State income tax refund",
        "schedule_ca_line": "Pt I Sec B line 1",
        "taxable": False,
        "summary": ("California does not tax a refund of state income tax, even if "
                     "you included it in federal income because you itemized "
                     "deductions in the year you paid it (the federal 'tax benefit "
                     "rule'). Subtract the refund amount on Schedule CA (540), Part I, "
                     "Section B, line 1, column B. This only applies to state income "
                     "tax refunds -- it does not cover other refunds or rebates."),
        "citation": "FTB 2025 Instructions for Schedule CA (540) -- Line 1, Taxable Refunds",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
    {
        "topic_key": "mortgage_forgiveness_debt_relief",
        "topic_label": "Mortgage forgiveness / cancellation of debt on a principal residence",
        "schedule_ca_line": "Pt I Sec B line 8c",
        "taxable": True,
        "summary": ("California does NOT conform to the federal exclusion for "
                     "cancellation-of-debt (COD) income from the discharge of "
                     "mortgage debt on your principal residence, for discharges "
                     "occurring after December 31, 2017. If you excluded this income "
                     "on your federal return, you must add it back as taxable income "
                     "on Schedule CA (540), Part I, Section B, line 8c, column C -- "
                     "even though it was federally tax-free."),
        "citation": "FTB 2025 Instructions for Schedule CA (540) -- Line 8c, Cancellation of Debt",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
    {
        "topic_key": "educator_expenses",
        "topic_label": "Educator expenses deduction",
        "schedule_ca_line": "Pt I Sec C line 11",
        "taxable": True,
        "summary": ("California does not conform to the federal educator expenses "
                     "deduction (the out-of-pocket classroom supplies deduction "
                     "available to eligible K-12 teachers and other educators). "
                     "Whatever amount you deducted federally on this line must be "
                     "added back for California purposes on Schedule CA (540), Part "
                     "I, Section C, line 11, column B -- it does not reduce your "
                     "California taxable income the way it reduces your federal AGI."),
        "citation": "FTB 2025 Instructions for Schedule CA (540) -- Line 11, Educator Expenses",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
    {
        "topic_key": "military_retirement_exclusion",
        "topic_label": "Military retirement pay / DoD Survivor Benefit Plan exclusion",
        "schedule_ca_line": "Pt I Sec A line 5a/5b",
        # taxable=None deliberately (same precedent as alimony_spousal_support):
        # the real answer is conditional on stated AGI (a hard eligibility
        # cliff, not a gradual phase-out) -- a blanket False would be
        # confidently wrong for a high earner over the $125k/$250k limit.
        # engine._income_military_retirement_answer computes the real,
        # AGI-specific verdict when AGI+filing status are stated; this topic
        # is the fallback for a bare question with neither.
        "taxable": None,
        "summary": ("NEW for tax years 2025 through 2029: California allows a "
                     "qualified taxpayer to exclude from income federal retirement "
                     "pay received for service in the uniformed services, and/or "
                     "annuity payments under a U.S. Department of Defense Survivor "
                     "Benefit Plan, up to $20,000 for EACH type of payment (so up to "
                     "$40,000 total if you receive both). This exclusion only "
                     "applies if your federal AGI does not exceed $125,000 (single, "
                     "HOH, or MFS) or $250,000 (married filing jointly, or a "
                     "surviving spouse) -- it is an eligibility cutoff, not a "
                     "gradual phase-out: if your AGI exceeds the limit, none of the "
                     "exclusion applies and the pay is fully taxable, same as "
                     "federal. The exclusion sunsets after tax year 2029. Please "
                     "state your AGI and filing status for an exact answer."),
        "citation": "R&TC Sections 17132.9 and 17132.10; FTB 2025 Instructions for Schedule CA (540) -- Line 5a/5b",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
    # Wildfire/disaster settlement family: 6 separately-named settlement/
    # relief programs (Kincade, Zogg, Thomas/Woolsey, Fire Victims Trust,
    # Chiquita Canyon, wildfire mitigation payments) plus a general
    # provision, ALL sharing the identical mechanic (excluded from CA
    # income, though federally taxable) -- modeled as ONE topic rather than
    # 7 separate rows, since the verdict and the underlying "why" are
    # identical for all of them; the summary enumerates each program's own
    # citation and sunset date individually so a question naming a specific
    # fire still gets a precise, correctly-dated answer. Deliberately
    # EXCLUDES the "Federal Disaster Tax Relief Act" wildfire relief payment
    # (line 8z) -- that one is the OPPOSITE mechanic (CA does NOT conform to
    # THAT federal exclusion, so it stays taxable) and would be actively
    # wrong to fold into this "generally excluded" topic.
    {
        "topic_key": "wildfire_disaster_settlement_exclusion",
        "topic_label": "Wildfire and disaster settlement payment exclusion",
        "schedule_ca_line": "Pt I Sec B line 8z (multiple programs)",
        "taxable": False,
        "summary": ("California excludes from income amounts received from several "
                     "specific, named wildfire-related settlements and disaster "
                     "programs, even though these amounts may be taxable federally. "
                     "Each program has its own sunset date -- the exclusion no "
                     "longer applies once a program's window closes: "
                     "(1) Kincade Fire (2019, PG&E settlement) -- tax years 2020 "
                     "through 2027, R&TC 17139.2. "
                     "(2) Zogg Fire (2020, PG&E settlement) -- tax years 2020 "
                     "through 2027, R&TC 17139.3. "
                     "(3) Thomas Fire (2017) and Woolsey Fire (2018) Southern "
                     "California Edison settlements -- through tax year 2026, "
                     "R&TC 17138.6. "
                     "(4) Fire Victims Trust (Camp Fire / PG&E bankruptcy) -- "
                     "through tax year 2027, R&TC 17138.5. "
                     "(5) Chiquita Canyon elevated temperature landfill event "
                     "payments -- tax years 2024 through 2028, R&TC 17157.5. "
                     "(6) California Wildfire Mitigation Financial Assistance "
                     "Program payments -- tax years 2024 through 2028, R&TC 17138.8. "
                     "(7) General qualified wildfire disaster settlement payments "
                     "(any settlement entity, in connection with a qualified "
                     "California wildfire disaster) -- tax years 2021 through 2029, "
                     "R&TC 17138.7. If you received a settlement in a prior year "
                     "and included it as income, you can file an amended return "
                     "within the normal statute of limitations. NOTE: this does NOT "
                     "cover general federal wildfire relief payments under the "
                     "Federal Disaster Tax Relief Act of 2023 -- California does "
                     "NOT conform to that specific federal exclusion, so those "
                     "payments remain taxable for California."),
        "citation": "R&TC Sections 17138.5, 17138.6, 17138.7, 17138.8, 17139.2, 17139.3, 17157.5",
        "source_url": "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html",
    },
]


def load():
    conn = db.get_conn()
    n_brackets = 0
    for status, rows in SCHEDULES.items():
        for floor, ceiling, base_amount, rate in rows:
            conn.execute(
                "INSERT INTO ca_income_tax_brackets "
                "(tax_year, filing_status, bracket_type, bracket_floor, bracket_ceiling, "
                "base_amount, rate, citation, source_url, as_of) "
                "VALUES (%s,%s,'standard',%s,%s,%s,%s,%s,%s, CURRENT_DATE) "
                "ON CONFLICT (tax_year, filing_status, bracket_type, bracket_floor) "
                "DO UPDATE SET bracket_ceiling=EXCLUDED.bracket_ceiling, "
                "base_amount=EXCLUDED.base_amount, rate=EXCLUDED.rate",
                (TAX_YEAR, status, floor, ceiling, base_amount, rate, RATE_CITATION, RATE_URL))
            n_brackets += 1
    # Behavioral Health Services Tax (2025 name; formerly "Mental Health
    # Services Tax") -- filing_status=NULL: applies the SAME way regardless
    # of filing status, confirmed against the FTB instructions text (no
    # per-status branching -- just "Form 540, line 19"). NOTE: standard SQL
    # never treats NULL = NULL, so ON CONFLICT on a column list that includes
    # filing_status CANNOT dedupe a NULL-filing_status row -- confirmed the
    # hard way (a second `load` run silently inserted a duplicate instead of
    # upserting). Delete-then-insert instead, which is correct regardless of
    # NULL semantics for this small reference row.
    conn.execute(
        "DELETE FROM ca_income_tax_brackets WHERE tax_year=%s AND bracket_type='mhs_surtax'",
        (TAX_YEAR,))
    conn.execute(
        "INSERT INTO ca_income_tax_brackets "
        "(tax_year, filing_status, bracket_type, bracket_floor, bracket_ceiling, "
        "base_amount, rate, citation, source_url, as_of) "
        "VALUES (%s, NULL, 'mhs_surtax', 1000000, NULL, 0, 0.01, %s, %s, CURRENT_DATE)",
        (TAX_YEAR, "FTB 2025 Form 540 Instructions, Line 62 (Behavioral Health Services Tax)",
         INSTR_URL))
    n_brackets += 1

    n_dedu = 0
    for status, amount in STANDARD_DEDUCTION.items():
        conn.execute(
            "INSERT INTO ca_standard_deduction (tax_year, filing_status, amount, "
            "citation, source_url, as_of) VALUES (%s,%s,%s,%s,%s, CURRENT_DATE) "
            "ON CONFLICT (tax_year, filing_status) DO UPDATE SET amount=EXCLUDED.amount",
            (TAX_YEAR, status, amount, "FTB 2025 Standard Deduction", DEDUCTION_URL))
        n_dedu += 1

    n_topics = 0
    for t in INCOME_TOPICS:
        conn.execute(
            "INSERT INTO income_tax_topics (topic_key, topic_label, taxable, tax_year, "
            "citation, summary, source_url, schedule_ca_line, tier, status, confidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'consumer','verified',1.0) "
            "ON CONFLICT (topic_key, tax_year) DO UPDATE SET "
            "taxable=EXCLUDED.taxable, summary=EXCLUDED.summary, citation=EXCLUDED.citation, "
            "schedule_ca_line=EXCLUDED.schedule_ca_line",
            (t["topic_key"], t["topic_label"], t["taxable"], TAX_YEAR,
             t["citation"], t["summary"], t["source_url"], t.get("schedule_ca_line")))
        n_topics += 1

    # Young Child Tax Credit reference numbers -- filing_status=NULL (applies
    # regardless), so ON CONFLICT can't dedupe it (NULL != NULL in SQL, the
    # same gotcha the mhs_surtax row above already hit) -- delete-then-insert.
    conn.execute(
        "DELETE FROM ca_income_credits WHERE credit_key='young_child_tax_credit' AND tax_year=%s",
        (TAX_YEAR,))
    conn.execute(
        "INSERT INTO ca_income_credits (credit_key, credit_label, tax_year, filing_status, "
        "max_amount, phase_out_start, phase_out_end, phase_out_rate, refundable, citation, "
        "source_url, as_of) "
        "VALUES ('young_child_tax_credit','Young Child Tax Credit (YCTC)',%s,NULL,"
        "1189,27425,32900,21.71,TRUE,%s,%s,CURRENT_DATE)",
        (TAX_YEAR, YCTC_CITATION, EITC_BOOKLET_URL))
    n_credits = 1

    # Foster Youth Tax Credit -- verified against FTB's 2025 3514 Booklet,
    # Step 10 (eligibility) and Part IX Line 34/36-39 (arithmetic): SAME
    # phase-out formula and numbers as YCTC ($1,189 max, $27,425 threshold,
    # $21.71 per $100 over) -- confirmed by reading the actual worksheet
    # lines, not assumed from the two credits sharing a dollar figure.
    # filing_status=NULL, same NULL != NULL delete-then-insert reasoning as YCTC.
    conn.execute(
        "DELETE FROM ca_income_credits WHERE credit_key='foster_youth_tax_credit' AND tax_year=%s",
        (TAX_YEAR,))
    conn.execute(
        "INSERT INTO ca_income_credits (credit_key, credit_label, tax_year, filing_status, "
        "max_amount, phase_out_start, phase_out_end, phase_out_rate, refundable, citation, "
        "source_url, as_of) "
        "VALUES ('foster_youth_tax_credit','Foster Youth Tax Credit (FYTC)',%s,NULL,"
        "1189,27425,32900,21.71,TRUE,%s,%s,CURRENT_DATE)",
        (TAX_YEAR, FYTC_CITATION, EITC_BOOKLET_URL))
    n_credits += 1

    # Nonrefundable Renter's Credit -- one row per filing status (real value,
    # ON CONFLICT works fine here since filing_status is never NULL for this
    # credit, unlike YCTC). phase_out_start/phase_out_rate stay NULL: this is
    # a hard income CEILING (full flat amount below it, $0 above), not a
    # gradual phase-out curve -- income_credits.compute_renters_credit()
    # reads phase_out_end as that ceiling.
    for status, (amount, ceiling) in RENTERS_CREDIT.items():
        conn.execute(
            "INSERT INTO ca_income_credits (credit_key, credit_label, tax_year, filing_status, "
            "max_amount, phase_out_end, refundable, citation, source_url, as_of) "
            "VALUES ('renters_credit','Nonrefundable Renter''s Credit',%s,%s,%s,%s,FALSE,%s,%s,"
            "CURRENT_DATE) "
            "ON CONFLICT (credit_key, tax_year, filing_status) DO UPDATE SET "
            "max_amount=EXCLUDED.max_amount, phase_out_end=EXCLUDED.phase_out_end",
            (TAX_YEAR, status, amount, ceiling, RENTERS_CREDIT_CITATION, RENTERS_CREDIT_URL))
        n_credits += 1

    print(f"loaded {n_brackets} bracket rows, {n_dedu} standard-deduction rows, "
          f"{n_topics} income topics, {n_credits} credit reference row(s) for tax_year={TAX_YEAR}")
    conn.close()


def load_eitc_table():
    """Loads the 658-row verified CalEITC table (see module docstring for
    extraction/verification method). Delete-then-insert (not per-row
    ON CONFLICT) -- simplest safe idempotency for a bulk reference-data
    reload at this row count."""
    rows = json.load(open(EITC_TABLE_PATH))
    conn = db.get_conn()
    conn.execute("DELETE FROM ca_eitc_table WHERE tax_year=%s", (TAX_YEAR,))
    for floor, ceiling, c0, c1, c2, c3 in rows:
        conn.execute(
            "INSERT INTO ca_eitc_table (tax_year, income_floor, income_ceiling, "
            "credit_0, credit_1, credit_2, credit_3, citation, source_url, as_of) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_DATE)",
            (TAX_YEAR, floor, ceiling, c0, c1, c2, c3, EITC_TABLE_CITATION, EITC_BOOKLET_URL))
    print(f"loaded {len(rows)} CalEITC table rows for tax_year={TAX_YEAR}")
    conn.close()


def embed():
    """Mirrors embed_rules.py's build()+embed() pattern, scoped to
    income_tax_topics -> income_rule_embeddings, so _income_route_candidates/
    _income_lookup (wired in Phase 2, inert until now) can actually fire."""
    conn = db.get_conn()
    for key, label, cond, summ in conn.execute(
            "SELECT topic_key, topic_label, condition, summary FROM income_tax_topics"
    ).fetchall():
        text = ". ".join(x for x in (label, cond, summ) if x)
        conn.execute(
            "INSERT INTO income_rule_embeddings (topic_key, kind, text) VALUES (%s,'topic',%s) "
            "ON CONFLICT (topic_key) DO UPDATE SET text=EXCLUDED.text, "
            "embedding = CASE WHEN income_rule_embeddings.text <> EXCLUDED.text THEN NULL "
            "ELSE income_rule_embeddings.embedding END",
            (key, text))
    rows = conn.execute(
        "SELECT topic_key, text FROM income_rule_embeddings WHERE embedding IS NULL").fetchall()
    done = 0
    for key, text in rows:
        v = None
        for _ in range(6):
            try:
                v = genai.embed_content(model=config.EMBED_MODEL, content=text,
                                        output_dimensionality=config.EMBED_DIM)["embedding"]
                break
            except Exception as e:
                msg = str(e).lower()
                if any(w in msg for w in ("429", "quota", "rate", "resource")):
                    time.sleep(25)
                    continue
                print(f"STOP after {done}: {type(e).__name__}: {str(e)[:140]}")
                v = "FATAL"
                break
        if v in (None, "FATAL"):
            break
        emb = "[" + ",".join(str(float(x)) for x in v) + "]"
        conn.execute("UPDATE income_rule_embeddings SET embedding=%s::vector WHERE topic_key=%s",
                     (emb, key))
        done += 1
        time.sleep(0.4)
    tot = conn.execute("SELECT count(*) FROM income_rule_embeddings").fetchone()[0]
    embedded = conn.execute(
        "SELECT count(*) FROM income_rule_embeddings WHERE embedding IS NOT NULL").fetchone()[0]
    print(f"embedded {done} this run; income_rule_embeddings {embedded}/{tot}")
    conn.close()


def status():
    conn = db.get_conn()
    for tbl in ("ca_income_tax_brackets", "ca_standard_deduction", "income_tax_topics",
                "ca_income_credits", "ca_eitc_table"):
        n = conn.execute(f"SELECT count(*) FROM {tbl} WHERE tax_year=%s", (TAX_YEAR,)).fetchone()[0]
        print(f"  {tbl:26} {n} rows for tax_year={TAX_YEAR}")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "load":
        load()
        status()
    elif cmd == "load_eitc_table":
        load_eitc_table()
        status()
    elif cmd == "embed":
        embed()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
