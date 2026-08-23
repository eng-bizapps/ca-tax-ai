"""Ring 2 / Phase 3 extension -- CA income tax CREDITS, a genuinely
different fact shape from bracket/deduction math (income_brackets.py):
CalEITC is a table lookup by (income, number of qualifying children), YCTC
is a flat amount with a linear phase-out. Mirrors this project's "separate
module per concept" precedent (local_rates.py, fees.py, income_brackets.py)
-- engine.py imports and calls this, it does not reimplement the logic
inline. See load_income_content.py's module docstring for how the
underlying data was sourced/verified.

Deliberately scoped to the SIMPLEST identifiable case, same "build the
default, defer the rest" discipline as income_brackets.py: assumes wage-only
earned income with federal AGI equal to CA earned income (skips FTB Form
3514 Part II's AGI-vs-earned-income reconciliation, which only matters when
they differ -- e.g. investment income, which is excluded below). Foster
Youth Tax Credit (FYTC) IS implemented (see compute_fytc/detect_fytc_signal
below) -- eligibility (foster-youth status at/after age 13) is handled as
an explicit checklist question rather than inferred, the same "specific
clarifying question, not a guess" pattern used for its own age/foster-care
disqualification sub-cases.
"""
import re

from income_brackets import DEFAULT_TAX_YEAR, detect_filing_status

# Terms that disqualify the simple CalEITC/YCTC computation -- self-employment
# income has its own worksheet (FTB 3514 line 18/Worksheet 3) not implemented
# here, and investment income above a threshold disqualifies EITC entirely
# (Form 3514 Part II) -- same "defer rather than guess" discipline as
# income_brackets.COMPLEXITY_EXCLUDE.
CREDIT_COMPLEXITY_EXCLUDE = {
    "self-employ", "self employ", "1099", "business", "s-corp", "s corp",
    "llc", "partnership", "sole proprietor", "schedule c", "k-1",
    "freelance", "freelancer", "freelancing", "contractor", "contracting",
    "contracted", "gig work", "gig economy",
    "investment income", "capital gain", "dividend", "rental", "renting", "rented",
}

CALEITC_TRIGGERS = {"caleitc", "cal eitc", "earned income tax credit",
                     "earned income credit", "eitc"}
YCTC_TRIGGERS = {"young child tax credit", "yctc", "young child credit"}

# number-word -> column index (3 = "3 or more", matching the table's own
# 4th column convention)
_CHILD_NUMBER_WORDS = {"zero": 0, "no": 0, "one": 1, "two": 2, "three": 3,
                        "four": 3, "five": 3, "six": 3}


def detect_children_count(question: str):
    """Returns 0/1/2/3 (3 = "3 or more", matching the EITC table's own
    convention) or None if the question doesn't state a number of
    qualifying children at all."""
    q = question.lower()
    m = re.search(r"(\d+)\s*(?:or more\s*)?(?:qualifying\s*)?children", q)
    if not m:
        m = re.search(r"(\d+)\s*(?:qualifying\s*)?child\b", q)
    if m:
        return min(int(m.group(1)), 3)
    for word, n in _CHILD_NUMBER_WORDS.items():
        if re.search(rf"\b{word}\s+(?:qualifying\s+)?child", q):
            return n
    return None


def detect_caleitc_signal(question: str):
    """Returns a children-count (0-3) iff this looks like a CalEITC
    computation request (trigger phrase, no complexity disqualifiers, a
    stated number of qualifying children); None otherwise."""
    q = question.lower()
    if any(term in q for term in CREDIT_COMPLEXITY_EXCLUDE):
        return None
    if not any(t in q for t in CALEITC_TRIGGERS):
        return None
    return detect_children_count(question)


def detect_caleitc_missing_children(question: str) -> bool:
    """True iff this is clearly a CalEITC request but doesn't state a
    number of qualifying children -- lets the caller ask for the missing
    piece instead of a generic defer, same pattern as
    income_brackets.detect_compute_missing_filing_status."""
    q = question.lower()
    if any(term in q for term in CREDIT_COMPLEXITY_EXCLUDE):
        return False
    if not any(t in q for t in CALEITC_TRIGGERS):
        return False
    return detect_children_count(question) is None


def lookup_eitc_table(conn, income: float, children: int, tax_year: int = DEFAULT_TAX_YEAR):
    """children must already be normalized to 0-3 (see detect_children_count
    -- never derived from raw user input directly, always clamped through
    that function first) since it selects which credit_N column to read."""
    col = f"credit_{min(max(children, 0), 3)}"
    row = conn.execute(
        f"SELECT {col}, citation, source_url FROM ca_eitc_table "
        "WHERE tax_year=%s AND income_floor<=%s AND income_ceiling>=%s LIMIT 1",
        (tax_year, income, income)).fetchone()
    if not row:
        return None
    return {"credit": float(row[0]), "citation": row[1], "source_url": row[2]}


