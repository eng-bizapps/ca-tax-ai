"""Ring 3, Phases 1-3 -- California nonresident income tax (Form 540NR /
Schedule CA (540NR)). The FIRST genuinely new compute engine built this
session for the income domain -- unlike the Schedule CA Tier 1/2 work,
which extended the existing RESIDENT bracket engine with more facts, this
is a different tax mechanism entirely (apportionment by an effective
rate), for a population (nonresidents) the existing engine has never
handled at all.

Verified directly against FTB's 2025 Form 540NR booklet and Schedule CA
(540NR) instructions (ftb.ca.gov), not derived from general tax knowledge.

CORE MECHANIC -- quoted verbatim from FTB's own plain-English summary
("How Nonresidents and Part-Year Residents Are Taxed"): "You determine
your California tax by multiplying your California taxable income by an
effective tax rate. The effective tax rate is the tax on total taxable
income, taken from the tax table, divided by total taxable income."

Concretely, with Form 540NR line numbers in brackets:
  total_taxable_income = total income, ALL sources, as if a full-year CA
    resident, minus the FULL standard/itemized deduction        [line 19]
  tax_on_total = income_brackets.compute_ca_tax(total_taxable_income) --
    the EXACT SAME Schedule X/Y/Z brackets used for residents,
    including the Behavioral Health Services surtax               [line 31]
  effective_rate = tax_on_total / total_taxable_income            [line 36]
  ca_agi = CA-source income only                            [Part IV line 1]
  deduction_pct = ca_agi / total_income, capped at 1.0        [Part IV line 3]
  ca_taxable_income = ca_agi - (full_deduction * deduction_pct)   [line 35]
  ca_tax = ca_taxable_income * effective_rate                    [~line 37]

income_brackets.compute_ca_tax() IS the "tax on total taxable income" step
verbatim -- this module wraps it, it does not reimplement bracket math.
This is why Phase 1 was tractable to start immediately: the hard part of
nonresident taxation is INCOME SOURCING (which dollars count as
California's), not the tax math once sourcing is known.

NOT MODELED (disclosed, not silently ignored): California's small flat
personal/dependent EXEMPTION CREDIT (roughly $150-300 for 2025, applied
after the bracket tax on the real form) isn't implemented anywhere in this
project yet -- a pre-existing gap in the RESIDENT engine too, found while
researching this feature, not introduced by it. Omitting it makes the
computed tax a small overestimate, never an underestimate.

PHASE 1 SCOPE (deliberately narrow, "build the default case, defer the
rest" discipline used throughout this project): FULL-YEAR nonresidents
only (not part-year residents -- that's Phase 3, needs a date-based period
split). WAGE income only (no business/rental/investment income -- same
complexity boundary as the existing resident wage-only path). Two natural-
language shortcuts for the unambiguous ALL/NONE cases: "worked entirely in
California" or "did not work in California at all".

PHASE 2 (added 2026-08-11, same session): PARTIAL CA-source income is now
supported too -- `compute_nonresident_wage_tax`'s second parameter changed
from a 0.0/1.0-only FRACTION to a CA-SOURCE DOLLAR AMOUNT (any value from
0 to total_wages inclusive). The underlying formula chain above already
generalized correctly to any split (verified algebraically during Phase 1
scoping, before Phase 1 was even built) -- Phase 1 just chose to GATE
partial values as an intentional scope limit, not because the math needed
it. Phase 2 removes that gate and adds a way to STATE the CA-source amount
directly (same "trust the input, don't derive it" precedent as
income_brackets.py's SALT/mortgage-interest-addback/misc-itemized/
charitable/SALT-cap-addback figures) rather than deriving it from a day
count or work-location breakdown -- see CA_SOURCE_AMOUNT_TERMS. The two
Phase 1 ALL/NONE phrasings still work (they just resolve to
ca_source_amount == total_wages or == 0 before calling the same function).

SANITY CHECK proving this is wired correctly: when ca_source_amount ==
total_wages (the old fraction=1.0 case), ca_agi == total income and
deduction_pct == 1.0, so ca_taxable_income == total_taxable_income
algebraically, and ca_tax == total_taxable_income * (tax_on_total /
total_taxable_income) == tax_on_total EXACTLY -- i.e. a nonresident who
earned 100% of their income in California owes IDENTICAL tax to a
resident with the same income. Verified this holds via direct comparison
against income_brackets.compute_ca_tax() before trusting the formula
chain (see income_item_sweep.py) -- re-verified again after the Phase 2
signature change to confirm the refactor didn't disturb it.

PHASE 3 (added 2026-08-13, same session): PART-YEAR RESIDENTS. Researched
directly against FTB's 2025 Form 540NR booklet, Schedule CA (540NR)
instructions (Column E / Part-Year Resident Worksheet), and FTB Publication
1100 ("Taxation of Nonresidents and Individuals Who Change Residency") --
NOT derived from general tax knowledge. Form 540NR is titled "California
Nonresident OR PART-YEAR RESIDENT Income Tax Return" -- both populations
use the exact same form, the same Schedule CA (540NR), and (confirmed via
Schedule CA (540NR) instructions' Column E section, quoted) the exact same
apportionment formula documented above. The ONLY thing that changes for a
part-year resident is how the CA-source amount (ca_agi / Part IV line 1)
is CONSTRUCTED. Pub 1100's own definition of California AGI, quoted
verbatim: "Gross income and deductions derived from California sources for
any part of the taxable year during which you were a nonresident plus all
items of gross income and all deductions, regardless of source, for any
part of the taxable year during which you were a resident." In other
words: ALL income earned during the CA-resident portion of the year counts
as California-source REGARDLESS of where it was physically earned, PLUS
only the CA-source (physically-worked-in-California) portion of income
earned during the nonresident portion of the year.

Because the formula itself is identical, Phase 3 needed ZERO new math --
compute_nonresident_wage_tax is reused completely unchanged; only the
DETECTION and STATED-AMOUNT-MEANING differ. Scope, same "narrowest safe
default" discipline as Phases 1-2: wage income only, and (unlike the
full-year-nonresident Phase 1 ALL/NONE phrase shortcuts) no phrase-based
shortcut for part-year residents -- the combined resident-period-plus-
nonresident-CA-source definition doesn't reduce cleanly to a simple
ALL/NONE case, so a part-year question always requires a STATED CA-source
dollar figure (reusing the same CA_SOURCE_AMOUNT_TERMS anchor phrases as
Phase 2, since the taxpayer is directly stating a number either way -- the
"trust the input" precedent applies just as much here, only the guidance
in the clarifying message about WHAT to include differs by population).

Confirmed the formula against a real FTB worked example (Pub 1100, Example
1, single filer, moved FL->CA April 1): CA AGI $68,000 / Total AGI $84,000
-> tax on total taxable income $2,341 on $60,000 total taxable income ->
effective rate .0390 -> prorated tax (before the personal-exemption-credit
proration, which this project doesn't model, see the NOT MODELED note
above) = $48,572 x .0390 = $1,894, matching FTB's own reported figure
exactly. (That example also has itemized deductions and interest income,
outside this project's wage-only/standard-deduction scope -- used only to
confirm the core effective-rate mechanism, not run through this module's
functions directly.)
"""
from income_brackets import DEFAULT_TAX_YEAR, FILING_STATUS_LABELS, compute_ca_tax, standard_deduction

