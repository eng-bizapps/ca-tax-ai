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
import re

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
                                     cannabis_280e_expenses: float = None):
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
    parameter existed."""
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
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "net_profit": net_profit, "se_tax": se["se_tax"],
            "cannabis_280e_expenses": cannabis_280e_expenses,
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
                              salt_cap_addback: float = None):
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
    addback. If none of these are given, itemized_amount is trusted as
    already CA-conforming, same as before these parameters existed."""
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