# --- CalEITC investment-income disqualification (FTB 3514 Step 2) -------
# Verified against the actual 2025 FTB 3514 Booklet
# (https://www.ftb.ca.gov/forms/2025/2025-3514-booklet.html), Step 2 /
# Worksheet 1, Line 13: "Is the amount on line 12 more than $4,814? Yes --
# Stop here, you cannot take the credit." IMPORTANT: this is NOT the same
# number as the federal EITC's investment-income limit (which is roughly
# $11,950 for 2025) -- CalEITC has its OWN, much lower threshold. A prior
# placeholder note in project memory guessed ~$11,950 by analogy to the
# federal figure; checking the actual current CA form (not assuming the
# federal number transfers) caught that the real CA figure is $4,814,
# the same "verify the primary source, don't extrapolate" discipline that
# caught the alimony date-window issue earlier this session.
# Scope: only the LITERAL "investment income" phrasing is handled here
# (mirrors CREDIT_COMPLEXITY_EXCLUDE's existing term) -- "capital gain"/
# "dividend" mentioned bare still defer via the unchanged exclude list,
# since disambiguating which stated dollar figure is which investment-
# income component would add real ambiguity for a first pass.
CALEITC_INVESTMENT_INCOME_LIMIT = 4814.0
CALEITC_INVESTMENT_INCOME_CITATION = "2025 FTB 3514 Booklet -- Step 2, Worksheet 1, Line 13"
CALEITC_INVESTMENT_INCOME_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3514-booklet.html"
INVESTMENT_INCOME_TERMS = {"investment income"}


def _caleitc_investment_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in INVESTMENT_INCOME_TERMS):
        return False
    other_exclude = CREDIT_COMPLEXITY_EXCLUDE - INVESTMENT_INCOME_TERMS
    if any(t in q for t in other_exclude):
        return False
    if not any(t in q for t in CALEITC_TRIGGERS):
        return False
    return True


def detect_caleitc_investment_signal(question: str):
    """Returns a children-count (0-3) iff this looks like a genuine
    'CalEITC with stated earned income AND investment income' question --
    same narrower-exclude-set pattern as detect_itemized_signal
    (investment-income phrasing is the TRIGGER here, not a disqualifier)."""
    q = question.lower()
    if not _caleitc_investment_base_signal_ok(q):
        return None
    return detect_children_count(question)


def detect_caleitc_investment_missing_children(question: str) -> bool:
    q = question.lower()
    if not _caleitc_investment_base_signal_ok(q):
        return False
    return detect_children_count(question) is None


def compute_caleitc_with_investment_income(conn, earned_income: float, investment_income: float,
                                             children: int, tax_year: int = DEFAULT_TAX_YEAR):
    """Investment income above the limit disqualifies CalEITC ENTIRELY,
    regardless of earned income or child count (FTB 3514 Step 2) -- the
    earned-income table lookup is never even reached in that case. Below
    the limit, the investment income amount itself plays no further role
    (CalEITC's credit AMOUNT is a function of earned income and children
    only, not investment income) -- it only gates eligibility."""
    if earned_income is None or investment_income is None or earned_income < 0 or investment_income < 0:
        return None
    if investment_income > CALEITC_INVESTMENT_INCOME_LIMIT:
        return {"disqualified": True, "investment_income": investment_income,
                "citation": CALEITC_INVESTMENT_INCOME_CITATION,
                "source_url": CALEITC_INVESTMENT_INCOME_SOURCE_URL}
    hit = lookup_eitc_table(conn, earned_income, children, tax_year)
    if not hit:
        return None
    return {**hit, "disqualified": False, "investment_income": investment_income}


def detect_ycta_signal(question: str) -> bool:
    q = question.lower()
    if any(term in q for term in CREDIT_COMPLEXITY_EXCLUDE):
        return False
    return any(t in q for t in YCTC_TRIGGERS)


def compute_ycta(conn, earned_income: float, tax_year: int = DEFAULT_TAX_YEAR):
    """Replicates FTB Form 3514 Part VII's EXACT two-step-rounding formula
    (divide excess by 100 and round to 2 decimals, THEN multiply by the
    phase-out rate and round to 2 decimals again) rather than a one-step
    excess*rate shortcut, so results match the official form to the penny.
    Returns None if the reference row isn't loaded or income is outside the
    eligible range [0, phase_out_end]."""
    row = conn.execute(
        "SELECT max_amount, phase_out_start, phase_out_end, phase_out_rate, "
        "citation, source_url FROM ca_income_credits "
        "WHERE credit_key='young_child_tax_credit' AND tax_year=%s", (tax_year,)).fetchone()
    if not row:
        return None
    max_amount, phase_out_start, phase_out_end, rate, citation, source_url = row
    max_amount, phase_out_start, phase_out_end, rate = (
        float(max_amount), float(phase_out_start), float(phase_out_end), float(rate))
    if earned_income < 0 or earned_income > phase_out_end:
        return None
    if earned_income <= phase_out_start:
        credit = max_amount
    else:
        excess = earned_income - phase_out_start
        step1 = round(excess / 100, 2)
        reduction = round(step1 * rate, 2)
        raw = max_amount - reduction
        credit = 1 if 0 < raw <= 1 else (round(raw) if raw > 1 else 0)
    return {"credit": float(credit), "citation": citation, "source_url": source_url}


