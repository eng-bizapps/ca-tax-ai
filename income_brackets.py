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
                                     tax_year: int = DEFAULT_TAX_YEAR):
    """net_profit is Schedule C NET PROFIT (revenue minus business expenses),
    not federal AGI or taxable income -- computing directly from net profit
    (rather than from an already-computed federal figure) means the federal
    Qualified Business Income deduction (IRC Section 199A, which California
    does NOT conform to) never enters the calculation at all, sidestepping
    that non-conformity entirely rather than requiring an addback."""
    if net_profit is None or net_profit <= 0:
        return None
    se = compute_se_tax(net_profit)
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    agi = net_profit - se["half_deduction"]
    taxable_income = max(0.0, agi - dedu["amount"])
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "net_profit": net_profit, "se_tax": se["se_tax"],
            "half_se_deduction": se["half_deduction"], "agi": agi,
            "standard_deduction": dedu["amount"]}


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


def compute_itemized_ca_tax(conn, income_amount: float, itemized_amount: float,
                              filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """income_amount is treated as both gross wage income and California AGI
    (no other adjustments -- same simple-case assumption as the plain
    wage-earner compute path). Returns None if AGI is at/above the CA
    itemized-deduction limitation threshold for this filing status (see
    module note above) -- caller must defer, not silently skip the
    reduction worksheet."""
    if income_amount is None or itemized_amount is None or income_amount < 0 or itemized_amount < 0:
        return None
    threshold = ITEMIZED_AGI_LIMIT_THRESHOLD.get(filing_status)
    if threshold is None or income_amount >= threshold:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    used_itemized = itemized_amount > dedu["amount"]
    deduction_used = itemized_amount if used_itemized else dedu["amount"]
    taxable_income = max(0.0, income_amount - deduction_used)
    calc = compute_ca_tax(conn, taxable_income, filing_status, tax_year)
    if not calc:
        return None
    return {**calc, "income_amount": income_amount, "itemized_amount": itemized_amount,
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


def detect_filing_status(question: str):
    """Abbreviations (mfj/mfs/hoh/qss) are recognized standalone -- they
    already encode "married"/etc on their own, so requiring the spelled-out
    word too (an earlier version of this function did) missed a real user
    typing just "filing MFS" with no other wording. Found via adversarial
    testing: that phrasing fell through to a generic defer despite stating
    a filing status. Hyphenated "head-of-household" is also recognized
    alongside the spaced form, same reasoning."""
    q = question.lower()
    if re.search(r"\bmfj\b", q) or ("married" in q and "joint" in q):
        return "mfj"
    if re.search(r"\bmfs\b", q) or ("married" in q and "separat" in q):
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
