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
Youth Tax Credit (FYTC) is NOT implemented -- its eligibility (foster-youth
status at/after age 13) can't be inferred from a general question, unlike
CalEITC/YCTC which only depend on income and child count.
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