# --- Foster Youth Tax Credit (FTB 2025 Form 3514, Step 10 / Part IX) ----
# Verified against the actual booklet: SAME phase-out formula/numbers as
# YCTC ($1,189 max, $27,425 threshold, $21.71 per $100 over) -- confirmed
# by reading Part IX's own Line 34/36-39 arithmetic, not assumed from the
# two credits sharing a dollar figure. FYTC has its OWN, additional
# eligibility gate on top: (1) allowed the CalEITC, (2) age 18-25
# inclusive at year end, (3) in foster care at age 13 or older, placed
# through the CA foster care system. Unlike every other credit in this
# module, FYTC genuinely depends on qualifying for ANOTHER credit first --
# rather than trust a bare "I qualify for CalEITC" claim, this REUSES
# lookup_eitc_table with the taxpayer's stated earned income and children
# count, the same "trust the input, verify what's checkable" discipline
# as every other compute path in this project.
FYTC_TRIGGERS = {"foster youth tax credit", "fytc"}
FYTC_AGE_MIN, FYTC_AGE_MAX = 18, 25
FYTC_FOSTER_CARE_TERMS = {"foster care", "foster youth", "former foster youth"}
_FYTC_AGE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:years?\s*old|yo|y/o|yrs?\.?\s*old)\b|"
    r"\bi(?:'m| am)\s+(\d{1,2})\b|\baged?\s+(\d{1,2})\b")


def _fytc_age(q: str):
    m = _FYTC_AGE_RE.search(q)
    if not m:
        return None
    return int(next(g for g in m.groups() if g is not None))


def fytc_stated_age(question: str):
    """Public wrapper around _fytc_age -- the caller (engine.py) needs the
    raw matched age number, not just pass/fail, to EXCLUDE it when
    isolating the income figure from a question containing multiple
    numbers (age, foster-care age, income). Found via testing: a bare
    proximity/nearest-keyword approach picked the wrong number when all
    three appear close together in one sentence."""
    return _fytc_age(question.lower())


def detect_fytc_age_ok(question: str):
    """Returns True/False/None -- None means age not stated at all."""
    age = _fytc_age(question.lower())
    if age is None:
        return None
    return FYTC_AGE_MIN <= age <= FYTC_AGE_MAX


def _fytc_foster_care_age_number(q: str):
    """Returns the specific age number (13-24) that satisfied the
    foster-care-at-13+ check, or None if the check passed via a
    non-numeric phrase ("13 or older") or didn't pass at all. Used by the
    caller to EXCLUDE this number when isolating the income figure from a
    question with multiple clustered numbers.

    CLAUSE-based (splits on commas/"and"), not proximity/window-based --
    found via testing (THREE bugs in sequence on this one line of logic,
    the sharpest lesson this session on how fragile raw-distance heuristics
    get with 3+ clustered numbers): "what is my FOSTER YOUTH TAX CREDIT if
    I am 20 years old, was in foster care at age 15, and made $9,975..."
    first wrongly matched the credit-name "foster youth" occurrence
    entirely; fixing that still left a window wide enough to reach the
    unrelated "20"; and even NEAREST-BY-DISTANCE landed on an exact tie
    (both "20" and "15" measured EXACTLY 20.0 characters from "foster
    care", with "20" winning only because it was found first in scan
    order). No distance-based tweak resolves an exact tie reliably.
    Splitting into clauses sidesteps the whole distance question: "was in
    foster care at age 15" is its own clause containing both the keyword
    and the age with NOTHING else competing, so this only looks for a
    13-24 number WITHIN THE SAME CLAUSE as a genuine (non-credit-name)
    foster-care mention."""
    for clause in re.split(r"[,;]|\band\b", q):
        for term in FYTC_FOSTER_CARE_TERMS:
            idx = clause.find(term)
            if idx == -1:
                continue
            after = clause[idx + len(term):idx + len(term) + 15].strip()
            if after.startswith("tax credit"):
                continue
            m = re.search(r"\b(1[3-9]|2[0-4])\b", clause)
            if m:
                return int(m.group(1))
    return None


def detect_fytc_foster_care_at_13(question: str) -> bool:
    """Requires an EXPLICIT statement that foster care was AT OR AFTER age
    13 -- FTB's specific eligibility fact. A bare "I was a foster youth"
    alone doesn't establish the age-13 cutoff, so it's deliberately NOT
    accepted on its own (would silently assume a fact never actually
    stated)."""
    q = question.lower()
    if not any(t in q for t in FYTC_FOSTER_CARE_TERMS):
        return False
    if re.search(r"\b(?:13|thirteen)\b.{0,20}(?:or older|or up|and older)", q):
        return True
    if re.search(r"(?:since|starting at|at)\s*(?:age\s*)?(?:13|thirteen)\b", q):
        return True
    # a stated foster-care ENTRY age 13-24 near the foster-care phrase
    # (proximity-based -- same distance-scoping principle as
    # income_eligibility's residency check, to avoid picking up an
    # unrelated number elsewhere in the question).
    return _fytc_foster_care_age_number(q) is not None


