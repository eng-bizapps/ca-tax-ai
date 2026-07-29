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
    "investment income", "capital gain", "dividend", "rental",
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
