"""Ring 2 / Phase 3 -- deterministic CA personal-income-tax bracket math.

LLM-free, same principle as sales tax's rate x price arithmetic: a NUMBER
that changes a taxpayer's answer must be computed by code, never composed by
a language model. Mirrors this project's "separate module per concept"
precedent (local_rates.py, fees.py, cannabis_local.py) -- engine.py imports
and calls this, it does not reimplement the math inline.

TAX-YEAR DEFAULT (per the Phase 2/3 plan's explicit ask to decide and state
this now): filing season for tax year N happens in calendar year N+1, so
"today" always has a most-recently-COMPLETE tax year that is the sensible
default when a question doesn't name one ("what's my tax bracket" almost
always means the return people are actually filing right now). DEFAULT_YEAR
must be updated each January when the new year's Rate Schedules are
published and loaded via load_income_content.py -- it is NOT auto-derived
from the system clock, since a new tax year's numbers aren't available on
January 1st itself (FTB typically publishes the following autumn/winter).
"""
import math
import re
from datetime import date, timedelta

DEFAULT_TAX_YEAR = 2025   # 2025 is the most recently complete tax year as of
                           # this writing (mid-2026); most 2025 returns are
                           # filed during 2026. Bump this + re-run
                           # load_income_content.py once 2026's schedules are
                           # published and loaded, not before.

FILING_STATUS_LABELS = {
    "single": "Single",
    "mfs": "Married/RDP Filing Separately",
    "mfj": "Married/RDP Filing Jointly",
    "hoh": "Head of Household",
    "qss": "Qualifying Surviving Spouse/RDP",
}

# Terms that disqualify the SIMPLE wage-earner+standard-deduction compute
# path -- any of these means the question involves a fact pattern (business
# income, itemizing, capital gains...) this first pass deliberately does NOT
# attempt, per the plan's "build the default case, defer rather than guess"
# discipline. Deterministic term match, same style as fees.py.
COMPLEXITY_EXCLUDE = {
    "self-employ", "self employ", "1099", "business", "s-corp", "s corp",
    "llc", "partnership", "capital loss", "rental", "renting", "rented",
    "itemize", "itemized", "itemizing", "dependent", "stock", "rsu", "trust",
    "estate", "freelance", "freelancing", "contractor", "contracting",
    "contracted", "sole proprietor", "k-1",
    "schedule c", "schedule e", "gambling", "gambled", "betting", "wagering",
    "alimony",
    # added alongside the IRA-deduction feature: without this, "how much
    # california tax do I owe on $80,000 in wages with a $6,000 IRA
    # deduction, single" would be intercepted by the plain wage-only path
    # (no OTHER complexity term present), silently IGNORING the stated
    # IRA deduction and computing tax on the full $80,000 -- overstating
    # the tax owed. Now correctly defers to the dedicated IRA-deduction
    # path instead (see income_brackets' IRA_DEDUCTION_TERMS's module
    # note -- that path uses COMPLEXITY_EXCLUDE minus these same terms,
    # so it isn't excluded from itself).
    "ira deduction", "ira contribution", "traditional ira", "deduct my ira",
    "deductible ira",
    # added alongside the HSA-investment-gain feature, same reasoning:
    # without this, a stated HSA gain alongside wages would be silently
    # DROPPED by the plain path (_amount() only grabs the first dollar
    # figure) rather than added to CA income. HSA loss terms included too
    # -- otherwise a "hsa investment loss" question isn't excluded by
    # anything (it doesn't literally say "capital loss"), and the plain
    # path silently drops the loss figure the same way. Found live via
    # the regression sweep after this feature's own gain-vs-loss guard
    # (which correctly excludes losses from ITS OWN detection) turned out
    # not to protect the OTHER paths from the same figure.
    "hsa investment gain", "hsa investment gains", "hsa capital gain",
    "gain inside my hsa", "gain inside an hsa", "sold investments in my hsa",
    "investments inside my hsa", "hsa investment sale",
    "hsa investment loss", "hsa capital loss", "loss inside my hsa",
    # added alongside the foreign-earned-income-exclusion feature: a
    # general-purpose addback that can accompany any income type, same
    # reasoning as IRA deduction/HSA gain -- without this, a stated Form
    # 2555 excluded amount would be silently dropped by the plain path.
    "foreign earned income exclusion", "foreign earned income and housing exclusion",
    "form 2555", "foreign housing exclusion", "excluded foreign earned income",
    "excluded under form 2555",
    # added alongside the Subpart F/GILTI features, same general-purpose
    # reasoning: both are unconditional CFC-inclusion subtractions that
    # can accompany any other income type -- without this, a stated
    # inclusion amount would be silently dropped by the plain path.
    "subpart f income", "subpart f inclusion", "subpart f",
    "irc section 951(a)", "irc 951(a)", "section 951(a) inclusion",
    "951(a) inclusion",
    "gilti", "global intangible low-taxed income", "global intangible low taxed income",
    "irc section 951a", "irc 951a", "section 951a inclusion", "951a inclusion",
    "form 8992",
    # added alongside the NRA foreign-income true-up feature (Line 8z),
    # same general-purpose reasoning: a federal-nonresident-alien self-
    # identification alongside a foreign-source income/loss figure can
    # accompany any other income type -- without this, the figure would
    # be silently dropped by the plain path.
    "nonresident alien", "non-resident alien", "federal nonresident alien",
    "form 1040-nr", "1040-nr", "1040nr",
    # added alongside the disaster loss carryover feature, same general-
    # purpose reasoning -- a stated carryover figure can accompany any
    # other income type.
    "disaster loss carryover", "disaster loss deduction",
    # added alongside the foreign housing deduction extension (Line
    # 24j), same reasoning as the foreign earned income exclusion above.
    "foreign housing deduction", "housing deduction from form 2555",
    "form 2555 housing deduction", "housing deduction under form 2555",
}
COMPUTE_TRIGGERS = {
    "tax bracket", "how much tax", "how much california tax",
    "how much state tax", "how much ca tax", "tax i owe", "tax owe",
    "tax liability", "compute my tax", "figure my tax", "calculate my tax",
    "what tax do i owe", "what will i owe",
}
DEDUCTION_TRIGGERS = {"standard deduction"}

# --- non-wage income types NOT excluded from the simple compute path ----
# Each verified directly against FTB's Schedule CA (540) instructions
# before being unlocked, following the SAME "generally matches federal,
# with only a narrow named exception" pattern each time:
#   Capital gains (Line 7a): "California taxes long and short term capital
#     gains as regular income. No special rate for long term capital gains
#     exists."
#   Dividends (Line 3): "Generally, no difference exists...however,
#     California taxes dividends derived from other states and their
#     municipal obligations."
#   Pensions (Line 5a/5b): "Generally, no adjustments are made on this
#     line. However, if you received Tier 2 railroad retirement benefits
#     or partially taxable distributions from a pension plan, you may
#     need to make the following adjustments."
#   Bonus income: confirmed via secondary sources (withholding-rate
#     explainers) that CA's 10.23% supplemental-wage rate is a WITHHOLDING
#     mechanic only -- it does not change the annual tax LIABILITY this
#     system computes. A bonus is simply wage income; no caveat needed at
#     all (unlike the others, which each keep a narrow disclosed exception).
# Since CA taxes all of these identically to ordinary wages (no federal-
# style preferential rate anywhere in this system), compute_ca_tax's
# existing bracket math applies unchanged -- only the ANSWER TEXT needs to
# correctly describe what kind of income was assumed (see
# detect_income_description below), not the math itself.
#
# "capital loss" stays in COMPLEXITY_EXCLUDE above (the $3,000/year
# loss-offset limit + carryforward is real complexity this path doesn't
# handle); "stock"/"rsu" also stay excluded (ambiguous -- could mean
# vesting income, a sale, or options, each taxed differently).
#
# ONE LANDMINE: a capital gain from selling a HOME is a completely
# different calculation (the Section 121 $250k/$500k primary-residence
# exclusion can make some or all of it non-taxable) -- "capital gain"
# alone is safe to compute, but "capital gain" + a home-sale word is NOT,
# so it still defers via this narrower, targeted guard rather than a
# blanket re-exclusion of "capital gain" itself.
HOME_SALE_TERMS = {"home", "house", "residence", "property"}

INCOME_TYPE_LABELS = {
    "capital gain": ("capital gains",
        " (assumes no California/federal cost-basis differences)"),
    "dividend": ("dividend income",
        " (assumes ordinary dividends -- exempt-interest dividends from "
        "out-of-state municipal bond funds may be treated differently)"),
    "interest": ("interest income",
        " (assumes ordinary taxable interest -- U.S. government bond "
        "interest is excluded from California tax, and out-of-state "
        "municipal bond interest is treated differently)"),
    "pension": ("pension income",
        " (assumes no Tier 2 railroad retirement benefits or partially-"
        "taxable annuity distributions apply)"),
    "annuity": ("annuity income",
        " (assumes no Tier 2 railroad retirement benefits or partially-"
        "taxable annuity distributions apply)"),
    "bonus": ("wage income including a bonus", ""),
}


def detect_income_description(question: str):
    """Returns (label, caveat) describing what kind of income the compute
    path is assuming, purely for the disclosed-assumption sentence in the
    answer text -- the underlying bracket math is identical regardless of
    income type (see the module note above)."""
    q = question.lower()
    for term, (label, caveat) in INCOME_TYPE_LABELS.items():
        if term in q:
            return label, caveat
    return "gross wage income", ""

# --- self-employment (sole-proprietor Schedule C) compute path -----------
# A NARROWER exclude list than COMPLEXITY_EXCLUDE above -- self-employment
# terms are the TRIGGER here, not a disqualifier, but everything genuinely
# more complex than "one sole-proprietor Schedule C, no other income" still
# defers: business ENTITIES beyond a sole proprietorship (s-corp/LLC/
# partnership -- different computation regimes entirely), any other income
# type (capital gains, rental, pension...), itemizing, and -- specifically
# for this path -- mixing in wage/W-2 income, since the MVP assumes
# self-employment is the ONLY income source (mixing income types is real
# added complexity deliberately deferred further).
SE_TRIGGERS = {
    "self-employed", "self employed", "self-employment", "self employment",
    "freelance", "freelancer", "freelancing", "independent contractor",
    "contractor", "contracting", "contracted",
    "sole proprietor", "gig work", "gig economy", "schedule c",
}
SE_COMPLEXITY_EXCLUDE = {
    "s-corp", "s corp", "llc", "partnership", "capital gain", "capital loss",
    "rental", "renting", "rented", "itemize", "itemized", "itemizing",
    "dependent", "bonus", "stock", "rsu",
    "trust", "estate", "k-1", "schedule e", "gambling", "gambled", "betting",
    "wagering", "alimony", "pension",
    "w-2", "w2", "wage", "wages", "salary", "salaried",
    # added alongside the excess-business-loss feature: this path assumes
    # the stated dollar figure is POSITIVE net profit (compute_se_tax has
    # no sign check of its own beyond net_profit<=0 -> None, which only
    # catches an EXPLICITLY negative number, not a positive figure the
    # user described in words as a LOSS) -- without this exclusion, "I'm
    # self-employed with a $700,000 business loss" would silently treat
    # $700,000 as PROFIT and compute SE tax on it, the same "wrong number
    # silently used" bug class as the contracted/salaried stemming gaps
    # found earlier this project. Business-loss questions now correctly
    # defer to the dedicated excess-business-loss path instead.
    "business loss", "net business loss", "aggregate business loss",
    "excess business loss",
    # added alongside the NOL-suspension feature: without this, a question
    # combining this path's trigger with an NOL carryover mention would
    # compute a profit/K-1 figure while silently IGNORING the NOL
    # carryover the user explicitly asked to deduct -- a confidently-
    # computed answer that omits a deduction the question was actually
    # about, overstating the tax owed. Now correctly defers to the
    # dedicated NOL path instead.
    "net operating loss", "nol carryover", "nol deduction",
    # added alongside the cannabis-280E feature: without this, "I'm
    # self-employed running a licensed cannabis business, $500,000 net
    # profit, $150,000 in disallowed 280E expenses" would compute plain SE
    # tax on the FEDERAL (280E-inflated) net profit, silently IGNORING the
    # CA-specific restoration the taxpayer is entitled to under R&TC
    # 17209 -- understating the deduction, overstating the tax owed. Now
    # correctly defers to the dedicated cannabis-280E path instead (which
    # reuses this same self-employment math via compute_self_employment_
    # ca_tax's cannabis_280e_expenses parameter). NOT added to
    # K1_COMPLEXITY_EXCLUDE -- a K-1 recipient needs no adjustment here at
    # all (see CANNABIS_LICENSE_TERMS's module note), so the K-1 path
    # computing normally on a licensed-cannabis K-1 is already correct,
    # not a gap to guard against.
    "licensed cannabis", "cannabis license", "maucrsa", "dcc-licensed",
    "dcc license",
    # added alongside the IRA-deduction feature: an IRA deduction is a
    # general above-the-line adjustment that can accompany ANY income
    # type, including self-employment -- without this, "$80,000 net
    # profit self-employed with a $6,000 IRA deduction, single" would
    # compute SE tax while silently ignoring the stated IRA deduction.
    # Unlike business-loss/NOL/cannabis, this ALSO needs to guard
    # K1_COMPLEXITY_EXCLUDE below, since an IRA deduction can just as
    # easily accompany K-1 income (no entity-level absorption applies to
    # a purely personal deduction like this one).
    "ira deduction", "ira contribution", "traditional ira", "deduct my ira",
    "deductible ira",
    # added alongside the HSA-investment-gain feature, same general-
    # purpose reasoning: an HSA gain can accompany self-employment income
    # just as easily as wages. Loss terms included too -- see
    # COMPLEXITY_EXCLUDE's matching comment for why.
    "hsa investment gain", "hsa investment gains", "hsa capital gain",
    "gain inside my hsa", "gain inside an hsa", "sold investments in my hsa",
    "investments inside my hsa", "hsa investment sale",
    "hsa investment loss", "hsa capital loss", "loss inside my hsa",
    # added alongside the fringe-benefit-restoration feature, same
    # reasoning as cannabis 280E: without this, a fringe-benefit
    # restoration figure stated alongside self-employment net profit
    # would be silently ignored, computing SE tax on the federal (TCJA-
    # limited) profit alone. NOT added to K1_COMPLEXITY_EXCLUDE -- same
    # as cannabis 280E, the entity absorbs this before issuing the K-1.
    "fringe benefit expense", "fringe benefit expenses", "employer fringe benefit",
    "employee parking", "employee transit", "employee transportation benefit",
    "employee transportation fringe", "on-premises meals", "on premises meals",
    "employee meal benefit", "fringe benefit limitation",
    "entertainment expense limitation",
    # added alongside the foreign-earned-income-exclusion feature, same
    # general-purpose reasoning as IRA deduction/HSA gain -- a Form 2555
    # excluded amount can accompany self-employment income just as
    # easily as wages.
    "foreign earned income exclusion", "foreign earned income and housing exclusion",
    "form 2555", "foreign housing exclusion", "excluded foreign earned income",
    "excluded under form 2555",
    # added alongside the Subpart F/GILTI features, same general-purpose
    # reasoning as IRA deduction/HSA gain -- a CFC-inclusion subtraction
    # can accompany self-employment income just as easily as wages.
    "subpart f income", "subpart f inclusion", "subpart f",
    "irc section 951(a)", "irc 951(a)", "section 951(a) inclusion",
    "951(a) inclusion",
    "gilti", "global intangible low-taxed income", "global intangible low taxed income",
    "irc section 951a", "irc 951a", "section 951a inclusion", "951a inclusion",
    "form 8992",
    # added alongside the NRA foreign-income true-up feature, same
    # general-purpose reasoning as IRA deduction/HSA gain -- a foreign-
    # source income/loss figure can accompany self-employment income
    # just as easily as wages.
    "nonresident alien", "non-resident alien", "federal nonresident alien",
    "form 1040-nr", "1040-nr", "1040nr",
    # added alongside the disaster loss carryover feature, same
    # general-purpose reasoning as IRA deduction/HSA gain.
    "disaster loss carryover", "disaster loss deduction",
    # added alongside the foreign housing deduction extension (Line
    # 24j), same reasoning as the foreign earned income exclusion above.
    "foreign housing deduction", "housing deduction from form 2555",
    "form 2555 housing deduction", "housing deduction under form 2555",
}

# Federal Schedule SE mechanics (California has no separate self-employment
# tax -- this exists ONLY to compute the deductible half, which DOES reduce
# California AGI: R&TC Section 17072(a) conforms to IRC Section 62's
# definition of adjusted gross income, which includes the one-half
# self-employment-tax deduction as an above-the-line adjustment; 17072(b)/(c)
# list the ONLY 2 exceptions California carves out -- educator expenses and
# whistleblower attorney fees -- neither is the SE-tax deduction, confirming
# conformity). Verified against a matched worked example this session:
# $80,000 net profit -> $11,304 SE tax -> $5,652 deduction.
SE_NET_EARNINGS_FACTOR = 0.9235
SE_SOCIAL_SECURITY_RATE = 0.124
SE_MEDICARE_RATE = 0.029
SE_SOCIAL_SECURITY_WAGE_BASE = 176100   # 2025; update yearly alongside DEFAULT_TAX_YEAR
SE_CITATION = "IRS Schedule SE; California R&TC Section 17072(a) (conforms to IRC Section 62)"
SE_SOURCE_URL = "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=17072.&lawCode=RTC"


def detect_self_employment_signal(question: str):
    """Returns a filing_status key iff this looks like a genuine
    sole-proprietor self-employment tax computation: an SE trigger term, an
    explicit filing status, a compute trigger phrase, and none of the
    narrower SE_COMPLEXITY_EXCLUDE terms. Mirrors detect_compute_signal's
    shape exactly, for the same reason: never guess past what's stated."""
    q = question.lower()
    if not any(t in q for t in SE_TRIGGERS):
        return None
    if any(t in q for t in SE_COMPLEXITY_EXCLUDE):
        return None
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return None
    return detect_filing_status(question)


def detect_self_employment_missing_filing_status(question: str) -> bool:
    """Mirrors detect_compute_missing_filing_status for the self-employment
    path -- same reasoning: filing status changes which bracket table
    applies, so it's still not safe to guess, but the caller can say
    exactly what's missing instead of a generic defer."""
    q = question.lower()
    if not any(t in q for t in SE_TRIGGERS):
        return False
    if any(t in q for t in SE_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return detect_filing_status(question) is None


def compute_se_tax(net_profit: float):
    """Federal Schedule SE math: 92.35% of net profit is subject to SE tax
    (the employer-equivalent-portion exclusion, mirroring how FICA works for
    employees); the 12.4% Social Security portion is capped at the wage
    base, the 2.9% Medicare portion is not. Returns the FULL se_tax (a
    federal-only liability, not itself a California tax) and the deductible
    half (which DOES affect California AGI -- see the module note above).
    Deliberately excludes the 0.9% Additional Medicare Tax (IRC Section
    1401(b)(2)) -- that surtax applies above $200k/$250k earned income but
    is NOT part of the deductible half, so it's correctly irrelevant here."""
    se_earnings = net_profit * SE_NET_EARNINGS_FACTOR
    ss_taxable = min(se_earnings, SE_SOCIAL_SECURITY_WAGE_BASE)
    se_tax = round(ss_taxable * SE_SOCIAL_SECURITY_RATE + se_earnings * SE_MEDICARE_RATE, 2)
    return {"se_tax": se_tax, "half_deduction": round(se_tax / 2, 2)}


def compute_self_employment_ca_tax(conn, net_profit: float, filing_status: str,
                                     tax_year: int = DEFAULT_TAX_YEAR,
                                     cannabis_280e_expenses: float = None,
                                     fringe_benefit_restoration: float = None):
    """net_profit is Schedule C NET PROFIT (revenue minus business expenses),
    not federal AGI or taxable income -- computing directly from net profit
    (rather than from an already-computed federal figure) means the federal
    Qualified Business Income deduction (IRC Section 199A, which California
    does NOT conform to) never enters the calculation at all, sidestepping
    that non-conformity entirely rather than requiring an addback.

    `cannabis_280e_expenses`, if given, is the amount of ordinary business
    expenses disallowed FEDERALLY under IRC Section 280E for a LICENSED
    commercial cannabis business, which California restores via R&TC
    Section 17209 -- see CANNABIS_LICENSE_TERMS's module note below. Since
    net_profit as stated is the federal (post-280E-disallowance) figure,
    California allowing the deduction federal law denies means CA taxable
    income is LOWER than the federal-conforming figure by this amount --
    SUBTRACTED from AGI, same "trust the input" precedent as every other
    add-on figure in this codebase. Deliberately does NOT touch se_tax
    (Federal Schedule SE is unaffected by CA's own conformity choices --
    real federal self-employment tax is still computed on the FULL,
    un-restored net_profit, exactly as compute_se_tax already does above).
    Only meaningful for a LICENSED (MAUCRSA/DCC) cannabis business --
    unlicensed cannabis businesses get NO restoration (federal 280E fully
    applies for CA purposes too in that case), so the plain path with this
    parameter omitted is already correct for them, same as before this
    parameter existed.

    `fringe_benefit_restoration`, if given, is the amount of employer
    fringe-benefit expense (entertainment, employee parking/transit,
    on-premises meals) disallowed or limited FEDERALLY under TCJA's IRC
    Section 274 changes, which California does NOT conform to -- see
    FRINGE_BENEFIT_TERMS's module note below. Same direction and same
    "trust the input" precedent as cannabis_280e_expenses: SUBTRACTED
    from AGI, se_tax untouched (federal Schedule SE still uses the full,
    un-restored net_profit). Only meaningful for a Schedule-C filer who
    is themselves an EMPLOYER (paid these benefits TO employees, not
    just themselves) -- see FRINGE_BENEFIT_TERMS's own scope-gating
    vocabulary."""
    if net_profit is None or net_profit <= 0:
        return None
    se = compute_se_tax(net_profit)
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = net_profit - se["half_deduction"]
    if cannabis_280e_expenses is not None:
        if cannabis_280e_expenses < 0:
            return None
        agi = agi - cannabis_280e_expenses
    if fringe_benefit_restoration is not None:
        if fringe_benefit_restoration < 0:
            return None
        agi = agi - fringe_benefit_restoration
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "net_profit": net_profit, "se_tax": se["se_tax"],
            "cannabis_280e_expenses": cannabis_280e_expenses,
            "fringe_benefit_restoration": fringe_benefit_restoration,
            "half_se_deduction": se["half_deduction"], "agi": agi,
            "standard_deduction": dedu["amount"]}


# --- K-1 pass-through income (business entities Phase B; trust/estate
# Phase B added 2026-08-13, same session) --------------------------------
# Verified against Schedule CA (540) Part I, Section B, Line 5 ("Rental
# Real Estate, Royalties, Partnerships, S Corporations, Trusts, etc.") --
# K-1 income (from a partnership/LLC/S-corp OR a trust/estate the taxpayer
# is a partner/member/shareholder/beneficiary of) flows through the EXACT
# SAME standard-deduction/bracket path already built for wages, since FTB
# taxes pass-through income as ordinary income with no special rate. This
# needed ZERO new tax math -- compute_ca_tax (already built) is reused
# directly, the same "zero new math, just a new income source" finding as
# nonresident tax Phase 3. Genuinely different from business entities
# Phase A (entity_tax.py) / trust-estate Phase A (not yet built): this
# computes what the INDIVIDUAL beneficiary/owner owes personally, not what
# the entity/trust/estate itself owes -- see entity_tax.py's
# K1_EXCLUDE_TERMS for the defense against a question mentioning both an
# entity type and "K-1" being wrongly routed to the entity-level answer.
#
# TRUST/ESTATE K-1 (Form 541 Schedule K-1), verified separately: lands on
# the SAME Schedule CA (540) Line 5 as business K-1 for ordinary/rental/
# portfolio income boxes (confirmed via the K-1 (541) beneficiary
# instructions, which explicitly route those boxes to "Schedule CA (540),
# Part I, Section B, line 5"). One REAL difference from business K-1,
# disclosed in the answer text: TAX-EXEMPT INTEREST shown on a trust/
# estate K-1 must be EXCLUDED, not taxed -- the taxpayer is asked to state
# the TAXABLE K-1 figure specifically. Capital-gain character technically
# routes to a different form (Schedule D) rather than Line 5, but since
# California taxes capital gains as ordinary income with no special rate,
# lumping it into this same compute path doesn't change the actual CA tax
# owed -- a safe, disclosed simplification, not a new complexity to build
# around. DNI/tier allocation is resolved at the FIDUCIARY level before
# the K-1 is even issued (the trust/estate's own problem, not the
# beneficiary's), so it doesn't add a new pitfall beyond business K-1's
# existing "trust the input" caveats. GRANTOR TRUSTS are a genuine
# exception, NOT handled by this path at all: FTB's own optional
# simplified reporting for grantor trusts means the income is taxed
# DIRECTLY to the grantor on the grantor's own personal return, not via a
# real K-1 -- see GRANTOR_TRUST_TERMS/detect_grantor_trust_mention, which
# redirects rather than computing a number.
#
# NOT MODELED (disclosed, not silently ignored -- confirmed via FTB
# Schedule K-1 (100S) instructions' own "most common adjustment items"
# list): the taxpayer's STATED (federal) K-1 amount is used AS-IS for the
# CA taxable-income base -- same "trust the input" precedent as every
# itemized-deduction figure in this engine -- but California generally
# does NOT equal the federal K-1 amount exactly. Real, FTB-disclosed
# differences this module does not attempt: (1) the entity's own CA
# annual/minimum tax ($800/1.5%/3.5%, federally deductible but NOT
# deductible against the CA tax base, normally added back -- see
# entity_tax.py for the entity-level amounts); (2) depreciation/basis
# differences (California does not conform to federal bonus depreciation);
# (3) narrower items like government bond interest. Basis limitations
# (IRC Section 1366(d)), at-risk rules (IRC Section 465), and passive
# activity loss limitations (IRC Section 469) are real, FTB-disclosed
# complexities that can each independently reduce how much of the stated
# K-1 amount is even currently deductible -- not attempted here, so the
# computed tax is a reasonable estimate assuming none of these limitations
# apply, not a guarantee. Also not modeled: California's ELECTIVE
# Pass-Through Entity (PTE) tax credit (AB150/SB113) -- a separate
# mechanism from the entity's mandatory annual tax that, if the entity
# elected into it, generates a shareholder/partner-level credit this
# module does not account for.
#
# SCOPE: K-1-ONLY income (the taxpayer's ONLY income is the stated K-1
# amount) -- mirrors the self-employment-only path's scope discipline
# exactly. Mixing K-1 with wage or self-employment income in one question
# is real added complexity, deliberately deferred (K1_COMPLEXITY_EXCLUDE
# below), same as the self-employment path originally deferred wage-mixing
# until its own dedicated mixed-income path was built.
K1_TRIGGERS = {
    "k-1", "k1", "schedule k-1", "schedule k1", "received a k-1", "got a k-1",
    "k-1 income", "pass-through income", "pass through income",
}
K1_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "rental", "renting", "rented", "dependent", "stock", "rsu",
    "gambling", "gambled", "betting", "wagering",
    "alimony", "pension", "self-employ", "self employ", "1099",
    "schedule c", "schedule e", "w-2", "w2", "wage", "wages", "salary", "salaried",
    # same reasoning as SE_COMPLEXITY_EXCLUDE's business-loss addition:
    # compute_k1_ca_tax has no sign check at all beyond None, so a K-1
    # amount described in words as a LOSS would otherwise be silently
    # treated as taxable K-1 income.
    "business loss", "net business loss", "aggregate business loss",
    "excess business loss",
    # added alongside the NOL-suspension feature: without this, a question
    # combining this path's trigger with an NOL carryover mention would
    # compute a profit/K-1 figure while silently IGNORING the NOL
    # carryover the user explicitly asked to deduct -- a confidently-
    # computed answer that omits a deduction the question was actually
    # about, overstating the tax owed. Now correctly defers to the
    # dedicated NOL path instead.
    "net operating loss", "nol carryover", "nol deduction",
    # added alongside the IRA-deduction feature: an IRA deduction can
    # accompany K-1 income just as easily as wages or self-employment --
    # no entity-level absorption applies here (unlike cannabis 280E,
    # which IS fully absorbed before the K-1 is issued), so without this
    # exclusion a K-1 amount stated alongside an IRA deduction would be
    # taxed in full, silently ignoring the deduction.
    "ira deduction", "ira contribution", "traditional ira", "deduct my ira",
    "deductible ira",
    # added alongside the HSA-investment-gain feature, same reasoning: no
    # entity-level absorption applies to a personal HSA gain either. Loss
    # terms included too -- see COMPLEXITY_EXCLUDE's matching comment.
    "hsa investment gain", "hsa investment gains", "hsa capital gain",
    "gain inside my hsa", "gain inside an hsa", "sold investments in my hsa",
    "investments inside my hsa", "hsa investment sale",
    "hsa investment loss", "hsa capital loss", "loss inside my hsa",
    # added alongside the foreign-earned-income-exclusion feature, same
    # reasoning as IRA deduction: no entity-level absorption applies to
    # a personal Form 2555 exclusion either.
    "foreign earned income exclusion", "foreign earned income and housing exclusion",
    "form 2555", "foreign housing exclusion", "excluded foreign earned income",
    "excluded under form 2555",
    # added alongside the Subpart F/GILTI features, same reasoning as IRA
    # deduction: no entity-level absorption applies to a personal CFC-
    # inclusion subtraction either.
    "subpart f income", "subpart f inclusion", "subpart f",
    "irc section 951(a)", "irc 951(a)", "section 951(a) inclusion",
    "951(a) inclusion",
    "gilti", "global intangible low-taxed income", "global intangible low taxed income",
    "irc section 951a", "irc 951a", "section 951a inclusion", "951a inclusion",
    "form 8992",
    # added alongside the NRA foreign-income true-up feature, same
    # reasoning as IRA deduction: no entity-level absorption applies to
    # this personal worldwide-income true-up either.
    "nonresident alien", "non-resident alien", "federal nonresident alien",
    "form 1040-nr", "1040-nr", "1040nr",
    # added alongside the disaster loss carryover feature, same
    # reasoning as IRA deduction: no entity-level absorption applies to
    # this personal carryover deduction either.
    "disaster loss carryover", "disaster loss deduction",
    # added alongside the foreign housing deduction extension (Line
    # 24j), same reasoning as the foreign earned income exclusion above.
    "foreign housing deduction", "housing deduction from form 2555",
    "form 2555 housing deduction", "housing deduction under form 2555",
}

# Trust/estate K-1s use FTB's optional simplified reporting for GRANTOR
# trusts instead of a real K-1 -- income taxed directly to the grantor on
# the grantor's OWN personal return (Form 540), not computed via this
# pass-through path at all. Checked as its own distinct signal (not part
# of K1_COMPLEXITY_EXCLUDE, which defers with a generic message) because
# the correct response here is a SPECIFIC redirect, not a generic defer --
# see detect_grantor_trust_mention.
GRANTOR_TRUST_TERMS = {"grantor trust"}

K1_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 5"
K1_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"


def detect_k1_signal(question: str):
    """Returns a filing_status key iff this looks like a genuine K-1-only
    income tax computation: a K-1 trigger term, a filing status, a compute
    trigger phrase, and none of the K1_COMPLEXITY_EXCLUDE terms. Mirrors
    detect_self_employment_signal's shape exactly."""
    q = question.lower()
    if not any(t in q for t in K1_TRIGGERS):
        return None
    if any(t in q for t in K1_COMPLEXITY_EXCLUDE):
        return None
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return None
    return detect_filing_status(question)


def detect_k1_missing_filing_status(question: str) -> bool:
    """Mirrors detect_self_employment_missing_filing_status."""
    q = question.lower()
    if not any(t in q for t in K1_TRIGGERS):
        return False
    if any(t in q for t in K1_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return detect_filing_status(question) is None


def detect_grantor_trust_mention(question: str) -> bool:
    """True iff a K-1 question mentions a grantor trust specifically --
    checked as its OWN signal (independent of K1_COMPLEXITY_EXCLUDE) since
    the correct response is a specific redirect (this income is taxed
    directly to the grantor, not via this K-1 path), not a generic defer.
    Requires K-1 language too, so a grantor-trust mention with no K-1
    context at all doesn't fire this (it wouldn't reach this function's
    caller in that case anyway, but keeps the check self-contained)."""
    q = question.lower()
    return any(t in q for t in K1_TRIGGERS) and any(t in q for t in GRANTOR_TRUST_TERMS)


def detect_trust_estate_k1(question: str) -> bool:
    """True iff a K-1 question appears to originate from a trust or
    estate (not a business entity) -- used only to decide whether to
    append the tax-exempt-interest exclusion disclosure to the answer
    text, not to gate whether the question is answered at all (both
    business and trust/estate K-1s use the identical compute path)."""
    q = question.lower()
    return "trust" in q or "estate" in q


def compute_k1_ca_tax(conn, k1_amount: float, filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """k1_amount is the taxpayer's STATED (federal) K-1 pass-through
    income, used as-is for CA taxable income -- see the module note above
    for the disclosed gap vs. the true CA-adjusted amount. Assumes this is
    the taxpayer's ONLY income (K-1-only, see module note on scope)."""
    if k1_amount is None or k1_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, k1_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "k1_amount": k1_amount, "taxable_income": taxable_income,
            "standard_deduction": dedu["amount"], "citation": K1_CITATION,
            "source_url": K1_SOURCE_URL}


# --- mixed wages + self-employment (the first multi-amount compute path) -
# The MVP self-employment path above deliberately assumed SE income was the
# ONLY income source (via SE_COMPLEXITY_EXCLUDE's wage/W-2/salary terms).
# This is the mixed case: a question stating TWO separate dollar figures,
# one wage-tagged and one SE-tagged (e.g. "$50,000 in wages and $30,000 in
# self-employment income"). Requires BOTH an SE trigger AND a wage-context
# term to be present -- this naturally can never fire on a wage-only or
# SE-only question (those lack one of the two required signals), so it
# can't shadow either single-source path; order between them doesn't
# matter. The math: only the SE portion gets the half-SE-tax deduction
# (wages never did and still don't need one); AGI = wages + net_profit -
# half_se_deduction; everything downstream reuses standard_deduction() and
# compute_ca_tax() unchanged, same as every other compute path.
WAGE_CONTEXT_TERMS = {"wage", "wages", "salary", "salaried", "w-2", "w2"}


def detect_mixed_wage_se_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'wages AND
    self-employment income' question: an SE trigger, a wage-context term,
    a filing status, a compute trigger, and none of the OTHER complexity
    disqualifiers (business entities beyond sole-proprietor, itemizing,
    capital gains, etc -- the same SE_COMPLEXITY_EXCLUDE set MINUS the
    wage-related terms, since wages are the whole point of this path)."""
    q = question.lower()
    if not any(t in q for t in SE_TRIGGERS):
        return None
    if not any(t in q for t in WAGE_CONTEXT_TERMS):
        return None
    non_wage_exclude = SE_COMPLEXITY_EXCLUDE - WAGE_CONTEXT_TERMS
    if any(t in q for t in non_wage_exclude):
        return None
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return None
    return detect_filing_status(question)


def detect_mixed_wage_se_missing_filing_status(question: str) -> bool:
    """Mirrors detect_compute_missing_filing_status for the mixed wages+SE
    path -- same reasoning: filing status changes which bracket table
    applies, so it's still not safe to guess."""
    q = question.lower()
    if not any(t in q for t in SE_TRIGGERS):
        return False
    if not any(t in q for t in WAGE_CONTEXT_TERMS):
        return False
    non_wage_exclude = SE_COMPLEXITY_EXCLUDE - WAGE_CONTEXT_TERMS
    if any(t in q for t in non_wage_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return detect_filing_status(question) is None


def compute_mixed_wage_se_ca_tax(conn, wage_amount: float, net_profit: float,
                                   filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    if wage_amount is None or net_profit is None or wage_amount < 0 or net_profit <= 0:
        return None
    se = compute_se_tax(net_profit)
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = wage_amount + net_profit - se["half_deduction"]
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "wage_amount": wage_amount, "net_profit": net_profit,
            "se_tax": se["se_tax"], "half_se_deduction": se["half_deduction"],
            "agi": agi, "standard_deduction": dedu["amount"]}


# --- itemized deductions (trust-the-input pattern, like SE net profit) ---
# CA Schedule CA (540) Line 29/30, verified against FTB's 2025 instructions
# (https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html):
#   - Line 30: use the GREATER of itemized deductions (line 29) or the
#     standard deduction -- EXCEPT married/RDP filing separately, where if
#     EITHER spouse itemizes, BOTH spouses must itemize (even if one
#     spouse's total is smaller than the standard deduction). This
#     assistant can't know the other spouse's choice, so MFS is excluded
#     from this path entirely (see detect_itemized_mfs_unsupported).
#   - Above an AGI threshold (2025: $252,203 single/MFS, $378,310 HOH,
#     $504,411 MFJ/QSS -- Form 540 line 13, i.e. federal AGI), itemized
#     deductions are REDUCED via a worksheet (6% of AGI over the
#     threshold, capped at 80% of the itemizable amount). This path
#     TRUSTS the user's stated itemized total as-is (same "trust the
#     input, don't derive it" pattern as self-employment NET PROFIT
#     sidestepping QBI) -- that trust is only valid BELOW this threshold,
#     so AGI at/above it defers rather than silently skipping the
#     reduction and overstating the deduction.
# "Trust the input" also means the stated figure is assumed to already be
# CA-conforming -- most importantly, NOT including state/local income
# taxes (deductible on federal Schedule A, never deductible against your
# own California return) -- disclosed explicitly in the answer text
# rather than assumed silently, since it's the single most common reason
# a taxpayer's federal Schedule A total would overstate their CA total.
ITEMIZED_AGI_LIMIT_THRESHOLD = {
    "single": 252203.0, "mfs": 252203.0, "hoh": 378310.0,
    "mfj": 504411.0, "qss": 504411.0,
}
ITEMIZED_DEDUCTION_CITATION = "FTB 2025 Instructions for Schedule CA (540) -- Line 29/30"
ITEMIZED_DEDUCTION_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

ITEMIZED_TERMS = {"itemized deduction", "itemized deductions", "itemize deductions",
                   "itemizing deductions"}

# Optional third figure for the itemized path (Schedule CA Line 5a): the
# state/local income tax (or SDI/general sales tax) portion already
# included in the stated itemized total -- California disallows this
# portion entirely, unlike federal. Purely additive/optional (see
# compute_itemized_ca_tax's salt_amount param) -- if not cleanly extracted,
# the itemized total is trusted as-is, same as before this existed.
SALT_TERMS = {"state income tax", "state and local tax", "state and local taxes",
              "state tax", "salt deduction", "state disability insurance", "sdi"}

# Optional fourth figure (Schedule CA Line 8): mortgage interest disallowed
# under FEDERAL limits that California still allows -- covers BOTH federal
# sub-rules with one fact, since both restore via the identical mechanic
# (add the disallowed amount back to the itemized total): (1) federal caps
# the mortgage-interest acquisition-debt principal at $750k/$375k-MFS,
# California still allows the pre-TCJA $1M/$500k-MFS cap; (2) federal
# suspended the deduction entirely for up to $100k/$50k-MFS of home-equity-
# indebtedness interest not used to buy/build/improve the home, California
# doesn't conform. Deliberately asks for the DISALLOWED AMOUNT directly
# (same "trust the input, don't derive it" precedent as itemized_amount/
# net self-employment profit) rather than trying to derive it from loan
# balance + origination date + proceeds-use facts, which would need a much
# larger fact-gathering conversation than this assistant's single-question
# model supports.
MORTGAGE_INTEREST_ADDBACK_TERMS = {
    "mortgage interest was limited", "mortgage interest limited",
    "mortgage interest was disallowed", "disallowed mortgage interest",
    "mortgage interest disallowed", "home equity interest disallowed",
    "home equity interest was disallowed", "disallowed home equity interest",
    "mortgage interest addback", "limited mortgage interest",
    "mortgage interest cap", "interest was disallowed", "interest was limited",
}

# Optional fifth figure (Schedule CA Part II, Lines 19-22 "Job Expenses and
# Certain Miscellaneous Deductions"): federal law suspended the pre-TCJA
# "miscellaneous itemized deductions subject to the 2% floor" category
# (unreimbursed employee expenses, tax-prep fees, certain other expenses)
# entirely; California does not conform -- it still allows this category,
# subject to the SAME 2%-of-AGI floor that applied federally before the
# TCJA suspension (IRC Section 67(a), a stable, decades-old mechanic, not a
# provision that has recently changed -- unlike most of this session's
# other Schedule CA research, no primary-source ambiguity to resolve here).
# Trusts the user's stated TOTAL of these misc. expenses (before the
# floor) -- same "trust the input" precedent as every other itemized-path
# figure -- and applies the floor itself: reinstated = max(0, misc_expenses
# - 0.02*AGI).
MISC_ITEMIZED_FLOOR_RATE = 0.02
MISC_ITEMIZED_TERMS = {
    "unreimbursed employee expense", "unreimbursed employee expenses",
    "tax preparation fee", "tax preparation fees", "tax prep fee", "tax prep fees",
    "miscellaneous itemized deduction", "miscellaneous itemized deductions",
    "misc itemized deduction", "misc itemized deductions",
    "job expenses", "job expense",
}


def compute_misc_itemized_reinstatement(misc_expenses: float, agi: float):
    """Schedule CA (540) Part II Lines 19-22 -- reinstates the pre-TCJA
    2%-of-AGI-floor miscellaneous itemized deduction category federal law
    suspended. Returns the amount to ADD to the itemized total (0 if the
    2% floor isn't cleared)."""
    if misc_expenses is None or agi is None or misc_expenses < 0 or agi < 0:
        return None
    floor = agi * MISC_ITEMIZED_FLOOR_RATE
    return max(0.0, misc_expenses - floor)


# Optional sixth figure (Schedule CA Part II, Lines 11-12 "Gifts by Cash or
# Check" / "Other than by Cash or Check"): verified against the primary
# source directly -- "California limits the amount of your deduction to
# 50% of your federal AGI" for QUALIFIED charitable contributions, for
# BOTH cash and non-cash gifts (federal's own cap varies -- 60% for cash
# post-TCJA, 50% for non-cash -- but CA's own limit is flatly 50% of AGI
# regardless, so this doesn't need to untangle which federal cap applied).
# SCOPE: only the general "qualified charitable contributions" cap --
# deliberately excludes the separate, narrower charitable CONSERVATION
# EASEMENT limit (CA 30% vs federal 50%, a genuinely different population/
# rate) and the College Access Tax Credit / disallowed-institution carve-
# outs, which stay in the ledger as their own not_applicable/narrow items.
# Trusts the user's stated charitable contribution total (same "trust the
# input" precedent as every other itemized-path figure) -- the CA-
# disallowed excess is SUBTRACTED, same direction as salt_amount.
CHARITABLE_AGI_CAP_RATE = 0.50
CHARITABLE_TERMS = {
    "charitable contribution", "charitable contributions", "charitable donation",
    "charitable donations", "charity donation", "charity donations",
    "donated to charity", "gave to charity",
}


def compute_charitable_cap(charitable_amount: float, agi: float):
    """Schedule CA (540) Part II Lines 11-12 -- caps the qualified
    charitable contribution deduction at 50% of federal AGI. Returns the
    amount to SUBTRACT from the itemized total (0 if the cap isn't
    exceeded)."""
    if charitable_amount is None or agi is None or charitable_amount < 0 or agi < 0:
        return None
    cap = agi * CHARITABLE_AGI_CAP_RATE
    return max(0.0, charitable_amount - cap)


# Optional seventh figure (Schedule CA Part II Line 5e), Tier 2's 7th and
# final item: federal law limits the SALT deduction to $40,000 ($20,000
# MFS) for the AGGREGATE of state/local income tax AND property tax.
# California does not conform. Verified against the primary source
# directly -- the instruction literally says "enter an adjustment on line
# 5e, column C for THE AMOUNT OVER THE FEDERAL LIMIT", i.e. a directly
# statable fact, not something requiring a property-tax/income-tax
# allocation. (Proved this algebraically before trusting it: net SALT
# adjustment = -salt_amount (Line 5a, income tax only) + max(0, total SALT
# paid - federal cap) (Line 5e) is IDENTICAL whether or not you separately
# know the property-tax/income-tax split of the excess -- the form's own
# framing and a from-scratch derivation starting from "CA should end up
# deducting exactly your property tax paid" agree exactly.) Same "trust
# the input" precedent as mortgage_interest_addback -- ADDED to the
# itemized total, same direction as mortgage_interest_addback and
# misc_itemized reinstatement.
SALT_CAP_ADDBACK_TERMS = {
    "salt was limited", "salt cap", "salt deduction was limited",
    "salt deduction was capped", "property tax was disallowed",
    "salt was capped", "salt deduction cap", "state and local tax was limited",
    "state and local tax cap", "over the federal salt limit",
}

# Optional eighth figure (Schedule CA (540) Part II Line 15): verified
# against FTB's own text -- "Under federal law, the personal casualty
# and theft loss deduction is suspended, with exception for personal
# casualty gains... California law does not conform. California allows
# personal casualty and theft loss and disaster loss deductions. If you
# have personal casualty and theft loss and/or disaster loss, complete
# another federal Form 4684, Casualties and Thefts, using California
# amounts." Post-TCJA federal law only allows this deduction for losses
# in a FEDERALLY DECLARED disaster area; California allows it
# regardless -- an ordinary theft/accident loss with no disaster
# declaration at all. Same non-conformity direction as mortgage_
# interest_addback/misc_itemized_expenses/salt_cap_addback -- ADDED to
# the itemized total.
#
# TWO-FACT DESIGN (a deliberate middle ground, not blind trust-the-input
# and not a full per-event rebuild): IRS Pub. 547 confirms the federal
# Form 4684 computation has THREE floors -- (1) a $100-per-casualty-
# EVENT floor, (2) netting against insurance/other reimbursement PER
# EVENT, both requiring per-event data this system genuinely cannot
# collect in one question (same conclusion already reached for the
# disaster-loss-carryover feature's ORIGINATING computation -- see that
# module's note: "this feature does NOT touch that per-item Form 4684
# computation... those floors were already baked into the loss when it
# originated") -- and (3) a 10%-of-AGI floor applied to the YEARLY TOTAL
# of all events combined, which is NOT per-event and depends on exactly
# two clean facts this system already asks for (a stated loss total,
# AGI). (1) and (2) stay pushed onto the taxpayer's single stated
# figure (casualty_loss_amount is defined as ALREADY net of the $100
# floor and insurance reimbursement -- same trust boundary as
# charitable_amount/capital_loss elsewhere in this codebase); (3) is
# computed here rather than trusted, since it's the one piece a
# taxpayer's self-reported "final number" is most likely to get wrong
# (confusing federal vs. CA AGI, or an arithmetic slip on a subtraction
# most people do by hand) -- directly mirroring compute_charitable_cap/
# compute_misc_itemized_reinstatement's existing shape (stated pre-floor
# amount in, system-computed AGI-based floor applied, deduction out).
#
# $100/10% FLOORS CONFIRMED CURRENT FOR TY2025 (verified directly, not
# assumed): OBBBA's casualty-loss changes (expanding eligible disasters
# from federal-only to federal-or-state-declared) take effect for tax
# years beginning after 12/31/2025 -- i.e. TY2026 forward, not TY2025 --
# and even then leave the $100/10% figures themselves unchanged
# ("deduction limits remain in place").
CASUALTY_LOSS_AGI_FLOOR_RATE = 0.10
CASUALTY_LOSS_TERMS = {
    "casualty loss", "theft loss", "casualty and theft loss",
    "casualty/theft loss", "casualty or theft loss",
}


def compute_casualty_loss_floor(casualty_loss_amount: float, agi: float):
    """Schedule CA (540) Part II Line 15 -- floors the taxpayer's stated
    casualty/theft loss (already net of the federal $100-per-event floor
    and any insurance/other reimbursement -- see module note above) at
    10% of AGI (IRS Pub. 547's "10% Rule", applied to the yearly TOTAL
    of all events combined, not per-event). Returns the amount to ADD to
    the itemized total (0 if the 10% floor isn't cleared)."""
    if casualty_loss_amount is None or agi is None or casualty_loss_amount < 0 or agi < 0:
        return None
    floor = agi * CASUALTY_LOSS_AGI_FLOOR_RATE
    return max(0.0, casualty_loss_amount - floor)


def _itemized_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in ITEMIZED_TERMS):
        return False
    other_exclude = COMPLEXITY_EXCLUDE - {"itemize", "itemized"}
    if any(t in q for t in other_exclude):
        return False
    if "capital gain" in q and any(t in q for t in HOME_SALE_TERMS):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_itemized_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'wage income with
    a stated itemized-deduction total' question, using the same narrower-
    exclude-set pattern as detect_self_employment_signal (itemize terms are
    the TRIGGER, not a disqualifier, here). Excludes MFS -- see module note
    above -- and NOT-a-filing-status (returns None, same as every other
    detect_*_signal, so the caller can distinguish 'missing' from 'MFS
    unsupported')."""
    q = question.lower()
    if not _itemized_base_signal_ok(q):
        return None
    fs = detect_filing_status(question)
    if fs == "mfs":
        return None
    return fs


def detect_itemized_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _itemized_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_itemized_mfs_unsupported(question: str) -> bool:
    q = question.lower()
    if not _itemized_base_signal_ok(q):
        return False
    return detect_filing_status(question) == "mfs"


def compute_itemized_deduction_phaseout(itemized_amount: float, agi: float, filing_status: str):
    """Schedule CA (540), Part II, Line 29 "Itemized Deductions Worksheet" --
    verified against FTB's 2025 instructions (the exact 10-step worksheet,
    not a secondhand summary): once AGI exceeds the filing-status threshold,
    itemized deductions are reduced by the SMALLER of (a) 80% of the
    "reducible" itemized total, or (b) 6% of the AGI over the threshold.

    SIMPLIFICATION, disclosed to the caller (not hidden): the worksheet's
    "reducible" amount excludes medical expenses (Sched A line 4),
    investment interest (line 9), casualty/theft losses (line 15), and
    gambling losses -- this treats the ENTIRE itemized total as reducible,
    which is exactly correct for the common case (a taxpayer whose itemized
    deductions are just SALT + mortgage interest + charitable, none of
    which are on the excluded list) and only an approximation for someone
    who ALSO has one of those less-common deduction types mixed in. This
    can only ever OVERSTATE the reduction relative to the true worksheet
    (since the true 80%-of-reducible term can only be smaller when items
    are excluded, and min() of a same-or-larger first term against an
    unchanged second term is never smaller) -- i.e. it can only make the
    computed tax a slight OVERestimate, never an underestimate.

    Returns None if AGI is at/below the threshold (no reduction applies --
    caller should just use itemized_amount as-is)."""
    threshold = ITEMIZED_AGI_LIMIT_THRESHOLD.get(filing_status)
    if threshold is None or agi <= threshold or itemized_amount <= 0:
        return None
    reducible_cap = itemized_amount * 0.80
    excess_agi_cap = (agi - threshold) * 0.06
    reduction = min(reducible_cap, excess_agi_cap)
    return {
        "reduction": reduction,
        "reduced_itemized": max(0.0, itemized_amount - reduction),
        "threshold": threshold,
    }


def compute_itemized_ca_tax(conn, income_amount: float, itemized_amount: float,
                              filing_status: str, tax_year: int = DEFAULT_TAX_YEAR,
                              salt_amount: float = None, mortgage_interest_addback: float = None,
                              misc_itemized_expenses: float = None, charitable_amount: float = None,
                              salt_cap_addback: float = None, casualty_loss_amount: float = None):
    """income_amount is treated as both gross wage income and California AGI
    (no other adjustments -- same simple-case assumption as the plain
    wage-earner compute path). Applies the Line 29 phase-out worksheet when
    AGI exceeds the filing-status threshold (see
    compute_itemized_deduction_phaseout) instead of silently deferring.
    `salt_amount`, if given, is the state/local income tax (or SDI/general
    sales tax) portion already included in itemized_amount -- California
    disallows that portion entirely (Schedule CA Line 5a), so it's
    subtracted out BEFORE the phase-out worksheet runs, same order as the
    real form (Part II adjustments happen before Line 29). `mortgage_
    interest_addback`, if given, is mortgage interest disallowed under
    FEDERAL limits (acquisition-debt cap or home-equity-indebtedness
    suspension) that California still allows -- see
    MORTGAGE_INTEREST_ADDBACK_TERMS's docstring -- added BACK to the
    itemized total, the opposite direction from salt_amount.
    `misc_itemized_expenses`, if given, is the taxpayer's stated TOTAL
    unreimbursed-employee/tax-prep/other misc. expenses BEFORE the 2%-of-
    AGI floor (Schedule CA Lines 19-22) -- see
    compute_misc_itemized_reinstatement -- the floored amount is added to
    the itemized total, same direction as mortgage_interest_addback.
    `charitable_amount`, if given, is the taxpayer's stated qualified
    charitable contribution total already included in itemized_amount --
    see compute_charitable_cap -- the CA-disallowed excess over 50% of AGI
    is subtracted, same direction as salt_amount. `salt_cap_addback`, if
    given, is the amount the taxpayer's federal SALT deduction was reduced
    by the $40,000/$20,000-MFS federal cap (Schedule CA Line 5e) -- see
    SALT_CAP_ADDBACK_TERMS's docstring for why this is trusted directly
    rather than derived from a property-tax/income-tax split -- added
    BACK to the itemized total, same direction as mortgage_interest_
    addback. `casualty_loss_amount`, if given, is the taxpayer's stated
    personal casualty/theft loss (Schedule CA Line 15) ALREADY net of
    the federal $100-per-event floor and insurance reimbursement -- see
    compute_casualty_loss_floor for the 10%-of-AGI floor this function
    applies -- the floored amount is added to the itemized total, same
    direction as mortgage_interest_addback. If none of these are given,
    itemized_amount is trusted as already CA-conforming, same as before
    these parameters existed."""
    if income_amount is None or itemized_amount is None or income_amount < 0 or itemized_amount < 0:
        return None
    if salt_amount is not None:
        if salt_amount < 0 or salt_amount > itemized_amount:
            return None
        itemized_amount = itemized_amount - salt_amount
    if mortgage_interest_addback is not None:
        if mortgage_interest_addback < 0:
            return None
        itemized_amount = itemized_amount + mortgage_interest_addback
    misc_reinstated = None
    if misc_itemized_expenses is not None:
        if misc_itemized_expenses < 0:
            return None
        misc_reinstated = compute_misc_itemized_reinstatement(misc_itemized_expenses, income_amount)
        itemized_amount = itemized_amount + misc_reinstated
    charitable_disallowed = None
    if charitable_amount is not None:
        if charitable_amount < 0:
            return None
        charitable_disallowed = compute_charitable_cap(charitable_amount, income_amount)
        if charitable_disallowed > itemized_amount:
            return None
        itemized_amount = itemized_amount - charitable_disallowed
    if salt_cap_addback is not None:
        if salt_cap_addback < 0:
            return None
        itemized_amount = itemized_amount + salt_cap_addback
    casualty_deductible = None
    if casualty_loss_amount is not None:
        if casualty_loss_amount < 0:
            return None
        casualty_deductible = compute_casualty_loss_floor(casualty_loss_amount, income_amount)
        itemized_amount = itemized_amount + casualty_deductible
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    phaseout = compute_itemized_deduction_phaseout(itemized_amount, income_amount, filing_status)
    ca_itemized_amount = phaseout["reduced_itemized"] if phaseout else itemized_amount
    used_itemized = ca_itemized_amount > dedu["amount"]
    deduction_used = ca_itemized_amount if used_itemized else dedu["amount"]
    taxable_income = max(0.0, income_amount - deduction_used)
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "income_amount": income_amount, "itemized_amount": itemized_amount,
            "salt_amount": salt_amount, "mortgage_interest_addback": mortgage_interest_addback,
            "misc_itemized_expenses": misc_itemized_expenses, "misc_reinstated": misc_reinstated,
            "charitable_amount": charitable_amount, "charitable_disallowed": charitable_disallowed,
            "salt_cap_addback": salt_cap_addback,
            "casualty_loss_amount": casualty_loss_amount, "casualty_deductible": casualty_deductible,
            "phaseout": phaseout, "ca_itemized_amount": ca_itemized_amount,
            "standard_deduction": dedu["amount"], "used_itemized": used_itemized,
            "deduction_used": deduction_used}


# --- capital losses (annual offset limit, same conformity pattern) -------
# Verified against FTB's 2025 Instructions for California Schedule D (540)
# (https://www.ftb.ca.gov/forms/2025/2025-540-d-instructions.html), Line 9:
# "If line 8 is a net capital loss, enter the smaller of the loss on line 8
# or $3,000 ($1,500 if you are married or an RDP filing separately)." This
# is the SAME $3,000/$1,500 annual limit as federal (IRC Section 1211),
# unlike itemized deductions there is no MFS forced-consistency wrinkle
# here -- MFS just uses the halved limit, so (unlike the itemized path)
# MFS does NOT need to be excluded.
# ONE THING DELIBERATELY LEFT OUT OF SCOPE: Schedule D (540) Line 6 lets a
# taxpayer carry over an UNUSED loss from a PRIOR year -- and that CA
# carryover can differ from the federal one (different basis/depreciation
# rules in years before the loss was realized), tracked on the "California
# Capital Loss Carryover Worksheet". This path only handles a CURRENT-YEAR
# loss with no prior-year carryover mentioned -- "trust the input" here
# means trusting that the stated loss figure is this year's loss only, not
# a multi-year running total the system would have to reconcile.
CAPITAL_LOSS_TERMS = {"capital loss", "capital losses"}
CAPITAL_LOSS_LIMIT_MFS = 1500.0
CAPITAL_LOSS_LIMIT_OTHER = 3000.0
CAPITAL_LOSS_CITATION = "FTB 2025 Instructions for California Schedule D (540) -- Line 9"
CAPITAL_LOSS_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-d-instructions.html"


def _capital_loss_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in CAPITAL_LOSS_TERMS):
        return False
    if "capital gain" in q:
        return False   # netting a gain against a loss in one question is out of scope
    other_exclude = COMPLEXITY_EXCLUDE - {"capital loss"}
    if any(t in q for t in other_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_capital_loss_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'income with a
    stated current-year capital loss' question -- same narrower-exclude-set
    pattern as detect_itemized_signal (capital-loss terms are the TRIGGER
    here, not a disqualifier). Unlike itemized deductions, MFS is a normal
    case here (just a smaller limit), not excluded."""
    q = question.lower()
    if not _capital_loss_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_capital_loss_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _capital_loss_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_capital_loss_ca_tax(conn, income_amount: float, loss_amount: float,
                                  filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as both gross income and California AGI
    before the loss offset (no other adjustments -- same simple-case
    assumption as every other compute path). Only the ANNUAL-LIMIT portion
    of the loss ($3,000, or $1,500 MFS) offsets income this year; any
    excess is disclosed as a carryover amount, not applied."""
    if income_amount is None or loss_amount is None or income_amount < 0 or loss_amount <= 0:
        return None
    limit = CAPITAL_LOSS_LIMIT_MFS if filing_status == "mfs" else CAPITAL_LOSS_LIMIT_OTHER
    deductible_loss = min(loss_amount, limit)
    carryover = max(0.0, loss_amount - deductible_loss)
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = max(0.0, income_amount - deductible_loss)
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "income_amount": income_amount, "loss_amount": loss_amount,
            "deductible_loss": deductible_loss, "carryover": carryover,
            "standard_deduction": dedu["amount"]}


# --- excess business loss limitation (Ring 3 extension, IRC Section 461(l)
# / FTB Form 3461) -- verified against FTB's 2025 Form 3461 Instructions
# (https://www.ftb.ca.gov/forms/2025/2025-3461-instructions.pdf) and the
# 2025 Schedule CA (540) instructions. California does NOT conform to the
# CURRENT federal version of this limitation (federal 461(l) was off for
# 2018-2020, then reinstated/extended by ARPA/the Inflation Reduction Act
# through 2028; FTB's instructions state directly that California does not
# conform to those federal extensions) -- CA has run its OWN continuous
# version since TCJA, with its own state-set (not federally-indexed) dollar
# threshold each year: 2025 is $313,000 (single/MFS/HOH), $626,000
# (MFJ/RDP joint).
#
# QSS THRESHOLD: Form 3461's own definitions section states only "single,
# head of household, or married/RDP filing separately" ($313,000) and
# "married/RDP taxpayers filing a joint return" ($626,000) -- it does not
# enumerate "Qualifying Surviving Spouse/RDP" by name. Resolved from THIS
# codebase's own established precedent rather than re-deriving from
# scratch: EVERY other CA filing-status dollar threshold already built here
# groups QSS with MFJ at the higher figure -- the standard deduction and
# the Schedule Y bracket table itself (both load_income_content.py), the
# itemized-deduction AGI phase-out threshold (ITEMIZED_AGI_LIMIT_THRESHOLD
# above, "$504,411 MFJ/QSS"), and a credit threshold in income_credits.py
# all treat qss identically to mfj. This matches general tax-law structure
# too: a QSS filer already computes tax using the MFJ/joint rate schedule
# (Schedule Y), so a rate-schedule-linked dollar threshold following the
# same pairing is the well-supported reading, not a guess -- but it IS a
# documented inference from a source that doesn't spell QSS out by name,
# unlike the other four figures, so it stays flagged here rather than
# presented as independently re-verified.
EXCESS_BUSINESS_LOSS_THRESHOLD = {
    "single": 313000.0, "mfs": 313000.0, "hoh": 313000.0,
    "mfj": 626000.0, "qss": 626000.0,
}
EXCESS_BUSINESS_LOSS_CITATION = "FTB 2025 Form 3461 Instructions -- Section C, Definitions"
EXCESS_BUSINESS_LOSS_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3461-instructions.pdf"

# Trust the taxpayer's stated AGGREGATE net business loss as a single
# figure (same "trust the input, don't derive it" precedent as SE net
# profit / itemized_amount / k1_amount) -- the real Form 3461 Part I/Part
# II worksheet nets MANY separate income/loss lines together (Schedule C,
# Schedule D business-portion gains/losses, Form 4797, Schedule E rental/
# royalty/K-1, Schedule F farm income) to reach this one number; this path
# does not attempt to derive that net from separately-stated components.
# Deliberately does NOT exclude business-type vocabulary the way the plain
# wage-only path does (s-corp/LLC/partnership/K-1/Schedule C/Schedule E are
# all legitimate SOURCES of the one aggregate figure here, not
# disqualifiers) -- a narrower, TARGETED exclude list instead, same
# discipline as itemized/capital-loss's own "other_exclude minus the
# trigger term" pattern.
EXCESS_BUSINESS_LOSS_TERMS = {
    "excess business loss", "aggregate business loss", "net business loss",
    "business loss",
}
EXCESS_BUSINESS_LOSS_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "dependent", "alimony",
    "gambling", "gambled", "betting", "wagering",
    "capital gain", "capital loss", "stock", "rsu",
    # added after the duplicate-value-extraction bug fix (2026-08-22)
    # made this a live collision, not just a theoretical one: SE_
    # COMPLEXITY_EXCLUDE/K1_COMPLEXITY_EXCLUDE already step the self-
    # employment/K-1 paths ASIDE for business-loss phrasing (so a stated
    # loss isn't silently treated as positive profit), on the assumption
    # the excess-business-loss path picks it up instead -- but for
    # phrasing like "$700,000 self-employed with a $700,000 business
    # loss" (self-employment income AND the loss described via the SAME
    # figure, no separately-stated "other income" at all), this path has
    # no genuinely separate figure to extract either. Once the shared
    # extraction helper was fixed to find each anchor by POSITION rather
    # than value, it started successfully (but wrongly) treating the
    # self-employment mention as a distinct "other income" fact. Mirrors
    # K1_COMPLEXITY_EXCLUDE's own self-employment exclusion.
    "self-employ", "self employ", "1099",
}


def _excess_business_loss_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in EXCESS_BUSINESS_LOSS_TERMS):
        return False
    if any(t in q for t in EXCESS_BUSINESS_LOSS_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_excess_business_loss_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'other income
    with a stated aggregate business loss' question -- same narrower-
    exclude-set pattern as detect_capital_loss_signal (business-loss terms
    are the TRIGGER here, not a disqualifier)."""
    q = question.lower()
    if not _excess_business_loss_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_excess_business_loss_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _excess_business_loss_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_excess_business_loss(business_loss_amount: float, filing_status: str):
    """Form 3461 Parts I-III collapsed to their end result: the aggregate
    net business loss (trusted as one stated figure -- see module note
    above) is capped at the filing-status threshold. Loss AT OR UNDER the
    threshold is not limited at all (fully deductible against other
    income, same as any other loss) -- Form 3461 itself only produces a
    nonzero "excess" once the loss EXCEEDS the threshold. The excess
    becomes a carryover EBL for next year (Schedule CA (540) Line 8z when
    later absorbed) -- NOT an NOL carryover (FTB: "any disallowed loss is
    treated as a carryover excess business loss instead of an NOL
    carryover"), so it stays unaffected by the separate 2024-2026 NOL
    suspension rule if/when that gets built. This module does not track
    the carryover into a future year's computation (same "current year
    only" scope as compute_capital_loss_ca_tax's own carryover note)."""
    if business_loss_amount is None or business_loss_amount <= 0:
        return None
    threshold = EXCESS_BUSINESS_LOSS_THRESHOLD.get(filing_status)
    if threshold is None:
        return None
    allowed_loss = min(business_loss_amount, threshold)
    excess = max(0.0, business_loss_amount - threshold)
    return {"threshold": threshold, "allowed_loss": allowed_loss, "excess_business_loss": excess}


def compute_excess_business_loss_ca_tax(conn, income_amount: float, business_loss_amount: float,
                                          filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is OTHER (non-business-loss) income -- e.g. wages --
    treated as gross income and California AGI before the loss offset, no
    other adjustments (same simple-case assumption as every other compute
    path). Only the EBL-limited allowed_loss offsets this year; any excess
    is disclosed as a carryforward, not applied."""
    if income_amount is None or income_amount < 0:
        return None
    ebl = compute_excess_business_loss(business_loss_amount, filing_status)
    if not ebl:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = max(0.0, income_amount - ebl["allowed_loss"])
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "income_amount": income_amount, "business_loss_amount": business_loss_amount,
            "threshold": ebl["threshold"], "allowed_loss": ebl["allowed_loss"],
            "excess_business_loss": ebl["excess_business_loss"],
            "standard_deduction": dedu["amount"]}


# --- Excess business loss CARRYOVER absorption (Schedule CA (540) Line
# 8z) -- verified against FTB's 2025 Schedule CA (540) instructions:
# "Excess business losses carryover from prior years -- If in the
# current year, the taxpayer has enough business income to fully offset
# all of the excess business loss carryover from prior year, then the
# carryover balance is applied to offset the business income. Refer to
# form FTB 3461 instructions for line 14b and line 15 for further
# instructions. Enter the excess business losses carryover from prior
# years on line 8z, column B..." Cross-referenced against Line 8p's own
# instruction above: "any disallowed loss will be treated as a carryover
# excess business loss INSTEAD OF an NOL carryover" -- so, unlike NOL
# carryforward (Line 8a-general, still deferred: a real MTI/suspension
# multi-year recomputation), there is nothing analogous to replicate
# here. This is genuinely a "trust the stated prior-year balance"
# extension of the ALREADY-BUILT Line 8p threshold formula, same "trust
# the input" precedent as capital-loss carryover (CAPITAL_LOSS_CARRYOVER
# above).
#
# TWO CASES MODELED, per FTB's own text:
#   FULL ABSORPTION (this year's business INCOME >= the stated carryover
#     balance): FTB's own words describe exactly this case -- a flat,
#     uncapped, dollar-for-dollar Line 8z subtraction of the whole
#     carryover balance. No threshold test applies (the taxpayer isn't
#     generating a NEW excess this year, just using up an old one).
#   THIS-YEAR LOSS (the current year's business result is ITSELF a
#     loss): the current-year loss and the carryover balance combine,
#     then get run through the EXACT SAME threshold formula already
#     verified for Line 8p (compute_excess_business_loss) -- Form 3461
#     Part III reapplies that identical cap to the combined figure, per
#     FTB's own cross-reference. Reuses existing, already-verified code
#     rather than new guesswork.
# ONE CASE DELIBERATELY LEFT OUT (returns None; caller routes to a
# specific needs_review message, not a guess): this year's business
# INCOME is positive but LESS than the carryover balance (partial
# absorption). FTB's Line 8z paragraph doesn't spell out this middle
# case in its own text -- it defers to "form FTB 3461 instructions for
# line 14b and line 15," a PDF worksheet not independently fetched this
# session (see [[claude-desktop-pdf-navigation-crash]] for why direct
# FTB PDF navigation is avoided). Rather than guess at an unverified
# worksheet's arithmetic, this stays unmodeled.
EBL_CARRYOVER_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 8z"
EBL_CARRYOVER_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

EBL_CARRYOVER_TERMS = {
    "excess business loss carryover", "excess business losses carryover",
    "business loss carryover from prior year", "business loss carryover",
    "prior year excess business loss", "carryover excess business loss",
    "excess business loss carryover from prior years",
}
# Anchor phrases for THIS YEAR's business result, split by sign -- which
# set matches determines is_loss_year, and the SAME set anchors the
# proximity search for that dollar figure. Deliberately distinct wording
# from EBL_CARRYOVER_TERMS above (no "business loss carryover"-style
# substring overlap), since a single question states BOTH this year's
# result AND the prior-year carryover as separate dollar figures.
EBL_CARRYOVER_INCOME_TERMS = {
    "business income this year", "business profit this year",
    "this year's business income", "this year's business profit",
    "current year business income", "current year business profit",
}
EBL_CARRYOVER_LOSS_TERMS = {
    "business loss this year", "another business loss this year",
    "this year's business loss", "current year business loss",
}
EBL_CARRYOVER_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "dependent", "alimony",
    "gambling", "gambled", "betting", "wagering",
    "capital gain", "capital loss", "stock", "rsu",
}


def _ebl_carryover_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in EBL_CARRYOVER_TERMS):
        return False
    if any(t in q for t in EBL_CARRYOVER_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_ebl_carryover_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'other income
    + this year's business result + a stated prior-year excess-business-
    loss carryover balance' question."""
    q = question.lower()
    if not _ebl_carryover_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_ebl_carryover_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _ebl_carryover_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_ebl_carryover_is_loss_year(question: str) -> bool:
    """True iff the question describes THIS YEAR's business result as a
    loss (routes to the combine-and-recap branch) rather than income
    (the full-absorption branch, the default when absent)."""
    q = question.lower()
    return any(t in q for t in EBL_CARRYOVER_LOSS_TERMS)


def compute_ebl_carryover_ca_tax(conn, other_income: float, business_result: float,
                                   carryover_balance: float, is_loss_year: bool,
                                   filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """other_income is non-business income (e.g. wages), treated as
    AGI-equivalent before this adjustment (no other adjustments, same
    simplification used throughout). business_result is this year's
    business INCOME (if is_loss_year is False) or this year's business
    LOSS (if True) -- always stated as a positive figure, sign implied
    by is_loss_year. carryover_balance is the stated prior-year excess-
    business-loss carryover (a positive figure). Returns None for the
    partial-absorption case (income year, business_result <
    carryover_balance) that FTB defers to an unverified PDF worksheet --
    see module note above; the caller routes that to a dedicated
    needs_review message rather than treating it the same as a genuine
    extraction failure."""
    if other_income is None or other_income < 0:
        return None
    if business_result is None or business_result < 0:
        return None
    if carryover_balance is None or carryover_balance <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    if is_loss_year:
        combined_loss = business_result + carryover_balance
        ebl = compute_excess_business_loss(combined_loss, filing_status)
        if not ebl:
            return None
        agi = max(0.0, other_income - ebl["allowed_loss"])
        taxable_income = max(0.0, agi - dedu["amount"])
        calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
        if not calc:
            return None
        return {**calc, "other_income": other_income, "business_result": business_result,
                "carryover_balance": carryover_balance, "is_loss_year": True,
                "combined_loss": combined_loss, "threshold": ebl["threshold"],
                "allowed_loss": ebl["allowed_loss"],
                "new_excess_business_loss": ebl["excess_business_loss"],
                "agi": agi, "standard_deduction": dedu["amount"]}
    if business_result < carryover_balance:
        return None
    agi = max(0.0, other_income + business_result - carryover_balance)
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "other_income": other_income, "business_result": business_result,
            "carryover_balance": carryover_balance, "is_loss_year": False,
            "agi": agi, "standard_deduction": dedu["amount"]}


# --- CA NOL (net operating loss) carryover deduction SUSPENSION (Ring 3
# extension, R&TC Section 17276.24 / FTB Form 3805V) -- verified against
# FTB's 2025 Instructions for Form 3805V
# (https://www.ftb.ca.gov/forms/2025/2025-3805v-instructions.html) and the
# 2025 Schedule CA (540) instructions' identical NOL Suspension paragraph.
#
# THE RULE (an OR test for the EXEMPTION, equivalently an AND test for
# suspension): "The NOL carryover deduction is suspended for the 2024,
# 2025, and 2026 taxable years, if your net business income is $1,000,000
# or more AND modified AGI is $1,000,000 or more." "However, taxpayers
# with net business income OR modified AGI of less than $1,000,000 ... are
# not affected by the NOL suspension rules." Suspended means the ENTIRE
# carryover deduction is disallowed THIS year (not partially reduced) --
# the full amount simply carries forward (with its carryover period
# extended by 1-3 years depending on when the loss was incurred, to make
# up for the suspended year -- disclosed, not computed, see below).
#
# SCOPE SIMPLIFICATION (disclosed in the answer text, not assumed away
# silently): FTB tests "net business income" and "modified AGI" as two
# SEPARATE figures -- net business income is normally only a COMPONENT of
# AGI, not identical to it (AGI also nets in other income sources and
# above-the-line adjustments like the deductible half of self-employment
# tax). This path assumes the taxpayer's ONLY income is their stated
# business income figure, with no other income or adjustments -- same
# "sole income source" scope discipline as the self-employment-only and
# K-1-only paths -- which makes that ONE stated figure stand in for BOTH
# halves of FTB's test. This is exact when the assumption holds; if the
# taxpayer actually has other income or adjustments this path doesn't know
# about, true modified AGI could differ from the figure used here, which
# could change the suspension outcome in either direction.
#
# NO PERCENTAGE CAP when not suspended (genuinely different from federal
# post-2017 law's 80%-of-taxable-income cap): "This is the maximum NOL
# carryover deduction you are allowed" is MTI (Modified Taxable Income)
# itself -- California allows 100% of MTI, dollar for dollar, for general
# NOLs (losses incurred 2008+, the only vintage this path models).
#
# NOT MODELED, disclosed: (1) the disaster-loss-carryover carveout (R&TC
# 17207.14) -- taxpayers with disaster loss carryovers are exempt from
# suspension regardless of income, a narrow population this path doesn't
# ask about, so it assumes an ORDINARY business NOL, not a disaster loss;
# (2) the exact extended carryforward-period bookkeeping for a suspended
# year's loss (disclosed only as "carries forward", not an exact year
# count); (3) pre-2008 NOLs (different carryover-period/usable-percentage
# rules under the NOL Carryover table -- this models general post-2008
# NOLs only, same population every other 20-year-carryover feature in
# this codebase assumes).
NOL_THRESHOLD = 1000000.0
NOL_CITATION = "FTB 2025 Instructions for Form 3805V -- NOL Suspension / General Information"
NOL_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3805v-instructions.html"

NOL_TERMS = {
    "net operating loss carryover", "net operating loss deduction",
    "nol carryover", "nol deduction", "net operating loss",
}
# Wage-context terms excluded here (unlike excess-business-loss, which
# treats them as the SECOND figure) -- this path has no mixed-income
# variant, so a stated wage figure alongside a business-income/NOL
# question means real complexity (does the $1M test income-source
# distinction actually matter now?) this MVP doesn't attempt; same
# "business loss" exclusion as excess-business-loss's own module note,
# since an EBL carryover and an NOL carryover are explicitly DIFFERENT
# buckets per FTB (see excess_business_loss's module note) -- mixing both
# in one question is deferred rather than guessed at.
NOL_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "dependent", "alimony",
    "gambling", "gambled", "betting", "wagering",
    "capital gain", "capital loss", "stock", "rsu",
    "disaster loss", "disaster",
    "wage", "wages", "salary", "salaried", "w-2", "w2",
    "business loss", "excess business loss",
}


def _has_nol_term(q: str) -> bool:
    return any(t in q for t in NOL_TERMS) or re.search(r"\bnol\b", q) is not None


def _nol_base_signal_ok(q: str) -> bool:
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_nol_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'business
    income with a stated NOL carryover deduction' question. Mirrors
    detect_excess_business_loss_signal's shape."""
    q = question.lower()
    if not _nol_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_nol_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _nol_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_nol_ca_tax(conn, business_income: float, nol_carryover_amount: float,
                        filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """business_income is treated as the taxpayer's ONLY income -- both
    "net business income" and "modified AGI" for the suspension test (see
    module note above on this simplification and its disclosed limits).

    Suspended (business_income >= $1,000,000): the ENTIRE carryover is
    disallowed this year -- nol_deduction=0, full nol_carryover_amount
    preserved as remaining_carryover.

    Not suspended: nol_deduction = min(nol_carryover_amount, MTI), where
    MTI (Modified Taxable Income) is taxable income BEFORE the NOL
    deduction (business income minus the standard deduction, floored at
    0) -- no percentage cap, unlike federal law post-2017."""
    if business_income is None or business_income <= 0:
        return None
    if nol_carryover_amount is None or nol_carryover_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    mti = max(0.0, business_income - dedu["amount"])
    suspended = business_income >= NOL_THRESHOLD
    nol_deduction = 0.0 if suspended else min(nol_carryover_amount, mti)
    remaining_carryover = nol_carryover_amount - nol_deduction
    taxable_income = max(0.0, mti - nol_deduction)
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "business_income": business_income, "nol_carryover_amount": nol_carryover_amount,
            "suspended": suspended, "nol_deduction": nol_deduction,
            "remaining_carryover": remaining_carryover, "mti": mti,
            "standard_deduction": dedu["amount"]}


# --- NOL carryover for a WAGE-ONLY filer with NO current-year business
# income (Schedule CA (540) Line 8a, the "wages/other income" population
# the schedule_ca_inventory.py ledger's own "Line 8a-general" row left
# deferred as "real new scope... would require generalizing the MTI/
# suspension test beyond business-income-only"). Found tractable via a
# closer look at the SAME suspension rule already verified and cited
# above for compute_nol_ca_tax -- no new FTB research needed, since the
# rule itself was already primary-sourced; what changed is recognizing
# what it implies for THIS population specifically.
#
# THE KEY INSIGHT: FTB's suspension test is explicitly an AND condition
# -- "suspended... if your net business income is $1,000,000 or more AND
# modified AGI is $1,000,000 or more." A taxpayer whose business has
# CLOSED (no current-year business income at all) has net business
# income of exactly $0 -- which can never satisfy ">= $1,000,000",
# regardless of wage level. The suspension clause is therefore
# STRUCTURALLY INERT for this population: the NOL deduction is NEVER
# suspended for a wage-only filer with a carryover from a now-closed
# business, at any income level. This isn't a simplifying assumption --
# it falls directly out of the AND test's own logic, the same kind of
# structural-guarantee reasoning already used for the AMT screen (there,
# a rate/exemption relationship guaranteed a $0 result; here, a $0
# business-income component guarantees the AND test can never trigger).
#
# SCOPE: requires an EXPLICIT confirmation that no current-year business
# income exists (a "closed business"/"wages only" signal) -- NEVER
# inferred from silence, same "don't guess toward understatement... or
# overstatement" discipline as every other eligibility-gated feature
# this session. Any signal of ONGOING self-employment/business income
# routes elsewhere (the existing compute_nol_ca_tax path, or a defer) --
# for that population, business income could genuinely be nonzero, and
# the suspension test's real complexity (this session's original
# deferral reasoning) still applies unchanged.
#
# MTI computed from WAGES (not business income) -- same "stated income
# figure stands in for modified AGI" simplification already used by
# compute_nol_ca_tax's business-income case, same disclosed limitation
# (true MAGI could differ if other income/adjustments exist this path
# doesn't know about).
NOL_WAGES_CLOSED_BUSINESS_TERMS = {
    "closed my business", "closed business", "former business", "business closed",
    "no longer have a business", "sold my business", "business is now closed",
    "used to have a business", "business has closed", "shut down my business",
    "wages only", "only wages", "no business income this year", "no current business income",
    "don't have a business anymore", "do not have a business anymore",
}
NOL_WAGES_ONGOING_BUSINESS_EXCLUDE_TERMS = {
    "self-employ", "self employ", "1099", "s-corp", "s corp", "llc", "partnership",
    "freelance", "freelancing", "contractor", "contracting", "contracted",
    "sole proprietor", "k-1", "schedule c", "schedule e",
    "business income this year", "current business income", "still run", "still operate",
    "excess business loss", "business loss",
}
NOL_WAGES_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "dependent", "alimony",
    "gambling", "gambled", "betting", "wagering",
    "capital gain", "capital loss", "stock", "rsu", "trust", "estate",
    "disaster loss", "disaster",
}


def _nol_wages_base_signal_ok(q: str) -> bool:
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_WAGES_ONGOING_BUSINESS_EXCLUDE_TERMS):
        return False
    if not any(t in q for t in NOL_WAGES_CLOSED_BUSINESS_TERMS):
        return False
    if any(t in q for t in NOL_WAGES_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_nol_wages_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'wage-only
    income, no current business, with a federal NOL carryover from a
    now-closed business' question -- requires an EXPLICIT no-current-
    business-income confirmation, never assumed from silence."""
    q = question.lower()
    if not _nol_wages_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_nol_wages_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _nol_wages_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_nol_wages_ambiguous(question: str) -> bool:
    """True iff NOL vocabulary is present but neither a closed-business
    confirmation nor an ongoing-business signal is stated -- routes to a
    dedicated clarifying question rather than assuming either way,
    since which case applies changes whether suspension can even apply."""
    q = question.lower()
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_WAGES_ONGOING_BUSINESS_EXCLUDE_TERMS):
        return False
    if any(t in q for t in NOL_WAGES_CLOSED_BUSINESS_TERMS):
        return False
    return True


def compute_nol_wages_ca_tax(conn, wages: float, nol_carryover_amount: float,
                               filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """See module note above -- suspension is structurally impossible
    for this population (net business income = $0 can never satisfy the
    suspension AND test's ">= $1,000,000" business-income leg), so this
    path has no suspended branch at all, unlike compute_nol_ca_tax."""
    if wages is None or wages < 0:
        return None
    if nol_carryover_amount is None or nol_carryover_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    mti = max(0.0, wages - dedu["amount"])
    nol_deduction = min(nol_carryover_amount, mti)
    remaining_carryover = nol_carryover_amount - nol_deduction
    taxable_income = max(0.0, mti - nol_deduction)
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "wages": wages, "nol_carryover_amount": nol_carryover_amount,
            "nol_deduction": nol_deduction, "remaining_carryover": remaining_carryover,
            "mti": mti, "standard_deduction": dedu["amount"],
            "citation": NOL_CITATION, "source_url": NOL_SOURCE_URL}


# --- NOL carryover for a MIXED-SOURCE filer: BOTH wages/other income
# AND current-year business income (Schedule CA (540) Line 8a
# "8a-general" population, the LAST item left in schedule_ca_inventory.py
# after the basis-difference batch -- originally deferred with the note
# "real new scope... would require generalizing the MTI/suspension test
# beyond business-income-only"). Re-examined 2026-08-28 at the user's
# request -- that original deferral reasoning doesn't actually survive
# contact with the suspension test's own text once you stop trying to
# make ONE stated figure stand in for BOTH halves of it.
#
# THE KEY INSIGHT: compute_nol_ca_tax (business-only) and
# compute_nol_wages_ca_tax (wages-only, closed business) both work
# around having only one stated income figure by letting it stand in
# for BOTH "net business income" and "modified AGI" at once -- a
# necessary simplification when only one number exists. A mixed-source
# taxpayer removes the NEED for that workaround entirely: asked for
# BOTH figures directly, "net business income" is simply the stated
# business_income figure (no conflation with total income required),
# and "modified AGI" is wages + business_income (a two-source sum, same
# disclosed "no other income/adjustments" simplification the other two
# paths already carry, just widened by one explicit source). No new
# algorithm, no new FTB research -- the SAME suspension AND test
# already verified and cited above, applied to its two actual inputs
# instead of one approximated input. If anything this is a MORE literal
# application of FTB's own test than the business-only path's
# necessary approximation.
#
# SCOPE: requires an EXPLICIT ongoing-business signal (reusing the same
# vocabulary compute_nol_wages_ca_tax already excludes ON, since that's
# exactly what distinguishes "current business exists" from "closed") AND
# explicit wage/other-income vocabulary (what distinguishes "mixed" from
# the business-only path, which already steps aside the moment wage
# vocabulary appears -- see NOL_COMPLEXITY_EXCLUDE above). A question with
# only business+NOL vocabulary (no wages stated) is NOT this path -- it
# correctly falls through to the existing business-only path unchanged.
NOL_MIXED_ONGOING_BUSINESS_TERMS = {
    # deliberately excludes "s-corp"/"llc"/"partnership" -- found live
    # colliding with the already-established entity_annual_tax feature
    # ("I run an s-corp with..." reads as an entity-tax question, not a
    # personal-income one); those terms belong to that feature's own
    # population, not this one's.
    "self-employ", "self employ",
    "freelance", "freelancing", "contractor", "contracting", "contracted",
    "sole proprietor", "k-1", "schedule c", "schedule e",
    "business income this year", "current business income", "still run", "still operate",
}
NOL_MIXED_WAGE_TERMS = {
    "wage", "wages", "salary", "salaried", "w-2", "w2", "w-2 income", "other income",
}
NOL_MIXED_BUSINESS_INCOME_TERMS = {
    "business income", "self-employment income", "self employment income",
    "net business income", "schedule c income", "k-1 income",
}
# same population-scoping discipline as the sibling NOL paths -- EBL and
# NOL are explicitly different buckets per FTB, so "business loss"/
# "excess business loss" language here means real ambiguity, deferred
# rather than guessed at, not folded into this feature's own trigger.
NOL_MIXED_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "dependent", "alimony",
    "gambling", "gambled", "betting", "wagering",
    "capital gain", "capital loss", "stock", "rsu", "trust", "estate",
    "disaster loss", "disaster", "business loss", "excess business loss",
}


def _nol_mixed_base_signal_ok(q: str) -> bool:
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_WAGES_CLOSED_BUSINESS_TERMS):
        return False  # closed-business language means the sibling wages-only path applies
    # "ongoing business" evidence: either an explicit self-employment-
    # flavored phrase, OR simply stating a "business income" figure at
    # all (a stated business-income anchor is itself direct evidence
    # current-year business income exists -- found live: the natural
    # phrasing "$X in business income" alone, with no separate
    # self-employed/schedule-C phrase, was being wrongly rejected before
    # this OR was added).
    if not (any(t in q for t in NOL_MIXED_ONGOING_BUSINESS_TERMS)
            or any(t in q for t in NOL_MIXED_BUSINESS_INCOME_TERMS)):
        return False
    if not any(t in q for t in NOL_MIXED_WAGE_TERMS):
        return False
    if any(t in q for t in NOL_MIXED_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_nol_mixed_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'wages/other
    income AND current-year business income, with a stated federal NOL
    carryover' question."""
    q = question.lower()
    if not _nol_mixed_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_nol_mixed_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _nol_mixed_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_nol_mixed_ca_tax(conn, wages: float, business_income: float, nol_carryover_amount: float,
                              filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """See module note above. Unlike compute_nol_ca_tax and
    compute_nol_wages_ca_tax, both suspension-test halves are genuinely
    separate stated figures here: net business income is business_income
    alone; modified AGI is wages + business_income."""
    if wages is None or wages < 0:
        return None
    if business_income is None or business_income < 0:
        return None
    if nol_carryover_amount is None or nol_carryover_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    modified_agi = wages + business_income
    mti = max(0.0, modified_agi - dedu["amount"])
    suspended = business_income >= NOL_THRESHOLD and modified_agi >= NOL_THRESHOLD
    nol_deduction = 0.0 if suspended else min(nol_carryover_amount, mti)
    remaining_carryover = nol_carryover_amount - nol_deduction
    taxable_income = max(0.0, mti - nol_deduction)
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "wages": wages, "business_income": business_income,
            "modified_agi": modified_agi, "nol_carryover_amount": nol_carryover_amount,
            "suspended": suspended, "nol_deduction": nol_deduction,
            "remaining_carryover": remaining_carryover, "mti": mti,
            "standard_deduction": dedu["amount"],
            "citation": NOL_CITATION, "source_url": NOL_SOURCE_URL}


# --- California disaster loss carryover deduction (Schedule CA (540)
# Line 9b1, FTB Form 3805V) -- verified against FTB's 2025 Schedule CA
# (540) instructions: "If you have a California disaster loss carryover
# deduction and there is income in the current taxable year, enter the
# total amount of disaster loss carryover deduction from your 2025 form
# FTB 3805V, Part III, line 2, column (f), as a positive number in
# column B." A flat "copy this cell from your own 3805V" pass-through --
# pulls from the EXACT SAME Part III, line 2, column (f) cell as Line
# 9b2's NOL carryover deduction (compute_nol_ca_tax above), just for a
# different loss type.
#
# ORIGIN OF THE CARRYOVER (not re-derived here, same "trust the input"
# precedent as the NOL carryover and capital-loss-carryover features):
# a disaster loss is a personal casualty/theft loss (FTB: "Disaster
# losses are casualty losses sustained as the result of a disaster... "
# declared by the President or Governor) that EXCEEDED that year's
# income when originally claimed at Schedule CA Part II Line 15 (a
# SEPARATE, still-deferred ledger item -- this feature does NOT touch
# that per-item Form 4684 computation at all; it only fires in a LATER
# year on the already-limited leftover balance). Building this does not
# require Line 15's per-item mechanics (FMV before/after, insurance
# reimbursement, the $100-per-event/10%-of-AGI floors) -- those floors
# were already baked into the loss when it originated.
#
# SIMPLER THAN NOL CARRYOVER (compute_nol_ca_tax above), not harder:
# disaster loss carryovers are EXPLICITLY EXEMPT from the 2024-2027 $1M
# NOL suspension rule regardless of income -- FTB's own suspension text
# (quoted at NOL_THRESHOLD's module note) carves out "taxpayers ... with
# disaster loss carryovers." So there is no suspended branch here at
# all: deduction = min(carryover_amount, MTI), remainder carries
# forward (up to 20 years for post-2011 declared disasters, disclosed
# not tracked). Also broader than NOL's population: FTB's own line text
# says "there is income in the current taxable year" -- NOT "business
# income" specifically -- so this applies against ANY income (wages
# included), unlike NOL carryover's sole-business-income scope.
DISASTER_LOSS_CARRYOVER_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 9b1"
DISASTER_LOSS_CARRYOVER_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

DISASTER_LOSS_CARRYOVER_TERMS = {
    "disaster loss carryover", "disaster loss deduction",
    "disaster loss carryover deduction", "california disaster loss carryover",
}
# Narrower than COMPLEXITY_EXCLUDE on purpose -- same lesson as HSA
# investment gain/foreign earned income/Subpart F/GILTI: this deduction
# is unconditional regardless of how the OTHER income was earned, so a
# bare self-employment mention is not a genuine reason to defer.
DISASTER_LOSS_CARRYOVER_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "dependent", "trust", "estate", "gambling", "gambled", "betting",
    "wagering", "alimony", "pension", "rental", "renting", "rented",
    "stock", "rsu",
}


def _disaster_loss_carryover_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in DISASTER_LOSS_CARRYOVER_TERMS):
        return False
    if any(t in q for t in DISASTER_LOSS_CARRYOVER_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_disaster_loss_carryover_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'other income
    with a stated California disaster loss carryover deduction'
    question."""
    q = question.lower()
    if not _disaster_loss_carryover_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_disaster_loss_carryover_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _disaster_loss_carryover_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_disaster_loss_carryover_ca_tax(conn, income_amount: float,
                                             disaster_loss_carryover_amount: float,
                                             filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as gross income and California AGI
    before this deduction (no other adjustments, same simplification
    used throughout). disaster_loss_carryover_amount is the taxpayer's
    stated California disaster loss carryover deduction from FTB 3805V,
    Part III, line 2, column (f) (the SAME cell NOL carryover pulls
    from -- see module note above). Unlike compute_nol_ca_tax, there is
    NO $1M suspension test here -- disaster loss carryovers are
    explicitly exempt -- so deduction = min(carryover_amount, MTI),
    remainder carries forward, no suspended branch."""
    if income_amount is None or income_amount < 0:
        return None
    if disaster_loss_carryover_amount is None or disaster_loss_carryover_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    mti = max(0.0, income_amount - dedu["amount"])
    deduction = min(disaster_loss_carryover_amount, mti)
    remaining_carryover = disaster_loss_carryover_amount - deduction
    taxable_income = max(0.0, mti - deduction)
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "income_amount": income_amount,
            "disaster_loss_carryover_amount": disaster_loss_carryover_amount,
            "deduction": deduction, "remaining_carryover": remaining_carryover,
            "mti": mti, "standard_deduction": dedu["amount"]}


# --- cannabis 280E business-expense decoupling (Ring 3 extension, R&TC
# Section 17209) -- verified against R&TC 17209's statutory text
# (leginfo.legislature.ca.gov) and the 2025 Schedule CA (540) instructions'
# Line 3 cannabis paragraph. Structurally this is a SELF-EMPLOYMENT
# computation with one extra CA-specific fact, not a new mechanism --
# reuses compute_self_employment_ca_tax via its cannabis_280e_expenses
# parameter (see that function's docstring for the math) rather than
# building a parallel compute path, same "extend one function with an
# optional fact" precedent as itemized deductions' 7 add-on parameters.
#
# THE RULE: IRC Section 280E disallows ANY deduction/credit for a trade or
# business "trafficking" in a federally-controlled substance -- cannabis
# remains federally controlled regardless of state legality, so a
# cannabis business's FEDERAL Schedule C can only deduct cost of goods
# sold, not ordinary business expenses (rent, wages, etc.), inflating
# federal net profit relative to true economic profit. California does
# NOT conform: R&TC 17209(a) turns 280E OFF entirely (not a partial
# carve-out) "for each taxable year beginning on or after January 1,
# 2020, and before January 1, 2030" -- FTB confirms licensees "may deduct
# cost of goods sold and all ordinary and necessary business expenses,
# such as rent and wages."
#
# THE LICENSING GATE MATTERS: R&TC 17209(b) restricts this to
# "commercial cannabis activity" by a "licensee" as defined under
# MAUCRSA/the Business and Professions Code (Division 10) -- i.e. state
# DCC-licensed operations only. FTB is explicit that UNLICENSED cannabis
# businesses get NO restoration ("may deduct cost of goods sold ... but
# may not deduct other business expenses") -- federal 280E fully applies
# for CA purposes too in that case, meaning the PLAIN self-employment path
# (cannabis_280e_expenses omitted) is already the CORRECT answer for an
# unlicensed operator, not a gap. Because getting this gate wrong would
# either wrongly grant or wrongly deny a real deduction, this path
# REQUIRES an explicit licensing statement in the question (not just
# "cannabis business") before computing at all -- mirrors this codebase's
# "never guess a material fact" discipline (missing filing status,
# ambiguous bare "partnership", etc.).
#
# SCOPE (per this feature's own research/scoping pass): sole-proprietor /
# single-member-LLC path ONLY (Schedule CA (540) Line 3, Column B) --
# reuses SE_COMPLEXITY_EXCLUDE unchanged (same k-1/s-corp/partnership/llc
# exclusions the self-employment path already enforces) since a K-1
# RECIPIENT needs NO new computation here: the entity itself (Form
# 565/568/100S) already does the 280E-decoupled computation before
# issuing the K-1, so this codebase's existing "trust the K-1 figure
# as-is" behavior (compute_k1_ca_tax) is already correct for licensed-
# cannabis K-1s too, confirmed by the Schedule CA Line 5 instructions
# containing zero cannabis-specific mentions (unlike Line 3).
CANNABIS_LICENSE_TERMS = {
    "licensed cannabis", "cannabis license", "maucrsa", "dcc-licensed",
    "dcc license",
}
CANNABIS_280E_EXPENSE_TERMS = {
    "280e", "disallowed expenses", "disallowed business expenses",
    "cannabis expenses",
}
CANNABIS_280E_CITATION = "R&TC Section 17209; FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 3"
CANNABIS_280E_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"


def _cannabis_280e_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in CANNABIS_LICENSE_TERMS):
        return False
    # SE_COMPLEXITY_EXCLUDE minus CANNABIS_LICENSE_TERMS -- same "narrower
    # exclude set, minus the trigger term" pattern as itemized/capital-loss
    # (SE_COMPLEXITY_EXCLUDE now CONTAINS the cannabis-license terms, added
    # for the collision guard on the plain self-employment path -- using it
    # unmodified here would make this trigger exclude itself).
    other_exclude = SE_COMPLEXITY_EXCLUDE - CANNABIS_LICENSE_TERMS
    if any(t in q for t in other_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_cannabis_280e_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'licensed
    cannabis business net profit with a stated 280E-disallowed-expense
    restoration' question. Mirrors detect_self_employment_signal's shape,
    reusing the SAME complexity boundary (SE_COMPLEXITY_EXCLUDE) since
    this is the sole-proprietor/SMLLC self-employment path with one extra
    fact, not a differently-scoped path."""
    q = question.lower()
    if not _cannabis_280e_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_cannabis_280e_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _cannabis_280e_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


# --- traditional IRA deduction pass-through (Ring 3 extension, Schedule CA
# (540) Part I Section C Line 20, IRC Section 408 election) -- verified
# against FTB's 2025 AND 2024 Instructions for Schedule CA (540)
# (https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html,
# .../2024/2024-540-ca-instructions.html), diffed year-over-year.
#
# COLUMN A vs B/C: "To take the election, the federal deduction is taken
# on line 20, column A. The election for California will be on line 20,
# column B or C." I.e. column A = the federal deduction as-is; B/C only
# get used when a REAL divergence exists.
#
# THE BIG FINDING THIS FEATURE WAS BUILT AROUND: SB 711 (the "Conformity
# Act of 2025", enacted Oct 1, 2025) moved California's general IRC
# conformity date from January 1, 2015 to January 1, 2025. Confirmed by
# diffing the instructions' own header text ("References... are to the
# [IRC] as of January 1, 2015" in the 2024 instructions vs "...January 1,
# 2025" in the 2025 instructions) AND by the Line 20 section itself: the
# 2024 instructions listed TWO concrete divergence triggers verbatim --
#   "IRA age -- If you report an IRA deduction on line 20, column A at
#   age 70 1/2 or older, include that amount... in... column B" (California
#   didn't conform to the SECURE Act's repeal of the age-70.5 cap), and
#   "Catch-up contributions for certain individuals -- If the amount...
#   is more than the amount allowed for California, enter the difference
#   ... on line 20, column B" (California didn't conform to CAA 2023's
#   IRA catch-up-contribution indexing).
# BOTH bullets are ABSENT from the 2025 instructions' Line 20 section,
# which now contains only the generic "408 election... column B or C,
# get FTB Pub. 1005" text -- consistent with California now conforming
# to both (SECURE Act 2019, CAA 2023) under the new Jan 1, 2025 date,
# since both predate that cutoff. So for TY2025 specifically, THESE TWO
# PREVIOUSLY-LIVE TRIGGERS NO LONGER APPLY.
#
# THE ONE CONFIRMED REMAINING TRIGGER: Line 4a/4b's instructions (IRA
# Distributions) cross-reference that "differences also occur if your
# California IRA deductions were different from your federal deductions
# because of differences between California and federal self-employment
# income" -- a worker-classification (Prop 22) mismatch between CA and
# federal earned income can change the contribution/deduction figure
# differently in each jurisdiction. Genuinely open-ended (requires
# knowing whether CA and federal earned income actually differ for this
# taxpayer), so self-employment/contractor income is EXCLUDED from this
# path's trigger entirely (via COMPLEXITY_EXCLUDE, which already lists
# self-employ/1099/contractor/freelance/sole-proprietor terms) rather
# than guessed at.
#
# NOT MODELED, disclosed: FTB Publication 1005 (Pension and Annuity
# Guidelines) -- the document FTB's OWN instructions point to three times
# for Line 20 detail -- exists ONLY as a PDF (no HTML version found for
# any year 2015-2025; a direct WebFetch also 403'd) and was not verified.
# If it documents additional divergence triggers beyond the two confirmed
# -repealed ones above, this path doesn't know about them. This module
# also does NOT independently derive federal IRA-deduction ELIGIBILITY
# from raw facts -- the $7,000/$8,000(age 50+) 2025 contribution limit,
# or the employer-plan MAGI phase-out -- it TRUSTS the taxpayer's stated
# federal deduction amount as already correct, same "trust the input"
# precedent as net_profit/itemized_amount/loss_amount elsewhere in this
# codebase. Roth IRA contributions are NEVER deductible (federal or CA)
# -- handled as a dedicated informational redirect (see
# detect_roth_ira_mention), not silently computed as if deductible.
IRA_DEDUCTION_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section C, Line 20"
IRA_DEDUCTION_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

IRA_DEDUCTION_TERMS = {
    "ira deduction", "ira contribution", "traditional ira", "deduct my ira",
    "deductible ira",
}
ROTH_IRA_TERMS = {"roth ira", "roth"}


def _ira_deduction_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in IRA_DEDUCTION_TERMS):
        return False
    if any(t in q for t in ROTH_IRA_TERMS):
        return False   # dedicated redirect (detect_roth_ira_mention), not this path
    other_exclude = COMPLEXITY_EXCLUDE - IRA_DEDUCTION_TERMS
    if any(t in q for t in other_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_ira_deduction_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'income with a
    stated traditional-IRA deduction' question. Mirrors
    detect_capital_loss_signal's shape (income + a deduction/offset figure
    -- structurally general-purpose, unlike the self-employment-specific
    paths, since an IRA deduction can accompany ANY income type)."""
    q = question.lower()
    if not _ira_deduction_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_ira_deduction_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _ira_deduction_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_roth_ira_mention(question: str) -> bool:
    """True iff the question asks about deducting a ROTH IRA contribution
    specifically -- Roth contributions are NEVER deductible (contributed
    with after-tax dollars; the benefit is tax-free qualified withdrawals
    later, not an upfront deduction), for either federal or California
    purposes. Checked as its OWN signal, same "specific redirect instead
    of a generic defer or a silently-wrong computation" precedent as
    detect_grantor_trust_mention."""
    q = question.lower()
    return any(t in q for t in ROTH_IRA_TERMS) and any(trig in q for trig in COMPUTE_TRIGGERS)


def compute_ira_deduction_ca_tax(conn, income_amount: float, ira_deduction_amount: float,
                                   filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as gross income and California AGI before
    the IRA deduction (no other adjustments -- same simple-case assumption
    as every other compute path). ira_deduction_amount is the taxpayer's
    stated FEDERAL traditional-IRA deduction (Line 20 column A) -- see the
    module note above for what is and isn't modeled. For TY2025, subtracts
    the stated amount UNCHANGED (no CA-specific adjustment applied) per
    the confirmed repeal of both previously-live Line 20 divergence
    triggers under SB 711's conformity-date change."""
    if income_amount is None or income_amount < 0:
        return None
    if ira_deduction_amount is None or ira_deduction_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = max(0.0, income_amount - ira_deduction_amount)
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "income_amount": income_amount, "ira_deduction_amount": ira_deduction_amount,
            "agi": agi, "standard_deduction": dedu["amount"]}


# --- QSBS (Qualified Small Business Stock, IRC Sections 1202/1045) full
# addback (Ring 3 extension, Schedule CA (540) Line 7a / Schedule D (540)
# Column (e)) -- verified against FTB's 2025 Instructions for California
# Schedule D (540) directly:
#   "Qualified Small Business Stock -- California does not conform to the
#   qualified small business stock deferral and gain exclusion under IRC
#   Sections 1045 and 1202. Enter the entire gain realized in column (e)."
# This is a COMPLETE, unqualified non-conformity, not partial -- corroborated
# by Cutler v. Franchise Tax Board (2012) and California's subsequent
# statutory repeal of its own QSBS provisions (R&TC Section 18152.5). Holds
# regardless of which federal exclusion tier applied (50%/75%/100% by
# acquisition date, or the OBBBA's newer post-7/4/2025 tier) -- the 2025
# Schedule CA (540) instructions explicitly list OBBBA's QSBS expansion as a
# provision California still does not conform to, so the add-back stays
# 100% no matter which federal rule produced the excluded amount.
#
# NOT affected by SB 711 (the Oct 2025 conformity-date change, 2015->2025):
# this is a specific statutory decoupling (R&TC 18152.5), not a generic
# "IRC as of date X" conformity gap -- confirmed by the OBBBA cross-
# reference above (California still doesn't conform to a 2025-enacted
# federal QSBS change, well after the new 2025 conformity date).
#
# TWO STATED FIGURES, deliberately not one: the taxpayer's FEDERAL TAXABLE
# gain (already reduced by whatever federal Section 1202/1045 exclusion or
# deferral applied) PLUS the amount excluded/deferred federally -- added
# back in FULL to reach the CA-taxable gain. Does NOT accept a single
# "total gain" figure and guess whether it's stated pre- or post-exclusion
# -- that ambiguity is exactly what this design avoids guessing at, same
# "never guess a material fact" discipline as everywhere else in this
# codebase. SCOPE: QSBS gain is treated as the taxpayer's ONLY income (no
# other adjustments) -- same sole-income-source discipline as the self-
# employment-only/K-1-only/business-loss-only paths.
QSBS_CITATION = "FTB 2025 Instructions for California Schedule D (540) -- Column (e)"
QSBS_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-d-instructions.html"

QSBS_TERMS = {
    "qsbs", "qualified small business stock", "section 1202", "irc 1202",
    "1202 exclusion", "section 1045", "1045 rollover",
}
QSBS_EXCLUDED_AMOUNT_TERMS = {
    "excluded", "exclusion", "deferred gain", "deferred under section 1045",
    "gain excluded", "amount excluded",
}


def _qsbs_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in QSBS_TERMS):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_qsbs_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'QSBS sale with
    a stated federal taxable gain and a stated excluded/deferred amount'
    question. No COMPLEXITY_EXCLUDE check needed -- QSBS_TERMS is specific
    enough vocabulary that it doesn't need the broader guard, unlike IRA
    deduction's general-purpose trigger."""
    q = question.lower()
    if not _qsbs_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_qsbs_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _qsbs_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_qsbs_ca_tax(conn, federal_taxable_gain: float, excluded_amount: float,
                          filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """federal_taxable_gain is the gain as it appears on the federal
    return (already reduced by whatever IRC Section 1202/1045 exclusion
    or deferral applied). excluded_amount is the amount excluded/deferred
    federally -- ADDED BACK IN FULL for California (R&TC 18152.5's non-
    conformity is total, not partial). The sum is the CA-taxable capital
    gain, treated as the taxpayer's ONLY income (see module note above)."""
    if federal_taxable_gain is None or federal_taxable_gain < 0:
        return None
    if excluded_amount is None or excluded_amount < 0:
        return None
    ca_gain = federal_taxable_gain + excluded_amount
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, ca_gain - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "federal_taxable_gain": federal_taxable_gain,
            "excluded_amount": excluded_amount, "ca_gain": ca_gain,
            "standard_deduction": dedu["amount"]}


# --- HSA-held investment sale gain addback (Ring 3 extension, Schedule CA
# (540) Line 7a / Schedule D (540)) -- verified against FTB's 2025
# Instructions for Schedule CA (540) directly: "the California basis of
# the assets listed [below] may be different from the federal basis due
# to differences between California and federal laws... Gain or loss from
# the sale of investments inside an HSA." California does not recognize
# HSAs as tax-favored AT ALL (same non-conformity family as the existing
# hsa_contributions_and_earnings topic cluster -- contributions aren't
# deductible for CA, interest/earnings are taxable as earned rather than
# tax-deferred) -- consistent with that, a REALIZED gain from selling
# securities held inside an HSA is taxed by California the same year it
# occurs, exactly as if the account were an ordinary taxable brokerage
# account. Federally this gain is INVISIBLE -- not reported anywhere on
# the federal return, since it stays inside the HSA's federally tax-
# advantaged wrapper (not taxed unless/until withdrawn for a non-
# qualified purpose, a separate existing topic). So unlike QSBS (federal
# reports a REDUCED gain, CA restores the excluded portion), this is a
# gain with NO federal counterpart at all -- the full stated amount is
# simply CA taxable income, no offsetting figure needed.
#
# SCOPE: GAINS only, not losses -- an HSA investment LOSS is just an
# ordinary capital loss subject to the EXISTING $3,000/$1,500-MFS annual
# offset limit (see compute_capital_loss_ca_tax above), with nothing
# HSA-specific about the mechanic; conflating the two would be scope
# creep without a real tax-law reason, so HSA-loss phrasing is excluded
# here rather than mishandled.
HSA_INVESTMENT_GAIN_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section A, Line 7a"
HSA_INVESTMENT_GAIN_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

HSA_INVESTMENT_GAIN_TERMS = {
    "hsa investment gain", "hsa investment gains", "hsa capital gain",
    "gain inside my hsa", "gain inside an hsa", "sold investments in my hsa",
    "investments inside my hsa", "hsa investment sale",
}
HSA_LOSS_TERMS = {"hsa investment loss", "hsa capital loss", "loss inside my hsa"}

# Narrower than COMPLEXITY_EXCLUDE on purpose -- see
# _hsa_investment_gain_base_signal_ok's comment for why self-employment/
# business/K-1 vocabulary is deliberately NOT here.
HSA_GAIN_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "dependent", "capital loss",
    "trust", "estate", "gambling", "gambled", "betting", "wagering",
    "alimony", "rental", "renting", "rented", "pension",
}


def _hsa_investment_gain_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in HSA_INVESTMENT_GAIN_TERMS):
        return False
    if any(t in q for t in HSA_LOSS_TERMS):
        return False   # ordinary capital-loss mechanics apply instead -- see module note
    # DELIBERATELY NOT COMPLEXITY_EXCLUDE-derived (unlike IRA deduction):
    # HSA gain taxability has nothing to do with how the OTHER income was
    # earned -- the CA/federal divergence is entirely about the HSA's own
    # tax-shelter status, orthogonal to wages vs. self-employment vs. K-1.
    # A narrower, purpose-built exclude list instead: only genuinely
    # unrelated complexity (a different loss/deduction mechanism, or a
    # third fact this 2-figure design can't hold) blocks this path.
    # Self-employment/business/K-1 mentions are explicitly NOT excluded
    # here -- confirmed via the self-employment collision test, where the
    # SE path correctly steps aside (SE_COMPLEXITY_EXCLUDE guard) and
    # THIS path correctly answers instead, unlike IRA deduction's
    # intentional full defer on the same combination.
    if any(t in q for t in HSA_GAIN_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_hsa_investment_gain_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'other income
    with a stated HSA-held investment gain' question. Mirrors
    detect_excess_business_loss_signal's shape."""
    q = question.lower()
    if not _hsa_investment_gain_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_hsa_investment_gain_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _hsa_investment_gain_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_hsa_investment_gain_ca_tax(conn, income_amount: float, hsa_gain_amount: float,
                                         filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is OTHER (non-HSA) income -- e.g. wages -- treated as
    gross income and California AGI before the HSA gain addition (no
    other adjustments, same simple-case assumption as every other
    compute path). hsa_gain_amount is the taxpayer's stated realized gain
    from selling investments held inside an HSA -- ADDED IN FULL to CA
    income (see module note above for why there's no federal figure to
    reconcile against)."""
    if income_amount is None or income_amount < 0:
        return None
    if hsa_gain_amount is None or hsa_gain_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = income_amount + hsa_gain_amount
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "income_amount": income_amount, "hsa_gain_amount": hsa_gain_amount,
            "agi": agi, "standard_deduction": dedu["amount"]}


# --- K-1 pass-through CAPITAL GAIN (Ring 3 extension, Schedule CA (540)
# Line 7a / Schedule D (540) Line 2) -- distinct from K1_CITATION's Line 5
# (ORDINARY K-1 income). Verified against FTB's 2025 Instructions for
# California Schedule D (540): "Combine gain(s) and loss(es) from all
# California Schedule(s) K-1 (100S, 541, 565, and 568)... Enter the net
# loss on line 2, column (d), or the net gain on line 2, column (e)." The
# CA-specific figure is printed directly on the taxpayer's California K-1
# (prepared by the pass-through entity itself, alongside the federal
# figures) -- same "trust the input" precedent as compute_k1_ca_tax
# already uses for Line 5. Reuses compute_k1_ca_tax's math UNCHANGED
# (same taxable-income mechanic) since California taxes capital gains as
# ordinary income with no special rate -- the only real difference is
# which line/citation applies, not the arithmetic.
#
# WHY THIS NEEDS ITS OWN TRIGGER, not just wider K1_TRIGGERS vocabulary:
# K1_COMPLEXITY_EXCLUDE deliberately excludes "capital gain" (a plain
# K-1-income question mentioning a capital gain means real complexity
# that path doesn't attempt) -- so "$50,000 in K-1 capital gains" is
# CORRECTLY refused by detect_k1_signal today, not a gap to just widen.
#
# SCOPE: GAINS only, mirroring the HSA-investment-gain precedent -- a K-1
# CAPITAL LOSS is subject to the standard $3,000/$1,500-MFS annual offset
# limit (see compute_capital_loss_ca_tax), a DIFFERENT mechanic from a
# straight addback; conflating the two would misstate the loss-limited
# case as fully deductible. K-1 capital LOSS phrasing is explicitly
# excluded here rather than mishandled.
K1_CAPITAL_GAIN_CITATION = "FTB 2025 Instructions for California Schedule D (540) -- Line 2"
K1_CAPITAL_GAIN_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-d-instructions.html"

K1_CAPITAL_GAIN_TERMS = {
    "k-1 capital gain", "k1 capital gain", "schedule k-1 capital gain",
    "schedule k1 capital gain", "k-1 capital gains", "k1 capital gains",
}
K1_CAPITAL_LOSS_TERMS = {
    "k-1 capital loss", "k1 capital loss", "schedule k-1 capital loss",
    "schedule k1 capital loss", "k-1 capital losses", "k1 capital losses",
}


def _k1_capital_gain_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in K1_CAPITAL_GAIN_TERMS):
        return False
    if any(t in q for t in K1_CAPITAL_LOSS_TERMS):
        return False   # ordinary capital-loss annual-limit mechanics apply instead
    other_exclude = K1_COMPLEXITY_EXCLUDE - {"capital gain"}
    if any(t in q for t in other_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_k1_capital_gain_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'K-1 capital
    gain, sole income' question. Mirrors detect_k1_signal's shape."""
    q = question.lower()
    if not _k1_capital_gain_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_k1_capital_gain_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _k1_capital_gain_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


# --- Capital loss CARRYOVER from a prior year (Ring 3 extension,
# Schedule CA (540) Line 7a / Schedule D (540) Line 6) -- verified
# against FTB's 2025 Instructions for California Schedule D (540): "If
# you were a resident of California for all prior years, enter your
# California capital loss carryover from 2024. However, if you were a
# nonresident of California during any taxable year that generated a
# portion of your 2024 capital loss carryover, recalculate your 2024
# capital loss carryover as if you resided in California for all prior
# years."
#
# MATH IS IDENTICAL to a current-year capital loss -- see
# compute_capital_loss_ca_tax above, whose OWN module note already
# flagged this exact carryover case as "deliberately left out of scope"
# when first built: a carryover loss is subject to the SAME
# $3,000/$1,500-MFS annual offset limit as any capital loss, no special
# arithmetic. Reuses compute_capital_loss_ca_tax UNCHANGED -- the real
# gap isn't the math, it's DISCLOSURE: the existing capital-loss answer
# text says "assumes your stated loss is a CURRENT-YEAR loss with no
# capital loss carryover from a prior year" -- factually WRONG
# (contradicts the question) when the taxpayer explicitly states this IS
# a carryover, even though the computed NUMBER is identical either way.
# This dedicated path exists to give the ACCURATE disclosure (resident-
# all-prior-years assumption, not a current-year-loss assumption) rather
# than a misleading one. Checked BEFORE the generic capital-loss path in
# the dispatcher (mirrors how K-1 capital gain had to precede the K-1
# fallback), since "capital loss carryover" contains "capital loss" as a
# substring and would otherwise be caught by the generic path first.
#
# SCOPE: resident-all-prior-years case ONLY, matching FTB's own carve-out
# -- a taxpayer who was a NONRESIDENT during any year that generated part
# of the carryover needs a recalculation this single-question model
# can't perform (requires year-by-year sourcing history), so nonresident/
# interstate-move mentions are excluded here rather than silently
# ignored.
CAPITAL_LOSS_CARRYOVER_CITATION = "FTB 2025 Instructions for California Schedule D (540) -- Line 6"
CAPITAL_LOSS_CARRYOVER_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-d-instructions.html"

CAPITAL_LOSS_CARRYOVER_TERMS = {
    "capital loss carryover", "capital losses carryover",
    "carryover capital loss", "loss carryover from last year",
    "loss carryover from 2024", "carryover from my schedule d",
    "capital loss carried over", "carried over from last year",
}
CAPITAL_LOSS_CARRYOVER_NONRESIDENT_TERMS = {
    "nonresident", "non-resident", "part-year resident", "part year resident",
    "moved to california", "moved from california", "moved out of california",
    "moved into california",
}


def _capital_loss_carryover_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in CAPITAL_LOSS_CARRYOVER_TERMS):
        return False
    if any(t in q for t in CAPITAL_LOSS_CARRYOVER_NONRESIDENT_TERMS):
        return False   # needs a year-by-year recalculation this model can't perform
    other_exclude = COMPLEXITY_EXCLUDE - {"capital loss"}
    if any(t in q for t in other_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_capital_loss_carryover_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'income with a
    stated prior-year capital loss carryover' question. Mirrors
    detect_capital_loss_signal's shape."""
    q = question.lower()
    if not _capital_loss_carryover_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_capital_loss_carryover_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _capital_loss_carryover_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


# --- Employer fringe-benefit expense restoration (Ring 3 extension,
# Schedule CA (540) Part I Section B Line 3, "Business Income or (Loss)")
# -- verified against FTB's 2025 Instructions for Schedule CA (540)
# directly: "Limitation on employer's deduction for fringe benefit
# expenses -- Under federal law, deductions for entertainment expenses
# are disallowed; the current 50% limit on the deductibility of business
# meals is expanded to meals provided through an in-house cafeteria or
# otherwise on the premises of the employer; deductions for employee
# transportation fringe benefits (e.g., parking and mass transit) are
# denied; and no deduction is allowed for transportation expenses that
# are the equivalent of commuting for employees... California law does
# not conform. Figure the difference between the amounts allowed using
# federal law and California law."
#
# STRUCTURALLY IDENTICAL to cannabis 280E: TCJA's IRC Section 274 changes
# disallow/limit these employer deductions federally; California kept
# the pre-TCJA, more permissive rule -- reuses
# compute_self_employment_ca_tax's fringe_benefit_restoration parameter
# (same restoration-subtracted-from-AGI mechanic, se_tax untouched).
#
# NOT affected by SB 711's conformity-date change (2015->2025): the
# CURRENT (post-SB-711) 2025 instructions still explicitly list this as
# non-conforming, confirming California's own IRC Section 274 rule
# persists as a specific decoupling rather than simply lagging the old
# conformity date -- same pattern as QSBS and cannabis 280E.
#
# SCOPE-GATING MATTERS HERE MORE THAN USUAL: this only applies to a
# Schedule-C filer who is themselves an EMPLOYER -- these are benefits
# paid TO EMPLOYEES (entertainment, employee parking/transit, on-
# premises meals), not something a sole owner with no staff can incur.
# Trigger vocabulary is deliberately built around "employee"/"employer"-
# flavored fringe-benefit phrasing rather than a bare "fringe benefit"
# mention, to keep that population boundary honest. Also bundles THREE
# federally-distinct sub-limitations (entertainment: 0% federal vs. CA's
# old-law rate; transportation fringe: 0% federal vs. full CA deduction;
# on-premises meals: 50% federal vs. CA's old-law rate) into one trusted
# figure -- same "trust the input" precedent as every other add-on
# figure in this codebase, not attempting to re-derive the federal
# limitation from raw per-category facts.
FRINGE_BENEFIT_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 3"
FRINGE_BENEFIT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

FRINGE_BENEFIT_TERMS = {
    "fringe benefit expense", "fringe benefit expenses", "employer fringe benefit",
    "employee parking", "employee transit", "employee transportation benefit",
    "employee transportation fringe", "on-premises meals", "on premises meals",
    "employee meal benefit", "fringe benefit limitation",
    "entertainment expense limitation",
}


def _fringe_benefit_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in FRINGE_BENEFIT_TERMS):
        return False
    other_exclude = SE_COMPLEXITY_EXCLUDE - FRINGE_BENEFIT_TERMS
    if any(t in q for t in other_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_fringe_benefit_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'self-
    employment net profit with a stated employer fringe-benefit
    restoration' question. Mirrors detect_self_employment_signal's shape,
    reusing SE_COMPLEXITY_EXCLUDE minus this path's own trigger terms,
    same pattern as cannabis 280E."""
    q = question.lower()
    if not _fringe_benefit_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_fringe_benefit_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _fringe_benefit_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


# --- CA non-conformity to IRC Section 469(c)(7), the federal "real
# estate professional" exception (Ring 3 extension, Schedule CA (540)
# Line 5 / FTB Form 3801) -- verified against FTB's 2025 Instructions for
# Form 3801: "California law does not conform to federal law for
# material participation in rental real estate activities. Beginning in
# 1994, and for federal purposes only, rental real estate activities
# conducted by persons in real property business are not automatically
# treated as passive activities... For California purposes, all rental
# activities are passive activities."
#
# THE MECHANIC: federally, a taxpayer who qualifies as a real estate
# professional (material participation in the specific rental activity,
# per IRC 469(c)(7)) gets that activity's loss treated as NONPASSIVE --
# fully deductible, no cap. California refuses this recharacterization:
# the SAME activity stays PASSIVE for CA, so the loss falls back to the
# ordinary $25,000 active-participation allowance with its $100,000-
# $150,000 MAGI phase-out (IRC 469(i)) -- CONFIRMED IDENTICAL formula and
# dollar thresholds to federal Form 8582's own passive rental-loss
# allowance (FTB 3801: "Generally, California law is the same as federal
# law concerning PAL limitations" -- CA reuses the federal MAGI figure
# directly, and federal Form 6198 at-risk mechanics with CA-basis
# inputs). CA does NOT compute a separate formula; it just refuses the
# exception that would have exempted the taxpayer from this formula in
# the first place. The DIFFERENCE (federal fully-allowed loss minus
# CA-allowed loss) is the Schedule CA (540) Line 5 Column C addition.
#
# MFS HANDLING (per FTB 3801's own instructions, mirroring IRC 469(i)(5)
# exactly): a taxpayer filing MFS who lived apart from their spouse ALL
# YEAR uses HALVED figures ($12,500 allowance, $50,000-$75,000 phase-out
# range). An MFS taxpayer who did NOT live apart all year gets NO
# allowance at all -- $0, full stop, regardless of MAGI. Since this
# feature can't assume which MFS scenario applies, it requires an
# explicit "lived apart" statement before computing for MFS; without one,
# it defers rather than guessing (same "never guess a material fact"
# discipline as everywhere else in this codebase).
#
# NOT MODELED, disclosed: this assumes the rental activity is the
# taxpayer's ONLY passive activity (no netting against other passive
# income/losses) and no prior-year suspended-loss carryover -- both
# genuinely require facts this single-question model doesn't gather.
# Also assumes MAGI equals the taxpayer's stated other (non-rental)
# income, same "trust the input, no other adjustments" simplification
# used throughout this codebase.
REAL_ESTATE_PRO_CITATION = "FTB 2025 Instructions for Form 3801 -- General Information, Material Participation in Real Property Business"
REAL_ESTATE_PRO_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3801-instructions.html"

REAL_ESTATE_PRO_TERMS = {
    "real estate professional", "real property professional",
    "real estate professional exception", "material participation in real property",
}
REAL_ESTATE_PRO_LOSS_TERMS = {"rental loss", "rental losses"}
MFS_LIVED_APART_TERMS = {"lived apart", "lived separately", "did not live together"}
MFS_LIVED_TOGETHER_TERMS = {"lived together", "did not live apart"}


def _real_estate_pro_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in REAL_ESTATE_PRO_TERMS):
        return False
    # subtract "rental"/"renting"/"rented" (this feature's own trigger
    # vocabulary always co-occurs with rental-activity wording) AND
    # "estate" (a shared-word collision, not a trigger-phrase overlap --
    # "real ESTATE professional" contains "estate", which COMPLEXITY_
    # EXCLUDE also uses to guard against trust/estate income questions;
    # found live via testing, same self-exclusion bug CLASS as cannabis
    # 280E's, just a substring-of-a-different-word variant this time).
    other_exclude = COMPLEXITY_EXCLUDE - {"rental", "renting", "rented", "estate"}
    if any(t in q for t in other_exclude):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_real_estate_pro_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'real estate
    professional with a rental loss' question AND (for MFS specifically)
    the lived-apart status is resolvable. Returns None for a bare MFS
    mention with no lived-apart/lived-together statement -- see
    detect_real_estate_pro_missing_mfs_status for that specific defer."""
    q = question.lower()
    if not _real_estate_pro_base_signal_ok(q):
        return None
    fs = detect_filing_status(question)
    if fs == "mfs" and not any(t in q for t in MFS_LIVED_APART_TERMS | MFS_LIVED_TOGETHER_TERMS):
        return None
    return fs


def detect_real_estate_pro_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _real_estate_pro_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_real_estate_pro_missing_mfs_status(question: str) -> bool:
    """True iff this is a clearly real-estate-professional-shaped MFS
    question missing only the lived-apart/lived-together fact -- gets a
    specific clarifying message instead of a generic defer."""
    q = question.lower()
    if not _real_estate_pro_base_signal_ok(q):
        return False
    if detect_filing_status(question) != "mfs":
        return False
    return not any(t in q for t in MFS_LIVED_APART_TERMS | MFS_LIVED_TOGETHER_TERMS)


def compute_real_estate_pro_allowance(magi: float, filing_status: str, lived_apart: bool = False):
    """The standard $25,000 active-participation rental-loss allowance
    with its $100,000-$150,000 MAGI phase-out (IRC 469(i)), which
    California mirrors exactly for a real-estate-professional's rental
    activity (see module note above for why CA and federal use the
    IDENTICAL formula here). Returns the CA-ALLOWED dollar amount (before
    applying it against the stated loss)."""
    if filing_status == "mfs" and not lived_apart:
        return 0.0
    if magi is None or magi < 0:
        return None
    if filing_status == "mfs" and lived_apart:
        base_allowance, phase_start, phase_end = 12500.0, 50000.0, 75000.0
    else:
        base_allowance, phase_start, phase_end = 25000.0, 100000.0, 150000.0
    if magi <= phase_start:
        return base_allowance
    if magi >= phase_end:
        return 0.0
    return max(0.0, base_allowance - 0.5 * (magi - phase_start))


def compute_real_estate_pro_ca_tax(conn, other_income: float, rental_loss: float,
                                     filing_status: str, lived_apart: bool = False,
                                     tax_year: int = DEFAULT_TAX_YEAR):
    """other_income is the taxpayer's non-rental income -- treated as
    both California AGI (before the rental loss) and Modified AGI for
    the phase-out test (no other adjustments, same simplification used
    throughout this codebase). rental_loss is the FEDERAL fully-allowed
    (nonpassive) loss amount -- CA allows only up to
    compute_real_estate_pro_allowance's result; the excess is added back
    for California (Line 5 Column C), not carried forward here (this
    module doesn't track passive-loss carryovers)."""
    if other_income is None or other_income < 0:
        return None
    if rental_loss is None or rental_loss <= 0:
        return None
    ca_allowance = compute_real_estate_pro_allowance(other_income, filing_status, lived_apart)
    if ca_allowance is None:
        return None
    ca_allowed_loss = min(rental_loss, ca_allowance)
    disallowed = rental_loss - ca_allowed_loss
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = max(0.0, other_income - ca_allowed_loss)
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "other_income": other_income, "rental_loss": rental_loss,
            "ca_allowance": ca_allowance, "ca_allowed_loss": ca_allowed_loss,
            "disallowed": disallowed, "standard_deduction": dedu["amount"]}


# --- Federal foreign earned income/housing exclusion addback (Ring 3
# extension, Schedule CA (540) Line 8d, Form 2555) -- verified against
# FTB's 2025 Instructions for Schedule CA (540) directly: "Federal
# foreign earned income and housing exclusion -- Enter in column C, as a
# positive number, the amount excluded from federal income on federal
# Schedule 1 (Form 1040), line 8d." A flat, unconditional restatement --
# no worksheet, no partial-addback language (contrast with Line 4a/4b's
# genuine "if the CA amount is more/less than federal" framing) --
# confirming California does not conform to IRC Section 911 AT ALL for
# this Schedule CA (540) RESIDENT-population form.
#
# THE "NEEDS RESIDENCY-HISTORY FACTS" ASSUMPTION IN THIS LEDGER ITEM WAS
# WRONG: Schedule CA (540) is titled "California Adjustments --
# Residents" -- it ALREADY presupposes full-year CA residency as an entry
# condition (part-year/nonresident apportionment lives on the SEPARATE
# Schedule CA (540NR) form/engine, not this one). Within that resident-
# only scope, CA taxes ALL worldwide income with no exception for
# foreign-earned amounts, so the addback has no date-based or partial
# condition -- same stale-assumption pattern already found and corrected
# for Line 8a's federal NOL addback.
#
# SIMPLER THAN QSBS: no offsetting federal figure to reconcile against --
# the WHOLE excluded amount is simply ADDED to CA income, mirroring the
# HSA-investment-gain pattern exactly (income + addback figure, no second
# figure needed).
FOREIGN_EARNED_INCOME_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 8d"
FOREIGN_EARNED_INCOME_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

FOREIGN_EARNED_INCOME_TERMS = {
    "foreign earned income exclusion", "foreign earned income and housing exclusion",
    "form 2555", "foreign housing exclusion", "excluded foreign earned income",
    "excluded under form 2555",
}
# --- Foreign housing DEDUCTION (Schedule CA (540) Line 24j, distinct
# from the exclusion above) -- verified against FTB's 2025 Schedule CA
# (540) instructions: "j. Housing deduction from federal Form 2555 -- If
# you claimed the foreign housing deduction for federal purposes, enter
# the amount from column A in column B." IRC 911(c)'s housing DEDUCTION
# (the self-employed counterpart to 911(a)/(c)'s housing EXCLUSION,
# available to employees only) is claimed as an above-the-line federal
# deduction on Schedule 1, not an exclusion -- mechanically different
# federal treatment of a conceptually similar cost, so it needed its own
# verification rather than assuming it shares Line 8d's non-conformity
# automatically.
#
# SAME NON-CONFORMITY, SAME NET DIRECTION AS THE EXCLUSION -- but
# arrived at differently: Line 24j lives in Schedule CA Part I SECTION
# C ("Other Adjustments to Income"), not Section B where Line 8d sits.
# Section C's column B/C encode "subtraction/addition to the DEDUCTION
# total," which is the OPPOSITE sign convention from Section A/B's
# "subtraction/addition to the INCOME total" -- tracing the arithmetic
# through Form 540's own Line 14/15/16 chain (Section C Line 26 sums
# column B, Line 27 = Line 10 minus Line 26, which then feeds Form 540's
# "California Adjustments -- Subtractions" line) confirms that a LARGER
# Line 24j column-B entry produces a SMALLER net subtraction at the
# bottom of the form -- i.e. disallowing the deduction RAISES CA taxable
# income, the exact same net effect as Line 8d's exclusion addback, even
# though it's entered in a column literally labeled "B" (subtraction).
# The ledger's own "subtraction" adjustment_type tag reflects the form's
# column LABEL, not the AGI-direction -- this is implemented as an
# ADDBACK (added to other_income), matching that direction, not a
# literal subtraction.
#
# CO-OCCURS WITH THE EXCLUSION FOR THE MODAL SELF-EMPLOYED-EXPAT CASE
# (not mutually exclusive -- IRC 911(c)'s housing deduction is available
# IN ADDITION TO the 911(a) earned-income exclusion for self-employment
# earnings), so this extends compute_foreign_earned_income_ca_tax with
# an optional parameter rather than a fully separate function/feature --
# same idiom as the SALT-cap/mortgage-interest optional params already
# bolted onto compute_itemized_ca_tax. Flat, unconditional, no cap-table
# replication (FTB doesn't ask for IRS Pub 54's per-location housing-
# cost-limitation table -- "trust the input," same as the exclusion).
FOREIGN_HOUSING_DEDUCTION_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section C, Line 24j"
FOREIGN_HOUSING_DEDUCTION_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

FOREIGN_HOUSING_DEDUCTION_TERMS = {
    "foreign housing deduction", "housing deduction from form 2555",
    "form 2555 housing deduction", "housing deduction under form 2555",
}
# Narrower than COMPLEXITY_EXCLUDE on purpose -- see
# _foreign_earned_income_base_signal_ok's comment for why self-employment
# vocabulary is deliberately NOT here.
FOREIGN_EARNED_INCOME_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "dependent", "trust", "estate", "gambling", "gambled", "betting",
    "wagering", "alimony", "pension", "rental", "renting", "rented",
    "stock", "rsu",
}


def _foreign_earned_income_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in FOREIGN_EARNED_INCOME_TERMS | FOREIGN_HOUSING_DEDUCTION_TERMS):
        return False
    # DELIBERATELY NOT COMPLEXITY_EXCLUDE-derived (same lesson as HSA
    # investment gain, not IRA deduction): the Form 2555 addback is
    # unconditional regardless of how the OTHER income was earned --
    # wages, self-employment, whatever -- so there's no genuine reason to
    # defer just because self-employment is mentioned, unlike IRA
    # deduction's real open SE-income-mismatch trigger. A narrower,
    # purpose-built exclude list instead.
    if any(t in q for t in FOREIGN_EARNED_INCOME_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_foreign_earned_income_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'other income
    with a stated Form 2555 excluded amount and/or foreign housing
    deduction amount' question. Mirrors detect_hsa_investment_gain_
    signal's shape (addback figure(s), no offsetting figure needed)."""
    q = question.lower()
    if not _foreign_earned_income_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_foreign_earned_income_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _foreign_earned_income_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_foreign_earned_income_ca_tax(conn, other_income: float, excluded_amount: float,
                                           filing_status: str, tax_year: int = DEFAULT_TAX_YEAR,
                                           housing_deduction_amount: float = 0.0):
    """other_income is the taxpayer's other (non-foreign-excluded) CA
    income -- e.g. wages -- treated as gross income/AGI before the
    addback (no other adjustments, same simplification used throughout
    this codebase). excluded_amount is the amount excluded federally
    under Form 2555's foreign earned income/housing EXCLUSION (Schedule
    CA Line 8d) -- ADDED IN FULL to CA income, since California taxes
    full-year residents on all worldwide income with no exception (see
    module note above). housing_deduction_amount (optional, Schedule CA
    Line 24j) is the amount DEDUCTED federally under Form 2555's foreign
    housing DEDUCTION -- also added in full, same non-conformity, same
    net direction (see the Line 24j module note above for why an
    entry in the form's "column B" still nets to an addback). The two
    federal mechanics commonly co-occur for self-employed expats but are
    independent -- either may be zero, but at least one must be
    positive."""
    if other_income is None or other_income < 0:
        return None
    excluded_amount = excluded_amount or 0.0
    housing_deduction_amount = housing_deduction_amount or 0.0
    if excluded_amount <= 0 and housing_deduction_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = other_income + excluded_amount + housing_deduction_amount
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "other_income": other_income, "excluded_amount": excluded_amount,
            "housing_deduction_amount": housing_deduction_amount,
            "agi": agi, "standard_deduction": dedu["amount"]}


# --- IRC 951(a) Subpart F income inclusion (Schedule CA (540) Line 8n) --
# California does NOT conform: FTB's own Line 8n instruction states
# verbatim "California law does not conform" and directs the taxpayer to
# carry the SAME federal-column amount into column B as a subtraction --
# no worksheet, no cap, no partial condition. Corroborated by the Line 3
# (dividends) instruction, which states CA taxes controlled foreign
# corporation earnings "in the year distributed" -- i.e. CA's baseline is
# to tax CFC earnings on actual distribution, not on this federal
# deemed/phantom inclusion. Subpart F (IRC 951(a), enacted 1962) predates
# TCJA and is a separate, independently-stated non-conformity from GILTI
# below, not a TCJA-era item.
#
# MECHANICALLY THIS FULLY CANCELS, NOT JUST REDUCES: federal AGI already
# includes the 951(a) inclusion as its own income item (federal AGI =
# other_income + inclusion_amount); the CA subtraction removes exactly
# that amount, so CA AGI = other_income, as if the inclusion never
# existed. This is a real, correct computation (not degenerate) -- it's
# just that the addition and subtraction happen to net to zero, unlike
# the FEIE addback above (a one-directional add with no federal
# counterpart in this codebase's model) or the QSBS/HSA-gain addbacks.
#
# "Trust the input" precedent applies as usual: does NOT independently
# verify >=10% CFC-shareholder/U.S.-shareholder status from raw facts --
# trusts the taxpayer's stated federal 951(a) inclusion amount as already
# correct.
SUBPART_F_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 8n"
SUBPART_F_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

SUBPART_F_TERMS = {
    "subpart f income", "subpart f inclusion", "subpart f",
    "irc section 951(a)", "irc 951(a)", "section 951(a) inclusion",
    "951(a) inclusion",
}
# Narrower than COMPLEXITY_EXCLUDE on purpose -- same lesson as HSA
# investment gain/foreign earned income: the Subpart F subtraction is
# unconditional regardless of how the OTHER income was earned, so a bare
# self-employment mention is not a genuine reason to defer.
SUBPART_F_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "dependent", "trust", "estate", "gambling", "gambled", "betting",
    "wagering", "alimony", "pension", "rental", "renting", "rented",
    "stock", "rsu",
}


def _subpart_f_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in SUBPART_F_TERMS):
        return False
    if any(t in q for t in SUBPART_F_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_subpart_f_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'other income
    with a stated federal Subpart F (951(a)) inclusion' question. Mirrors
    detect_foreign_earned_income_signal's shape, but SUBTRACTS instead of
    adds -- California doesn't conform to the federal INCLUSION here, so
    the stated amount comes back OUT of CA income instead of going in."""
    q = question.lower()
    if not _subpart_f_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_subpart_f_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _subpart_f_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_subpart_f_ca_tax(conn, other_income: float, inclusion_amount: float,
                               filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """other_income is the taxpayer's other California income (e.g.
    wages), treated as AGI-equivalent and NOT including the federal
    Subpart F inclusion (no other adjustments, same simplification used
    throughout this codebase). inclusion_amount is the amount the
    taxpayer included federally under IRC 951(a) -- federal AGI equals
    other_income + inclusion_amount, and the CA subtraction removes it in
    full (California does not conform; see module note above), so CA AGI
    reduces back down to just other_income."""
    if other_income is None or other_income < 0:
        return None
    if inclusion_amount is None or inclusion_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    federal_agi = other_income + inclusion_amount
    agi = other_income
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "other_income": other_income, "inclusion_amount": inclusion_amount,
            "federal_agi": federal_agi, "agi": agi, "standard_deduction": dedu["amount"]}


# --- IRC 951A(a) GILTI inclusion (Schedule CA (540) Line 8o) -- same
# non-conformity pattern as Subpart F above, TCJA-era (2017) instead of
# pre-existing law: FTB's Line 8o instruction states verbatim "California
# law does not conform" for GILTI, and this is separately confirmed in
# the instructions' own "What's New / Federal Tax Reform" TCJA bullet
# list ("Global intangible low-taxed income (GILTI) under IRC Section
# 951A"). Same flat, unconditional column-A-to-column-B restatement, no
# worksheet, no numeric example.
#
# IRC SECTION 250 DEDUCTION IS A NON-ISSUE FOR THIS MODEL: the federal
# 50% GILTI deduction (IRC 250) is only available to C corporations, or
# individuals who make an IRC 962 election to be taxed as if a
# corporation. An ordinary individual CFC shareholder with no 962
# election reports the GROSS/full GILTI inclusion on federal Schedule 1
# line 8o to begin with -- there's no 250 deduction netted into that
# number already, so the flat full-amount subtraction is correct as
# written for the standard (non-962-electing) case. A 962-electing
# taxpayer is a narrower edge case within an already-narrow population,
# out of scope for this single-fact model (not independently detected or
# flagged from raw facts).
GILTI_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 8o"
GILTI_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

GILTI_TERMS = {
    "gilti", "global intangible low-taxed income", "global intangible low taxed income",
    "irc section 951a", "irc 951a", "section 951a inclusion", "951a inclusion",
    "form 8992",
}
# Same "narrower than COMPLEXITY_EXCLUDE on purpose" reasoning as Subpart
# F above -- the GILTI subtraction is unconditional regardless of how the
# other income was earned.
GILTI_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "dependent", "trust", "estate", "gambling", "gambled", "betting",
    "wagering", "alimony", "pension", "rental", "renting", "rented",
    "stock", "rsu",
}


def _gilti_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in GILTI_TERMS):
        return False
    if any(t in q for t in GILTI_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_gilti_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'other income
    with a stated federal GILTI (951A) inclusion' question. Mirrors
    detect_subpart_f_signal's shape exactly -- same subtraction/non-
    conformity mechanic, different IRC section and vintage."""
    q = question.lower()
    if not _gilti_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_gilti_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _gilti_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_gilti_ca_tax(conn, other_income: float, inclusion_amount: float,
                           filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Same mechanic as compute_subpart_f_ca_tax -- other_income excludes
    the GILTI inclusion; federal AGI = other_income + inclusion_amount;
    the CA subtraction removes the inclusion in full, so CA AGI reduces
    back down to just other_income. See module note above for the IRC
    250/962-election scope note."""
    if other_income is None or other_income < 0:
        return None
    if inclusion_amount is None or inclusion_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    federal_agi = other_income + inclusion_amount
    agi = other_income
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "other_income": other_income, "inclusion_amount": inclusion_amount,
            "federal_agi": federal_agi, "agi": agi, "standard_deduction": dedu["amount"]}


# --- Foreign income of nonresident aliens -- worldwide-income true-up
# (Schedule CA (540) Line 8z) -- verified against FTB's 2025 Schedule CA
# (540) instructions: "Foreign income of nonresident aliens -- Adjust
# federal income to reflect worldwide income computed under California
# law. Enter losses from foreign sources on line 8z, column B. Enter
# foreign source income on line 8z, column C." A flat, unconditional,
# two-directional restatement -- no worksheet, no cap, no netting
# mentioned.
#
# "NONRESIDENT ALIEN" HERE IS THE FEDERAL TAX-STATUS TERM (IRC 7701(b),
# Form 1040-NR filer), NOT a California-residency term -- confirmed by
# its OTHER two uses on this SAME resident-only instructions page (the
# Line 2a/19a alimony paragraphs: "If you are a nonresident alien and
# received alimony..."), and this document is titled "California
# Adjustments -- Residents" throughout. The population is a full-year CA
# RESIDENT who is ALSO a federal nonresident alien (e.g. someone who
# hasn't met the federal substantial-presence/green-card test despite
# being CA-domiciled). Because a federal 1040-NR generally reports only
# U.S.-source/effectively-connected income, California -- which taxes
# residents on WORLDWIDE income -- needs this line to true up the
# difference. Confirmed (not assumed) this doesn't belong to or
# duplicate the separate income_nonresident.py (Form 540NR/Schedule CA
# (540NR)) engine -- CA-residency and federal-NRA status are independent
# axes, and grepping that module found zero existing "nonresident
# alien"/"foreign income"/"1040-NR" handling.
#
# Requires an EXPLICIT federal-NRA self-identification phrase in the
# question (not just any "foreign income" mention) -- deliberately
# narrow, same discipline as the two-fact features elsewhere in this
# codebase, because "foreign income" alone is far too generic a phrase
# to safely trigger a worldwide-income recharacterization on its own.
NRA_FOREIGN_INCOME_CITATION = "FTB 2025 Schedule CA (540) Instructions -- Part I, Section B, Line 8z"
NRA_FOREIGN_INCOME_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

NRA_SELF_ID_TERMS = {
    "nonresident alien", "non-resident alien", "federal nonresident alien",
    "form 1040-nr", "1040-nr", "1040nr",
}
NRA_FOREIGN_INCOME_AMOUNT_TERMS = {
    "foreign source income", "foreign income", "income from foreign sources",
}
NRA_FOREIGN_LOSS_AMOUNT_TERMS = {
    "foreign source loss", "foreign source losses", "foreign loss", "foreign losses",
    "loss from foreign sources",
}
NRA_FOREIGN_INCOME_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "dependent", "trust", "estate", "gambling", "gambled", "betting",
    "wagering", "alimony", "pension", "rental", "renting", "rented",
    "stock", "rsu",
}


def _nra_foreign_income_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in NRA_SELF_ID_TERMS):
        return False
    if not any(t in q for t in NRA_FOREIGN_INCOME_AMOUNT_TERMS | NRA_FOREIGN_LOSS_AMOUNT_TERMS):
        return False
    if any(t in q for t in NRA_FOREIGN_INCOME_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_nra_foreign_income_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'CA-resident,
    federal-nonresident-alien, other income plus a stated foreign-source
    income or loss figure' question."""
    q = question.lower()
    if not _nra_foreign_income_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_nra_foreign_income_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _nra_foreign_income_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_nra_foreign_income_is_loss(question: str) -> bool:
    """True iff the foreign-source figure is described as a LOSS
    (subtraction, column B) rather than income (addition, column C).
    The loss/income term sets are disjoint vocabulary (no shared stem
    the way HSA gain/loss share "hsa investment"), so a plain substring
    check on the loss set alone is unambiguous."""
    q = question.lower()
    return any(t in q for t in NRA_FOREIGN_LOSS_AMOUNT_TERMS)


def compute_nra_foreign_income_ca_tax(conn, other_income: float, foreign_amount: float,
                                        is_loss: bool, filing_status: str,
                                        tax_year: int = DEFAULT_TAX_YEAR):
    """other_income is the taxpayer's other (U.S.-source/effectively-
    connected) income already reported federally -- e.g. wages -- treated
    as AGI-equivalent before this adjustment (no other adjustments, same
    simplification used throughout). foreign_amount is the stated
    foreign-source income (is_loss False, ADDED -- federal Form 1040-NR
    generally excludes non-ECI foreign income entirely, so California
    adds it back to reach worldwide income) or foreign-source loss
    (is_loss True, SUBTRACTED, floored at zero -- symmetric treatment,
    same floor pattern as every other subtraction path in this module)."""
    if other_income is None or other_income < 0:
        return None
    if foreign_amount is None or foreign_amount <= 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    if is_loss:
        agi = max(0.0, other_income - foreign_amount)
    else:
        agi = other_income + foreign_amount
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "other_income": other_income, "foreign_amount": foreign_amount,
            "is_loss": is_loss, "agi": agi, "standard_deduction": dedu["amount"]}


# --- Personal/Blind/Senior/Dependent Exemption Credits (Form 540 Lines
# 7-10, Line 32 AGI Limitation Worksheet) -- Income Coverage Blueprint
# Phase 3's highest-frequency finding: every California resident filer
# receives at least the Personal Exemption Credit, yet nothing in this
# codebase computed it before now -- income_nonresident.py's own module
# docstring already flagged this as a KNOWN, DISCLOSED gap ("a pre-
# existing gap in the RESIDENT engine too... omitting it makes the
# computed tax a small overestimate, never an underestimate"). This is
# the first feature built to actually CLOSE that gap, as its OWN
# standalone opt-in question path rather than retrofitting every
# existing feature's computation -- a deliberate scope decision (see
# form540_inventory.py's ledger note) since integrating it into
# compute_ca_tax itself would change ~300 already-verified regression
# expectations across every feature built this session.
#
# DOLLAR FIGURES VERIFIED DIRECTLY against the actual 2025 Form 540 PDF
# (Side 1 Lines 7-9, Side 2 Line 10) and the 2025 Form 540 Booklet's AGI
# Limitation Worksheet (Page 14) -- not secondary tax-prep aggregator
# sites, after an earlier research pass's OTHER specific-sounding claim
# (the Behavioral Health Services Tax) turned out to be flatly wrong
# once independently checked, which raised the bar for what "verified"
# means before trusting a number here.
#
# THE CREDIT IS A TAX CREDIT (subtracted from computed TAX, Form 540
# Line 32), NOT a deduction from taxable income -- structurally
# different from the standard/itemized deduction this codebase already
# applies before bracket computation. IMPORTANT ORDERING: the credit
# reduces the BRACKET tax only (Form 540 Lines 31-48's chain) -- the
# Behavioral Health Services Tax surtax (Line 62) is computed
# INDEPENDENTLY on taxable income and added back AFTER, unaffected by
# this or any other nonrefundable credit. compute_exemption_credit_ca_
# tax below applies the credit to compute_ca_tax's own "bracket_tax"
# field specifically, then re-adds "surtax" unchanged -- NOT a
# subtraction from "total_tax" directly, which would incorrectly also
# shrink the surtax portion.
#
# MECHANIC (Form 540 Line 7's own wording): personal exemption units
# default to 2 for MFJ/QSS, 1 for every other filing status -- this is
# NOT a separately-set $306 figure, it's the SAME $153 rate multiplied
# by a unit count baked into the filing status itself. Blind/Senior
# exemptions are SEPARATE optional units (0, 1, or 2 each -- one per
# qualifying spouse/RDP) at the SAME $153 rate, but are NOT MODELED in
# this first build (always 0) -- see EXEMPTION_CREDIT_TERMS's module
# note for why the trigger vocabulary itself was scoped narrowly this
# round; a documented, disclosed simplification (omission makes the
# computed credit a small UNDERestimate, i.e. computed TAX a small safe-
# direction OVERestimate, same direction as every other undiscovered
# gap this codebase already accepts). Dependent exemptions are a
# SEPARATE $475-per-dependent group, phased out INDEPENDENTLY from the
# personal/blind/senior group per FTB's own AGI Limitation Worksheet.
#
# PHASE-OUT (verbatim from the AGI Limitation Worksheet, lines a-n): for
# every $2,500 of AGI over the filing-status threshold ($1,250 if MFS),
# ROUNDED UP to the next whole step, each personal/blind/senior
# exemption UNIT loses $6, and each dependent exemption loses $6
# separately -- NOT a percentage and NOT a hard cliff. Both groups are
# floored at $0 independently (e.g. a taxpayer with enough dependents
# can have the personal-group credit phase out to $0 while the
# dependent-group credit is still meaningfully positive, or vice versa).
EXEMPTION_CREDIT_CITATION = "FTB 2025 Form 540, Side 1 Lines 7-9, Side 2 Line 10, and Line 32 AGI Limitation Worksheet"
EXEMPTION_CREDIT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-booklet.html"

PERSONAL_EXEMPTION_UNIT_AMOUNT = 153.0
DEPENDENT_EXEMPTION_AMOUNT = 475.0
EXEMPTION_CREDIT_AGI_THRESHOLD = {
    "single": 252203.0, "mfs": 252203.0, "hoh": 378310.0,
    "mfj": 504411.0, "qss": 504411.0,
}
EXEMPTION_CREDIT_PHASEOUT_STEP = 2500.0       # $1,250 for MFS
EXEMPTION_CREDIT_PHASEOUT_STEP_MFS = 1250.0
EXEMPTION_CREDIT_PHASEOUT_PER_STEP = 6.0      # $6 lost per exemption unit per step

# Requires EXPLICIT "exemption credit" phrasing to trigger -- deliberately
# narrower than the most natural real-world phrasing ("...with 2
# dependents", no "exemption credit" wording at all) would allow,
# because bare "dependent"/"children" vocabulary is ALREADY heavily used
# elsewhere in this codebase for OTHER features' own purposes (HOH
# determination, Joint Custody HOH Credit, Dependent Parent Credit,
# CalEITC/YCTC/FYTC's child-count facts) -- widening this feature's
# trigger to bare dependent-count phrasing risks silently stealing
# questions those features are already correctly handling, a
# dispatcher-collision audit this build does not attempt. Narrower-but-
# safe now, same "build the tractable slice, don't force the rest"
# discipline as everywhere else in this codebase; broadening the
# trigger vocabulary is a real, but separate, future extension (see
# form540_inventory.py's ledger note).
EXEMPTION_CREDIT_TERMS = {
    "exemption credit", "exemption credits", "personal exemption credit",
    "dependent exemption credit", "blind exemption credit",
    "senior exemption credit", "california exemption credit",
}
# Deliberately (almost) the FULL COMPLEXITY_EXCLUDE, not a narrower
# purpose-built set -- unlike the itemized-deduction-style optional
# add-ons, this feature mirrors the PLAIN wage-only bracket path's own
# scope exactly (compute_ca_tax on a single gross-income figure, no
# SE-tax deduction, no K-1/entity math). A self-employment or K-1
# question that ALSO mentions "exemption credit" must defer, not
# silently treat the net-profit/K-1 figure as wage-equivalent -- same
# "wrong number silently used" bug class already fixed for other
# features this session (e.g. IRA deduction + self-employment).
#
# "dependent" SUBTRACTED BACK OUT -- found live via testing: bare
# "dependent" sits in COMPLEXITY_EXCLUDE to block OTHER paths (HOH/
# credit questions usually signal real complexity for THOSE features),
# but this feature's own dependent-exemption-credit fact is exactly
# "N dependents" -- reusing COMPLEXITY_EXCLUDE unmodified made this
# feature self-exclude the instant a user stated the one optional fact
# it's specifically built to accept. Same "subtract the trigger term
# back out of a reused shared exclude set" pattern as cannabis 280E's
# fix for the identical self-referential-exclusion bug class earlier
# this session.
EXEMPTION_CREDIT_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE - {"dependent"}


def _exemption_credit_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in EXEMPTION_CREDIT_TERMS):
        return False
    if any(t in q for t in EXEMPTION_CREDIT_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_exemption_credit_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'wage income
    with a stated exemption-credit question' -- requires the explicit
    EXEMPTION_CREDIT_TERMS phrasing (see that set's module note for why
    bare dependent-count phrasing is deliberately NOT enough here)."""
    q = question.lower()
    if not _exemption_credit_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_exemption_credit_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _exemption_credit_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_exemption_credit_dependent_count(question: str):
    """Returns a stated dependent COUNT (int) if the question states one
    for the dependent exemption credit, or None if not stated (treated
    as 0 dependents -- the personal exemption credit alone is still
    computed; a missing dependent count does NOT block the whole
    feature the way a missing filing status does)."""
    q = question.lower()
    m = re.search(r"(\d+)\s*dependent", q)
    if m:
        return int(m.group(1))
    if re.search(r"\bone dependent\b", q):
        return 1
    if re.search(r"\btwo dependents\b", q):
        return 2
    if re.search(r"\bthree dependents\b", q):
        return 3
    return None


def compute_exemption_credit(filing_status: str, dependent_count: int = 0, agi: float = None):
    """Form 540 Lines 7-10 + Line 32 AGI Limitation Worksheet. Personal
    exemption units default to 2 for MFJ/QSS, 1 otherwise (Line 7's own
    "enter 1 or 2" mechanic -- not a separate input). Blind/senior
    exemption units are NOT modeled in this first build (always 0) --
    see the module note above. dependent_count defaults to 0 (personal
    exemption credit alone, the universal case). Returns the personal/
    blind/senior GROUP total and the dependent GROUP total (phased out
    INDEPENDENTLY per FTB's own worksheet), each floored at 0
    separately, plus their sum."""
    if filing_status not in EXEMPTION_CREDIT_AGI_THRESHOLD:
        return None
    if dependent_count is None or dependent_count < 0:
        return None
    personal_units = 2 if filing_status in ("mfj", "qss") else 1
    personal_group_before = personal_units * PERSONAL_EXEMPTION_UNIT_AMOUNT
    dependent_group_before = dependent_count * DEPENDENT_EXEMPTION_AMOUNT

    result = {
        "personal_units": personal_units, "dependent_count": dependent_count,
        "personal_group_before": personal_group_before,
        "dependent_group_before": dependent_group_before,
        "phaseout_applied": False, "steps": 0, "reduction_per_unit": 0.0,
    }
    threshold = EXEMPTION_CREDIT_AGI_THRESHOLD[filing_status]
    if agi is None or agi <= threshold:
        result["personal_group"] = personal_group_before
        result["dependent_group"] = dependent_group_before
        result["total"] = personal_group_before + dependent_group_before
        return result

    step_size = EXEMPTION_CREDIT_PHASEOUT_STEP_MFS if filing_status == "mfs" else EXEMPTION_CREDIT_PHASEOUT_STEP
    steps = math.ceil((agi - threshold) / step_size)
    reduction_per_unit = steps * EXEMPTION_CREDIT_PHASEOUT_PER_STEP
    personal_group_after = max(0.0, personal_group_before - reduction_per_unit * personal_units)
    dependent_group_after = max(0.0, dependent_group_before - reduction_per_unit * dependent_count)
    result.update({
        "phaseout_applied": True, "steps": steps, "reduction_per_unit": reduction_per_unit,
        "personal_group": personal_group_after, "dependent_group": dependent_group_after,
        "total": personal_group_after + dependent_group_after, "threshold": threshold,
    })
    return result


def compute_exemption_credit_ca_tax(conn, income_amount: float, filing_status: str,
                                      dependent_count: int = 0, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as gross income/AGI, same simplification
    as every other compute path -- also used directly as the Line 32
    AGI Limitation Worksheet's own AGI figure (Form 540 Line 13, BEFORE
    the standard/itemized deduction). The exemption credit reduces
    compute_ca_tax's "bracket_tax" specifically, floored at 0, then the
    surtax is re-added UNCHANGED -- see the module note above for why
    (the Behavioral Health Services surtax is computed independently on
    taxable income and is not reduced by nonrefundable credits)."""
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    exemption = compute_exemption_credit(filing_status, dependent_count, income_amount)
    if not exemption:
        return None
    bracket_tax_before_credit = calc["bracket_tax"]
    bracket_tax_after_credit = max(0.0, bracket_tax_before_credit - exemption["total"])
    total_tax_after_credit = round(bracket_tax_after_credit + calc["surtax"], 2)
    return {**calc, "income_amount": income_amount, "standard_deduction": dedu["amount"],
            "taxable_income": taxable_income, "exemption": exemption,
            "bracket_tax_before_credit": bracket_tax_before_credit,
            "bracket_tax": bracket_tax_after_credit, "total_tax": total_tax_after_credit}


# --- Estimated Use Tax Lookup Table (Form 540 Line 91) -- Income
# Coverage Blueprint Phase 3's second-priority finding: California
# residents who buy from out-of-state/online retailers without CA tax
# collected owe USE tax, self-reported on Form 540 Line 91, and FTB
# publishes a flat AGI-band lookup table specifically so most filers
# never have to track individual purchases. Genuinely simpler than the
# exemption credit above: no filing status at all, just California AGI.
#
# TABLE VERIFIED DIRECTLY against the actual 2025 Form 540 Booklet PDF
# (downloaded and extracted locally, Page 17-18, "Estimated Use Tax
# Lookup Table") -- all 14 flat-dollar bands plus the top formula band,
# not sampled/extrapolated from a partial quote.
#
# USES CALIFORNIA AGI (Form 540 LINE 17), NOT federal AGI (Line 13) --
# FTB's own instruction: "include the use tax liability that corresponds
# to your California Adjusted Gross Income (found on Line 17)." This
# codebase's usual "income_amount treated as gross income/AGI" trust-
# the-input simplification applies the same way here.
#
# SCOPE CAP, confirmed from FTB's own text and NOT modeled as a silent
# assumption: the lookup table only covers "individual non-business
# items you purchased for less than $1,000 each." Purchases of $1,000+
# per item, or any business purchase, must use the separate Use Tax
# Worksheet (actual price x district tax rate) instead -- a genuinely
# different, multi-input computation this feature does not attempt.
# Detected explicitly (USE_TAX_OVER_CAP_TERMS) and routed to a dedicated
# clarifying message rather than silently misapplying the flat lookup
# to a purchase FTB's own rule says doesn't qualify for it.
USE_TAX_CITATION = "FTB 2025 Form 540 Instructions, Line 91 -- Estimated Use Tax Lookup Table"
USE_TAX_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-booklet.html"

USE_TAX_LOOKUP_TABLE = [
    # (agi_floor, agi_ceiling_inclusive, flat_amount)
    (0.0, 19999.99, 1.0),
    (20000.0, 29999.99, 2.0),
    (30000.0, 39999.99, 3.0),
    (40000.0, 49999.99, 4.0),
    (50000.0, 59999.99, 5.0),
    (60000.0, 69999.99, 6.0),
    (70000.0, 79999.99, 7.0),
    (80000.0, 89999.99, 8.0),
    (90000.0, 99999.99, 9.0),
    (100000.0, 124999.99, 11.0),
    (125000.0, 149999.99, 14.0),
    (150000.0, 174999.99, 16.0),
    (175000.0, 199999.99, 19.0),
]
USE_TAX_TOP_THRESHOLD = 199999.99   # "More than $199,999" -- i.e. $200,000+
USE_TAX_TOP_RATE = 0.0001            # 0.010% of AGI
USE_TAX_PER_ITEM_CAP = 1000.0

USE_TAX_TERMS = {"use tax", "estimated use tax", "use tax lookup"}
USE_TAX_OVER_CAP_TERMS = {
    "1,000 or more", "$1,000 or more", "over $1,000", "over 1,000",
    "more than $1,000", "more than 1,000", "1000 or more", "$1000 or more",
    "business purchase", "for my business", "business item", "for business use",
}
USE_TAX_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "trust", "estate", "gambling", "gambled", "betting", "wagering",
}


def detect_use_tax_signal(question: str):
    """True iff this looks like a genuine 'estimate my use tax from
    stated AGI' question -- 'use tax' is distinctive enough vocabulary
    on its own (no separate compute-trigger phrase required, unlike
    other features, since a stated AGI figure alongside this term is
    already unambiguous intent). Does NOT require filing status --
    FTB's own lookup table is AGI-only, no filing-status distinction."""
    q = question.lower()
    if not any(t in q for t in USE_TAX_TERMS):
        return False
    if any(t in q for t in USE_TAX_COMPLEXITY_EXCLUDE):
        return False
    return True


def detect_use_tax_over_cap(question: str) -> bool:
    """True iff the question explicitly describes a purchase the
    Estimated Use Tax Lookup Table doesn't cover ($1,000+ per item, or
    any business purchase) -- routes to a dedicated clarifying message
    instead of silently misapplying the flat AGI lookup to a purchase
    FTB's own rule excludes from it."""
    q = question.lower()
    return any(t in q for t in USE_TAX_OVER_CAP_TERMS)


def compute_estimated_use_tax(ca_agi: float):
    """Form 540 Line 91's Estimated Use Tax Lookup Table -- a flat
    dollar amount by California AGI band, or 0.01% of AGI above
    $199,999. Returns None if ca_agi is missing/negative."""
    if ca_agi is None or ca_agi < 0:
        return None
    if ca_agi > USE_TAX_TOP_THRESHOLD:
        return round(ca_agi * USE_TAX_TOP_RATE, 2)
    for floor, ceiling, amount in USE_TAX_LOOKUP_TABLE:
        if floor <= ca_agi <= ceiling:
            return amount
    return None


# --- Other State Tax Credit (Schedule S (540), credit code 187) --
# Income Coverage Blueprint Phase 3's third build, and the most complex
# feature this session: California residents whose income was taxed by
# BOTH California and another state (remote workers, multi-state
# earners) can credit some of the double taxation back. Likely the most
# commonly-needed UNBUILT credit given how common CA-plus-another-state
# income situations are -- flagged as the highest-priority remaining
# Phase 3 credit finding.
#
# VERIFIED DIRECTLY against the actual 2025 Schedule S PDF (Part I/II's
# own line-by-line worksheet, downloaded and extracted locally, cross-
# checked against a 2016 standalone copy to confirm the mechanic hasn't
# materially changed) -- NOT the single-proration shape an earlier
# broad survey pass assumed. It's TWO INDEPENDENT prorations, one per
# side, and the credit is the LESSER of the two:
#   Line 5  = min(1.0, double-taxed income taxable by CA / CA AGI)
#   Line 6  = CA tax liability x Line 5
#   Line 10 = min(1.0, double-taxed income taxable by other state / other-state AGI)
#   Line 11 = other-state tax paid x Line 10
#   Line 12 (the credit) = min(Line 6, Line 11)
# Confirmed: the CA-side denominator is CA AGI (Form 540 Line 17), NOT
# CA taxable income -- a real distinction from what a naive reading of
# "proration" might assume.
#
# SIMPLIFICATION, disclosed (not silently assumed): Schedule S Part I
# tracks the double-taxed income amount in TWO columns -- taxable by CA
# (b) and taxable by the other state (c) -- which can genuinely differ,
# but for the common single-item case (e.g. wages earned entirely in
# another state while a CA resident) they're the same dollar figure.
# This feature asks for ONE double-taxed-income amount and uses it for
# BOTH Line 3 and Line 8 -- correct for that common case, an
# approximation if a taxpayer's actual Part I breakdown diverges.
#
# "CA TAX LIABILITY" (Line 2) IS NOT A SEPARATE STATED FACT -- Schedule
# S's own Line 2 instruction points straight to "Form 540, line 48",
# a number this codebase already computes (compute_ca_tax's bracket_tax)
# rather than something to ask the taxpayer to independently state and
# risk getting wrong. Genuinely stated facts needed are the 4 figures
# external to this return: CA income (also serves as the Line 4 CA AGI
# denominator), double-taxed income, the other state's AGI, and tax
# actually paid to the other state -- all things a multi-state filer
# would read directly off their other-state return.
#
# ORDERING, same pattern as the exemption credit above: the credit
# reduces compute_ca_tax's "bracket_tax" specifically (Schedule S's own
# Line 2 = Form 540 Line 48, which is BEFORE the Behavioral Health
# Services surtax is added at Line 62) -- surtax is re-added unchanged
# afterward, not reduced by this or any other nonrefundable credit.
#
# ELIGIBILITY, confirmed not assumed: this credit's own resident-
# specific computation path matches exactly this codebase's existing
# resident-only (Schedule CA (540)/Form 540) population -- no mismatch.
# One real limitation confirmed from FTB's own text and NOT modeled:
# "No credit is allowed if the other state allows California residents
# a credit for net income taxes paid to California" (an anti-double-
# benefit rule) -- not detectable from a single question, disclosed in
# the answer text rather than silently ignored. Also not modeled: the
# credit cannot offset California AMT (this codebase doesn't compute
# AMT at all, so this is moot in practice, not a gap specific to this
# feature).
OTHER_STATE_TAX_CREDIT_CITATION = "FTB 2025 Schedule S (540) Instructions, Part I-II"
OTHER_STATE_TAX_CREDIT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-s-instructions.html"

OTHER_STATE_TAX_CREDIT_TERMS = {
    "other state tax credit", "other state's tax credit", "schedule s",
    "credit for taxes paid to another state", "credit for tax paid to another state",
}
DOUBLE_TAXED_INCOME_TERMS = {
    "double-taxed income", "double taxed income", "income taxed by both states",
}
OTHER_STATE_AGI_TERMS = {
    "other state agi", "other state's agi", "other-state agi",
    "the other state's adjusted gross income", "agi in the other state",
    "adjusted gross income in the other state",
}
OTHER_STATE_TAX_PAID_TERMS = {
    "tax paid to the other state", "other state tax paid",
    "income tax paid to the other state", "paid in the other state",
}
OTHER_STATE_TAX_CREDIT_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _other_state_tax_credit_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in OTHER_STATE_TAX_CREDIT_TERMS):
        return False
    if any(t in q for t in OTHER_STATE_TAX_CREDIT_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_other_state_tax_credit_signal(question: str):
    """Returns filing_status iff this looks like a genuine Other State
    Tax Credit question -- mirrors the plain wage-only path's scope
    exactly (full COMPLEXITY_EXCLUDE, no self-referential collision
    since none of this feature's own vocabulary overlaps that set)."""
    q = question.lower()
    if not _other_state_tax_credit_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_other_state_tax_credit_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _other_state_tax_credit_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_other_state_tax_credit(ca_tax_liability: float, ca_agi: float, double_taxed_income: float,
                                     other_state_agi: float, other_state_tax_paid: float):
    """Schedule S (540) Part II, Lines 2-12, verbatim worksheet -- see
    module note above for the exact line mapping and the "same double-
    taxed income figure used for both columns" simplification."""
    if any(v is None for v in (ca_tax_liability, ca_agi, double_taxed_income,
                                 other_state_agi, other_state_tax_paid)):
        return None
    if ca_agi <= 0 or other_state_agi <= 0:
        return None
    if ca_tax_liability < 0 or double_taxed_income < 0 or other_state_tax_paid < 0:
        return None
    ca_ratio = min(1.0, double_taxed_income / ca_agi)
    ca_side = round(ca_tax_liability * ca_ratio, 2)
    other_ratio = min(1.0, double_taxed_income / other_state_agi)
    other_side = round(other_state_tax_paid * other_ratio, 2)
    credit = min(ca_side, other_side)
    return {"ca_ratio": ca_ratio, "ca_side": ca_side,
            "other_ratio": other_ratio, "other_side": other_side, "credit": credit}


def compute_other_state_tax_credit_ca_tax(conn, income_amount: float, filing_status: str,
                                            double_taxed_income: float, other_state_agi: float,
                                            other_state_tax_paid: float, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as CA gross income/AGI, same
    simplification as everywhere else -- also serves as Schedule S
    Line 4 (CA AGI). "CA tax liability" (Line 2) is NOT a separate
    stated fact -- computed via compute_ca_tax on this income, same as
    the plain wage-bracket path, not asked from the taxpayer (see
    module note above). The credit reduces bracket_tax specifically,
    with the surtax re-added unchanged afterward -- see module note."""
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    credit_calc = compute_other_state_tax_credit(
        calc["bracket_tax"], income_amount, double_taxed_income, other_state_agi, other_state_tax_paid)
    if not credit_calc:
        return None
    bracket_tax_before_credit = calc["bracket_tax"]
    bracket_tax_after_credit = max(0.0, bracket_tax_before_credit - credit_calc["credit"])
    total_tax_after_credit = round(bracket_tax_after_credit + calc["surtax"], 2)
    return {**calc, "income_amount": income_amount, "standard_deduction": dedu["amount"],
            "taxable_income": taxable_income, "double_taxed_income": double_taxed_income,
            "other_state_agi": other_state_agi, "other_state_tax_paid": other_state_tax_paid,
            "credit": credit_calc, "bracket_tax_before_credit": bracket_tax_before_credit,
            "bracket_tax": bracket_tax_after_credit, "total_tax": total_tax_after_credit}


# --- Pass-Through Entity (PTE) Elective Tax Credit (FTB 3804-CR,
# credit code 242) -- Income Coverage Blueprint Phase 3's fourth build.
# A CORRECTION was needed before building: the broad Phase 3 survey
# sketched this as a pure single-number pass-through ("the 9.3% figure
# is already on the K-1, trust it"), but a dedicated verification pass
# found this is the THIRD claim from that same survey to be wrong or
# incomplete once independently checked (after the Behavioral Health
# Services Tax miss and the Other State Tax Credit's formula). FTB 3804-
# CR is a genuine small worksheet, not a scalar: Part I sums K-1 credit
# amount(s); Part II adds a PRIOR-YEAR CARRYOVER, caps the CURRENT-YEAR
# usable amount at remaining CA tax liability (nonrefundable), and
# tracks an unused remainder that carries forward up to 5 years.
#
# THE 9.3% RATE ITSELF IS 100% ENTITY-SIDE, confirmed directly from FTB
# 3804/3804-CR instructions -- the electing PTE computes 9.3% of its
# qualified net income and reports the resulting DOLLAR credit on each
# owner's K-1; the individual taxpayer never recomputes the rate, only
# transcribes the stated figure. This module trusts that stated figure
# entirely, same "trust the input" precedent as every other K-1-sourced
# item in this codebase.
#
# TRACTABLE SLICE, same pattern as NOL/EBL/disaster-loss/capital-loss
# carryovers already built this session: CURRENT-YEAR absorption only.
# total_available = k1_credit_amount + prior_year_carryover (prior-year
# carryover optional, defaults to 0 -- the common first-time-claimant
# case); credit_used = min(total_available, this taxpayer's OWN CA tax
# liability); any excess is DISCLOSED as carrying forward (up to 5
# years, no carryback, verified from FTB's own text), not tracked into
# a future computation this stateless system can't perform.
#
# NOT MODELED, disclosed rather than silently assumed: Schedule P's
# credit-ORDERING against OTHER nonrefundable credits (if a taxpayer
# also claims the Other State Tax Credit or another Schedule P Section B
# credit, FTB's own form claims this credit AFTER those -- this feature
# caps only against the taxpayer's own gross CA tax liability, not
# against what's left after other credits, since each standalone
# feature in this codebase is independent, same precedent as the
# exemption credit/OSTC not coordinating with each other either); the
# AMT/TMT nuance (this credit cannot reduce AMT itself, but CAN reduce
# regular tax below TMT -- moot here since this system doesn't compute
# AMT/TMT at all); the SMLLC-specific limitation (a narrower sub-case);
# and the "not assignable, one credit per married/RDP couple" rules.
PTE_CREDIT_CITATION = "FTB 3804-CR Instructions, Part I-II; R&TC Section 17052.10"
PTE_CREDIT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2024/2024-3804-cr-instructions.html"

PTE_CREDIT_TERMS = {
    "pte elective tax credit", "pass-through entity elective tax credit",
    "pte credit", "form 3804-cr",
}
PTE_CREDIT_CARRYOVER_TERMS = {
    "pte credit carryover", "prior year pte credit", "pte carryover",
    "pte credit carryover from a prior year",
}
PTE_CREDIT_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _pte_credit_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in PTE_CREDIT_TERMS):
        return False
    if any(t in q for t in PTE_CREDIT_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_pte_credit_signal(question: str):
    """Returns filing_status iff this looks like a genuine PTE Elective
    Tax Credit question -- mirrors the plain wage-only path's scope
    exactly (full COMPLEXITY_EXCLUDE; no self-referential collision,
    none of this feature's own vocabulary overlaps that set)."""
    q = question.lower()
    if not _pte_credit_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_pte_credit_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _pte_credit_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_pte_credit(k1_credit_amount: float, prior_year_carryover: float, ca_tax_liability: float):
    """FTB 3804-CR Part II, current-year absorption only -- see module
    note above. prior_year_carryover may be 0 (the common first-time-
    claimant case)."""
    if k1_credit_amount is None or k1_credit_amount < 0:
        return None
    prior_year_carryover = prior_year_carryover or 0.0
    if prior_year_carryover < 0:
        return None
    if ca_tax_liability is None or ca_tax_liability < 0:
        return None
    total_available = k1_credit_amount + prior_year_carryover
    credit_used = min(total_available, ca_tax_liability)
    remaining_carryover = total_available - credit_used
    return {"total_available": total_available, "credit_used": credit_used,
            "remaining_carryover": remaining_carryover}


def compute_pte_credit_ca_tax(conn, income_amount: float, filing_status: str, k1_credit_amount: float,
                                prior_year_carryover: float = 0.0, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as CA gross income/AGI, same
    simplification as everywhere else. The credit reduces bracket_tax
    specifically, with surtax re-added unchanged afterward -- same
    ordering as the exemption credit/OSTC (Schedule P's own credit-
    ordering happens before the surtax is added at Form 540 Line 62)."""
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    credit_calc = compute_pte_credit(k1_credit_amount, prior_year_carryover, calc["bracket_tax"])
    if not credit_calc:
        return None
    bracket_tax_before_credit = calc["bracket_tax"]
    bracket_tax_after_credit = max(0.0, bracket_tax_before_credit - credit_calc["credit_used"])
    total_tax_after_credit = round(bracket_tax_after_credit + calc["surtax"], 2)
    return {**calc, "income_amount": income_amount, "standard_deduction": dedu["amount"],
            "taxable_income": taxable_income, "k1_credit_amount": k1_credit_amount,
            "prior_year_carryover": prior_year_carryover, "credit": credit_calc,
            "bracket_tax_before_credit": bracket_tax_before_credit,
            "bracket_tax": bracket_tax_after_credit, "total_tax": total_tax_after_credit}


# --- Late-filing / late-payment penalties (Form 540 Line 112, R&TC
# Sections 19131/19132) -- Income Coverage Blueprint Phase 3's fifth
# build, and architecturally DIFFERENT from every feature built earlier
# this session: no filing status, no bracket/income computation at all
# -- a flat percentage of a stated UNPAID BALANCE, filing-status-
# agnostic (confirmed directly from the statute text).
#
# A dedicated verification pass found this is genuinely MORE complex
# than the broad Phase 3 survey's sketch ("5%/month up to 25%, min $135
# if >60 days late" for filing; "5% + 0.5%/month" for payment) --
# missing the payment penalty's own 25% cap/40-month ceiling, and
# entirely missing the REQUIRED offset between the two penalties
# (R&TC 19132(b)): the late-payment penalty is reduced dollar-for-
# dollar by the late-filing penalty for the same period -- skipping
# this would DOUBLE-COUNT whenever both penalties apply to the same
# late return, the common case. This is now the FOURTH claim from that
# same broad survey pass to be wrong or incomplete once independently
# checked (after the Behavioral Health Services Tax, OSTC's formula,
# and the PTE credit's "pure pass-through" claim).
#
# CORE FORMULAS, verified directly against R&TC 19131(a)/19132(a),(b):
#   Late-filing: 5% of the unpaid balance per month or FRACTION
#     thereof (any partial month counts as a full month), from the
#     ORIGINAL due date, capped at 25%.
#   Late-payment: 5% flat + 0.5% per month or fraction thereof, capped
#     at 25% total (the cap is reached exactly at 40 months, matching
#     the statute's own "not to exceed 40 months" language).
#   Offset: late_payment_assessed = max(0, late_payment_computed -
#     late_filing_computed) -- NOT optional; this is a verified,
#     tractable rule using the SAME inputs already collected, not a
#     scope decision to skip.
#   Total = late_filing + late_payment_assessed.
#
# DELIBERATELY OUT OF SCOPE, disclosed rather than silently omitted --
# unlike the offset above, each of these genuinely needs facts beyond
# what a single question reasonably asks for, or is a moving target:
#   - The $135-minimum-penalty test: this only applies once a return is
#     MORE than 60 days late measured from CA's AUTOMATIC 6-month filing
#     extension (October 15, granted to every taxpayer with no request
#     needed) -- a SEPARATE clock from the 5%/month accrual, which
#     starts from the ORIGINAL due date (April 15) regardless of the
#     extension. Modeling this correctly needs a second date input this
#     feature doesn't ask for; in practice this floor only binds for
#     filers roughly 8+ months late, a narrow slice of the population
#     this feature already serves via the core formula.
#   - Interest: mandatory, separate from both penalties, compounds
#     DAILY at a rate that changes semi-annually (confirmed 0%-8% range
#     over the past ~15 years) -- a genuinely moving external parameter,
#     not something to hard-code.
#   - Reasonable-cause abatement: a case-by-case FTB determination based
#     on facts and circumstances -- not something this system should
#     auto-apply from a stated fact. Detected as its own signal
#     (LATE_PENALTY_COMPLEXITY_EXCLUDE) and redirected to a dedicated
#     message rather than silently computed as if no exception applied.
#   - The one-time Timeliness Penalty Abatement (R&TC 19132.5): a real,
#     tractable-in-principle relief mechanism, but adds several more
#     gating facts to an already multi-fact feature -- left for a
#     future extension, not this build.
#
# SIMPLIFICATION: assumes filing and full payment happened at the SAME
# time (one stated "months late" figure drives both penalty
# accruals) -- correct when a taxpayer pays in full when they finally
# file, the common case; a real approximation if payment happened on a
# different date than filing.
LATE_PENALTY_CITATION = "R&TC Sections 19131 (late filing) and 19132 (late payment)"
LATE_PENALTY_SOURCE_URL = "https://www.ftb.ca.gov/pay/penalties-and-interest/index.html"

LATE_PENALTY_FILING_RATE = 0.05        # 5% per month, capped at 25%
LATE_PENALTY_FILING_CAP = 0.25
LATE_PENALTY_PAYMENT_BASE_RATE = 0.05  # 5% flat
LATE_PENALTY_PAYMENT_MONTHLY_RATE = 0.005   # + 0.5% per month
LATE_PENALTY_PAYMENT_CAP = 0.25

LATE_PENALTY_TERMS = {
    "late filing penalty", "late payment penalty", "late-filing penalty",
    "late-payment penalty", "penalty for filing late", "penalty for paying late",
    "penalty for late filing", "penalty for late payment",
}
LATE_PENALTY_REASONABLE_CAUSE_TERMS = {
    "reasonable cause", "penalty abatement", "waive my penalty", "waive the penalty",
}


def detect_late_penalty_signal(question: str) -> bool:
    """No filing status needed -- these penalties are a flat percentage
    of a stated unpaid balance, confirmed filing-status-agnostic
    directly from R&TC 19131/19132's own text."""
    q = question.lower()
    if not any(t in q for t in LATE_PENALTY_TERMS):
        return False
    if any(t in q for t in LATE_PENALTY_REASONABLE_CAUSE_TERMS):
        return False
    return True


def detect_late_penalty_reasonable_cause_mention(question: str) -> bool:
    """True iff the question asks about a reasonable-cause exception or
    penalty abatement specifically -- a case-by-case FTB determination,
    not something this system should compute a waived/reduced outcome
    for. Checked as its OWN signal (same 'specific redirect instead of
    a generic defer or a silently-wrong computation' pattern as Roth
    IRA's dedicated redirect)."""
    q = question.lower()
    return (any(t in q for t in LATE_PENALTY_TERMS)
            and any(t in q for t in LATE_PENALTY_REASONABLE_CAUSE_TERMS))


def detect_late_penalty_months_late(question: str):
    """Returns a months-late figure (float, may be fractional) or None
    -- a COUNT, not a dollar amount, extracted via its own "N months
    late" pattern rather than the shared dollar-amount regex (same
    "this is a count, not a dollar figure" distinction as the exemption
    credit's dependent-count extraction)."""
    q = question.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*months?\s*late", q)
    if m:
        return float(m.group(1))
    return None


def compute_late_penalties(unpaid_balance: float, months_late: float):
    """R&TC 19131(a)/19132(a),(b) -- core monthly-accrual formulas plus
    the required offset. See module note above for what's deliberately
    out of scope (the $135-minimum dual-clock test, interest,
    reasonable cause, the one-time abatement). "Month or fraction
    thereof" -- ANY partial month counts as a full month, so
    months_late is rounded UP before either formula runs."""
    if unpaid_balance is None or unpaid_balance < 0:
        return None
    if months_late is None or months_late <= 0:
        return None
    months_late_int = math.ceil(months_late)

    late_filing_pct = min(LATE_PENALTY_FILING_RATE * months_late_int, LATE_PENALTY_FILING_CAP)
    late_filing_penalty = round(unpaid_balance * late_filing_pct, 2)

    late_payment_pct = min(
        LATE_PENALTY_PAYMENT_BASE_RATE + LATE_PENALTY_PAYMENT_MONTHLY_RATE * months_late_int,
        LATE_PENALTY_PAYMENT_CAP)
    late_payment_computed = round(unpaid_balance * late_payment_pct, 2)

    late_payment_assessed = max(0.0, round(late_payment_computed - late_filing_penalty, 2))
    total_penalty = round(late_filing_penalty + late_payment_assessed, 2)
    return {
        "months_late_int": months_late_int,
        "late_filing_penalty": late_filing_penalty,
        "late_payment_computed": late_payment_computed,
        "late_payment_assessed": late_payment_assessed,
        "total_penalty": total_penalty,
    }


# --- California additional tax on early retirement distributions (FTB
# 3805P Part I, R&TC Section 17085) -- Income Coverage Blueprint Phase
# 3's sixth build, and the FIRST item this session where the "broad
# survey sketch was wrong" pattern led to a NARROWER build than
# originally scoped, not a corrected formula. A dedicated verification
# pass confirmed the 2.5% rate (not folklore -- R&TC 17085(c)(1) states
# "2 1/2 percent" directly, FTB 3805P Line 4 confirms "Multiply line 3
# by 2 1/2% (.025)") but found real complexity the survey's one-line
# sketch didn't capture: FTB's own instructions state directly
# "California does not conform to all of the federal exceptions to the
# additional tax on early distributions" -- TWO confirmed CA-specific
# exception-list divergences from the federal Form 5329 list (federal
# codes 17 -- phased-retirement annuitants -- and 18 -- auto-enrollment
# permissible withdrawals -- are federally valid but explicitly "Not
# applicable" for California), a 25-code, YEAR-VERSIONED exception list
# (not static -- SB 711's 2025 conformity jump added several 2025-new
# codes), a 6% override (not 2.5%) for early SIMPLE IRA distributions
# within the plan's first 2 years, and ENTIRELY DIFFERENT rates for
# other account types this module does NOT model (12.5% for Archer MSA
# non-qualified distributions under R&TC 17215; 50% for Medicare
# Advantage MSA non-qualified distributions).
#
# SCOPE, deliberately narrower than "the whole form": this models ONLY
# Part I (IRA/qualified-plan/annuity/MEC early distributions) at the
# 2.5% base rate (or 6% if the taxpayer explicitly states a SIMPLE IRA
# distribution within its first 2 years) -- the single most common
# real-world case, a taxpayer with NO exception who simply took an
# early withdrawal. Rather than build a partial/unverified 25-code
# exception table, ANY exception-flavored language in the question
# (disability, death, divorce/QDRO, medical expenses, first-time home,
# higher education, substantially equal periodic payments, birth/
# adoption, disaster, military, public safety, terminal illness,
# domestic abuse, emergency expense, etc.) routes to a dedicated
# clarifying message explaining that CA's exception list mostly but NOT
# fully matches federal's, rather than silently guessing whether that
# specific exception applies for California -- given FTB's own
# confirmed divergence on 2 of 25+ codes, guessing here is a real risk
# of a confidently WRONG answer, not just an incomplete one. Archer/
# Medicare Advantage MSA distributions are separately detected and
# deferred (different rates entirely, not modeled).
EARLY_DISTRIBUTION_CITATION = "FTB Form 3805P Instructions, Part I; R&TC Section 17085"
EARLY_DISTRIBUTION_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3805p-instructions.html"

EARLY_DISTRIBUTION_BASE_RATE = 0.025    # 2.5%, R&TC 17085(c)(1)
EARLY_DISTRIBUTION_SIMPLE_RATE = 0.06   # 6% for early-SIMPLE-IRA, 17085(c)(2)

EARLY_DISTRIBUTION_TERMS = {
    "early distribution tax", "early withdrawal tax", "early distribution penalty",
    "early withdrawal penalty", "additional tax on early distribution",
    "additional tax on my early distribution", "form 3805p", "ftb 3805p",
}
EARLY_DISTRIBUTION_SIMPLE_TERMS = {
    "simple ira", "simple plan",
}
# Broad by design (favors deferring over guessing) -- any of these
# alongside EARLY_DISTRIBUTION_TERMS routes to the dedicated exception
# clarifying message rather than a computed number, since FTB's own
# text confirms California's exception list does NOT fully match
# federal's and this module doesn't model the full 25-code table.
EARLY_DISTRIBUTION_EXCEPTION_TERMS = {
    "disability", "disabled", "died", "death", "deceased", "divorce", "qdro",
    "medical expense", "medical expenses", "first-time home", "first time home",
    "higher education", "education expense", "substantially equal periodic",
    "birth", "adoption", "disaster", "reservist", "military", "public safety",
    "terminal illness", "domestic abuse", "emergency expense", "exception",
    "levy", "irs levy", "ftb levy",
}
EARLY_DISTRIBUTION_OTHER_ACCOUNT_TERMS = {
    "archer msa", "medicare advantage msa", "coverdell", "able account", "hsa",
}
EARLY_DISTRIBUTION_COMPLEXITY_EXCLUDE = {
    "itemize", "itemized", "itemizing", "capital gain", "capital loss",
    "trust", "estate", "gambling", "gambled", "betting", "wagering",
}


def detect_early_distribution_signal(question: str) -> bool:
    """No filing status needed -- a flat percentage of a stated taxable
    distribution amount, confirmed rate-only (not bracket-dependent)
    directly from R&TC 17085's own text. Returns False (routes
    elsewhere) if exception-flavored language or a non-Part-I account
    type is present -- see the module note above for why guessing on
    either is a real risk, not just an incompleteness."""
    q = question.lower()
    if not any(t in q for t in EARLY_DISTRIBUTION_TERMS):
        return False
    if any(t in q for t in EARLY_DISTRIBUTION_COMPLEXITY_EXCLUDE):
        return False
    if any(t in q for t in EARLY_DISTRIBUTION_EXCEPTION_TERMS):
        return False
    if any(t in q for t in EARLY_DISTRIBUTION_OTHER_ACCOUNT_TERMS):
        return False
    return True


def detect_early_distribution_exception_mention(question: str) -> bool:
    """True iff exception-flavored language is present alongside this
    feature's own trigger vocabulary -- routes to a dedicated
    clarifying message (FTB's exception list doesn't fully match
    federal's, confirmed divergence on 2+ codes) rather than a silently
    guessed computation."""
    q = question.lower()
    return (any(t in q for t in EARLY_DISTRIBUTION_TERMS)
            and any(t in q for t in EARLY_DISTRIBUTION_EXCEPTION_TERMS))


def detect_early_distribution_other_account_mention(question: str) -> bool:
    """True iff a non-Part-I account type (Archer MSA, Medicare
    Advantage MSA, Coverdell, ABLE, HSA) is mentioned -- these use
    DIFFERENT rates (12.5%, 50%, or no CA analog) this module does not
    model, so a Part-I-only computation would be silently wrong if
    applied to them."""
    q = question.lower()
    return (any(t in q for t in EARLY_DISTRIBUTION_TERMS)
            and any(t in q for t in EARLY_DISTRIBUTION_OTHER_ACCOUNT_TERMS))


def compute_early_distribution_tax(taxable_distribution: float, is_simple_early: bool = False):
    """R&TC 17085(c) -- 2.5% of the TAXABLE portion of an early
    retirement distribution (already net of basis/rollovers -- same
    "trust the input" precedent as every other stated-figure feature in
    this codebase, not a gross-distribution figure), or 6% if the
    taxpayer explicitly states a SIMPLE IRA distribution within its
    first 2 plan-years."""
    if taxable_distribution is None or taxable_distribution < 0:
        return None
    rate = EARLY_DISTRIBUTION_SIMPLE_RATE if is_simple_early else EARLY_DISTRIBUTION_BASE_RATE
    tax = round(taxable_distribution * rate, 2)
    return {"rate": rate, "tax": tax}


# --- Nonrefundable Child and Dependent Care Expenses Credit (FTB Form
# 3506, credit code 232) -- Income Coverage Blueprint Phase 3's seventh
# build. A dedicated verification pass found the SAME "credit = flat %
# of the federal credit" claim from the broad survey is a valid
# shortcut only for a common but SPECIFIC case -- the SIXTH claim from
# that survey needing correction, this time about scope/validity rather
# than a missing formula detail.
#
# FTB Form 3506 is structurally a FULL PARALLEL WORKSHEET (qualifying
# expenses x a federal-AGI-based decimal chart that replicates the
# federal Form 2441 formula exactly, THEN x a SECOND, genuinely CA-
# specific federal-AGI-based decimal chart) -- it does NOT read a
# federal credit dollar figure as an input anywhere on the form. BUT:
# when (a) the taxpayer is a full-year CA resident, (b) all care was
# provided IN California (CA restricts qualifying expenses to CA-source
# care; federal has no such restriction), and (c) no employer dependent-
# care benefits were received, the form's own Line 7 chart exactly
# reproduces the federal calculation, so CA credit = federal credit x
# the CA-specific Line 9 percentage becomes an exact equivalence, not
# an approximation. This module models ONLY that case -- detected
# scope-exclusion terms (nonresident/part-year/out-of-state-care/
# employer-dependent-care-benefits) route to a dedicated clarifying
# message rather than silently applying the shortcut where it doesn't
# hold.
#
# LINE 9 PERCENTAGE TABLE, verified directly against the actual 2025
# FTB Form 3506 PDF (federal AGI, NOT California AGI -- confirmed twice
# in the instructions):
#   $40,000 or less           -> 50%
#   $40,000-$70,000           -> 43%
#   $70,000-$100,000          -> 34%
#   over $100,000             -> DISQUALIFIED ENTIRELY (a hard
#                                 eligibility cutoff, not a phase-down
#                                 to a smaller percentage -- FTB's own
#                                 form literally says "Stop. You do not
#                                 qualify for this credit.")
# Nonrefundable, no carryover (confirmed directly from Schedule P
# (540)'s own credit classification: code 232 has "no carryover
# provisions" -- any unused amount is simply lost, not tracked forward).
CDC_CREDIT_CITATION = "FTB Form 3506 Instructions, Line 9; Schedule P (540)"
CDC_CREDIT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3506-instructions.html"

CDC_CREDIT_AGI_CUTOFF = 100000.0

CDC_CREDIT_TERMS = {
    "child and dependent care credit", "child and dependent care expenses credit",
    "dependent care expenses credit", "dependent care credit", "child care credit",
    "form 3506",
}
CDC_CREDIT_FEDERAL_CREDIT_TERMS = {
    "federal child and dependent care credit", "federal dependent care credit",
    "federal credit", "my federal credit",
}
CDC_CREDIT_FEDERAL_AGI_TERMS = {
    "federal agi", "federal adjusted gross income",
}
# Detected and deferred rather than silently assumed away -- the "CA
# credit = federal credit x Line 9 percentage" shortcut this module
# relies on is only an exact equivalence for full-year CA residents
# with all care provided in California and no employer dependent-care
# benefits received.
CDC_CREDIT_OUT_OF_SCOPE_TERMS = {
    "nonresident", "non-resident", "part-year", "part year",
    "out of state", "out-of-state", "care outside california",
    "dependent care benefits", "box 10", "employer benefits",
    "employer-provided", "employer provided",
}


def detect_cdc_credit_signal(question: str) -> bool:
    q = question.lower()
    if not any(t in q for t in CDC_CREDIT_TERMS):
        return False
    if any(t in q for t in CDC_CREDIT_OUT_OF_SCOPE_TERMS):
        return False
    return True


def detect_cdc_credit_out_of_scope(question: str) -> bool:
    """True iff this feature's own trigger vocabulary is present
    alongside a scope-exclusion term -- routes to a dedicated
    clarifying message since the federal-credit-shortcut equivalence
    this module relies on doesn't hold for nonresidents/part-year
    residents, out-of-state care, or employer dependent-care benefits."""
    q = question.lower()
    return (any(t in q for t in CDC_CREDIT_TERMS)
            and any(t in q for t in CDC_CREDIT_OUT_OF_SCOPE_TERMS))


def compute_cdc_credit(federal_credit_amount: float, federal_agi: float):
    """FTB Form 3506 Line 9 -- see module note above for the exact
    federal-AGI-based percentage table and the equivalence conditions
    this shortcut relies on. Returns a dict with disqualified=True (not
    None) when federal_agi exceeds $100,000, so the caller can give a
    specific "you don't qualify" message rather than a generic
    can't-compute defer."""
    if federal_credit_amount is None or federal_credit_amount < 0:
        return None
    if federal_agi is None or federal_agi < 0:
        return None
    if federal_agi > CDC_CREDIT_AGI_CUTOFF:
        return {"disqualified": True, "pct": None, "credit": 0.0}
    if federal_agi <= 40000.0:
        pct = 0.50
    elif federal_agi <= 70000.0:
        pct = 0.43
    else:
        pct = 0.34
    credit = round(federal_credit_amount * pct, 2)
    return {"disqualified": False, "pct": pct, "credit": credit}


# --- Child Adoption Costs Credit (Form 540 Credit Chart code 197, worksheet
# in the 2025 Form 540 booklet p.15) -- Income Coverage Blueprint Phase 3's
# eighth build. A dedicated verification pass corrected an unverified ledger
# note (itself carried over from the original Phase 3 survey, whose formula
# claims have been wrong or incomplete on 7 of 8 features built so far):
# the note's 50%/$2,500-per-child/public-agency-custody claims all checked
# out true, but it was missing (a) a SECOND eligibility gate (the child must
# also be a US citizen or legal resident -- confirmed from FTB's own
# verbatim text: "In the custody of a California public agency or a
# California political subdivision" AND "A citizen or legal resident of the
# United States"), (b) the enumerated 3-category cost list (agency/DSS fees,
# unreimbursed medical expenses, family travel expenses -- not a generic
# "costs" blob), (c) a multi-year failed-then-successful-adoption
# aggregation rule, and (d) a Schedule CA (540) Line 27 addback if the same
# costs were also itemized on federal Schedule A. There is NO separate
# numbered FTB form for this credit (no "FTB 3600" exists, unlike the
# ledger's original guess) -- it's computed entirely on the unnumbered
# worksheet embedded in the Form 540 booklet itself.
#
# Modeled: the credit formula (50% of stated qualifying costs, capped
# $2,500/child) AND the real nonrefundable-with-carryover mechanic --
# unlike the CDC credit (built as a pure standalone formula since its
# federal-AGI-based amounts are small enough relative to typical CA tax
# liability at that income range to rarely bind), this credit's $2,500 cap
# is large relative to the likely CA tax liability of its target population
# (families adopting from CA foster care/public-agency custody, often
# moderate income) -- so the SAME "current-year-absorption, capped at CA
# tax liability, carryforward disclosed not tracked" pattern already used
# for the PTE credit is used here too, not the CDC credit's simpler
# standalone shape.
#
# NOT modeled, disclosed in the answer text: the second (citizenship/legal-
# residency) eligibility gate is ASSUMED satisfied rather than elicited as
# its own fact (a child already in CA public agency custody is virtually
# always a US citizen or legal resident in practice, and asking for a third
# gating fact on top of "public agency custody" was judged to add friction
# without meaningfully changing the answer for the realistic population);
# the multi-year failed-attempt aggregation rule; the federal-Schedule-A
# itemized-deduction addback (a separate Schedule CA (540) Line 27 item,
# already tracked as not_applicable in schedule_ca_inventory.py); the
# per-adoption-not-per-return cap for taxpayers adopting multiple children
# in one year (each needs its own separate $2,500-capped computation, not
# summed here); and Schedule P's credit-ordering against other nonrefundable
# credits (same "each standalone feature is independent" precedent as
# OSTC/PTE not coordinating with each other).
ADOPTION_CREDIT_CITATION = "2025 Form 540 Booklet, Credit for Child Adoption Costs Worksheet, Code 197"
ADOPTION_CREDIT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-booklet.pdf"
ADOPTION_CREDIT_RATE = 0.50
ADOPTION_CREDIT_CAP_PER_CHILD = 2500.0

ADOPTION_CREDIT_TERMS = {
    "adoption credit", "adoption costs credit", "adoption cost credit",
    "child adoption credit", "credit for adoption", "credit for child adoption",
    "credit for child adoption costs", "adoption costs tax credit",
}
ADOPTION_CREDIT_PUBLIC_AGENCY_TERMS = {
    "public agency", "foster care", "foster child", "foster son", "foster daughter",
    "county custody", "state custody", "county foster", "dependency court",
    "adopted from foster care", "adopted through foster care",
    "california public agency", "ca public agency", "department of social services",
    "adopted through the county", "adopted through the state",
    "adopted through a public agency",
}
ADOPTION_CREDIT_OUT_OF_SCOPE_TERMS = {
    "private adoption", "privately", "independent adoption",
    "international adoption", "adopted internationally", "adopted from another country",
    "out of state adoption", "out-of-state adoption", "adopted from another state",
    "stepparent adoption", "step-parent adoption", "stepchild adoption",
}
ADOPTION_CREDIT_COST_TERMS = {
    "adoption costs", "adoption expenses", "adoption cost", "adoption fees",
    "cost of adopting", "costs of adopting", "spent on the adoption",
    "paid in adoption costs", "paid in adoption expenses", "qualifying costs",
}
ADOPTION_CREDIT_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _adoption_credit_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in ADOPTION_CREDIT_TERMS):
        return False
    if any(t in q for t in ADOPTION_CREDIT_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_adoption_credit_signal(question: str):
    """Returns filing_status iff this looks like a genuine, IN-SCOPE
    adoption credit question -- requires an explicit CA-public-agency/
    foster-care signal (the credit's real eligibility gate) and no
    out-of-scope term. Ambiguous phrasing (adoption credit mentioned with
    NEITHER a public-agency signal NOR an out-of-scope term) deliberately
    returns None here so it falls through to a dedicated clarifying
    message asking specifically about public-agency custody, rather than
    silently assuming eligibility."""
    q = question.lower()
    if not _adoption_credit_base_signal_ok(q):
        return None
    if any(t in q for t in ADOPTION_CREDIT_OUT_OF_SCOPE_TERMS):
        return None
    if not any(t in q for t in ADOPTION_CREDIT_PUBLIC_AGENCY_TERMS):
        return None
    return detect_filing_status(question)


def detect_adoption_credit_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _adoption_credit_base_signal_ok(q):
        return False
    if any(t in q for t in ADOPTION_CREDIT_OUT_OF_SCOPE_TERMS):
        return False
    if not any(t in q for t in ADOPTION_CREDIT_PUBLIC_AGENCY_TERMS):
        return False
    return detect_filing_status(question) is None


def detect_adoption_credit_out_of_scope(question: str) -> bool:
    """True iff this feature's own trigger vocabulary is present
    alongside an explicit out-of-scope term (private/international/
    out-of-state/stepparent adoption) -- the credit's own eligibility
    restriction to CA-public-agency custody genuinely excludes these,
    per FTB's own text: "This credit does not apply when a child is
    adopted from another country or another state, or was not in the
    custody of a California public agency or a California political
    subdivision.\""""
    q = question.lower()
    return (any(t in q for t in ADOPTION_CREDIT_TERMS)
            and any(t in q for t in ADOPTION_CREDIT_OUT_OF_SCOPE_TERMS))


def detect_adoption_credit_ambiguous_eligibility(question: str) -> bool:
    """True iff this feature's trigger vocabulary is present but NEITHER
    a public-agency signal NOR an out-of-scope term is stated -- routes
    to a dedicated clarifying message asking specifically about the
    eligibility gate, rather than silently guessing."""
    q = question.lower()
    if not any(t in q for t in ADOPTION_CREDIT_TERMS):
        return False
    if any(t in q for t in ADOPTION_CREDIT_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in ADOPTION_CREDIT_PUBLIC_AGENCY_TERMS):
        return False
    return True


def compute_adoption_credit(qualifying_costs: float):
    """50% of stated qualifying costs, capped at $2,500 per child."""
    if qualifying_costs is None or qualifying_costs < 0:
        return None
    credit_available = round(min(qualifying_costs * ADOPTION_CREDIT_RATE, ADOPTION_CREDIT_CAP_PER_CHILD), 2)
    return {"credit_available": credit_available}


def compute_adoption_credit_ca_tax(conn, income_amount: float, filing_status: str, qualifying_costs: float,
                                     tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as CA gross income/AGI, same
    simplification as everywhere else. Nonrefundable, capped at CA tax
    liability this year (current-year absorption only, same pattern as
    the PTE credit) -- excess disclosed as carrying forward indefinitely
    per FTB's own text, not tracked across years by this system. Reduces
    bracket_tax specifically, surtax re-added unchanged afterward, same
    ordering as every other credit built this session."""
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    credit_calc = compute_adoption_credit(qualifying_costs)
    if not credit_calc:
        return None
    bracket_tax_before_credit = calc["bracket_tax"]
    credit_used = min(credit_calc["credit_available"], bracket_tax_before_credit)
    remaining_carryover = credit_calc["credit_available"] - credit_used
    bracket_tax_after_credit = max(0.0, bracket_tax_before_credit - credit_used)
    total_tax_after_credit = round(bracket_tax_after_credit + calc["surtax"], 2)
    return {**calc, "income_amount": income_amount, "standard_deduction": dedu["amount"],
            "taxable_income": taxable_income, "qualifying_costs": qualifying_costs,
            "credit": {**credit_calc, "credit_used": credit_used, "remaining_carryover": remaining_carryover},
            "bracket_tax_before_credit": bracket_tax_before_credit,
            "bracket_tax": bracket_tax_after_credit, "total_tax": total_tax_after_credit}


# --- College Access Tax Credit (Form 540 Credit Chart code 235, FTB Form
# 3592) -- Income Coverage Blueprint Phase 3's ninth build. A dedicated
# verification pass confirmed the ledger note's core claim -- 50% of a
# stated contribution, for the 2025 tax year specifically -- but the rate
# is YEAR-KEYED, not a permanent constant (60% TY2014, 55% TY2015, 50%
# TY2016-TY2027), and the credit is far more than a self-computed pass-
# through: (a) the taxpayer's contribution isn't automatically creditable
# -- they must FIRST apply to CEFA (the CA Educational Facilities
# Authority), receive a reservation, make the contribution matching that
# reservation, THEN receive a certification stating the actual certified
# amount; (b) a $500 million/year statewide allocation pool, first-come-
# first-served, meaning the true certified amount could be less than 50%
# of an intended contribution if the pool nears exhaustion (though by the
# time a contribution is actually MADE, per CEFA's own process it has
# already been matched to a granted reservation -- so for a COMPLETED
# contribution specifically, 50% x the contributed amount is a safe
# estimate of what was certified, not a guess); (c) nonrefundable with a
# SIX-year carryover (not five, unlike the PTE credit built earlier this
# session -- verify carryover length per-credit, don't assume it's always
# five); (d) the credit sunsets for tax years beginning on/after January 1,
# 2028 (guarded below via CATC_SUNSET_TAX_YEAR, though moot at
# DEFAULT_TAX_YEAR=2025); (e) a separate $5,000,000 aggregate business-
# credit ceiling applies for TY2024-2026 specifically (disclosed, not
# modeled -- moot for the vast majority of individual filers); and (f) a
# Schedule CA (540) Line 11 Column B addback applies if the same
# contribution was also deducted on federal Schedule A (already tracked
# as not_applicable in schedule_ca_inventory.py's own "College Access Tax
# Credit contribution addback" entry -- not duplicated here).
#
# Modeled: 50% of a stated CONTRIBUTION amount (not a separately-elicited
# "certified amount" -- see (b) above for why these are treated as
# equivalent for a completed contribution), capped at current-year CA tax
# liability with the carryover disclosed (not tracked), same pattern as
# the PTE credit and the adoption credit. NOT modeled, disclosed in the
# answer text: the $500M pool/first-come-first-served allocation itself
# (this assistant cannot know CEFA's real-time pool status); the
# application/reservation prerequisite (assumes the taxpayer already
# completed CEFA's process, since they're describing a completed
# contribution); the $5M aggregate business-credit ceiling; the Schedule
# CA Line 11 addback; non-CA-resident eligibility (CEFA's own FAQ
# explicitly declines to answer this, so not confirmed either way); and
# Schedule P's credit-ordering against other nonrefundable credits.
CATC_CITATION = "FTB Form 3592 (2025) Instructions, Sections B-D; R&TC Section 17053.85"
CATC_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3592.pdf"
CATC_RATE = 0.50
CATC_SUNSET_TAX_YEAR = 2028   # sunsets for tax years beginning on/after this

CATC_TERMS = {
    "college access tax credit", "college access credit",
    "catc fund", "college access tax credit fund", "form 3592",
}
CATC_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _catc_credit_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in CATC_TERMS):
        return False
    if any(t in q for t in CATC_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_catc_credit_signal(question: str):
    """Returns filing_status iff this looks like a genuine College Access
    Tax Credit question -- mirrors the plain wage-only path's scope
    exactly (full COMPLEXITY_EXCLUDE; no self-referential collision)."""
    q = question.lower()
    if not _catc_credit_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_catc_credit_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _catc_credit_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def compute_catc_credit(contribution_amount: float):
    """50% of a stated CONTRIBUTION amount -- see module note above for
    why this is treated as equivalent to the CEFA-certified amount for a
    COMPLETED contribution. No per-taxpayer dollar cap exists (bounded
    only by the $500M/year statewide pool, not modeled here)."""
    if contribution_amount is None or contribution_amount < 0:
        return None
    credit_available = round(contribution_amount * CATC_RATE, 2)
    return {"credit_available": credit_available}


def compute_catc_credit_ca_tax(conn, income_amount: float, filing_status: str, contribution_amount: float,
                                 tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as CA gross income/AGI, same
    simplification as everywhere else. Nonrefundable, capped at CA tax
    liability this year (current-year absorption only), SIX-year
    carryover disclosed not tracked -- verify carryover length per-
    credit, this one differs from the PTE credit's five years. Returns
    None for tax_year >= CATC_SUNSET_TAX_YEAR (the credit has sunset;
    moot at DEFAULT_TAX_YEAR=2025 but a real future gate). Reduces
    bracket_tax specifically, surtax re-added unchanged afterward, same
    ordering as every other credit built this session."""
    if tax_year >= CATC_SUNSET_TAX_YEAR:
        return None
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    credit_calc = compute_catc_credit(contribution_amount)
    if not credit_calc:
        return None
    bracket_tax_before_credit = calc["bracket_tax"]
    credit_used = min(credit_calc["credit_available"], bracket_tax_before_credit)
    remaining_carryover = credit_calc["credit_available"] - credit_used
    bracket_tax_after_credit = max(0.0, bracket_tax_before_credit - credit_used)
    total_tax_after_credit = round(bracket_tax_after_credit + calc["surtax"], 2)
    return {**calc, "income_amount": income_amount, "standard_deduction": dedu["amount"],
            "taxable_income": taxable_income, "contribution_amount": contribution_amount,
            "credit": {**credit_calc, "credit_used": credit_used, "remaining_carryover": remaining_carryover},
            "bracket_tax_before_credit": bracket_tax_before_credit,
            "bracket_tax": bracket_tax_after_credit, "total_tax": total_tax_after_credit}


# --- Individual Shared Responsibility (ISR) Penalty (Form 540 Line 92,
# FTB 3853) -- Income Coverage Blueprint Phase 3's tenth build, and the
# FIRST item this session where a dedicated research pass found the
# ledger's "too complex, same class as AMT" verdict itself WRONG, not just
# incomplete -- the same "look harder for a common-case slice" discipline
# that already worked once for the early-distribution tax. The full
# formula is a self-contained, linear 5-step worksheet published directly
# in FTB 3853's own 2025 instructions (pp.13-16), with every dollar figure
# stated explicitly -- not synthesized from scattered code sections the
# way AMT's ~11 preference categories would require.
#
# VERIFIED 2025 FORMULA (FTB 3853 Instructions 2025, Individual Shared
# Responsibility Penalty Worksheet, Steps 1-5):
#   penalty = min(max(flat_dollar, pct_income), avg_premium_cap)
#   flat_dollar = min($950 x n_adults + $475 x n_children, $2,850)
#   pct_income = 2.5% x max(0, household_income - filing_threshold)
#   avg_premium_cap = $377/month x 12 x min(household_size, 5)
#   -- and BEFORE any of that: if household_income <= filing_threshold,
#      the ENTIRE penalty is $0 (Step 4's mandatory gate, not a separate
#      "exemption" a filer must think to claim).
#
# SCOPED to the tractable common case, mirroring the early-distribution-
# tax pattern exactly: uninsured the ENTIRE tax year (full-year, no
# partial-year proration), no coverage exemption claimed (hardship,
# unaffordability, religious, tribal, incarceration, short-coverage-gap,
# etc. -- all genuinely case-by-case per FTB's own text, same class as
# the late-penalty reasonable-cause redirect), and nobody in the
# household turned 18 during the year (their per-person rate would
# otherwise need to change mid-year, forcing the full monthly worksheet).
# Any of those signals routes to a dedicated clarifying/out-of-scope
# message rather than a guess.
#
# ALSO DELIBERATELY SIMPLIFIED, disclosed in the answer text: assumes the
# filer (and spouse/RDP, if MFJ) are under 65 -- the filing-threshold
# lookup table below uses ONLY the under-65 rows; a 65+ filer's real
# threshold is higher, which this build does not use (a real, narrow
# overstatement risk near the threshold boundary, same disclosure
# discipline as every other "common case only" scope decision this
# session); treats the stated household-income figure as the complete
# "applicable household income" (doesn't separately add CA tax-exempt
# interest income or a dependent's own MAGI if that dependent
# independently has a filing requirement -- both narrow additions per
# FTB's definition); and doesn't model the "unclaimed-but-claimable
# household member" edge case (a household can include someone the filer
# COULD but does not claim as a dependent).
#
# CA ADJUSTED GROSS INCOME filing-threshold chart (FTB 3853 Instructions
# 2025, "Do I Have to File?", p.18, UNDER-65 rows only), keyed by filing
# status and dependent count (2 = "2 or more"). Cross-verified against
# the instructions' own worked examples (Example 1: MFJ, 3 dependents,
# threshold $64,419 -- matches mfj[2] below exactly). QSS has no valid
# 0-dependent combination (a qualifying surviving spouse requires a
# dependent child by definition).
ISR_FILING_THRESHOLD_AGI = {
    "single": {0: 18353.0, 1: 34186.0, 2: 46061.0},
    "hoh":    {0: 18353.0, 1: 34186.0, 2: 46061.0},
    "mfs":    {0: 18353.0, 1: 34186.0, 2: 46061.0},
    "mfj":    {0: 36711.0, 1: 52544.0, 2: 64419.0},
    "qss":    {1: 34186.0, 2: 46061.0},
}

ISR_PENALTY_CITATION = "FTB 3853 (2025) Instructions, Individual Shared Responsibility Penalty Worksheet, Steps 1-5; R&TC Section 61050"
ISR_PENALTY_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3853-instructions.pdf"
ISR_PENALTY_ADULT_RATE = 950.0
ISR_PENALTY_CHILD_RATE = 475.0
ISR_PENALTY_FLAT_CAP = 2850.0
ISR_PENALTY_INCOME_RATE = 0.025
ISR_PENALTY_AVG_PREMIUM_MONTHLY = 377.0
ISR_PENALTY_MAX_HOUSEHOLD_FOR_CAP = 5

ISR_PENALTY_TERMS = {
    "individual shared responsibility penalty", "isr penalty", "shared responsibility penalty",
    "no health insurance penalty", "health insurance penalty", "uninsured penalty",
    "penalty for not having health insurance", "penalty for being uninsured",
    "form 3853", "health care mandate penalty", "individual mandate penalty",
}
ISR_PENALTY_FULL_YEAR_TERMS = {
    "uninsured all year", "no health insurance all year", "did not have health insurance all year",
    "didn't have health insurance all year", "no insurance all year",
    "without health insurance all year", "uninsured the entire year", "no coverage all year",
    "without coverage all year", "uninsured for the whole year", "uninsured for the entire year",
}
ISR_PENALTY_EXCLUDE_TERMS = {
    "exemption", "hardship", "unaffordable", "affordability", "religious", "incarcerated",
    "incarceration", "tribal", "short coverage gap", "medi-cal", "medical", "health care sharing ministry",
    "healthcare sharing ministry", "part of the year", "part-year", "part year", "some months",
    "a few months", "turned 18", "turning 18", "18th birthday", "65 or older", "over 65",
    "age 65", "65 years old",
}


def _isr_penalty_base_signal_ok(q: str) -> bool:
    """No COMPUTE_TRIGGERS requirement -- this feature's own trigger
    vocabulary ("ISR penalty", "shared responsibility penalty", etc.) is
    specific enough on its own, same precedent as the late-filing/late-
    payment penalty."""
    if not any(t in q for t in ISR_PENALTY_TERMS):
        return False
    if any(t in q for t in ISR_PENALTY_EXCLUDE_TERMS):
        return False
    return True


def detect_isr_penalty_signal(question: str):
    """Returns filing_status iff this looks like a genuine, IN-SCOPE ISR
    penalty question -- requires an explicit full-year-uninsured
    confirmation (the tractable slice's own gating fact); ambiguous or
    partial-year phrasing deliberately returns None here."""
    q = question.lower()
    if not _isr_penalty_base_signal_ok(q):
        return None
    if not any(t in q for t in ISR_PENALTY_FULL_YEAR_TERMS):
        return None
    return detect_filing_status(question)


def detect_isr_penalty_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _isr_penalty_base_signal_ok(q):
        return False
    if not any(t in q for t in ISR_PENALTY_FULL_YEAR_TERMS):
        return False
    return detect_filing_status(question) is None


def detect_isr_penalty_out_of_scope(question: str) -> bool:
    """True iff this feature's own trigger vocabulary is present alongside
    an exemption/partial-year/65+/mid-year-18th-birthday term -- each of
    these genuinely changes the computation (or requires a case-by-case
    FTB determination) in a way this scoped build does not attempt."""
    q = question.lower()
    return (any(t in q for t in ISR_PENALTY_TERMS)
            and any(t in q for t in ISR_PENALTY_EXCLUDE_TERMS))


def detect_isr_penalty_ambiguous_coverage(question: str) -> bool:
    """True iff ISR-penalty vocabulary is present but the question states
    neither a full-year-uninsured confirmation nor an out-of-scope term --
    routes to a dedicated clarifying question rather than assuming
    full-year coverage status either way."""
    q = question.lower()
    if not any(t in q for t in ISR_PENALTY_TERMS):
        return False
    if any(t in q for t in ISR_PENALTY_EXCLUDE_TERMS):
        return False
    if any(t in q for t in ISR_PENALTY_FULL_YEAR_TERMS):
        return False
    return True


def detect_isr_penalty_household_adults(question: str):
    """Returns a stated adult-count (int, including the filer and spouse/
    RDP if MFJ) or None if not stated -- REQUIRED, unlike dependent
    counts elsewhere in this module, since the formula has no sensible
    default (every household has at least one adult, but guessing which
    one risks silently under/overstating the flat-dollar and premium-cap
    terms)."""
    q = question.lower()
    m = re.search(r"(\d+)\s*adults?\b", q)
    if m:
        return int(m.group(1))
    if re.search(r"\bone adult\b", q):
        return 1
    if re.search(r"\btwo adults\b", q):
        return 2
    if re.search(r"\bthree adults\b", q):
        return 3
    return None


def detect_isr_penalty_household_children(question: str):
    """Returns a stated child-count (int) or None if not stated -- treated
    as 0 children by the caller if not stated, same "missing count means
    zero" precedent as the exemption credit's dependent count."""
    q = question.lower()
    m = re.search(r"(\d+)\s*(?:children|child)\b", q)
    if m:
        return int(m.group(1))
    if re.search(r"\bone child\b", q):
        return 1
    if re.search(r"\btwo children\b", q):
        return 2
    if re.search(r"\bthree children\b", q):
        return 3
    if re.search(r"\bno children\b", q):
        return 0
    return None


def compute_isr_penalty(filing_status: str, n_adults: int, n_children: int, household_income: float):
    """See module note above for the verified 2025 formula and its
    scoping. dependent_count for the filing-threshold lookup is derived
    (not separately asked): adults beyond the filer/spouse, plus all
    children, are treated as dependents claimed."""
    if n_adults is None or n_adults < 1:
        return None
    n_children = n_children or 0
    if n_children < 0:
        return None
    if household_income is None or household_income < 0:
        return None
    thresholds = ISR_FILING_THRESHOLD_AGI.get(filing_status)
    if not thresholds:
        return None
    base_adults = 2 if filing_status == "mfj" else 1
    dependent_count = max(0, n_adults - base_adults) + n_children
    threshold_bucket = min(dependent_count, 2)
    filing_threshold = thresholds.get(threshold_bucket)
    if filing_threshold is None:
        return None   # e.g. QSS with 0 dependents -- not a valid combination
    if household_income <= filing_threshold:
        return {"exempt_below_threshold": True, "penalty": 0.0, "filing_threshold": filing_threshold}
    flat_dollar = round(min(ISR_PENALTY_ADULT_RATE * n_adults + ISR_PENALTY_CHILD_RATE * n_children,
                             ISR_PENALTY_FLAT_CAP), 2)
    pct_income = round(ISR_PENALTY_INCOME_RATE * (household_income - filing_threshold), 2)
    base_penalty = max(flat_dollar, pct_income)
    household_size = min(n_adults + n_children, ISR_PENALTY_MAX_HOUSEHOLD_FOR_CAP)
    avg_premium_cap = round(ISR_PENALTY_AVG_PREMIUM_MONTHLY * 12 * household_size, 2)
    penalty = round(min(base_penalty, avg_premium_cap), 2)
    return {"exempt_below_threshold": False, "penalty": penalty, "filing_threshold": filing_threshold,
            "flat_dollar": flat_dollar, "pct_income": pct_income, "avg_premium_cap": avg_premium_cap}


# --- FTB 3800 kiddie tax on a child's unearned income (Form 540 Line 31)
# -- form540_inventory.py's LAST remaining deferred_new_engine item.
# Re-examined 2026-08-28 at the user's request, proposing a "one-shot
# template" reframing: instead of building persistent cross-question
# memory (Phase 1, not started), ask for BOTH the child's AND the
# parent's already-known figures in ONE question -- since the actual
# blocker here was never a UI/memory problem, it was needing two
# people's numbers simultaneously, which this codebase's existing
# multi-fact extraction (already proven on ~25 other features) handles
# fine within a single question.
#
# VERIFIED against FTB's 2025 Instructions for Form FTB 3800 (Tax
# Computation for Certain Children with Unearned Income),
# https://www.ftb.ca.gov/forms/2025/2025-3800-instructions.html --
# fetched directly, not assumed from general "kiddie tax" knowledge,
# given this session's 8-for-9 track record of shallow notes needing
# correction once actually checked. CONFIRMED: California kept the
# ORIGINAL "parent's marginal rate" method (not the TCJA 2018-2019
# trust-rate method the federal government briefly used then reverted
# via the SECURE Act) -- Form 3800 explicitly cross-references federal
# Form 8615's parent-identification convention, and every line the CA
# instructions text DID spell out (1, 2, 6, 7, 9, 10, 15, 17, 18) is
# IDENTICAL to federal 8615's own well-established structure. The
# instructions skip explicitly spelling out lines 3-5, 8, 11-14, 16
# (pure arithmetic between the confirmed anchor lines, not new rules) --
# reconstructed from federal 8615's known shape, cross-checked for
# consistency against every CA-specific line that WAS spelled out (all
# matched exactly). $2,700 threshold confirmed directly from FTB's own
# text ("unearned income over $2,700 is taxed at the parent's rate").
#
# THE WORKSHEET (single child, no other 3800-filing siblings, standard
# deduction, no earned income -- see SCOPE below):
#   Line 1 = child's unearned income
#   Line 2 = $2,700 (standard-deduction case)
#   Line 3 = max(0, Line1 - Line2)
#   Line 4 = child's own taxable income (AGI minus child's standard
#            deduction -- under the no-earned-income assumption, child's
#            AGI = child's unearned income)
#   Line 5 = min(Line3, Line4) -- "net unearned income"; if <=0, Form
#            3800 doesn't apply at all, FTB's own text: "figure the tax
#            in the normal manner on the child's Form 540"
#   Line 6 = parent's taxable income (TRUSTED AS STATED -- same "trust
#            the already-computed figure" precedent as every other
#            second-return input this session, e.g. OSTC/PTE credit;
#            re-deriving it from a bare parent AGI would silently ignore
#            whatever OTHER adjustments/credits the parent's own return
#            might carry that this feature has no way of knowing about)
#   Line 7 = 0 (no other children -- disclosed scope limit)
#   Line 8 = Line5 + Line6 + Line7
#   Line 9 = tax on Line8 at the PARENT's rate/filing status
#   Line 10 = parent's own actual tax (tax on Line6 alone, parent's rate)
#   Line 11 = max(0, Line9 - Line10) -- tentative tax attributable to
#             stacking the net unearned income on top of parent's income
#   Line 12 = Line5 / (Line5 + Line7) = 1.0 exactly (single-child case)
#   Line 13 = Line11 x Line12 = Line11 (single-child case)
#   Line 14 = max(0, Line4 - Line5) -- child's OWN remaining taxable
#             income, taxed at the CHILD's own rate
#   Line 15 = tax on Line14 at the CHILD's own rate/filing status
#   Line 16 = Line13 + Line15 -- total tax under the kiddie-tax method
#   Line 17 = tax on Line4 (child's FULL taxable income) at the CHILD's
#             OWN rate -- a floor: what the child would owe WITHOUT
#             kiddie tax at all
#   Line 18 = max(Line16, Line17) -- FINAL amount, entered on the
#             child's own Form 540 line 31
# Lines 9/10/15/17 use BRACKET TAX ONLY (compute_ca_tax's "bracket_tax"),
# matching Form 3800's own literal "use the tax table" semantics -- the
# Behavioral Health Services Tax surtax is a SEPARATE Form 540 line
# applied to the RETURN's own bottom-line taxable income, not part of
# the worksheet's internal tax-table lookups, so it's added once at the
# end (on the child's own Line 4 taxable income), not baked into every
# intermediate line.
#
# SCOPE, disclosed not hidden: single child only (no line 7/12
# multi-child combination); standard deduction only (no itemized Line-2
# alternative); child assumed to have NO earned income (child's AGI =
# child's unearned income exactly -- explicit earned-income language
# routes to a dedicated out-of-scope redirect rather than silently
# using the wrong Line-1 worksheet); child's own filing status DEFAULTS
# to single (a dependent minor overwhelmingly files single, disclosed
# in the answer text rather than demanding a fact nobody expects to
# need); FTB's own age/support/joint-return qualification tests for
# WHETHER Form 3800 applies at all are disclosed as an assumption, not
# gated on (a user specifically asking about "kiddie tax" has already
# self-selected into this population).
KIDDIE_TAX_THRESHOLD = 2700.0
KIDDIE_TAX_CITATION = "FTB 2025 Instructions for Form FTB 3800 -- Purpose, Part I, Part II, Part III"
KIDDIE_TAX_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3800-instructions.html"

KIDDIE_TAX_TERMS = {
    "kiddie tax", "form 3800", "ftb 3800", "child's unearned income",
    "my child's unearned income", "tax computation for certain children",
    "child's investment income taxed at",
}
KIDDIE_TAX_CHILD_INCOME_TERMS = {
    "child's unearned income", "my child's unearned income", "unearned income",
    "child's investment income", "my child's investment income",
}
KIDDIE_TAX_PARENT_INCOME_TERMS = {
    "parent's taxable income", "my taxable income", "parent's own taxable income",
}
KIDDIE_TAX_OUT_OF_SCOPE_TERMS = {
    # NOT bare "earned income" -- it's a literal substring of "UNearned
    # income," this feature's own core vocabulary; found live testing
    # the feature's own basic phrasing before any regression test was
    # locked in. Compound phrases only, each specific enough not to
    # collide.
    "has earned income", "child's earned income", "part-time job", "has a job",
    "child's wages", "child's salary", "child's paycheck",
    "other children", "another child", "multiple children",
    "itemize", "itemized", "itemizing",
}
KIDDIE_TAX_COMPLEXITY_EXCLUDE = {
    "dependent", "alimony", "gambling", "gambled", "betting", "wagering",
    "disaster loss", "disaster", "trust", "estate",
}


def _kiddie_tax_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in KIDDIE_TAX_TERMS):
        return False
    if any(t in q for t in KIDDIE_TAX_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in KIDDIE_TAX_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_kiddie_tax_signal(question: str):
    """Returns the PARENT's filing status iff this looks like a genuine
    single-child, standard-deduction, no-earned-income kiddie-tax
    question. The child's own filing status is not extracted -- it
    defaults to single (see module note)."""
    q = question.lower()
    if not _kiddie_tax_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_kiddie_tax_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _kiddie_tax_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_kiddie_tax_out_of_scope(question: str) -> bool:
    """True iff kiddie-tax vocabulary is present alongside language this
    narrow build doesn't attempt (earned income, multiple children,
    itemizing) -- routes to a dedicated redirect rather than silently
    using the wrong worksheet branch."""
    q = question.lower()
    if not any(t in q for t in KIDDIE_TAX_TERMS):
        return False
    if any(t in q for t in KIDDIE_TAX_COMPLEXITY_EXCLUDE):
        return False
    return any(t in q for t in KIDDIE_TAX_OUT_OF_SCOPE_TERMS)


def compute_kiddie_tax_ca_tax(conn, child_unearned_income: float, parent_taxable_income: float,
                               parent_filing_status: str, child_filing_status: str = "single",
                               tax_year: int = DEFAULT_TAX_YEAR):
    """See module note above for the full worksheet derivation and its
    disclosed scope (single child, standard deduction, no earned
    income)."""
    if child_unearned_income is None or child_unearned_income < 0:
        return None
    if parent_taxable_income is None or parent_taxable_income < 0:
        return None
    child_dedu = standard_deduction(conn, child_filing_status, tax_year)
    if not child_dedu:
        return None
    line1 = child_unearned_income
    line3 = max(0.0, line1 - KIDDIE_TAX_THRESHOLD)
    line4 = max(0.0, child_unearned_income - child_dedu["amount"])
    line5 = min(line3, line4)

    child_own_calc = compute_ca_tax(conn, line4, child_filing_status, tax_year)
    if not child_own_calc:
        return None

    if line5 <= 0:
        total_tax = round(child_own_calc["bracket_tax"] + child_own_calc["surtax"], 2)
        return {"kiddie_tax_applies": False, "child_taxable_income": line4,
                "net_unearned_income": line5, "total_tax": total_tax,
                "marginal_rate": child_own_calc["marginal_rate"],
                "child_standard_deduction": child_dedu["amount"],
                "citation": KIDDIE_TAX_CITATION, "source_url": KIDDIE_TAX_SOURCE_URL}

    parent_calc_combined = compute_ca_tax(conn, line5 + parent_taxable_income, parent_filing_status, tax_year)
    parent_calc_own = compute_ca_tax(conn, parent_taxable_income, parent_filing_status, tax_year)
    if not parent_calc_combined or not parent_calc_own:
        return None
    line9 = parent_calc_combined["bracket_tax"]
    line10 = parent_calc_own["bracket_tax"]
    line11 = max(0.0, line9 - line10)
    line13 = line11  # single-child case: line5/(line5+line7=0) = 1.0

    line14 = max(0.0, line4 - line5)
    child_remaining_calc = compute_ca_tax(conn, line14, child_filing_status, tax_year)
    if not child_remaining_calc:
        return None
    line15 = child_remaining_calc["bracket_tax"]
    line16 = round(line13 + line15, 2)
    line17 = round(child_own_calc["bracket_tax"], 2)
    line18 = max(line16, line17)
    kiddie_tax_controls = line16 > line17

    total_tax = round(line18 + child_own_calc["surtax"], 2)
    return {"kiddie_tax_applies": True, "kiddie_tax_controls": kiddie_tax_controls,
            "child_taxable_income": line4, "net_unearned_income": line5,
            "parent_taxable_income": parent_taxable_income,
            "tentative_tax_parent_rate": round(line13, 2),
            "child_own_rate_tax_on_remaining": round(line15, 2),
            "child_own_rate_tax_on_full": line17,
            "total_tax": total_tax, "marginal_rate": child_own_calc["marginal_rate"],
            "surtax": child_own_calc["surtax"], "surtax_citation": child_own_calc["surtax_citation"],
            "child_standard_deduction": child_dedu["amount"],
            "citation": KIDDIE_TAX_CITATION, "source_url": KIDDIE_TAX_SOURCE_URL}


# --- California Alternative Minimum Tax "screen" (Schedule P (540), Form
# 540 Line 61) -- Income Coverage Blueprint Phase 3's eleventh build, and
# a DIFFERENT shape of tractable slice than every other Phase 3 item:
# not a narrowed-but-still-general formula, but a genuine "does this
# apply to you at all" screen for the specific population that's already
# this whole codebase's baseline case -- a standard-deduction, wage-only
# filer with zero AMT preference items.
#
# The GENERAL AMT case (itemizers, ISO exercisers, passive-activity/
# depreciation adjusters, private-activity-bond holders, K-1 preference
# pass-throughs -- Schedule P (540) Side 1 Lines 2-13) genuinely still
# needs the full ~11-category AMTI build this ledger already correctly
# deferred -- nothing here changes that population's complexity.
#
# What a dedicated verification pass found, directly from FTB's own 2025
# Schedule P (540) form text: for a taxpayer who did NOT itemize, Line 1
# says explicitly: "If you did not itemize deductions, enter your
# standard deduction from Form 540, line 18, and go to line 6" -- and
# every other AMTI adjustment/preference line (2-13, 16-18, 20) is
# itemizing- or preference-item-specific, so for this narrow population
# they're all $0. AMTI (Line 21) therefore collapses to regular taxable
# income + standard deduction added back, which is arithmetically just
# CA AGI -- no preference-item modeling needed at all for this case.
# CA's AMT rate is a FLAT 7.0% (Schedule P (540) Side 2 Line 24) -- NOT
# the federal 26%/28% two-tier structure; confirm this explicitly for
# any future AMT work, since assuming the federal structure would be
# wrong. The exemption is a 3-tier phase-out (25% of AMTI over a
# threshold, fully zeroed at a second threshold), all five filing-status
# dollar figures verified directly from Schedule P (540) Side 2's own
# exemption worksheet/table (internally self-consistent: exemption =
# 25% x (zero-out threshold - phase-out-start threshold) exactly, for
# all 3 threshold pairs).
#
# Rather than trusting a purely empirical "AMT is always $0 for this
# population" claim, this is built as a REAL formula computation (regular
# tax via the existing bracket engine vs. TMT = 7% x max(0, AMTI -
# phased-out exemption), AMT owed = max(0, TMT - regular tax)) -- so it
# self-verifies against its own inputs rather than hard-coding an assumed
# always-zero answer, and would correctly surface a nonzero result if the
# structural "regular tax stays ahead of TMT" relationship this research
# found ever didn't hold for some input. "Regular tax" for the comparison
# uses total_tax (bracket_tax + the Behavioral Health Services surtax) --
# the more conservative (higher) figure, erring toward NOT finding AMT
# owed rather than toward finding it, when in doubt.
#
# OUT OF SCOPE, routes to a dedicated redirect: itemized deductions,
# incentive stock options, passive activity, depreciation adjustments,
# private activity bonds, and other preference items (COMPLEXITY_EXCLUDE
# already blocks itemizing/stock/K-1/self-employment/capital-loss/rental;
# a few AMT-preference-specific terms are added on top).
AMT_SCREEN_CITATION = "2025 Schedule P (540), Side 1 Line 1, Side 2 Lines 22-24; R&TC Section 17062"
AMT_SCREEN_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-p.pdf"
AMT_RATE = 0.07
AMT_EXEMPTION_PHASEOUT_RATE = 0.25

# Verified directly from Schedule P (540) Side 2's exemption table/
# worksheet. QSS shares MFJ's figures (both use the joint-return column).
AMT_EXEMPTION = {
    "single": 92749.0, "hoh": 92749.0, "mfs": 61830.0,
    "mfj": 123667.0, "qss": 123667.0,
}
AMT_EXEMPTION_PHASEOUT_START = {
    "single": 347808.0, "hoh": 347808.0, "mfs": 231868.0,
    "mfj": 463745.0, "qss": 463745.0,
}

AMT_SCREEN_TERMS = {
    "alternative minimum tax", "schedule p (540)", "tentative minimum tax",
    "form 540 line 61", "amt liability", "owe amt", "subject to amt", "amt exposure",
    "do i owe amt", "california amt",
}
AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS = {
    "passive activity", "passive income", "private activity bond", "depreciation",
    "municipal bond", "incentive stock option", "iso exercise", "exercised stock options",
    "tax-exempt interest", "circulation cost", "mining cost", "research cost",
    "intangible drilling", "net operating loss",
}
AMT_SCREEN_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE | AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS


def _amt_screen_has_preference_exclusion(q: str) -> bool:
    """Bare "nol" needs a word-boundary regex (same precedent as the
    NOL-suspension feature's own detector) -- a plain substring check
    risks false hits inside unrelated words."""
    if any(t in q for t in AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS):
        return True
    return re.search(r"\bnol\b", q) is not None


def _amt_screen_base_signal_ok(q: str) -> bool:
    """No COMPUTE_TRIGGERS requirement -- this feature's own trigger
    vocabulary is specific enough on its own, same precedent as the
    ISR penalty and late-filing/late-payment penalty."""
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if any(t in q for t in COMPLEXITY_EXCLUDE):
        return False
    if _amt_screen_has_preference_exclusion(q):
        return False
    return True


def detect_amt_screen_signal(question: str):
    q = question.lower()
    if not _amt_screen_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_amt_screen_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _amt_screen_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_amt_screen_out_of_scope(question: str) -> bool:
    """True iff AMT vocabulary is present alongside an AMT-preference-
    specific term (ISO, passive activity, depreciation, private activity
    bond, etc.) -- these genuinely require the full ~11-category AMTI
    build this scoped screen does not attempt. Deliberately narrower than
    the base signal's full COMPLEXITY_EXCLUDE gate (a generic self-
    employment/itemizing collision just silently doesn't fire, same
    convention as every other feature here; only AMT-preference-specific
    language gets its own dedicated redirect)."""
    q = question.lower()
    return any(t in q for t in AMT_SCREEN_TERMS) and _amt_screen_has_preference_exclusion(q)


def compute_amt_screen_ca_tax(conn, income_amount: float, filing_status: str,
                                tax_year: int = DEFAULT_TAX_YEAR):
    """See module note above -- AMTI collapses to CA AGI for this narrow
    population (standard deduction, wage-only, zero preference items).
    Returns a real computed comparison, not a hard-coded assumption."""
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    regular = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not regular:
        return None
    exemption_base = AMT_EXEMPTION.get(filing_status)
    phaseout_start = AMT_EXEMPTION_PHASEOUT_START.get(filing_status)
    if exemption_base is None or phaseout_start is None:
        return None
    amti = income_amount
    reduction = max(0.0, AMT_EXEMPTION_PHASEOUT_RATE * (amti - phaseout_start))
    exemption = max(0.0, exemption_base - reduction)
    tmt = round(AMT_RATE * max(0.0, amti - exemption), 2)
    regular_tax = regular["total_tax"]
    amt_owed = round(max(0.0, tmt - regular_tax), 2)
    return {**regular, "amti": amti, "exemption": exemption, "tmt": tmt,
            "regular_tax": regular_tax, "amt_owed": amt_owed}


# --- California AMT, ISO exercise addback extension (Schedule P (540)
# Part I Line 10) -- re-examined 2026-08-28 at the user's request ("dig
# into AMT first"), extending the narrow screen above to the single most
# common real-world reason an ordinary (non-itemizing) taxpayer actually
# hits AMT post-TCJA: exercising incentive stock options and holding the
# stock past year-end. The GENERAL AMT population (itemizers, passive-
# activity/depreciation adjusters, private-activity-bond holders, other
# Schedule P Part I preference items) still correctly stays deferred --
# this is one more narrow, verified slice on top of the existing screen,
# not a step toward the full ~11-category build.
#
# VERIFIED against FTB's 2025 Instructions for Schedule P (540), Part I,
# Line 10, fetched directly (not assumed from general "ISO AMT"
# knowledge): "you must generally include on line 10 the excess of: The
# fair market value (FMV) of the stock acquired through the exercise of
# the option ... when your rights in the stock first become
# transferable, or when these rights are no longer subject to a
# substantial risk of forfeiture, over The amount you paid for the
# stock." This is the ISO "bargain element" -- for REGULAR tax, exercising
# an ISO (without disposing of the stock) creates NO taxable income at
# all (the entire point of an ISO); the bargain element is added ONLY to
# AMTI, never to regular taxable income. Confirmed via general
# information text: "In general, California law conforms to IRC Sections
# 55 through 59, relating to alternative minimum tax (AMT), as of January
# 1, 2015" -- IRC 56(b)(3) (the ISO AMT preference) is a long-stable,
# pre-2015 provision, so this conformity-date snapshot creates no
# divergence risk for this specific line, unlike several other
# conformity-date items found elsewhere in this project.
#
# CRITICAL CARVE-OUT, directly from the same source: "If you acquired
# stock by exercising an ISO and you disposed of that stock in the same
# year, the tax treatment under regular tax and AMT is the same (no
# adjustment is required)." A same-year sale means NO AMT preference item
# at all -- routes to a dedicated redirect (not a guess either way, since
# incorrectly assuming "still holding" when the stock was actually sold
# same-year would OVERSTATE AMT owed, and incorrectly assuming "sold"
# when actually still holding would UNDERSTATE it).
#
# SCOPE, disclosed not hidden: requires literal ISO language (word-
# boundary "iso" or "incentive stock option") -- deliberately does NOT
# trigger on a bare "exercised stock options" mention, since that phrase
# alone is genuinely ambiguous between an ISO (this line's population)
# and a non-qualified stock option (NSO, which is just ordinary W-2
# income for BOTH regular tax and AMT, no special preference-item
# treatment at all) -- guessing which one would risk silently applying
# the wrong mechanic to an NSO exercise. California Qualified Stock
# Options (CQSOs, R&TC 17502, a narrower CA-specific provision with its
# own different regular-tax exclusion) are also explicitly out of scope,
# not conflated with ISOs. Standard deduction only (matching the base
# screen); one ISO exercise event only (no multi-grant aggregation); no
# OTHER preference items alongside it (passive activity, depreciation,
# private activity bonds, etc. -- reuses the base screen's own exclusion
# set for these, unchanged).
AMT_ISO_TERMS = {
    "incentive stock option", "exercised my iso", "iso bargain element",
    "exercised incentive stock options", "iso exercise",
}
AMT_ISO_CQSO_EXCLUDE_TERMS = {
    "california qualified stock option", "cqso",
}
AMT_ISO_SAME_YEAR_SALE_TERMS = {
    "sold the stock", "sold the shares", "sold my iso", "sold it the same year",
    "disposed of the stock", "disqualifying disposition", "sold in the same year",
    "sold them the same year", "sold same year",
}
AMT_ISO_BARGAIN_ELEMENT_TERMS = {
    "bargain element", "iso bargain element", "iso spread",
}
# same non-ISO preference items the base screen already excludes on --
# an ISO exercise ALONGSIDE, say, passive activity adjustments still
# needs the full build, not this narrow extension.
AMT_ISO_OTHER_PREFERENCE_EXCLUDE_TERMS = AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS - {
    "incentive stock option", "iso exercise", "exercised stock options",
}
# bare "stock" is already in the shared COMPLEXITY_EXCLUDE (to defer to
# K-1/QSBS-style stock questions), which would self-exclude THIS
# feature's own natural vocabulary ("stock options", "the stock") --
# found live testing the feature's own basic phrasing before any
# regression test was locked in, same "subtract the trigger term back
# out" fix as several other features this session (cannabis 280E,
# exemption credit's "dependent", rental depreciation's "rental").
AMT_ISO_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE - {"stock"}


def _amt_iso_has_iso_term(q: str) -> bool:
    if any(t in q for t in AMT_ISO_TERMS):
        return True
    return re.search(r"\biso\b", q) is not None


def _amt_iso_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if not _amt_iso_has_iso_term(q):
        return False
    if any(t in q for t in AMT_ISO_CQSO_EXCLUDE_TERMS):
        return False
    if any(t in q for t in AMT_ISO_SAME_YEAR_SALE_TERMS):
        return False
    if any(t in q for t in AMT_ISO_COMPLEXITY_EXCLUDE):
        return False
    if any(t in q for t in AMT_ISO_OTHER_PREFERENCE_EXCLUDE_TERMS):
        return False
    return True


def detect_amt_iso_signal(question: str):
    q = question.lower()
    if not _amt_iso_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_amt_iso_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _amt_iso_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_amt_iso_same_year_sale(question: str) -> bool:
    """True iff ISO language is present alongside same-year-sale/
    disposition language -- routes to a dedicated redirect explaining
    NO AMT adjustment applies at all in that case (not a guess, a direct
    consequence of Schedule P's own carve-out text)."""
    q = question.lower()
    if not _amt_iso_has_iso_term(q):
        return False
    return any(t in q for t in AMT_ISO_SAME_YEAR_SALE_TERMS)


def compute_amt_iso_ca_tax(conn, income_amount: float, iso_bargain_element: float,
                            filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Thin extension of compute_amt_screen_ca_tax: regular tax is
    computed identically (the ISO bargain element creates NO regular-tax
    income at exercise), only AMTI/exemption/TMT are recomputed with the
    bargain element folded in."""
    if iso_bargain_element is None or iso_bargain_element < 0:
        return None
    base = compute_amt_screen_ca_tax(conn, income_amount, filing_status, tax_year)
    if not base:
        return None
    exemption_base = AMT_EXEMPTION.get(filing_status)
    phaseout_start = AMT_EXEMPTION_PHASEOUT_START.get(filing_status)
    if exemption_base is None or phaseout_start is None:
        return None
    amti = income_amount + iso_bargain_element
    reduction = max(0.0, AMT_EXEMPTION_PHASEOUT_RATE * (amti - phaseout_start))
    exemption = max(0.0, exemption_base - reduction)
    tmt = round(AMT_RATE * max(0.0, amti - exemption), 2)
    amt_owed = round(max(0.0, tmt - base["regular_tax"]), 2)
    return {**base, "amti": amti, "exemption": exemption, "tmt": tmt, "amt_owed": amt_owed,
            "iso_bargain_element": iso_bargain_element}


# --- California AMT, ITEMIZER extension (Schedule P (540) Part I Line 3)
# -- re-examined 2026-08-28, continuing the same "dig into AMT" request
# right after the ISO addback above. Covers a taxpayer who itemizes
# deductions with NO other AMT preference items (no ISO, no passive
# activity, no depreciation adjustments, no private activity bonds).
#
# VERIFIED against FTB's 2025 Schedule P (540) instructions, Part I Line
# 3, fetched directly: "Enter on this line any of the following from
# Schedule CA (540), Part II, lines 5b and 5c, column A and line 6
# (column A minus column B): State and local personal property taxes;
# State, local, or foreign real property taxes." This is specifically
# PROPERTY tax (personal property + real estate) -- a REAL correction
# caught before building anything: my initial hypothesis was that this
# would mirror the "SALT addback" already built for CA's own REGULAR-
# tax itemized-deduction rules (compute_itemized_ca_tax's salt_amount
# parameter, which removes state/local INCOME tax entirely, since
# California already disallows that for regular tax too). But Line 3 is
# a DIFFERENT, narrower category -- property tax IS a valid CA itemized
# deduction for REGULAR tax, unlike state/local income tax, and is only
# disallowed specifically for AMT. Confirmed by reading the actual
# instructions text directly rather than assuming the two "SALT-
# flavored" addbacks meant the same thing.
#
# Reuses compute_itemized_ca_tax UNCHANGED for the regular-tax figure
# (property tax stays IN the itemized total there, since it's a valid
# regular-tax deduction) -- AMTI is computed SEPARATELY as that
# function's own taxable_income PLUS the stated property-tax addback
# (income_amount - allowed-for-AMT itemized deductions = taxable_income
# + property_tax_addback, since property tax is the ONE component of
# the allowed itemized total this scope removes for AMT purposes),
# same thin-wrapper pattern as the ISO extension reusing
# compute_amt_screen_ca_tax.
#
# SCOPE, disclosed not hidden: if the taxpayer's stated itemized amount
# doesn't actually exceed the standard deduction (compute_itemized_ca_
# tax's own "used_itemized" flag is False), this feature DECLINES --
# they didn't really itemize on their real return, so no property-tax
# AMT addback applies to them at all (Schedule P Line 1's own
# instruction routes non-itemizers to the standard-deduction/Line-6
# path instead, i.e. the ALREADY-BUILT base AMT screen). Does NOT
# compose with the 6 other OPTIONAL itemized-deduction adjustments
# compute_itemized_ca_tax already supports (SALT, mortgage-interest
# addback, misc itemized, charitable cap, SALT-cap addback, casualty
# loss) -- deliberately scoped to keep extraction to exactly 3 dollar
# figures (income, itemized total, property-tax addback), the same
# "narrow but real" discipline as every other build this session; a
# question mentioning any of those 6 terms routes to a dedicated
# redirect rather than silently ignoring them. Does NOT attempt CA's
# own itemized-deduction AGI-based phaseout interacting differently
# with AMT specifically -- taxable_income already reflects whatever
# phaseout applied for regular tax, and this extension only adds back
# the one verified AMT-specific disallowed component on top.
AMT_ITEMIZED_CITATION = "2025 Schedule P (540), Part I Line 3; R&TC Section 17062.1"
AMT_ITEMIZED_TERMS = ITEMIZED_TERMS
AMT_ITEMIZED_PROPERTY_TAX_TERMS = {
    "property tax", "property taxes", "real estate tax", "real estate taxes",
    "real property tax", "real property taxes", "personal property tax",
    "personal property taxes",
}
# Vocabulary for the separate AMT mortgage-interest extension below
# (compute_amt_mortgage_ca_tax) -- defined here, ahead of its own compute
# function, only so it can be folded into this exclude set: a question
# using the OLD, undifferentiated mortgage_interest_addback phrasing
# should still make THIS (property-tax) extension decline, same as the 5
# other adjustment types, rather than silently answering while ignoring
# a stated mortgage fact it can't safely fold in.
AMT_MORTGAGE_NONACQUISITION_TERMS = {
    "not used to buy, build, or improve", "not used to buy, build or improve",
    "wasn't used to buy, build, or improve", "wasn't used to buy, build or improve",
    "was not used to buy, build, or improve", "was not used to buy, build or improve",
    "loan proceeds were not used to buy", "loan proceeds weren't used to buy",
    "non-acquisition mortgage interest", "nonacquisition mortgage interest",
    "non-acquisition debt interest", "nonacquisition debt interest",
    "not qualified housing interest",
}
# Separate, SHORTER anchor used only for AMOUNT EXTRACTION (engine.py),
# not detection -- AMT_MORTGAGE_NONACQUISITION_TERMS's own qualifying
# phrase ("...that was not used to buy, build, or improve...") sits too
# far from its own dollar figure in natural phrasing ("$90,000 in
# mortgage interest that was not used to buy, build, or improve the
# home" -- the "in mortgage interest that was " connector alone is 30
# chars, past _amount_near_anchor_edge's default 25-char window) --
# found live via a 3-figure order-independence test where this anchor's
# own qualifying phrase ended up numerically CLOSER to an unrelated
# itemized-total figure in an earlier clause than to its own value,
# causing the itemized-total extraction (which runs first) to wrongly
# claim this feature's own dollar figure. "mortgage interest" alone is
# short and sits immediately next to its value in natural phrasing, and
# is safe to use standalone here because the signal gate already
# excludes any question also mentioning the OLDER, undifferentiated
# mortgage-interest vocabulary before extraction is ever reached.
AMT_MORTGAGE_INTEREST_ANCHOR_TERMS = {
    "mortgage interest", "home equity interest", "home-equity interest",
}
# the 6 optional itemized-deduction adjustments compute_itemized_ca_tax
# already supports, PLUS the new mortgage extension's own vocabulary --
# out of scope for THIS extension specifically, to keep extraction to
# exactly 3 figures.
AMT_ITEMIZED_OTHER_ADJUSTMENT_EXCLUDE_TERMS = (
    SALT_TERMS | MORTGAGE_INTEREST_ADDBACK_TERMS | MISC_ITEMIZED_TERMS
    | CHARITABLE_TERMS | SALT_CAP_ADDBACK_TERMS | CASUALTY_LOSS_TERMS
    | AMT_MORTGAGE_NONACQUISITION_TERMS
)
AMT_ITEMIZED_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE - {"itemize", "itemized", "itemizing"}


def _amt_itemized_base_signal_ok(q: str) -> bool:
    """No COMPUTE_TRIGGERS requirement -- same precedent as the base AMT
    screen and the ISO extension: AMT + itemizing + property-tax
    vocabulary together is already specific enough on its own (found
    live: requiring it too was a copy-paste inconsistency from a
    DIFFERENT feature family, not an intentional choice -- it silently
    rejected this feature's own natural phrasing, "do I owe california
    amt if... single?", before being caught)."""
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if not any(t in q for t in AMT_ITEMIZED_TERMS):
        return False
    if not any(t in q for t in AMT_ITEMIZED_PROPERTY_TAX_TERMS):
        return False
    if _amt_screen_has_preference_exclusion(q):
        return False
    if any(t in q for t in AMT_ITEMIZED_OTHER_ADJUSTMENT_EXCLUDE_TERMS):
        return False
    if any(t in q for t in AMT_ITEMIZED_COMPLEXITY_EXCLUDE):
        return False
    return True


def detect_amt_itemized_signal(question: str):
    q = question.lower()
    if not _amt_itemized_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_amt_itemized_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _amt_itemized_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_amt_itemized_out_of_scope(question: str) -> bool:
    """True iff AMT + itemizing vocabulary is present alongside any of
    the 6 other optional itemized-deduction adjustments this extension
    doesn't attempt together with AMT -- routes to a dedicated redirect
    rather than silently dropping them."""
    q = question.lower()
    if not (any(t in q for t in AMT_SCREEN_TERMS) and any(t in q for t in AMT_ITEMIZED_TERMS)):
        return False
    return any(t in q for t in AMT_ITEMIZED_OTHER_ADJUSTMENT_EXCLUDE_TERMS)


def compute_amt_itemized_ca_tax(conn, income_amount: float, itemized_amount: float,
                                 property_tax_addback: float, filing_status: str,
                                 tax_year: int = DEFAULT_TAX_YEAR):
    """See module note above. Regular tax computed via the UNCHANGED
    compute_itemized_ca_tax (property tax stays a valid regular-tax
    itemized deduction); AMTI is that function's own taxable_income
    PLUS the property-tax addback (disallowed for AMT specifically).
    Returns None (declines) if the stated itemized amount doesn't
    actually exceed the standard deduction -- Schedule P Line 1's own
    instruction routes non-itemizers to the standard-deduction path
    instead, where this addback doesn't apply at all."""
    if property_tax_addback is None or property_tax_addback < 0:
        return None
    base = compute_itemized_ca_tax(conn, income_amount, itemized_amount, filing_status, tax_year)
    if not base:
        return None
    if not base["used_itemized"]:
        return None
    exemption_base = AMT_EXEMPTION.get(filing_status)
    phaseout_start = AMT_EXEMPTION_PHASEOUT_START.get(filing_status)
    if exemption_base is None or phaseout_start is None:
        return None
    amti = base["taxable_income"] + property_tax_addback
    reduction = max(0.0, AMT_EXEMPTION_PHASEOUT_RATE * (amti - phaseout_start))
    exemption = max(0.0, exemption_base - reduction)
    tmt = round(AMT_RATE * max(0.0, amti - exemption), 2)
    regular_tax = base["total_tax"]
    amt_owed = round(max(0.0, tmt - regular_tax), 2)
    return {**base, "amti": amti, "exemption": exemption, "tmt": tmt,
            "regular_tax": regular_tax, "amt_owed": amt_owed,
            "property_tax_addback": property_tax_addback}


# --- AMT extension: home mortgage interest NOT used to buy, build, or
# improve the home (Schedule P (540) Part I Line 4) -- narrows the AMT
# "general case" further, alongside the ISO and property-tax extensions
# above. Line 4 disallows, for AMT purposes only, interest on
# "non-acquisition debt": home-equity-type borrowing not used to buy,
# build, or substantially improve the home securing it. This is a
# USE-OF-PROCEEDS test, not a loan-size test -- FTB's own examples: a
# home-equity loan used to buy a ski boat goes on Line 4; the same loan
# used to install a swimming pool does NOT (a home improvement IS
# acquisition debt).
#
# Deliberately does NOT reuse compute_itemized_ca_tax's existing
# mortgage_interest_addback TERM SET for this feature's own trigger --
# verified against that parameter's own docstring/comment (~line 766-779)
# that it bundles TWO different federal-nonconformity sub-rules into one
# trusted number: (1) interest on genuine acquisition debt between the
# federal $750k cap and CA's pre-TCJA $1M cap (still acquisition debt --
# Line 4 does NOT disallow this), and (2) interest on debt not used to
# buy/build/improve the home (Line 4 DOES disallow this). Reusing the
# whole stated figure would overstate AMTI for anyone whose federal
# disallowance was purely the cap-size difference. This extension asks
# for the NARROWER, Line-4-specific figure via its own vocabulary
# instead -- but the underlying MECHANIC (add the disallowed interest
# back to the itemized total for regular tax, since CA doesn't conform
# to federal's suspension either way) is the same, so the compute
# function below still passes this narrower figure straight into
# compute_itemized_ca_tax's existing mortgage_interest_addback parameter
# unchanged for the regular-tax leg.
AMT_MORTGAGE_CITATION = "2025 Schedule P (540), Part I Line 4; R&TC Section 17062.1"
AMT_MORTGAGE_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE - {"itemize", "itemized", "itemizing"}


def _amt_mortgage_base_signal_ok(q: str) -> bool:
    """Same no-COMPUTE_TRIGGERS precedent as every other AMT extension --
    AMT + itemizing + non-acquisition-mortgage-interest vocabulary
    together is specific enough on its own."""
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if not any(t in q for t in AMT_ITEMIZED_TERMS):
        return False
    if not any(t in q for t in AMT_MORTGAGE_NONACQUISITION_TERMS):
        return False
    if _amt_screen_has_preference_exclusion(q):
        return False
    if any(t in q for t in AMT_ITEMIZED_OTHER_ADJUSTMENT_EXCLUDE_TERMS - AMT_MORTGAGE_NONACQUISITION_TERMS):
        return False
    if any(t in q for t in AMT_ITEMIZED_PROPERTY_TAX_TERMS):
        return False
    if any(t in q for t in AMT_MORTGAGE_COMPLEXITY_EXCLUDE):
        return False
    return True


def detect_amt_mortgage_signal(question: str):
    q = question.lower()
    if not _amt_mortgage_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_amt_mortgage_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _amt_mortgage_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_amt_mortgage_out_of_scope(question: str) -> bool:
    """True iff AMT + itemizing vocabulary is present alongside the
    property-tax addback or any of the OTHER itemized-deduction
    adjustments (including the OLD, undifferentiated mortgage-interest-
    addback phrasing) this extension doesn't attempt together with its
    own narrower mortgage fact -- routes to a dedicated redirect rather
    than silently dropping one."""
    q = question.lower()
    if not (any(t in q for t in AMT_SCREEN_TERMS) and any(t in q for t in AMT_ITEMIZED_TERMS)):
        return False
    if any(t in q for t in AMT_ITEMIZED_PROPERTY_TAX_TERMS):
        return True
    return any(t in q for t in AMT_ITEMIZED_OTHER_ADJUSTMENT_EXCLUDE_TERMS - AMT_MORTGAGE_NONACQUISITION_TERMS)


def compute_amt_mortgage_ca_tax(conn, income_amount: float, itemized_amount: float,
                                 nonacquisition_mortgage_interest: float, filing_status: str,
                                 tax_year: int = DEFAULT_TAX_YEAR):
    """Regular tax computed via the UNCHANGED compute_itemized_ca_tax,
    reusing its existing mortgage_interest_addback parameter -- California
    doesn't conform to federal's TCJA suspension of non-acquisition-debt
    mortgage interest, so this amount is added BACK to the regular-tax
    itemized total first. AMTI then adds it back a SECOND time, since AMT
    disallows non-acquisition-debt interest regardless of what CA's
    regular tax allows. Returns None (declines) if the stated itemized
    amount doesn't actually exceed the standard deduction, same Schedule
    P Line 1 routing as the property-tax extension."""
    if nonacquisition_mortgage_interest is None or nonacquisition_mortgage_interest < 0:
        return None
    base = compute_itemized_ca_tax(conn, income_amount, itemized_amount, filing_status, tax_year,
                                    mortgage_interest_addback=nonacquisition_mortgage_interest)
    if not base:
        return None
    if not base["used_itemized"]:
        return None
    exemption_base = AMT_EXEMPTION.get(filing_status)
    phaseout_start = AMT_EXEMPTION_PHASEOUT_START.get(filing_status)
    if exemption_base is None or phaseout_start is None:
        return None
    amti = base["taxable_income"] + nonacquisition_mortgage_interest
    reduction = max(0.0, AMT_EXEMPTION_PHASEOUT_RATE * (amti - phaseout_start))
    exemption = max(0.0, exemption_base - reduction)
    tmt = round(AMT_RATE * max(0.0, amti - exemption), 2)
    regular_tax = base["total_tax"]
    amt_owed = round(max(0.0, tmt - regular_tax), 2)
    return {**base, "amti": amti, "exemption": exemption, "tmt": tmt,
            "regular_tax": regular_tax, "amt_owed": amt_owed,
            "nonacquisition_mortgage_interest": nonacquisition_mortgage_interest}


# --- AMT extension: NOL (net operating loss) deduction added back for AMT
# (Schedule P (540) Part I Line 16) -- narrows the AMT "general case"
# further, alongside the ISO/property-tax/mortgage extensions above.
# Line 16 is a straight add-back of whatever REGULAR-tax NOL deduction was
# already claimed this year -- confirmed against FTB's own 2025
# instruction text ("NOL deductions from Schedule CA (540)... enter as a
# positive amount"). Genuinely simpler than, and NOT to be confused with,
# Line 20 (the separate AMT NOL deduction, which needs its own multi-year
# AMT-basis carryover recompute) -- Line 20 stays out of scope, same
# reasoning as every other multi-year-basis item this session.
#
# This codebase already has 3 separate, shipped NOL features with an
# IDENTICAL output shape (each returns **compute_ca_tax(...) plus
# nol_deduction/taxable_income/mti): compute_nol_ca_tax (business-only),
# compute_nol_wages_ca_tax (wage-only/closed-business), compute_nol_
# mixed_ca_tax (wages + ongoing business). Line 16's mechanic
# (amti = taxable_income + nol_deduction) is identical across all three
# and is a no-op in the suspended branch (nol_deduction=0 there, so amti
# just equals taxable_income/mti unchanged) -- wraps all 3 rather than
# just one, avoiding an arbitrary gap ("AMT+NOL only works if your
# business never closed") with no corresponding reduction in real risk,
# since none of the 3 needs new FTB research.
AMT_NOL_CITATION = "2025 Schedule P (540), Part I Line 16; R&TC Section 17062.1"
# AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS already contains "net operating
# loss", and _amt_screen_has_preference_exclusion also does an
# unconditional \bnol\b regex check -- this feature's OWN vocabulary IS
# "net operating loss"/NOL, so it cannot reuse that shared helper for its
# own gate (same self-exclusion trap the ISO extension had to avoid for
# bare "stock"). Every OTHER preference item still correctly excludes.
AMT_NOL_OTHER_PREFERENCE_EXCLUDE_TERMS = AMT_SCREEN_PREFERENCE_EXCLUDE_TERMS - {"net operating loss"}


def _amt_nol_has_other_preference_exclusion(q: str) -> bool:
    """Deliberately does NOT check bare \\bnol\\b (unlike the shared
    _amt_screen_has_preference_exclusion) -- "nol"/"net operating loss"
    IS this feature's own required vocabulary, not something to exclude
    on."""
    return any(t in q for t in AMT_NOL_OTHER_PREFERENCE_EXCLUDE_TERMS)


def _amt_nol_base_signal_ok(q: str) -> bool:
    """Business-income-only population: AMT vocabulary + the existing
    (non-AMT) business-only NOL feature's own signal. No COMPUTE_TRIGGERS
    requirement -- same precedent as every other AMT extension (AMT
    vocabulary + NOL vocabulary together is specific enough on its own)."""
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_COMPLEXITY_EXCLUDE):
        return False
    if _amt_nol_has_other_preference_exclusion(q):
        return False
    return True


def detect_amt_nol_signal(question: str):
    q = question.lower()
    if not _amt_nol_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_amt_nol_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _amt_nol_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def _amt_nol_wages_base_signal_ok(q: str) -> bool:
    """Wage-only/closed-business population."""
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_WAGES_ONGOING_BUSINESS_EXCLUDE_TERMS):
        return False
    if not any(t in q for t in NOL_WAGES_CLOSED_BUSINESS_TERMS):
        return False
    if any(t in q for t in NOL_WAGES_COMPLEXITY_EXCLUDE):
        return False
    if _amt_nol_has_other_preference_exclusion(q):
        return False
    return True


def detect_amt_nol_wages_signal(question: str):
    q = question.lower()
    if not _amt_nol_wages_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_amt_nol_wages_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _amt_nol_wages_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_amt_nol_wages_ambiguous(question: str) -> bool:
    """True iff AMT + NOL vocabulary is present but neither a closed-
    business confirmation nor an ongoing-business signal is stated --
    mirrors the existing (non-AMT) wages-only feature's own ambiguity
    detector, gated additionally on AMT vocabulary being present."""
    q = question.lower()
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_WAGES_ONGOING_BUSINESS_EXCLUDE_TERMS):
        return False
    if any(t in q for t in NOL_WAGES_CLOSED_BUSINESS_TERMS):
        return False
    return True


def _amt_nol_mixed_base_signal_ok(q: str) -> bool:
    """Mixed wages + ongoing-business population."""
    if not any(t in q for t in AMT_SCREEN_TERMS):
        return False
    if not _has_nol_term(q):
        return False
    if any(t in q for t in NOL_WAGES_CLOSED_BUSINESS_TERMS):
        return False
    if not (any(t in q for t in NOL_MIXED_ONGOING_BUSINESS_TERMS)
            or any(t in q for t in NOL_MIXED_BUSINESS_INCOME_TERMS)):
        return False
    if not any(t in q for t in NOL_MIXED_WAGE_TERMS):
        return False
    if any(t in q for t in NOL_MIXED_COMPLEXITY_EXCLUDE):
        return False
    if _amt_nol_has_other_preference_exclusion(q):
        return False
    return True


def detect_amt_nol_mixed_signal(question: str):
    q = question.lower()
    if not _amt_nol_mixed_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_amt_nol_mixed_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _amt_nol_mixed_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def _amt_nol_addback(base, filing_status: str):
    """Shared AMTI/exemption/TMT arithmetic for all 3 NOL population
    variants -- a deliberate DRY exception (3x repetition WITHIN one
    slice, unlike every other AMT slice's 1x-each-across-different-
    slices style). amti = taxable_income + nol_deduction, which always
    equals base["mti"] exactly (never floored, since nol_deduction =
    min(nol_carryover_amount, mti) by construction in all 3 underlying
    functions) -- a no-op in the suspended branch, where nol_deduction=0
    and amti collapses to taxable_income (=mti) unchanged.
    compute_nol_wages_ca_tax's result has no "suspended" key at all
    (structurally impossible for that population) -- untouched here,
    passes through fine."""
    if not base:
        return None
    exemption_base = AMT_EXEMPTION.get(filing_status)
    phaseout_start = AMT_EXEMPTION_PHASEOUT_START.get(filing_status)
    if exemption_base is None or phaseout_start is None:
        return None
    amti = base["taxable_income"] + base["nol_deduction"]
    reduction = max(0.0, AMT_EXEMPTION_PHASEOUT_RATE * (amti - phaseout_start))
    exemption = max(0.0, exemption_base - reduction)
    tmt = round(AMT_RATE * max(0.0, amti - exemption), 2)
    regular_tax = base["total_tax"]
    amt_owed = round(max(0.0, tmt - regular_tax), 2)
    return {**base, "amti": amti, "exemption": exemption, "tmt": tmt,
            "regular_tax": regular_tax, "amt_owed": amt_owed}


def compute_amt_nol_ca_tax(conn, business_income: float, nol_carryover_amount: float,
                            filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    return _amt_nol_addback(
        compute_nol_ca_tax(conn, business_income, nol_carryover_amount, filing_status, tax_year),
        filing_status)


def compute_amt_nol_wages_ca_tax(conn, wages: float, nol_carryover_amount: float,
                                  filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    return _amt_nol_addback(
        compute_nol_wages_ca_tax(conn, wages, nol_carryover_amount, filing_status, tax_year),
        filing_status)


def compute_amt_nol_mixed_ca_tax(conn, wages: float, business_income: float, nol_carryover_amount: float,
                                  filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    return _amt_nol_addback(
        compute_nol_mixed_ca_tax(conn, wages, business_income, nol_carryover_amount, filing_status, tax_year),
        filing_status)


# --- Underpayment of Estimated Tax Penalty, SHORT METHOD ONLY (Form 540
# Line 113, FTB Form 5805 Side 2 Part II) -- Income Coverage Blueprint
# Phase 3's twelfth build, and a THIRD consecutive case where a dedicated
# research pass found the ledger's "too complex" verdict overly
# conservative -- but this time the finding is genuinely split, not a
# clean reversal like ISR penalty or a narrow screen like AMT.
#
# FTB 5805's REGULAR METHOD (Worksheet II) is exactly the same
# disqualifying mechanism this project already correctly excluded from
# the late-filing/late-payment penalty build: per-installment days-
# unpaid x a periodic interest rate that changes mid-year (8% through
# 6/30/25, then 7% through 4/15/26), with payment-ordering rules across
# 4 installment columns. THIS WAS STAYING DEFERRED until re-examined
# 2026-08-28 at the user's explicit request -- see the dedicated
# "REGULAR METHOD" section below (compute_underpayment_penalty_regular)
# for the full worksheet transcription and its own build notes. Unlike
# every other "too complex" verdict reversed this session, this one
# genuinely needed a whole new capability (date-math -- this codebase
# had ZERO date-parsing infrastructure before this build), not just one
# more stated fact on an existing formula.
#
# But FTB also publishes a SHORT METHOD (Form 5805 Side 2, Part II,
# lines 7-13) that collapses the whole per-diem/rate-period computation
# into ONE flat annual constant, printed directly on the 2025 form:
# Line 11 = Line 10 x .05028767 -- a single stated fact for the year,
# the same kind of year-specific constant as a standard deduction
# amount, not a live daily computation. It's eligible ONLY for
# taxpayers who "made no estimated tax payments or [whose] only
# payments were California income tax withheld" (2025 Form 5805
# Instructions, Short Method) -- i.e., ordinary withholding-only filers,
# a real and common population. Scoped to EXACTLY that population;
# anyone who made ANY estimated tax payment this year is out of scope
# (the short method's own eligibility for that population depends on
# whether those payments were made exactly on the required due dates,
# reintroducing the timing question this slice deliberately avoids) and
# routes to a dedicated redirect, same for the separate Farmer/Fisherman
# exception (Form 5805F, an entirely different pathway).
#
# VERIFIED MECHANIC (2025 Form 5805, Side 1 "Important" box + Side 2
# Part II):
#   1. De minimis safe harbor: if (current-year tax after credits -
#      withholding) < $500 ($250 MFS), STOP -- no penalty, no form
#      needed.
#   2. Zero-prior-year-liability safe harbor: if the prior tax year was
#      a full 12 months with NO tax liability at all, STOP -- no
#      penalty.
#   3. Required annual payment = the LESSER of 90% of current-year tax,
#      or 100% of prior-year tax (110% if prior-year CA AGI exceeded
#      $150,000 / $75,000 MFS) -- EXCEPT taxpayers with CURRENT-year CA
#      AGI >= $1,000,000 / $500,000 MFS must use the 90%-of-current-year
#      test only (no lesser-of comparison).
#   4. Underpayment = required annual payment - withholding; if <= 0,
#      STOP -- no penalty.
#   5. Penalty = underpayment x .05028767 (assumes payment was NOT made
#      early -- the form's own sanctioned conservative default: "If any
#      payment was made earlier than the due date, you may use the
#      short method, but using it may cause you to pay a larger penalty
#      than using the regular method... likely to be small").
#
# "Current-year tax" is computed via the existing bracket engine (same
# "don't ask for something we can derive" discipline as OSTC/PTE/
# adoption/CATC), BEFORE credits -- this system has no unified way to
# apply a taxpayer's various credits, same simplification already used
# by the plain wage-compute path. "Prior-year tax" and "prior-year AGI"
# CANNOT be derived (no multi-year engine exists yet -- Phase 4 of the
# blueprint, not started) and must be stated facts, same as PTE credit's
# prior-year carryover. Both are REQUIRED, not defaulted, deliberately:
# defaulting prior-year AGI to "under threshold" (the 100% test) would
# risk UNDERSTATING the required annual payment -- and therefore
# understating or missing a real penalty -- for the real population
# whose prior-year AGI exceeded the threshold, the same "never guess
# toward understatement" discipline as early-distribution tax's
# exception-language scoping.
UNDERPAYMENT_CITATION = "2025 FTB Form 5805 Instructions, Short Method (Form 5805 Side 2, Part II); R&TC Section 19136"
UNDERPAYMENT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-5805.pdf"
UNDERPAYMENT_SHORT_METHOD_RATE = 0.05028767
UNDERPAYMENT_DE_MINIMIS_SINGLE = 500.0
UNDERPAYMENT_DE_MINIMIS_MFS = 250.0
UNDERPAYMENT_CURRENT_YEAR_SAFE_HARBOR_RATE = 0.90
UNDERPAYMENT_PRIOR_YEAR_SAFE_HARBOR_RATE_STANDARD = 1.00
UNDERPAYMENT_PRIOR_YEAR_SAFE_HARBOR_RATE_HIGH_AGI = 1.10
UNDERPAYMENT_PRIOR_AGI_HIGH_THRESHOLD_MFS = 75000.0
UNDERPAYMENT_PRIOR_AGI_HIGH_THRESHOLD_DEFAULT = 150000.0
UNDERPAYMENT_FORCE_90_ONLY_THRESHOLD_MFS = 500000.0
UNDERPAYMENT_FORCE_90_ONLY_THRESHOLD_DEFAULT = 1000000.0

UNDERPAYMENT_TERMS = {
    "underpayment penalty", "underpayment of estimated tax", "estimated tax penalty",
    "estimated tax underpayment", "form 5805", "underestimated my tax",
    "penalty for underpaying", "penalty for underpaying my taxes",
}
UNDERPAYMENT_OUT_OF_SCOPE_TERMS = {
    "estimated payment", "estimated tax payment", "estimated payments",
    "quarterly payment", "quarterly estimated", "made estimated payments",
    "paid estimated tax", "farmer", "fisherman", "fisher", "regular method",
    "annualized income",
}
UNDERPAYMENT_PRIOR_YEAR_TAX_TERMS = {
    "prior year tax", "prior-year tax", "last year's tax", "2024 tax",
    "2024 tax liability", "prior year tax liability", "last year's tax liability",
}
UNDERPAYMENT_PRIOR_YEAR_AGI_TERMS = {
    "prior year agi", "prior-year agi", "last year's agi", "2024 agi",
    "prior year adjusted gross income", "last year's income", "2024 income",
}
UNDERPAYMENT_WITHHOLDING_TERMS = {
    "withholding", "withheld", "tax withheld", "california withholding", "ca withholding",
}
UNDERPAYMENT_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _underpayment_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in UNDERPAYMENT_TERMS):
        return False
    if any(t in q for t in UNDERPAYMENT_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in UNDERPAYMENT_COMPLEXITY_EXCLUDE):
        return False
    return True


def detect_underpayment_signal(question: str):
    q = question.lower()
    if not _underpayment_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_underpayment_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _underpayment_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_underpayment_out_of_scope(question: str) -> bool:
    """True iff underpayment-penalty vocabulary is present alongside the
    Farmer/Fisherman exception or the annualized-income method -- both
    need an entirely different form/worksheet, not either method built
    here. NARROWED 2026-08-28 (was the full UNDERPAYMENT_OUT_OF_SCOPE_
    TERMS set, back when any estimated-tax-payment mention meant "not
    supported") -- now that the Regular Method is built, a plain
    estimated-payment mention routes to THAT feature instead of this
    generic redirect; only the genuinely still-unsupported terms remain
    here. Intentional behavior change, not a regression."""
    q = question.lower()
    return (any(t in q for t in UNDERPAYMENT_TERMS)
            and any(t in q for t in UNDERPAYMENT_REGULAR_HARD_OUT_OF_SCOPE_TERMS))


def compute_required_annual_payment(current_year_tax: float, prior_year_tax: float, prior_year_agi: float,
                                     withholding: float, filing_status: str, current_year_income: float):
    """Shared steps 1-4 of FTB 5805's mechanic -- both the Short Method
    (Side 2 Part II) and the Regular Method (Worksheet II) start here:
    the two safe harbors that stop the ENTIRE penalty before any
    per-method math, then the required annual payment (lesser of 90% of
    current-year tax or 100%/110% of prior-year tax, with the forced-
    90%-only override for current-year CA AGI >= $1M/$500k MFS) and the
    resulting ANNUAL underpayment (required annual payment minus
    withholding -- NOT minus any estimated payments, since safe-harbor
    sufficiency is judged on withholding alone; estimated payments only
    affect WHEN the shortfall gets made up, which is what the Regular
    Method's own per-quarter mechanic figures out separately). Extracted
    2026-08-28 out of what was compute_underpayment_penalty's own inlined
    logic, so the Regular Method build could reuse it verbatim rather
    than duplicating it -- refactor is behavior-preserving, verified
    against all pre-existing Short Method regression cases."""
    if current_year_tax is None or current_year_tax < 0:
        return None
    if prior_year_tax is None or prior_year_tax < 0:
        return None
    if prior_year_agi is None or prior_year_agi < 0:
        return None
    if withholding is None or withholding < 0:
        return None
    is_mfs = filing_status == "mfs"
    de_minimis = UNDERPAYMENT_DE_MINIMIS_MFS if is_mfs else UNDERPAYMENT_DE_MINIMIS_SINGLE
    balance_due = current_year_tax - withholding
    if balance_due < de_minimis:
        return {"required_annual_payment": None, "safe_harbor_reason": "de_minimis_balance"}
    if prior_year_tax == 0:
        return {"required_annual_payment": None, "safe_harbor_reason": "zero_prior_year_liability"}
    pct_current = UNDERPAYMENT_CURRENT_YEAR_SAFE_HARBOR_RATE * current_year_tax
    high_agi_threshold = UNDERPAYMENT_PRIOR_AGI_HIGH_THRESHOLD_MFS if is_mfs else UNDERPAYMENT_PRIOR_AGI_HIGH_THRESHOLD_DEFAULT
    prior_year_rate = (UNDERPAYMENT_PRIOR_YEAR_SAFE_HARBOR_RATE_HIGH_AGI if prior_year_agi > high_agi_threshold
                        else UNDERPAYMENT_PRIOR_YEAR_SAFE_HARBOR_RATE_STANDARD)
    pct_prior = prior_year_rate * prior_year_tax
    force_90_threshold = UNDERPAYMENT_FORCE_90_ONLY_THRESHOLD_MFS if is_mfs else UNDERPAYMENT_FORCE_90_ONLY_THRESHOLD_DEFAULT
    if current_year_income >= force_90_threshold:
        required_annual_payment = pct_current
    else:
        required_annual_payment = min(pct_current, pct_prior)
    underpayment = required_annual_payment - withholding
    if underpayment <= 0:
        return {"required_annual_payment": round(required_annual_payment, 2), "safe_harbor_reason": "safe_harbor_met"}
    return {"required_annual_payment": round(required_annual_payment, 2), "safe_harbor_reason": None,
            "underpayment": round(underpayment, 2)}


def compute_underpayment_penalty(current_year_tax: float, prior_year_tax: float, prior_year_agi: float,
                                   withholding: float, filing_status: str, current_year_income: float):
    """FTB 5805 Short Method, Side 2 Part II -- see module note above for
    the verified 5-step mechanic and its scope. Steps 1-4 delegated to
    compute_required_annual_payment; the only Short-Method-specific step
    is the final flat-rate multiplication."""
    rap = compute_required_annual_payment(current_year_tax, prior_year_tax, prior_year_agi,
                                           withholding, filing_status, current_year_income)
    if rap is None:
        return None
    if rap["safe_harbor_reason"] in ("de_minimis_balance", "zero_prior_year_liability"):
        return {"penalty": 0.0, "reason": rap["safe_harbor_reason"]}
    if rap["safe_harbor_reason"] == "safe_harbor_met":
        return {"penalty": 0.0, "reason": "safe_harbor_met",
                "required_annual_payment": rap["required_annual_payment"]}
    penalty = round(rap["underpayment"] * UNDERPAYMENT_SHORT_METHOD_RATE, 2)
    return {"penalty": penalty, "reason": "penalty_owed",
            "required_annual_payment": rap["required_annual_payment"],
            "underpayment": rap["underpayment"]}


def compute_underpayment_penalty_ca_tax(conn, income_amount: float, filing_status: str, prior_year_tax: float,
                                          prior_year_agi: float, withholding: float,
                                          tax_year: int = DEFAULT_TAX_YEAR):
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    current_year_tax = calc["total_tax"]
    penalty_calc = compute_underpayment_penalty(current_year_tax, prior_year_tax, prior_year_agi,
                                                  withholding, filing_status, income_amount)
    if not penalty_calc:
        return None
    return {**calc, "income_amount": income_amount, "taxable_income": taxable_income,
            "current_year_tax": current_year_tax, "prior_year_tax": prior_year_tax,
            "prior_year_agi": prior_year_agi, "withholding": withholding, "penalty": penalty_calc}


# --- Underpayment of Estimated Tax Penalty, REGULAR METHOD (Form 540
# Line 113, FTB Form 5805 Worksheet II) -- re-examined 2026-08-28 at the
# user's explicit request after four other "too complex" verdicts this
# session turned out to have a tractable narrow slice hiding in them.
# This one is genuinely different in kind, not degree: not a missing-
# input problem, but a whole new capability -- this codebase had ZERO
# date-parsing/date-math infrastructure anywhere before this build.
# Covers the population the Short Method's own eligibility rule
# excludes: anyone who made a LATE or partial estimated tax payment
# (the Short Method requires either zero estimated payments, or all of
# them made exactly on the required due dates).
#
# VERIFIED against FTB's 2025 Instructions for Form FTB 5805's
# "Worksheet II Regular Method to Figure Your Underpayment and
# Penalty," fetched and transcribed directly (line-by-line, not
# paraphrased), then independently cross-checked against federal Form
# 2210 Part III Section A, which California's worksheet mirrors almost
# exactly (CA lines 1-9 correspond to federal lines 10-18, same
# column-a bypass, same b/c-only Line 7, same Line 8/Line 9 mutual
# exclusivity) -- a second source of confidence beyond the single FTB
# fetch.
#
# PART I -- FIGURE YOUR UNDERPAYMENT, four columns in due-date order
# (a=4/15/25, b=6/15/25, c=9/15/25, d=1/15/26). A genuine RUNNING LEDGER,
# not a flat formula -- each column's carried-forward state feeds the
# next:
#   Line 1 = required_annual_payment / 4 (equal quarterly split -- the
#            annualized-income-installment alternative, Worksheet
#            Part III, is a separate and even bigger worksheet, NOT
#            modeled, same as fiscal-year filers and the Farmer/
#            Fisherman exception).
#   Line 2 = payments credited to that column (estimated payments
#            bucketed by ACTUAL PAYMENT DATE into that due-date window,
#            same Form-2210 convention -- NOT self-labeled by the
#            taxpayer, see bucket_regular_method_payments -- plus
#            withholding allocated ratably 1/4 per column, the standard
#            IRS/FTB default absent an actual-date election, which is
#            not modeled).
#   Line 3/4/5 (columns b,c,d only; column a bypasses straight to
#            Line 6 = Line 2 directly): carry forward the previous
#            column's overpayment (Line 9) and any still-unresolved
#            shortfall (Line 7 + Line 8).
#   Line 6 = max(0, Line4 - Line5).
#   Line 7 (columns b,c ONLY -- N/A for a and d): if Line6 == 0, the
#            still-unresolved carry-forward amount (max(0, Line5-Line4)),
#            else 0.
#   Line 8 ("Underpayment") / Line 9 ("Overpayment"): MUTUALLY
#            EXCLUSIVE per column -- if Line1 >= Line6, Line8 =
#            Line1-Line6 (Line9 stays 0); else Line9 = Line6-Line1
#            (Line8 stays 0). Hand-traced multiple consecutive-
#            underpayment chains to confirm no debt is ever silently
#            dropped by the Line7/Line8 carry-forward -- it's a proper
#            running ledger.
#
# PART II -- THE PENALTY, two rate periods (8% 4/15/25-6/30/25, 7%
# 7/1/25-4/15/26 -- the mid-year rate change already verified for the
# Short Method). Each column's Line8 underpayment accrues interest from
# its OWN due date until it's "paid" -- FTB's text ("the date the
# amount on line 8 was paid") assumes the taxpayer already knows this
# by hand from filling out a paper form quarter-by-quarter; closing
# that gap for a single-question format is the one genuinely
# underspecified part of this build. Derivation, verified independently
# rather than guessed: because Line8/Line9 are mutually exclusive per
# column, and Line5 additively carries forward EVERYTHING still owed
# via the previous column's Line7+Line8, the FIRST later column k where
# Line9[k] > 0 is exactly the point where all debt accumulated through
# column k-1 gets cleared -- no case exists where a later payment
# PARTIALLY clears an older shortfall without producing a Line9 event.
# So: resolved_date[col] = due_date[k] for the earliest later k with
# Line9[k] > 0, else the worksheet's own stated backstop, 4/15/2026
# ("...or 4/15/26, whichever is earlier").
#
# This deliberately uses column k's DUE DATE, not the exact stated
# payment date within that column's bucket -- a conservative choice
# (never understates the penalty, same "assumes payment was NOT made
# early" philosophy already stated for the Short Method above) that is
# also the NECESSARY one: since multiple payments can land in the same
# due-date bucket (see bucketing above), there is no single well-
# defined "the" payment date to point to once bucketed, and trying to
# track which specific payment within a bucket resolves an old debt
# reintroduces exactly the split-resolution complexity this design
# avoids. Disclosed explicitly in the answer text as a modeling choice,
# not hidden.
#
# EXTRACTION DESIGN -- date-bucketing, not quarter-labeling. Considered
# asking users to label each payment "Q1/Q2/Q3/Q4" directly; rejected,
# since a late payment made in August is FTB's OWN convention for
# column (c) even if the user thinks of it as "catching up Q1" --
# asking users to self-classify invites exactly the kind of mislabeling
# this project's "never guess" discipline exists to avoid. Instead:
# accept a flat list of "$AMOUNT on MM/DD/YYYY" pairs (also accepting
# MM/DD/YY, but only TEACHING the 4-digit form in clarifying messages)
# and let the engine bucket each into the due-date window its date
# actually falls in -- removes the mislabeling risk entirely rather
# than asking the user to get it right. See engine.py's _dates/
# _mask_dates/_pair_amounts_with_dates for the extraction side --
# _amounts()'s regex has no word-boundary guard and misparses a literal
# date like "4/15/2025" into phantom amounts 15.0/2025.0 (verified
# live), so dates must be masked out before dollar-amount extraction
# runs, a new requirement this feature is the first to need.
#
# NOT SUPPORTED, disclosed not hidden: the annualized income
# installment method (Part III); fiscal-year filers; the Farmer/
# Fisherman exception (FTB 5805F, different rules entirely); a
# withholding actual-date election (ratable 1/4-per-quarter is used,
# the standard default); payments dated before the tax year starts or
# after the 4th installment's due date (bucket_regular_method_payments
# returns None for these, routing to the template-teaching message
# rather than guessing how to treat them).
UNDERPAYMENT_REGULAR_CITATION = "2025 FTB Form 5805 Instructions, Regular Method (Form 5805 Worksheet II); R&TC Section 19136"
UNDERPAYMENT_REGULAR_DUE_DATES = (date(2025, 4, 15), date(2025, 6, 15), date(2025, 9, 15), date(2026, 1, 15))
UNDERPAYMENT_REGULAR_TAX_YEAR_START = date(2025, 1, 1)
UNDERPAYMENT_REGULAR_FINAL_BACKSTOP = date(2026, 4, 15)
UNDERPAYMENT_REGULAR_RATE1_END = date(2025, 6, 30)
UNDERPAYMENT_REGULAR_RATE2_START = date(2025, 7, 1)
UNDERPAYMENT_REGULAR_RATE_PERIOD_1 = 0.08
UNDERPAYMENT_REGULAR_RATE_PERIOD_2 = 0.07
UNDERPAYMENT_REGULAR_DAY_BASIS = 365

UNDERPAYMENT_CURRENT_INCOME_TERMS = {
    "my income is", "my income was", "current year income", "current-year income",
    "this year's income", "california income is", "income is", "income was",
}
# "farmer"/"fisherman"/"fisher"/"annualized income" stay hard-excluded
# (need a different form or a separate, even bigger worksheet); every
# OTHER term already in UNDERPAYMENT_OUT_OF_SCOPE_TERMS (estimated
# payment/quarterly payment/paid estimated tax/"regular method" itself)
# becomes THIS feature's own positive trigger instead of a dead end.
UNDERPAYMENT_REGULAR_HARD_OUT_OF_SCOPE_TERMS = {"farmer", "fisherman", "fisher", "annualized income"}
UNDERPAYMENT_REGULAR_METHOD_SIGNAL_TERMS = UNDERPAYMENT_OUT_OF_SCOPE_TERMS - UNDERPAYMENT_REGULAR_HARD_OUT_OF_SCOPE_TERMS


def _underpayment_regular_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in UNDERPAYMENT_TERMS):
        return False
    if not any(t in q for t in UNDERPAYMENT_REGULAR_METHOD_SIGNAL_TERMS):
        return False
    if any(t in q for t in UNDERPAYMENT_REGULAR_HARD_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in UNDERPAYMENT_COMPLEXITY_EXCLUDE):
        return False
    return True


def detect_underpayment_regular_method_signal(question: str):
    q = question.lower()
    if not _underpayment_regular_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_underpayment_regular_method_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _underpayment_regular_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def bucket_regular_method_payments(payment_date_pairs):
    """Buckets each (amount, date) pair into the FTB due-date window it
    falls in -- a payment's period is determined by WHEN it was
    actually paid (Form 2210/5805 convention), not which quarter it was
    "intended" for or self-labeled as. Returns None if any date falls
    outside the modeled range (before the tax year starts, or after the
    4th installment's own due date) -- a payment that late/early
    interacts with the return-due-date mechanic differently, not
    modeled, so this signals the caller to defer rather than guess."""
    due = UNDERPAYMENT_REGULAR_DUE_DATES
    window_starts = (
        UNDERPAYMENT_REGULAR_TAX_YEAR_START,
        due[0] + timedelta(days=1),
        due[1] + timedelta(days=1),
        due[2] + timedelta(days=1),
    )
    cols = [0.0, 0.0, 0.0, 0.0]
    for amount, d in payment_date_pairs:
        if d < UNDERPAYMENT_REGULAR_TAX_YEAR_START or d > due[3]:
            return None
        for i in (3, 2, 1, 0):
            if d >= window_starts[i]:
                cols[i] += amount
                break
    return tuple(cols)


def compute_underpayment_penalty_regular(current_year_tax: float, prior_year_tax: float, prior_year_agi: float,
                                          withholding: float, filing_status: str, current_year_income: float,
                                          quarterly_payments):
    """FTB 5805 REGULAR METHOD, Worksheet II Parts I & II -- see module
    note above for the full verified mechanic and the resolution-date
    derivation. `quarterly_payments` = (col_a, col_b, col_c, col_d),
    already bucketed by due-date window via
    bucket_regular_method_payments -- this function is pure arithmetic,
    no date parsing here."""
    rap = compute_required_annual_payment(current_year_tax, prior_year_tax, prior_year_agi,
                                           withholding, filing_status, current_year_income)
    if rap is None:
        return None
    if rap["safe_harbor_reason"] in ("de_minimis_balance", "zero_prior_year_liability"):
        return {"penalty": 0.0, "reason": rap["safe_harbor_reason"]}
    if rap["safe_harbor_reason"] == "safe_harbor_met":
        return {"penalty": 0.0, "reason": "safe_harbor_met",
                "required_annual_payment": rap["required_annual_payment"]}

    required_installment = rap["required_annual_payment"] / 4.0
    withholding_per_col = withholding / 4.0
    line1 = [required_installment] * 4
    line2 = [quarterly_payments[i] + withholding_per_col for i in range(4)]
    line6 = [0.0, 0.0, 0.0, 0.0]
    line7 = [0.0, 0.0, 0.0, 0.0]
    line8 = [0.0, 0.0, 0.0, 0.0]
    line9 = [0.0, 0.0, 0.0, 0.0]

    for i in range(4):
        if i == 0:
            line6[0] = line2[0]
        else:
            line3 = line9[i - 1]
            line4 = line2[i] + line3
            line5 = line7[i - 1] + line8[i - 1]
            line6[i] = max(0.0, line4 - line5)
            if i in (1, 2) and line6[i] == 0.0:
                line7[i] = max(0.0, line5 - line4)
        if line1[i] >= line6[i]:
            line8[i] = line1[i] - line6[i]
        else:
            line9[i] = line6[i] - line1[i]

    due = UNDERPAYMENT_REGULAR_DUE_DATES
    total_penalty = 0.0
    columns = []
    for i in range(4):
        if line8[i] <= 0:
            columns.append({"underpayment": 0.0, "penalty": 0.0})
            continue
        resolved = UNDERPAYMENT_REGULAR_FINAL_BACKSTOP
        for k in range(i + 1, 4):
            if line9[k] > 0:
                resolved = due[k]
                break
        col_penalty = 0.0
        if i in (0, 1):
            end1 = min(resolved, UNDERPAYMENT_REGULAR_RATE1_END)
            days1 = max(0, (end1 - due[i]).days)
            col_penalty += line8[i] * (days1 / UNDERPAYMENT_REGULAR_DAY_BASIS) * UNDERPAYMENT_REGULAR_RATE_PERIOD_1
        period2_start = UNDERPAYMENT_REGULAR_RATE2_START if i in (0, 1) else due[i]
        if resolved > period2_start:
            end2 = min(resolved, UNDERPAYMENT_REGULAR_FINAL_BACKSTOP)
            days2 = max(0, (end2 - period2_start).days)
            col_penalty += line8[i] * (days2 / UNDERPAYMENT_REGULAR_DAY_BASIS) * UNDERPAYMENT_REGULAR_RATE_PERIOD_2
        col_penalty = round(col_penalty, 2)
        total_penalty += col_penalty
        columns.append({"underpayment": round(line8[i], 2), "penalty": col_penalty})

    total_penalty = round(total_penalty, 2)
    return {"penalty": total_penalty, "reason": "penalty_owed" if total_penalty > 0 else "no_underpayment",
            "required_annual_payment": rap["required_annual_payment"], "columns": columns}


def compute_underpayment_penalty_regular_ca_tax(conn, income_amount: float, filing_status: str,
                                                 prior_year_tax: float, prior_year_agi: float, withholding: float,
                                                 quarterly_payments, tax_year: int = DEFAULT_TAX_YEAR):
    if income_amount is None or income_amount < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    taxable_income = max(0.0, income_amount - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    current_year_tax = calc["total_tax"]
    penalty_calc = compute_underpayment_penalty_regular(current_year_tax, prior_year_tax, prior_year_agi,
                                                          withholding, filing_status, income_amount,
                                                          quarterly_payments)
    if not penalty_calc:
        return None
    return {**calc, "income_amount": income_amount, "taxable_income": taxable_income,
            "current_year_tax": current_year_tax, "prior_year_tax": prior_year_tax,
            "prior_year_agi": prior_year_agi, "withholding": withholding, "penalty": penalty_calc}


# --- Generic CA/federal capital-gain basis differences (Schedule CA (540)
# Part I Section A Line 7a; Schedule D (540) Column (c)) -- previously
# deferred as needing "cumulative historical CA-vs-federal basis tracking
# nobody has on hand from any single document." Re-examined 2026-08-23:
# that's true if this feature tried to DERIVE the CA basis from raw
# acquisition/depreciation history -- but Schedule CA Line 7a doesn't
# care HOW the CA-adjusted gain was derived, only what it IS. Reusing the
# SAME "trust the stated figure" precedent already used for every K-1/
# carryover/credit feature in this codebase: ask for the taxpayer's own
# ALREADY-COMPUTED federal and California capital gain figures directly,
# rather than trying to reconstruct either from scratch. This is a
# genuine simplification, not a scope dodge -- CA AGI only ever needs the
# CA-computed gain (other_income + ca_gain), the federal figure is used
# purely to compute and disclose the Line 7a adjustment amount for
# transparency, mirroring the form's own column structure.
#
# SCOPED to GAINS only (both figures non-negative) -- a basis difference
# affecting a LOSS interacts with the separate capital-loss annual-limit
# mechanic (already built elsewhere), a genuinely different case not
# attempted here. Also explicitly OUT OF SCOPE, routes elsewhere or
# defers rather than guesses: QSBS (Section 1202/1045 exclusions have
# their own dedicated feature and different mechanic entirely), K-1
# capital gains (own dedicated feature), home/residence sales (Section
# 121 exclusion is a completely different formula, not just a basis
# question -- tracked as ITS OWN separate deferred ledger item), and
# installment sales (FTB 3805E has its own multi-payment structure --
# also tracked as its own separate deferred ledger item).
GENERIC_BASIS_DIFF_CITATION = "2025 Schedule CA (540) Instructions -- Part I, Section A, Line 7a; Schedule D (540) Instructions, Column (c)"
GENERIC_BASIS_DIFF_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

GENERIC_BASIS_DIFF_TERMS = {
    "basis difference", "basis differences", "cost basis difference",
    "cost basis differences", "different basis for california",
    "california basis differs", "california basis is different",
    "federal and california basis differ", "different cost basis for california",
}
GENERIC_BASIS_DIFF_FEDERAL_GAIN_TERMS = {
    "federal capital gain", "federal capital gains", "federal gain",
    "gain for federal purposes", "gain on my federal return",
}
GENERIC_BASIS_DIFF_CA_GAIN_TERMS = {
    "california capital gain", "california capital gains", "ca capital gain",
    "california gain", "gain for california purposes", "gain on my california return",
}
GENERIC_BASIS_DIFF_OUT_OF_SCOPE_TERMS = {
    "qsbs", "qualified small business stock", "section 1202", "irc 1202",
    "section 1045", "irc 1045",
    "k-1 capital gain", "k1 capital gain", "schedule k-1 capital gain",
    "home sale", "sale of my home", "sale of my house", "sold my home",
    "sold my house", "personal residence",
    "installment sale", "installment payments", "installment agreement",
}
GENERIC_BASIS_DIFF_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _generic_basis_diff_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in GENERIC_BASIS_DIFF_TERMS):
        return False
    if any(t in q for t in GENERIC_BASIS_DIFF_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in GENERIC_BASIS_DIFF_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_generic_basis_diff_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'stated
    federal capital gain + stated California capital gain (a basis
    difference)' question."""
    q = question.lower()
    if not _generic_basis_diff_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_generic_basis_diff_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _generic_basis_diff_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_generic_basis_diff_out_of_scope(question: str) -> bool:
    """True iff this feature's own trigger vocabulary is present
    alongside QSBS/K-1/home-sale/installment-sale language -- each of
    those has its own genuinely different mechanic, not just a basis
    question this simplified path can answer."""
    q = question.lower()
    return (any(t in q for t in GENERIC_BASIS_DIFF_TERMS)
            and any(t in q for t in GENERIC_BASIS_DIFF_OUT_OF_SCOPE_TERMS))


def compute_generic_basis_diff_ca_tax(conn, other_income: float, federal_gain: float, ca_gain: float,
                                        filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """other_income excludes the capital gain itself (stated separately).
    CA AGI only ever needs ca_gain (the taxpayer's own CA-basis-computed
    figure) -- federal_gain is used only to compute/disclose the
    Line 7a adjustment amount, mirroring the form's own column
    structure. Scoped to GAINS only (both figures >= 0)."""
    if other_income is None or other_income < 0:
        return None
    if federal_gain is None or federal_gain < 0:
        return None
    if ca_gain is None or ca_gain < 0:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = other_income + ca_gain
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    adjustment = round(ca_gain - federal_gain, 2)
    return {**calc, "other_income": other_income, "federal_gain": federal_gain, "ca_gain": ca_gain,
            "adjustment": adjustment, "agi": agi, "taxable_income": taxable_income,
            "standard_deduction": dedu["amount"]}


# --- Installment sale gain (FTB 3805E) with a CA/federal basis difference
# (Schedule CA (540) Line 7a) -- previously deferred for TWO reasons: (1)
# the generic basis-differences problem (now solved, see above), and (2)
# FTB 3805E itself being "a multi-input form (price/basis/gross-profit-
# ratio/payments-received) rather than a single stated fact." Re-examined
# 2026-08-23: reason (2) only applies if this feature tried to COMPUTE
# the gross-profit-ratio-times-payments-received recognition math from
# scratch. It doesn't need to -- a taxpayer with an ongoing installment
# sale already has (or, for the federal side, is legally required to
# have) a Form 6252/3805E computing THIS YEAR's recognized gain; this
# feature just needs that already-computed recognized-gain figure for
# federal and for California, same as the generic basis-difference
# feature. Reuses compute_generic_basis_diff_ca_tax's exact math (same
# "CA AGI only needs the CA figure, federal figure is for the disclosed
# adjustment" mechanic) -- installment-sale gain recognized this year is
# structurally just another capital gain from CA AGI's perspective, only
# the SOURCE of the basis difference (a multi-year payment schedule
# instead of a one-time sale) differs, and that's already baked into
# whatever recognized-gain figures the taxpayer states. Scoped to a
# single installment sale's gain for the CURRENT year only (not the
# taxpayer's full remaining installment schedule).
INSTALLMENT_SALE_BASIS_DIFF_CITATION = "2025 Schedule CA (540) Instructions -- Part I, Section A, Line 7a; FTB Form 3805E Instructions"
INSTALLMENT_SALE_BASIS_DIFF_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

INSTALLMENT_SALE_BASIS_DIFF_TERMS = {
    "installment sale", "installment sale gain", "form 3805e", "ftb 3805e", "3805e",
    # deliberately does NOT include "installment sale basis difference" --
    # found live: that compound phrase contains "basis difference" as a
    # substring (a GENERIC_BASIS_DIFF_TERMS trigger) AND "installment
    # sale" (one of GENERIC_BASIS_DIFF_OUT_OF_SCOPE_TERMS's own terms),
    # so it satisfied the GENERIC feature's own out-of-scope collision
    # check and got intercepted there, before this dedicated feature
    # (checked later in the dispatcher) ever ran. "installment sale"
    # alone is already a sufficient, unambiguous trigger.
}
INSTALLMENT_SALE_BASIS_DIFF_OUT_OF_SCOPE_TERMS = {
    "qsbs", "qualified small business stock", "section 1202", "irc 1202",
    "section 1045", "irc 1045",
    "k-1 capital gain", "k1 capital gain", "schedule k-1 capital gain",
    "home sale", "sale of my home", "sale of my house", "sold my home",
    "sold my house", "personal residence",
}
INSTALLMENT_SALE_BASIS_DIFF_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _installment_sale_basis_diff_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in INSTALLMENT_SALE_BASIS_DIFF_TERMS):
        return False
    if any(t in q for t in INSTALLMENT_SALE_BASIS_DIFF_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in INSTALLMENT_SALE_BASIS_DIFF_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_installment_sale_basis_diff_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'installment
    sale, stated federal recognized gain this year + stated California
    recognized gain this year' question."""
    q = question.lower()
    if not _installment_sale_basis_diff_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_installment_sale_basis_diff_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _installment_sale_basis_diff_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_installment_sale_basis_diff_out_of_scope(question: str) -> bool:
    q = question.lower()
    return (any(t in q for t in INSTALLMENT_SALE_BASIS_DIFF_TERMS)
            and any(t in q for t in INSTALLMENT_SALE_BASIS_DIFF_OUT_OF_SCOPE_TERMS))


def compute_installment_sale_basis_diff_ca_tax(conn, other_income: float, federal_gain: float, ca_gain: float,
                                                 filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Thin wrapper over compute_generic_basis_diff_ca_tax -- installment-
    sale gain recognized this year is, from CA AGI's perspective,
    structurally just another capital gain; see module note above for
    why the multi-year payment-schedule mechanics don't need to be
    reproduced here. Returns the SAME shape, with its own citation."""
    calc = compute_generic_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "citation": INSTALLMENT_SALE_BASIS_DIFF_CITATION,
            "source_url": INSTALLMENT_SALE_BASIS_DIFF_SOURCE_URL}


# --- Gain on personal residence sale where CA/federal depreciation
# diverged (Schedule CA (540) Line 7a) -- "same historical-depreciation-
# tracking family as the generic basis-differences item." Re-examined
# 2026-08-23 with the SAME reframing: reuses compute_generic_basis_diff_ca_tax
# unchanged, asking for the taxpayer's already-computed federal and
# California gain figures directly. Scoped NARROWLY on purpose -- this
# population specifically means a residence that had BUSINESS/RENTAL USE
# (home office, partial rental conversion) generating a depreciation
# history to diverge on; an ordinary personal-use-only home sale has no
# depreciation at all and isn't what this item is about. Trigger
# therefore requires BOTH home/residence-sale language AND an explicit
# depreciation mention together (an AND-combination, not a flat term
# set, since the natural phrasing space is combinatorial) -- a bare home-
# sale question without depreciation language does NOT match here.
#
# Explicitly assumes the stated gain figures are ALREADY NET of any IRC
# Section 121 exclusion ($250k/$500k MFJ) -- CA generally conforms to
# Section 121 itself, so the exclusion mechanic isn't re-derived here,
# only the depreciation-driven basis/gain difference underneath it.
HOME_SALE_BASIS_DIFF_CITATION = "2025 Schedule CA (540) Instructions -- Part I, Section A, Line 7a"
HOME_SALE_BASIS_DIFF_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

HOME_SALE_BASIS_DIFF_HOME_TERMS = {
    "home sale", "house sale", "residence sale", "sale of my home",
    "sale of my house", "sold my home", "sold my house", "personal residence",
    "primary residence",
}
HOME_SALE_BASIS_DIFF_OUT_OF_SCOPE_TERMS = {
    "qsbs", "qualified small business stock", "section 1202", "irc 1202",
    "section 1045", "irc 1045",
    "k-1 capital gain", "k1 capital gain", "schedule k-1 capital gain",
    "installment sale", "installment payments",
}
HOME_SALE_BASIS_DIFF_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _home_sale_basis_diff_has_trigger(q: str) -> bool:
    """Requires BOTH home/residence-sale language AND an explicit
    depreciation mention -- an ordinary personal-use-only home sale has
    no depreciation history to diverge on, so a bare home-sale question
    is deliberately NOT matched here (falls through to whatever other
    path, if any, handles a plain home sale)."""
    return "depreciation" in q and any(t in q for t in HOME_SALE_BASIS_DIFF_HOME_TERMS)


def _home_sale_basis_diff_base_signal_ok(q: str) -> bool:
    if not _home_sale_basis_diff_has_trigger(q):
        return False
    if any(t in q for t in HOME_SALE_BASIS_DIFF_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in HOME_SALE_BASIS_DIFF_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_home_sale_basis_diff_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'home sale
    with diverged CA/federal depreciation, stated federal gain + stated
    California gain (both already net of any Section 121 exclusion)'
    question."""
    q = question.lower()
    if not _home_sale_basis_diff_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_home_sale_basis_diff_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _home_sale_basis_diff_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_home_sale_basis_diff_out_of_scope(question: str) -> bool:
    q = question.lower()
    return (_home_sale_basis_diff_has_trigger(q)
            and any(t in q for t in HOME_SALE_BASIS_DIFF_OUT_OF_SCOPE_TERMS))


def compute_home_sale_basis_diff_ca_tax(conn, other_income: float, federal_gain: float, ca_gain: float,
                                          filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Thin wrapper over compute_generic_basis_diff_ca_tax -- both
    federal_gain and ca_gain are assumed to already be NET of any
    Section 121 exclusion (see module note above)."""
    calc = compute_generic_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "citation": HOME_SALE_BASIS_DIFF_CITATION,
            "source_url": HOME_SALE_BASIS_DIFF_SOURCE_URL}


# --- Schedule D-1 ordinary business-property gain with a CA/federal
# basis difference (Schedule CA (540) Part I Section B Line 4; parallels
# federal Form 4797, Section 1231/1245/1250 gains) -- distinct from the
# CAPITAL-asset gains on Line 7a (Section A) above. Re-examined
# 2026-08-23 with the same reframing: the divergence driver (CA's
# standing non-conformity to bonus depreciation, IRC 168(k)) requires
# cumulative historical CA-basis depreciation tracking to DERIVE -- but
# again, this feature doesn't need to derive it, only accept the
# taxpayer's own already-computed federal and California Form 4797/
# Schedule D-1 gain figures directly. Reuses compute_generic_basis_diff_ca_tax's
# exact math -- an ordinary Section 1231/1245/1250 gain affects CA AGI
# the same additive way a capital gain does.
#
# SCOPED to GAINS only, same as the other basis-difference features (a
# NET LOSS on business-property sales is a real, disclosed limitation --
# ordinary losses under this line are NOT subject to the capital-loss
# annual limit, a genuinely different mechanic this build doesn't
# attempt, to avoid introducing a new sign-detection extraction risk for
# a comparatively rarer case).
SCHEDULE_D1_BASIS_DIFF_CITATION = "2025 Schedule CA (540) Instructions -- Part I, Section B, Line 4; Schedule D-1 Instructions (parallels federal Form 4797)"
SCHEDULE_D1_BASIS_DIFF_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

SCHEDULE_D1_BASIS_DIFF_TERMS = {
    "schedule d-1", "form 4797", "section 1231", "section 1245", "section 1250",
    "business property sale", "sale of business property", "1231 gain",
    "1245 recapture", "1250 recapture",
}
SCHEDULE_D1_BASIS_DIFF_FEDERAL_GAIN_TERMS = {
    "federal gain", "federal capital gain", "gain for federal purposes",
    "gain on my federal return", "federal 4797 gain", "federal section 1231 gain",
}
SCHEDULE_D1_BASIS_DIFF_CA_GAIN_TERMS = {
    "california gain", "california capital gain", "gain for california purposes",
    "gain on my california return", "california 4797 gain", "california section 1231 gain",
}
SCHEDULE_D1_BASIS_DIFF_OUT_OF_SCOPE_TERMS = {
    "loss", "net loss", "business property loss",
}
SCHEDULE_D1_BASIS_DIFF_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE - {"business"}


def _schedule_d1_basis_diff_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in SCHEDULE_D1_BASIS_DIFF_TERMS):
        return False
    if any(t in q for t in SCHEDULE_D1_BASIS_DIFF_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in SCHEDULE_D1_BASIS_DIFF_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_schedule_d1_basis_diff_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'Schedule D-1/
    Form 4797 ordinary business-property GAIN with stated federal and
    California figures (a basis difference)' question. GAINS only --
    any loss-flavored language defers instead of guessing at the
    ordinary-loss mechanic."""
    q = question.lower()
    if not _schedule_d1_basis_diff_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_schedule_d1_basis_diff_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _schedule_d1_basis_diff_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_schedule_d1_basis_diff_out_of_scope(question: str) -> bool:
    """True iff this feature's own trigger vocabulary is present
    alongside loss-flavored language -- an ordinary Section 1231/1245/
    1250 LOSS is not subject to the capital-loss annual limit, a
    genuinely different mechanic this scoped (gains-only) build doesn't
    attempt."""
    q = question.lower()
    return (any(t in q for t in SCHEDULE_D1_BASIS_DIFF_TERMS)
            and any(t in q for t in SCHEDULE_D1_BASIS_DIFF_OUT_OF_SCOPE_TERMS))


def compute_schedule_d1_basis_diff_ca_tax(conn, other_income: float, federal_gain: float, ca_gain: float,
                                            filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Thin wrapper over compute_generic_basis_diff_ca_tax -- an ordinary
    Section 1231/1245/1250 gain affects CA AGI the same additive way a
    capital gain does. GAINS only (both figures >= 0); see module note
    above for why a net loss isn't attempted here."""
    calc = compute_generic_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "citation": SCHEDULE_D1_BASIS_DIFF_CITATION,
            "source_url": SCHEDULE_D1_BASIS_DIFF_SOURCE_URL}


# --- Rental/royalty depreciation basis difference, ORDINARY (non-real-
# estate-professional) case (Schedule CA (540) Part I Section B Line 5;
# FTB 3885A) -- "for an activity that's PASSIVE under both CA and
# federal law (the ordinary landlord/K-1 case, dominant real-world
# driver of this line), the PAL mechanic itself is confirmed identical
# CA/federal... [divergence] comes purely from feeding CA-basis vs
# federal-basis income/loss... into that otherwise-identical
# calculation." Re-examined 2026-08-23 with the same reframing: asks
# for the taxpayer's own already-computed federal and California net
# rental/royalty income figures directly (each already reflecting its
# own jurisdiction's PAL-limited result), rather than reproducing the
# PAL mechanic or the depreciation history behind it. Reuses
# compute_generic_basis_diff_ca_tax's exact math.
#
# SCOPED to GAINS (net rental/royalty INCOME) only, same as the other
# basis-difference features -- unlike installment sale/home sale, this
# is a real, disclosed limitation with meaningful reach: rental
# activities commonly show a LOSS after depreciation, and a net loss
# here would need to interact with the ALREADY-BUILT real-estate-
# professional allowance (compute_real_estate_pro_ca_tax) or the
# passive-activity-loss suspension (not modeled at all in this
# codebase) for correctness -- genuinely more than a sign flip, not
# attempted here. Real-estate-professional language explicitly routes
# to that separate, already-built feature instead of this one.
RENTAL_DEPRECIATION_BASIS_DIFF_CITATION = "2025 Schedule CA (540) Instructions -- Part I, Section B, Line 5; FTB 3885A Instructions"
RENTAL_DEPRECIATION_BASIS_DIFF_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

RENTAL_DEPRECIATION_BASIS_DIFF_TERMS = {
    "rental depreciation", "royalty depreciation", "ftb 3885a", "form 3885a",
    "rental basis difference", "royalty basis difference",
    "passive activity depreciation", "rental property depreciation",
}
RENTAL_DEPRECIATION_BASIS_DIFF_OUT_OF_SCOPE_TERMS = {
    "real estate professional", "real property professional",
    "rental loss", "rental losses", "royalty loss", "royalty losses",
    "qsbs", "qualified small business stock", "section 1202", "irc 1202",
    "section 1045", "irc 1045",
    "k-1 capital gain", "k1 capital gain",
}
RENTAL_DEPRECIATION_BASIS_DIFF_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE - {"rental", "renting", "rented"}


def _rental_depreciation_basis_diff_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in RENTAL_DEPRECIATION_BASIS_DIFF_TERMS):
        return False
    if any(t in q for t in RENTAL_DEPRECIATION_BASIS_DIFF_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in RENTAL_DEPRECIATION_BASIS_DIFF_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_rental_depreciation_basis_diff_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'ordinary
    rental/royalty activity, stated federal and California NET INCOME
    figures (a depreciation basis difference)' question. INCOME (gains)
    only -- a net loss defers instead of guessing at the PAL/real-
    estate-professional interaction."""
    q = question.lower()
    if not _rental_depreciation_basis_diff_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_rental_depreciation_basis_diff_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _rental_depreciation_basis_diff_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_rental_depreciation_basis_diff_out_of_scope(question: str) -> bool:
    """True iff this feature's own trigger vocabulary is present
    alongside real-estate-professional or loss-flavored language --
    both need a genuinely different mechanic (the already-built real-
    estate-professional allowance, or the unmodeled PAL suspension),
    not just this simplified income-only basis question."""
    q = question.lower()
    return (any(t in q for t in RENTAL_DEPRECIATION_BASIS_DIFF_TERMS)
            and any(t in q for t in RENTAL_DEPRECIATION_BASIS_DIFF_OUT_OF_SCOPE_TERMS))


def compute_rental_depreciation_basis_diff_ca_tax(conn, other_income: float, federal_gain: float, ca_gain: float,
                                                     filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Thin wrapper over compute_generic_basis_diff_ca_tax -- net rental/
    royalty income affects CA AGI the same additive way a capital gain
    does, once each jurisdiction's own (otherwise-identical) PAL
    limitation has already been applied to produce the stated figures.
    INCOME (gains) only; see module note above for why a net loss isn't
    attempted here."""
    calc = compute_generic_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "citation": RENTAL_DEPRECIATION_BASIS_DIFF_CITATION,
            "source_url": RENTAL_DEPRECIATION_BASIS_DIFF_SOURCE_URL}


# --- Farm income (Schedule F) depreciation basis difference, INCOME
# only (Schedule CA (540) Part I Section B Line 6; FTB 3801/3885A) --
# "confirmed same bonus-depreciation/168(k) CA-basis problem as Lines
# 4/5, applied to Schedule F assets via FTB 3885A." Re-examined
# 2026-08-23: the 2026-08-15 research checked for a Line-5-style
# tractable sub-population (a farm-specific carve-out analogous to the
# real-estate-professional exception) and correctly found none -- but
# that's a DIFFERENT question than whether the SAME "trust the stated
# already-computed figure" reframing (which doesn't need a carve-out at
# all, just asks for the result directly) applies here too. It does --
# structurally this is Line 5's exact mechanic, substituting Schedule F
# farm income for Schedule E rental income. Reuses
# compute_generic_basis_diff_ca_tax's exact math. The research's OTHER
# two findings (passive-activity: no CA divergence exists for farming
# at all; NOL: not actually a distinct Line 6 mechanism) are UNCHANGED
# and still correctly out of scope for this line.
#
# SCOPED to INCOME (gains) only, same discipline as rental depreciation
# -- a net farm LOSS is a real, disclosed limitation not attempted here,
# for consistency across every basis-difference feature in this family
# rather than introducing a new signed-value extraction risk this late.
FARM_DEPRECIATION_BASIS_DIFF_CITATION = "2025 Schedule CA (540) Instructions -- Part I, Section B, Line 6; FTB 3801/3885A Instructions"
FARM_DEPRECIATION_BASIS_DIFF_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

FARM_DEPRECIATION_BASIS_DIFF_TERMS = {
    "farm depreciation", "farm income depreciation", "schedule f depreciation",
    "farm basis difference", "farming depreciation",
}
FARM_DEPRECIATION_BASIS_DIFF_OUT_OF_SCOPE_TERMS = {
    "farm loss", "farm losses", "farming loss",
    "qsbs", "qualified small business stock", "section 1202", "irc 1202",
    "section 1045", "irc 1045",
    "k-1 capital gain", "k1 capital gain",
}
FARM_DEPRECIATION_BASIS_DIFF_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _farm_depreciation_basis_diff_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in FARM_DEPRECIATION_BASIS_DIFF_TERMS):
        return False
    if any(t in q for t in FARM_DEPRECIATION_BASIS_DIFF_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in FARM_DEPRECIATION_BASIS_DIFF_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_farm_depreciation_basis_diff_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'farm
    (Schedule F) activity, stated federal and California NET INCOME
    figures (a depreciation basis difference)' question. INCOME (gains)
    only -- a net loss defers instead of guessing."""
    q = question.lower()
    if not _farm_depreciation_basis_diff_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_farm_depreciation_basis_diff_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _farm_depreciation_basis_diff_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_farm_depreciation_basis_diff_out_of_scope(question: str) -> bool:
    q = question.lower()
    return (any(t in q for t in FARM_DEPRECIATION_BASIS_DIFF_TERMS)
            and any(t in q for t in FARM_DEPRECIATION_BASIS_DIFF_OUT_OF_SCOPE_TERMS))


def compute_farm_depreciation_basis_diff_ca_tax(conn, other_income: float, federal_gain: float, ca_gain: float,
                                                   filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Thin wrapper over compute_generic_basis_diff_ca_tax -- net farm
    income affects CA AGI the same additive way a capital gain does.
    INCOME (gains) only; see module note above for why a net loss isn't
    attempted here."""
    calc = compute_generic_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "citation": FARM_DEPRECIATION_BASIS_DIFF_CITATION,
            "source_url": FARM_DEPRECIATION_BASIS_DIFF_SOURCE_URL}


# --- IRA distribution basis/timing difference (Schedule CA (540) Part I
# Section A Line 4a/4b; FTB Pub 1005) -- "requires historical CA-vs-
# federal contribution basis, not a single stated fact." Re-examined
# 2026-08-23 with the SAME reframing applied to every other basis-
# difference item this pass: the taxable PORTION of an IRA distribution
# can differ between CA and federal because of differing after-tax
# contribution-basis history (e.g. a year CA didn't conform to a federal
# IRA deduction, or vice versa) -- but this feature doesn't need to
# reconstruct that contribution history. It only needs the taxpayer's
# own already-computed federal AND California TAXABLE DISTRIBUTION
# figures for this year (the CA figure typically already derived via
# FTB Pub 1005's own worksheet). Reuses compute_generic_basis_diff_ca_tax's
# exact math -- an IRA distribution's taxable portion affects CA AGI the
# same additive way a capital gain does.
IRA_DISTRIBUTION_BASIS_DIFF_CITATION = "2025 Schedule CA (540) Instructions -- Part I, Section A, Line 4a/4b; FTB Publication 1005"
IRA_DISTRIBUTION_BASIS_DIFF_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"

IRA_DISTRIBUTION_BASIS_DIFF_TERMS = {
    "ira distribution basis difference", "ira distribution basis",
    "ira basis difference", "different ira basis for california",
    "california ira basis differs", "ira distribution basis differs",
}
IRA_DISTRIBUTION_BASIS_DIFF_FEDERAL_TERMS = {
    "federal taxable distribution", "federal taxable ira distribution",
    "taxable for federal purposes", "federal distribution",
}
IRA_DISTRIBUTION_BASIS_DIFF_CA_TERMS = {
    "california taxable distribution", "california taxable ira distribution",
    "taxable for california purposes", "california distribution",
}
IRA_DISTRIBUTION_BASIS_DIFF_OUT_OF_SCOPE_TERMS = {
    "roth ira", "roth conversion", "early distribution", "early withdrawal",
}
IRA_DISTRIBUTION_BASIS_DIFF_COMPLEXITY_EXCLUDE = COMPLEXITY_EXCLUDE


def _ira_distribution_basis_diff_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in IRA_DISTRIBUTION_BASIS_DIFF_TERMS):
        return False
    if any(t in q for t in IRA_DISTRIBUTION_BASIS_DIFF_OUT_OF_SCOPE_TERMS):
        return False
    if any(t in q for t in IRA_DISTRIBUTION_BASIS_DIFF_COMPLEXITY_EXCLUDE):
        return False
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return True


def detect_ira_distribution_basis_diff_signal(question: str):
    """Returns filing_status iff this looks like a genuine 'IRA
    distribution, stated federal AND California taxable-distribution
    figures (a contribution-basis difference)' question."""
    q = question.lower()
    if not _ira_distribution_basis_diff_base_signal_ok(q):
        return None
    return detect_filing_status(question)


def detect_ira_distribution_basis_diff_missing_filing_status(question: str) -> bool:
    q = question.lower()
    if not _ira_distribution_basis_diff_base_signal_ok(q):
        return False
    return detect_filing_status(question) is None


def detect_ira_distribution_basis_diff_out_of_scope(question: str) -> bool:
    """True iff this feature's own trigger vocabulary is present
    alongside Roth/early-distribution language -- both have their own
    genuinely different mechanics (Roth conversions have no comparable
    CA/federal basis-timing question the same way; early distributions
    have their own dedicated additional-tax feature) not attempted here."""
    q = question.lower()
    return (any(t in q for t in IRA_DISTRIBUTION_BASIS_DIFF_TERMS)
            and any(t in q for t in IRA_DISTRIBUTION_BASIS_DIFF_OUT_OF_SCOPE_TERMS))


def compute_ira_distribution_basis_diff_ca_tax(conn, other_income: float, federal_gain: float, ca_gain: float,
                                                 filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Thin wrapper over compute_generic_basis_diff_ca_tax -- an IRA
    distribution's taxable portion affects CA AGI the same additive way
    a capital gain does. federal_gain/ca_gain here mean the federal and
    California TAXABLE DISTRIBUTION amounts, not a capital gain."""
    calc = compute_generic_basis_diff_ca_tax(conn, other_income, federal_gain, ca_gain, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "citation": IRA_DISTRIBUTION_BASIS_DIFF_CITATION,
            "source_url": IRA_DISTRIBUTION_BASIS_DIFF_SOURCE_URL}


def detect_filing_status(question: str):
    """Abbreviations (mfj/mfs/hoh/qss) are recognized standalone -- they
    already encode "married"/etc on their own, so requiring the spelled-out
    word too (an earlier version of this function did) missed a real user
    typing just "filing MFS" with no other wording. Found via adversarial
    testing: that phrasing fell through to a generic defer despite stating
    a filing status. Hyphenated "head-of-household" is also recognized
    alongside the spaced form, same reasoning.

    Uses a \\bmarried\\b WORD-BOUNDARY check, not a plain substring test --
    found via testing the Joint Custody HOH credit (which combines
    "unmarried" with "joint custody" in one sentence): a plain "married"
    in q substring check ALSO matches inside "unmarried" (un+MARRIED), so
    that combination was wrongly detected as MFJ filing status. This is
    the SAME bug class as several other fixes this session (a literal
    substring match firing inside a negated/prefixed word), this time in
    the single most shared function in the income domain -- every compute
    path calls this, so the fix benefits all of them, not just the one
    question shape that exposed it."""
    q = question.lower()
    if re.search(r"\bmfj\b", q) or (re.search(r"\bmarried\b", q) and "joint" in q):
        return "mfj"
    if re.search(r"\bmfs\b", q) or (re.search(r"\bmarried\b", q) and "separat" in q):
        return "mfs"
    if "head of household" in q or "head-of-household" in q or re.search(r"\bhoh\b", q):
        return "hoh"
    if "qualifying widow" in q or "qualifying surviving spouse" in q or re.search(r"\bqss\b", q):
        return "qss"
    if re.search(r"\bsingle\b", q):
        return "single"
    return None


def detect_compute_signal(question: str):
    """Returns a filing_status key iff this question is a good candidate for
    the deterministic wage-earner+standard-deduction bracket computation:
    an explicit filing status, an explicit trigger phrase, and NONE of the
    complexity-disqualifying terms. Returns None otherwise (caller falls
    through to the informational tier -- a safe defer, never a guess)."""
    q = question.lower()
    if any(term in q for term in COMPLEXITY_EXCLUDE):
        return None
    if "capital gain" in q and any(t in q for t in HOME_SALE_TERMS):
        return None   # home-sale gain -- Section 121 exclusion, different calc entirely
    if any(t in q for t in QSBS_TERMS):
        return None   # QSBS -- a stated gain figure is ambiguous (pre- or post-exclusion?
                       # see QSBS_TERMS's module note); the dedicated QSBS path asks for
                       # both figures explicitly rather than guessing which one this is
    if _has_nol_term(q):
        return None   # NOL carryover -- found live building the wages-only NOL path: a
                       # question mentioning an NOL carryover with no OTHER
                       # COMPLEXITY_EXCLUDE term (e.g. no "business" word at all, the
                       # ambiguous closed-vs-ongoing-business case) would otherwise let
                       # this plain single-amount path grab the FIRST dollar figure in
                       # the question (often the NOL amount itself, not income) and
                       # answer with a silently wrong number instead of deferring to
                       # detect_nol_signal/detect_nol_wages_signal/detect_nol_wages_ambiguous
    if any(t in q for t in GENERIC_BASIS_DIFF_TERMS):
        return None   # basis difference -- SAME collision class as NOL, found live
                       # building the generic-basis-difference path: this plain path's
                       # "capital gain" income-type label (INCOME_TYPE_LABELS) already
                       # recognizes bare "capital gain" wording, so a "basis difference"
                       # question mentioning "federal capital gain"/"California capital
                       # gain" would otherwise be answered here using the FIRST dollar
                       # figure (often "other income", not either gain figure) instead
                       # of deferring to detect_generic_basis_diff_signal
    if any(t in q for t in INSTALLMENT_SALE_BASIS_DIFF_TERMS):
        return None   # installment sale -- same collision class as the generic basis
                       # difference above (same anchor vocabulary, same risk)
    if any(t in q for t in SCHEDULE_D1_BASIS_DIFF_TERMS):
        return None   # Schedule D-1/Form 4797 -- same collision class, added proactively
                       # this time (found live 3 times already for basis-difference
                       # features: generic, NOL, and by extension every sibling)
    if any(t in q for t in RENTAL_DEPRECIATION_BASIS_DIFF_TERMS):
        return None   # rental/royalty depreciation -- same collision class, added
                       # proactively before testing
    if any(t in q for t in FARM_DEPRECIATION_BASIS_DIFF_TERMS):
        return None   # farm depreciation -- same collision class, added proactively
    if any(t in q for t in IRA_DISTRIBUTION_BASIS_DIFF_TERMS):
        return None   # IRA distribution basis -- same collision class, added proactively
    if any(t in q for t in KIDDIE_TAX_TERMS):
        return None   # kiddie tax -- same collision class: a stated "child's unearned
                       # income" figure and a stated parent's filing status would
                       # otherwise let this plain single-taxpayer path grab one of those
                       # two figures and answer with a wrong number instead of deferring
                       # to detect_kiddie_tax_signal, added proactively
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return None
    return detect_filing_status(question)


def detect_compute_missing_filing_status(question: str) -> bool:
    """True iff this question is clearly a tax-computation request (a
    trigger phrase, no complexity disqualifiers) but doesn't name a filing
    status -- i.e. detect_compute_signal would have fired except for the
    missing filing status specifically. Filing status changes WHICH bracket
    table applies (materially, not a rounding nuance like the wage-only/
    standard-deduction assumptions the compute path already discloses
    rather than asks about), so it's still not safe to guess -- but the
    caller can now tell the user exactly what's missing instead of a
    generic, unhelpful defer."""
    q = question.lower()
    if any(term in q for term in COMPLEXITY_EXCLUDE):
        return False
    if "capital gain" in q and any(t in q for t in HOME_SALE_TERMS):
        return False
    if any(t in q for t in QSBS_TERMS):
        return False   # mirrors detect_compute_signal's QSBS guard
    if not any(trig in q for trig in COMPUTE_TRIGGERS):
        return False
    return detect_filing_status(question) is None


def detect_deduction_question(question: str) -> bool:
    q = question.lower()
    return any(trig in q for trig in DEDUCTION_TRIGGERS)


def standard_deduction(conn, filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    r = conn.execute(
        "SELECT amount, citation, source_url FROM ca_standard_deduction "
        "WHERE tax_year=%s AND filing_status=%s", (tax_year, filing_status)).fetchone()
    if not r:
        return None
    return {"amount": float(r[0]), "citation": r[1], "source_url": r[2]}


def compute_ca_tax(conn, taxable_income: float, filing_status: str,
                    tax_year: int = DEFAULT_TAX_YEAR):
    """Segment lookup (not summation -- FTB's own schedules already publish
    the cumulative base_amount at each bracket floor, see load_income_content.py)
    + the Behavioral Health Services Tax surtax, computed as its own separate
    step. Returns None if there's no bracket data for this (tax_year,
    filing_status) -- caller must treat that as "can't compute", not $0."""
    if taxable_income < 0:
        taxable_income = 0.0
    rows = conn.execute(
        "SELECT bracket_floor, bracket_ceiling, base_amount, rate, citation, source_url "
        "FROM ca_income_tax_brackets WHERE tax_year=%s AND filing_status=%s "
        "AND bracket_type='standard' ORDER BY bracket_floor", (tax_year, filing_status)
    ).fetchall()
    if not rows:
        return None
    seg = rows[-1]   # default to the top (unbounded) bracket
    for floor, ceiling, base_amount, rate, citation, source_url in rows:
        if taxable_income >= float(floor) and (ceiling is None or taxable_income <= float(ceiling)):
            seg = (floor, ceiling, base_amount, rate, citation, source_url)
            break
    floor, _ceiling, base_amount, rate, citation, source_url = seg
    bracket_tax = round(float(base_amount) + float(rate) * (taxable_income - float(floor)), 2)

    surtax_row = conn.execute(
        "SELECT bracket_floor, rate, citation FROM ca_income_tax_brackets "
        "WHERE tax_year=%s AND bracket_type='mhs_surtax' LIMIT 1", (tax_year,)).fetchone()
    surtax = 0.0
    surtax_citation = None
    if surtax_row and taxable_income > float(surtax_row[0]):
        surtax = round(float(surtax_row[1]) * (taxable_income - float(surtax_row[0])), 2)
        surtax_citation = surtax_row[2]

    return {
        "taxable_income": taxable_income, "bracket_tax": bracket_tax,
        "marginal_rate": float(rate), "surtax": surtax,
        "surtax_citation": surtax_citation, "total_tax": round(bracket_tax + surtax, 2),
        "citation": citation, "source_url": source_url,
    }