def fytc_foster_care_age_number(question: str):
    """Public wrapper -- same reasoning as fytc_stated_age."""
    return _fytc_foster_care_age_number(question.lower())


def _fytc_base_signal_ok(q: str) -> bool:
    if not any(t in q for t in FYTC_TRIGGERS):
        return False
    if any(term in q for term in CREDIT_COMPLEXITY_EXCLUDE):
        return False
    return True


def detect_fytc_signal(question: str):
    """Returns a children-count (0-3) iff ALL FYTC-specific facts are
    present (foster-care-at-13+, age 18-25) -- children count is still
    needed even though FYTC's own amount doesn't depend on it, because
    verifying the CalEITC-eligibility gate requires it. Returns None if
    ANY fact is missing, mirroring detect_caleitc_signal's shape."""
    q = question.lower()
    if not _fytc_base_signal_ok(q):
        return None
    if not detect_fytc_foster_care_at_13(question):
        return None
    if detect_fytc_age_ok(question) is not True:
        return None
    return detect_children_count(question)


def detect_fytc_age_disqualified(question: str) -> bool:
    """True iff FYTC is triggered, foster-care-at-13+ is confirmed, and
    age is EXPLICITLY stated but outside 18-25 -- a clean, independent
    "no" once the other hard-gate fact already checks out, same pattern
    as income_eligibility's explicit-failing-age-test case for HOH
    (distinguishing a stated-but-disqualifying fact from a merely missing
    one)."""
    q = question.lower()
    if not _fytc_base_signal_ok(q):
        return False
    if not detect_fytc_foster_care_at_13(question):
        return False
    return detect_fytc_age_ok(question) is False


def detect_fytc_checklist_incomplete(question: str) -> bool:
    """True iff this is clearly an FYTC question (trigger present, no
    other complexity disqualifier) but detect_fytc_signal couldn't reach
    a verdict -- lets the caller give the full checklist instead of a
    generic defer, same pattern as income_eligibility's HOH checklist."""
    q = question.lower()
    if not _fytc_base_signal_ok(q):
        return False
    return detect_fytc_signal(question) is None


def compute_fytc(conn, earned_income: float, children: int, tax_year: int = DEFAULT_TAX_YEAR):
    """Two independent gates before any dollar amount: (1) CalEITC-
    eligible, verified via the REAL table lookup (not a bare claim) -- if
    the stated earned income + children combination doesn't produce a
    CalEITC credit, FYTC is not allowed either, per FTB Step 10's first
    requirement; (2) the FYTC-specific phase-out arithmetic (identical
    formula to compute_ycta, own DB row) applied only once gate (1)
    passes. Returns a dict with eligible_for_caleitc=False (and no
    credit key) if gate (1) fails, so the caller can disclose exactly
    why -- not just a bare $0."""
    eitc_hit = lookup_eitc_table(conn, earned_income, children, tax_year)
    if not eitc_hit or eitc_hit["credit"] <= 0:
        return {"eligible_for_caleitc": False}
    row = conn.execute(
        "SELECT max_amount, phase_out_start, phase_out_end, phase_out_rate, "
        "citation, source_url FROM ca_income_credits "
        "WHERE credit_key='foster_youth_tax_credit' AND tax_year=%s", (tax_year,)).fetchone()
    if not row:
        return None
    max_amount, phase_out_start, phase_out_end, rate, citation, source_url = row
    max_amount, phase_out_start, phase_out_end, rate = (
        float(max_amount), float(phase_out_start), float(phase_out_end), float(rate))
    if earned_income < 0 or earned_income > phase_out_end:
        return {"eligible_for_caleitc": True, "credit": 0.0,
                "citation": citation, "source_url": source_url}
    if earned_income <= phase_out_start:
        credit = max_amount
    else:
        excess = earned_income - phase_out_start
        step1 = round(excess / 100, 2)
        reduction = round(step1 * rate, 2)
        raw = max_amount - reduction
        credit = 1 if 0 < raw <= 1 else (round(raw) if raw > 1 else 0)
    return {"eligible_for_caleitc": True, "credit": float(credit),
            "citation": citation, "source_url": source_url}


RENTERS_CREDIT_TRIGGERS = {"renter's credit", "renters credit", "renter credit"}


def detect_renters_credit_signal(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in RENTERS_CREDIT_TRIGGERS)