CITATION = "FTB 2025 Form 540NR Instructions -- Line 19/31/35/36; Schedule CA (540NR) Part IV"
SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540nr-booklet.html"

NONRESIDENT_TRIGGERS = {
    "nonresident of california", "non-resident of california", "california nonresident",
    "not a california resident", "not a resident of california",
    "i am not a resident of california", "i live outside california",
    "i don't live in california", "i do not live in california",
}

CA_SOURCE_ALL_TERMS = {
    "worked entirely in california", "worked only in california",
    "all my work was in california", "all my income was earned in california",
    "worked exclusively in california", "all of my wages were earned in california",
}
CA_SOURCE_NONE_TERMS = {
    "did not work in california", "never worked in california",
    "worked entirely outside california", "worked entirely outside of california",
    "did not work in california at all", "worked only outside california",
    "none of my work was in california", "did not perform any work in california",
}

# Phase 2: anchor phrases for a STATED PARTIAL CA-source dollar figure,
# e.g. "$80,000 in wages, $30,000 of which was earned working in
# California". Deliberately worded to NOT overlap with CA_SOURCE_ALL_TERMS/
# CA_SOURCE_NONE_TERMS's phrasing (different verb structure: "was earned
# in/working in" vs "worked entirely/only/never in") so a Phase 1
# ALL/NONE question never accidentally matches this too.
CA_SOURCE_AMOUNT_TERMS = {
    "was earned working in california", "was earned in california",
    "of which was california source", "of which was california-source",
    "earned while working in california", "was california-source income",
    "in california source wages", "in california-source wages",
}

# Phase 3: part-year resident signal phrases. Deliberately worded around
# "part of the year" / "moved" / "became" so they never overlap with
# NONRESIDENT_TRIGGERS's full-year phrasing (e.g. "not a resident of
# california") -- a part-year resident is neither a full-year resident nor
# a full-year nonresident, and needs its own detection path with its own
# CA-source-amount MEANING (see module docstring), even though it reuses
# the same numeric extraction (CA_SOURCE_AMOUNT_TERMS) and the same
# compute function.
PART_YEAR_TRIGGERS = {
    "part-year resident of california", "part year resident of california",
    "partial-year resident of california", "partial year resident of california",
    "moved to california during the year", "moved out of california during the year",
    "became a california resident during the year",
    "stopped being a california resident during the year",
    "i was a california resident for part of the year",
    "i was a resident of california for part of the year",
    "moved into california partway through the year",
    "moved out of california partway through the year",
}


def detect_nonresident_signal(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in NONRESIDENT_TRIGGERS)