def detect_renters_credit_missing_filing_status(question: str) -> bool:
    """Same pattern as income_brackets.detect_compute_missing_filing_status
    -- the renter's credit amount is itself filing-status-tiered ($60 vs
    $120), so it can't be computed without knowing which tier applies."""
    return detect_renters_credit_signal(question) and detect_filing_status(question) is None


def compute_renters_credit(conn, income: float, filing_status: str, tax_year: int = DEFAULT_TAX_YEAR):
    """Flat amount below a hard income CEILING (phase_out_end used as that
    ceiling, not a gradual curve -- see load_income_content.py), $0 above
    it. Returns None if the reference row isn't loaded or income exceeds
    the ceiling for this filing status."""
    row = conn.execute(
        "SELECT max_amount, phase_out_end, citation, source_url FROM ca_income_credits "
        "WHERE credit_key='renters_credit' AND tax_year=%s AND filing_status=%s",
        (tax_year, filing_status)).fetchone()
    if not row:
        return None
    max_amount, ceiling, citation, source_url = row
    max_amount, ceiling = float(max_amount), float(ceiling)
    if income < 0 or income > ceiling:
        return None
    return {"credit": max_amount, "citation": citation, "source_url": source_url}


# --- Senior Head of Household Credit (Credit Code 163) ------------------
# Verified against ftb.ca.gov's dedicated Senior HOH Credit page AND the
# 2025 Form 540 instructions' own line-by-line credit worksheet (the
# latter caught a real distinction the credit page's plain-language
# summary blurred): ALL of (1) age 65+ on Dec 31, (2) qualified as HOH
# for at least 1 of the past 2 years, (3) the qualifying person died
# within the past 2 years, (4) AGI not over $98,652 for the year -- the
# 540 instructions state this ceiling as "AGI", not the credit page's
# looser "income" wording. The CALCULATION, separately, uses Form 540
# LINE 19 (CA TAXABLE income, i.e. AGI minus the standard/itemized
# deduction) -- min(2% of taxable income, $1,860). These are two
# DIFFERENT dollar figures (AGI > taxable income whenever a deduction
# applies), and the $98,652 ceiling is a SEPARATE, independent
# eligibility gate, not just where the formula naturally caps: 2% of
# $98,652 is $1,973.04 (above the $1,860 cap), so incomes from $93,000
# (where 2% alone would already hit $1,860) up to $98,652 are still
# capped at exactly $1,860, not disqualified -- only AT/ABOVE $98,652
# AGI does eligibility itself fail.
# "TRUST THE INPUT" on the income figure, same pattern as itemized
# deductions/self-employment net profit: deriving taxable income from
# AGI would require assuming a CURRENT filing status for someone whose
# qualifying person just died (genuinely ambiguous), so this accepts ONE
# stated AGI figure, uses it directly for the eligibility ceiling (where
# it's the CORRECT figure per the 540 instructions), and also uses it
# for the 2% calculation as a disclosed simplification in place of the
# technically-distinct taxable-income figure -- this can only OVERSTATE
# the credit slightly (AGI >= taxable income always), and makes no
# difference at all once the result is at/near the $1,860 cap, which
# covers most taxpayers close enough to the ceiling for the distinction
# to matter in the first place.
SENIOR_HOH_TRIGGERS = {"senior head of household credit", "senior hoh credit"}
SENIOR_HOH_AGE_MIN = 65
SENIOR_HOH_INCOME_CEILING = 98652.0
SENIOR_HOH_MAX_CREDIT = 1860.0
SENIOR_HOH_RATE = 0.02
SENIOR_HOH_CITATION = "FTB Senior Head of Household Credit (Credit Code 163)"
SENIOR_HOH_SOURCE_URL = "https://www.ftb.ca.gov/file/personal/credits/senior-hoh-credit.html"

SENIOR_HOH_QUALIFIED_HOH_TERMS = {
    "qualified for head of household", "qualified as head of household",
    "was head of household", "filed as head of household",
    "was hoh", "qualified for hoh", "qualified as hoh",
}
SENIOR_HOH_PERSON_DIED_TERMS = {"died", "passed away", "passed on"}
SENIOR_HOH_RECENCY_TERMS = {
    "past 2 years", "past two years", "last 2 years", "last two years",
    "within the last 2 years", "within the last two years",
    "this year", "last year", "1 year ago", "one year ago", "2 years ago", "two years ago",
}
_SENIOR_HOH_AGE_RE = re.compile(
    r"\b(\d{1,3})\s*(?:years?\s*old|yo|y/o|yrs?\.?\s*old)\b|"
    r"\bi(?:'m| am)\s+(\d{1,3})\b|\baged?\s+(\d{1,3})\b")


def _senior_hoh_age(q: str):
    m = _SENIOR_HOH_AGE_RE.search(q)
    if not m:
        return None
    return int(next(g for g in m.groups() if g is not None))


def detect_senior_hoh_age_ok(question: str):
    """Returns True/False/None -- None means age not stated at all."""
    age = _senior_hoh_age(question.lower())
    if age is None:
        return None
    return age >= SENIOR_HOH_AGE_MIN


def senior_hoh_stated_age(question: str):
    """Public wrapper -- lets the caller EXCLUDE this number when
    isolating the income figure, same reasoning as fytc_stated_age."""
    return _senior_hoh_age(question.lower())


def detect_senior_hoh_person_died_recently(question: str) -> bool:
    """Requires a death term AND a recency qualifier -- FTB's specific
    'within the past 2 years' requirement. A bare 'died' with no time
    context doesn't establish this, so it's deliberately not accepted
    alone (would silently assume a fact never actually stated)."""
    q = question.lower()
    if not any(t in q for t in SENIOR_HOH_PERSON_DIED_TERMS):
        return False
    return any(t in q for t in SENIOR_HOH_RECENCY_TERMS)


def _senior_hoh_base_signal_ok(q: str) -> bool:
    return any(t in q for t in SENIOR_HOH_TRIGGERS)


def detect_senior_hoh_signal(question: str) -> bool:
    """True iff ALL Senior HOH facts are present: the trigger, previously
    qualified for HOH, qualifying person died recently, and age 65+.
    Income is checked separately (needs its own extraction + the ceiling
    gate), mirroring detect_fytc_signal's separation of concerns."""
    q = question.lower()
    if not _senior_hoh_base_signal_ok(q):
        return False
    if not any(t in q for t in SENIOR_HOH_QUALIFIED_HOH_TERMS):
        return False
    if not detect_senior_hoh_person_died_recently(question):
        return False
    return detect_senior_hoh_age_ok(question) is True


def detect_senior_hoh_age_disqualified(question: str) -> bool:
    """Clean 'no' when the other facts check out but stated age is
    explicitly under 65 -- same distinction as every other explicit-
    failing-hard-gate case this session (HOH's married-all-year, FYTC's
    age-out-of-range)."""
    q = question.lower()
    if not _senior_hoh_base_signal_ok(q):
        return False
    if not any(t in q for t in SENIOR_HOH_QUALIFIED_HOH_TERMS):
        return False
    if not detect_senior_hoh_person_died_recently(question):
        return False
    return detect_senior_hoh_age_ok(question) is False


def detect_senior_hoh_checklist_incomplete(question: str) -> bool:
    q = question.lower()
    if not _senior_hoh_base_signal_ok(q):
        return False
    return not (detect_senior_hoh_signal(question) or detect_senior_hoh_age_disqualified(question))


def compute_senior_hoh_credit(agi: float):
    """min(2% of AGI, $1,860), but ONLY below the $98,652 AGI ceiling --
    a separate eligibility gate, not just where the formula's own cap
    kicks in (see module note above -- AGI is used for BOTH the
    eligibility check, where it's technically correct, and the 2%
    calculation, where it's a disclosed simplification for taxable
    income). Returns None for negative income (shouldn't happen with
    'trust the input', defensive only)."""
    if agi is None or agi < 0:
        return None
    if agi >= SENIOR_HOH_INCOME_CEILING:
        return {"credit": 0.0, "citation": SENIOR_HOH_CITATION,
                "source_url": SENIOR_HOH_SOURCE_URL, "eligible_income": False}
    credit = min(round(agi * SENIOR_HOH_RATE, 2), SENIOR_HOH_MAX_CREDIT)
    return {"credit": credit, "citation": SENIOR_HOH_CITATION,
            "source_url": SENIOR_HOH_SOURCE_URL, "eligible_income": True}


# --- Joint Custody Head of Household Credit (Code 170) / Credit for
# Dependent Parent (Code 173) -- SHARE THE SAME AMOUNT WORKSHEET ---------
# Verified against ftb.ca.gov's dedicated Joint Custody HOH page AND the
# 2025 Form 540 instructions (which spell out the shared worksheet AND
# the Dependent Parent Credit's own eligibility list right after it,
# confirming they use the IDENTICAL formula -- 30% of Form 540 LINE 35,
# capped at $610 -- "If you qualify for [both], claim only one").
# Line 35 is NOT an income figure at all (unlike every other credit in
# this module) -- it's the taxpayer's COMPUTED CA TAX LIABILITY itself
# (tax from the rate schedule, line 31, minus exemption credits, line
# 32 -- i.e. tax owed BEFORE this and other "special credits" are
# applied). This project's bracket engine (income_brackets.compute_ca_tax)
# doesn't model CA's exemption-credit mechanic at all, so deriving line
# 35 from scratch would be its own new, unverified computation -- same
# "trust the input" call as AGI for Senior HOH: this accepts the
# taxpayer's STATED tax-liability figure directly rather than deriving
# it, and says so plainly in the answer text.
JOINT_CUSTODY_HOH_TRIGGERS = {"joint custody head of household credit", "joint custody hoh credit"}
JOINT_CUSTODY_MAX_CREDIT = 610.0
JOINT_CUSTODY_RATE = 0.30
JOINT_CUSTODY_CITATION = "FTB Joint Custody Head of Household Credit (Credit Code 170)"
JOINT_CUSTODY_SOURCE_URL = "https://www.ftb.ca.gov/file/personal/credits/joint-custody-hoh-credit.html"