def detect_part_year_signal(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in PART_YEAR_TRIGGERS)


def detect_ca_source_fraction(question: str):
    """Returns 1.0, 0.0, or None (ambiguous, ANOTHER dollar-figure question,
    or genuinely undetermined). Checked in this order because a question
    could plausibly mention both phrasings in a confused way; requiring an
    exact, singular match keeps this conservative -- if both or neither
    fire, defer to the caller, which also tries the Phase 2 stated-amount
    path before giving up entirely."""
    q = question.lower()
    all_ca = any(t in q for t in CA_SOURCE_ALL_TERMS)
    no_ca = any(t in q for t in CA_SOURCE_NONE_TERMS)
    if all_ca and not no_ca:
        return 1.0
    if no_ca and not all_ca:
        return 0.0
    return None


def detect_nonresident_missing_source(question: str) -> bool:
    """True iff nonresident signal is present but NEITHER the ALL/NONE
    phrasing NOR ANY CA-source-amount anchor phrase appears at all --
    i.e. the question gives no hint whatsoever of how much (if any) income
    is California-source. Distinguishes 'genuinely missing this fact' (use
    this generic message) from 'a CA-source phrase IS present but the
    surrounding numbers are ambiguous' (the caller's own numeric
    extraction already handles that case and returns its own None,
    correctly falling through to plain needs_review instead of this
    specific clarifying message)."""
    if not detect_nonresident_signal(question):
        return False
    if detect_ca_source_fraction(question) is not None:
        return False
    q = question.lower()
    return not any(t in q for t in CA_SOURCE_AMOUNT_TERMS)


def detect_part_year_missing_source(question: str) -> bool:
    """True iff part-year-resident signal is present but no CA-source-
    amount anchor phrase appears at all. Unlike the full-year nonresident
    case, there's no ALL/NONE phrase shortcut to check first -- part-year
    residents don't have a clean ALL/NONE case (see module docstring), so
    a stated dollar figure is always required, and its absence is always
    what's missing."""
    if not detect_part_year_signal(question):
        return False
    q = question.lower()
    return not any(t in q for t in CA_SOURCE_AMOUNT_TERMS)


def compute_nonresident_wage_tax(conn, total_wages: float, ca_source_amount: float,
                                   filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Phase 2: ca_source_amount is a DOLLAR FIGURE (the portion of
    total_wages earned working physically in California), not a fraction
    -- any value from 0 to total_wages inclusive is now supported (Phase 1
    only accepted the two endpoints as a deliberate scope gate; the
    formula itself always generalized to any split, see module
    docstring). Returns None if ca_source_amount is missing, negative, or
    exceeds total_wages (an invalid/nonsensical split).

    Phase 3 reuses this function COMPLETELY UNCHANGED for part-year
    residents -- confirmed via FTB Pub 1100 that the formula is identical,
    only WHAT ca_source_amount means differs (resident-period income in
    full, plus only the CA-source share of nonresident-period income; see
    module docstring). The caller is responsible for constructing the
    right ca_source_amount for whichever population is asking."""
    if total_wages is None or total_wages < 0:
        return None
    if ca_source_amount is None or ca_source_amount < 0 or ca_source_amount > total_wages:
        return None
    dedu = standard_deduction(conn, filing_status, tax_year)
    if not dedu:
        return None
    total_taxable_income = max(0.0, total_wages - dedu["amount"])

    if ca_source_amount == 0.0:
        return {
            "ca_tax": 0.0, "total_taxable_income": total_taxable_income,
            "tax_on_total": 0.0, "effective_rate": 0.0, "ca_taxable_income": 0.0,
            "standard_deduction": dedu["amount"], "ca_source_amount": 0.0,
            "ca_source_fraction": 0.0, "marginal_rate": None, "surtax": 0.0,
            "citation": CITATION, "source_url": SOURCE_URL,
        }

    calc = compute_ca_tax(conn, total_taxable_income, filing_status, tax_year)
    if not calc:
        return None
    tax_on_total = calc["total_tax"]
    effective_rate = (tax_on_total / total_taxable_income) if total_taxable_income > 0 else 0.0
    ca_source_fraction = min(1.0, ca_source_amount / total_wages) if total_wages > 0 else 0.0
    ca_agi = ca_source_amount
    prorated_deduction = dedu["amount"] * ca_source_fraction
    ca_taxable_income = max(0.0, ca_agi - prorated_deduction)
    ca_tax = round(ca_taxable_income * effective_rate, 2)
    return {
        "ca_tax": ca_tax, "total_taxable_income": total_taxable_income,
        "tax_on_total": tax_on_total, "effective_rate": effective_rate,
        "ca_taxable_income": ca_taxable_income, "standard_deduction": dedu["amount"],
        "prorated_deduction": prorated_deduction, "ca_source_amount": ca_source_amount,
        "ca_source_fraction": ca_source_fraction, "marginal_rate": calc["marginal_rate"],
        "surtax": calc["surtax"], "citation": CITATION, "source_url": SOURCE_URL,
    }