JOINT_CUSTODY_TERMS = {"joint custody", "custody agreement", "custody of"}
_JOINT_CUSTODY_CHILD_RE = re.compile(
    r"\bmy\b[\w\s\-']{0,30}?\b(child|children|son|daughter|"
    r"stepchild|stepson|stepdaughter|grandchild|grandson|granddaughter)\b")
JOINT_CUSTODY_HALF_EXPENSES_TERMS = {
    "more than half the household expenses", "more than half their expenses",
    "more than half his expenses", "more than half her expenses",
    "over half the household expenses", "over half their expenses",
    "half the expenses",
}
JOINT_CUSTODY_NOT_MARRIED_TERMS = {"not married", "unmarried", "wasn't married", "was not married"}
JOINT_CUSTODY_MARRIED_APART_TERMS = {
    "married but lived apart all year", "married but lived apart the whole year",
    "married but lived apart the entire year", "lived apart from my spouse all year",
    "lived apart from my spouse the whole year", "lived apart from my spouse the entire year",
}


def detect_joint_custody_marital_ok(question: str) -> bool:
    q = question.lower()
    return (any(t in q for t in JOINT_CUSTODY_NOT_MARRIED_TERMS)
            or any(t in q for t in JOINT_CUSTODY_MARRIED_APART_TERMS))


def _joint_custody_residency_days(q: str):
    """Returns the stated day count if EXPLICITLY given as a number of
    days -- FTB's 146-219 day range is precise enough that approximating
    from a stated month count risks a wrong boundary call, so a bare
    month figure is deliberately NOT accepted here (defers instead)."""
    m = re.search(r"\b(\d{2,3})\s*days?\b", q)
    return int(m.group(1)) if m else None


def joint_custody_stated_days(question: str):
    """Public wrapper -- lets the caller exclude this number when
    isolating the tax-liability dollar figure."""
    return _joint_custody_residency_days(question.lower())


def detect_joint_custody_residency_ok(question: str):
    """True/False/None -- None if no explicit day count stated."""
    days = _joint_custody_residency_days(question.lower())
    if days is None:
        return None
    return 146 <= days <= 219


def _joint_custody_base_signal_ok(q: str) -> bool:
    return any(t in q for t in JOINT_CUSTODY_HOH_TRIGGERS)


def detect_joint_custody_signal(question: str) -> bool:
    q = question.lower()
    if not _joint_custody_base_signal_ok(q):
        return False
    if not any(t in q for t in JOINT_CUSTODY_TERMS):
        return False
    if not _JOINT_CUSTODY_CHILD_RE.search(q):
        return False
    if not any(t in q for t in JOINT_CUSTODY_HALF_EXPENSES_TERMS):
        return False
    if not detect_joint_custody_marital_ok(question):
        return False
    return detect_joint_custody_residency_ok(question) is True


def detect_joint_custody_residency_disqualified(question: str) -> bool:
    """Clean 'no' when every other fact checks out but the stated day
    count is explicitly outside 146-219 -- not a generic defer."""
    q = question.lower()
    if not _joint_custody_base_signal_ok(q):
        return False
    if not any(t in q for t in JOINT_CUSTODY_TERMS):
        return False
    if not _JOINT_CUSTODY_CHILD_RE.search(q):
        return False
    if not any(t in q for t in JOINT_CUSTODY_HALF_EXPENSES_TERMS):
        return False
    if not detect_joint_custody_marital_ok(question):
        return False
    return detect_joint_custody_residency_ok(question) is False


def detect_joint_custody_checklist_incomplete(question: str) -> bool:
    q = question.lower()
    if not _joint_custody_base_signal_ok(q):
        return False
    return not (detect_joint_custody_signal(question)
                or detect_joint_custody_residency_disqualified(question))


def compute_shared_hoh_parent_credit(tax_liability: float, citation: str, source_url: str):
    """The IDENTICAL 30%-of-tax-liability-capped-at-$610 formula, shared
    by Joint Custody HOH and Dependent Parent -- one function, citation
    passed in so each caller's result carries its own correct credit
    name/code."""
    if tax_liability is None or tax_liability < 0:
        return None
    credit = min(round(tax_liability * JOINT_CUSTODY_RATE, 2), JOINT_CUSTODY_MAX_CREDIT)
    return {"credit": credit, "citation": citation, "source_url": source_url}


DEPENDENT_PARENT_TRIGGERS = {"dependent parent credit"}
DEPENDENT_PARENT_CITATION = "FTB Credit for Dependent Parent (Code 173)"
DEPENDENT_PARENT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-instructions.html"

DEPENDENT_PARENT_MFS_TERMS = {
    "married filing separately", "married/rdp filing separately",
    "filing separately", "mfs",
}
DEPENDENT_PARENT_SPOUSE_APART_TERMS = {
    "spouse was not a member of my household", "spouse wasn't a member of my household",
    "spouse did not live with me", "spouse didn't live with me",
    "spouse was not part of my household",
    "lived apart from my spouse for the last six months",
    "lived apart from my spouse for the last 6 months",
    "lived apart from my spouse the last six months",
    "lived apart from my spouse the last 6 months",
}
DEPENDENT_PARENT_HALF_EXPENSES_TERMS = {
    "more than half the household expenses", "over half the household expenses",
    "more than half of my parent's home expenses", "furnished over half",
    "paid more than half", "more than half the expenses",
}
_DEPENDENT_PARENT_RE = re.compile(
    r"\bmy\b[\w\s\-']{0,30}?\b(mother|father|mom|dad|parent)\b")


def _dependent_parent_base_signal_ok(q: str) -> bool:
    return any(t in q for t in DEPENDENT_PARENT_TRIGGERS)


def detect_dependent_parent_signal(question: str) -> bool:
    q = question.lower()
    if not _dependent_parent_base_signal_ok(q):
        return False
    if not any(t in q for t in DEPENDENT_PARENT_MFS_TERMS):
        return False
    if not any(t in q for t in DEPENDENT_PARENT_SPOUSE_APART_TERMS):
        return False
    if not any(t in q for t in DEPENDENT_PARENT_HALF_EXPENSES_TERMS):
        return False
    return bool(_DEPENDENT_PARENT_RE.search(q))


def detect_dependent_parent_checklist_incomplete(question: str) -> bool:
    q = question.lower()
    if not _dependent_parent_base_signal_ok(q):
        return False
    return not detect_dependent_parent_signal(question)


# --- Military Retirement Pay / DoD Survivor Benefit Plan Exclusion -------
# Verified against the 2025 Instructions for Schedule CA (540), Part I,
# Section A, line 5a/5b (R&TC Sections 17132.9, 17132.10): NEW for tax
# years 2025-2029. Up to $20,000 EXCLUDED per payment type (military
# retirement pay and DoD Survivor Benefit Plan annuities are separate
# $20k caps -- "not to exceed $20,000 for each type of payments described
# in this paragraph"), but ONLY if federal AGI does not exceed $125,000
# (single/HOH/MFS) or $250,000 (MFJ or surviving spouse) -- an ELIGIBILITY
# CLIFF, not a gradual phase-out like YCTC: one dollar over the AGI limit
# and NONE of the exclusion applies, not a reduced amount.
#
# Deliberately scoped like Renter's Credit (flat amount below a hard
# ceiling) rather than attempting to also extract the exact dollar amount
# of retirement pay/annuity received -- that would be a second, easily-
# confused number alongside AGI (the same "two clustered dollar amounts"
# extraction risk that caused real bugs building FYTC earlier this
# session). Answers the ELIGIBILITY question definitively from AGI alone
# (which is what most people actually ask -- "is my military retirement
# taxable in California") and states the $20k-per-type cap as a fact,
# rather than computing an exact excluded dollar amount from a second,
# error-prone extraction.
MILITARY_RETIREMENT_TRIGGERS = {
    "military retirement", "military retired pay", "military pension",
    "survivor benefit plan", "dod survivor benefit", "military pay",
}
MILITARY_RETIREMENT_CAP_PER_TYPE = 20000.0
MILITARY_RETIREMENT_AGI_CEILING = {
    "single": 125000.0, "mfs": 125000.0, "hoh": 125000.0,
    "mfj": 250000.0, "qss": 250000.0,
}
MILITARY_RETIREMENT_CITATION = (
    "R&TC Sections 17132.9 and 17132.10; FTB 2025 Instructions for Schedule CA (540) -- "
    "Line 5a/5b")
MILITARY_RETIREMENT_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html"


def detect_military_retirement_signal(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in MILITARY_RETIREMENT_TRIGGERS)


def detect_military_retirement_missing_filing_status(question: str) -> bool:
    """Same pattern as detect_renters_credit_missing_filing_status -- the
    AGI ceiling itself is filing-status-tiered ($125k vs $250k)."""
    return detect_military_retirement_signal(question) and detect_filing_status(question) is None


def compute_military_retirement_exclusion(agi: float, filing_status: str):
    """Returns {'eligible': bool, 'ceiling': float, 'cap_per_type': float,
    'citation', 'source_url'} -- eligible=False means the AGI cliff was
    exceeded and NONE of the exclusion applies (fully taxable, same as
    federal), not a reduced amount."""
    ceiling = MILITARY_RETIREMENT_AGI_CEILING.get(filing_status)
    if ceiling is None or agi < 0:
        return None
    return {
        "eligible": agi <= ceiling, "ceiling": ceiling,
        "cap_per_type": MILITARY_RETIREMENT_CAP_PER_TYPE,
        "citation": MILITARY_RETIREMENT_CITATION,
        "source_url": MILITARY_RETIREMENT_SOURCE_URL,
    }
