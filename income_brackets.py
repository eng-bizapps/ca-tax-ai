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
    "llc", "partnership", "capital gain", "capital loss", "rental",
    "itemize", "itemized", "dependent", "bonus", "stock", "rsu", "trust",
    "estate", "freelance", "contractor", "sole proprietor", "k-1",
    "schedule c", "schedule e", "gambling", "alimony", "pension",
}
COMPUTE_TRIGGERS = {
    "tax bracket", "how much tax", "how much california tax",
    "how much state tax", "how much ca tax", "tax i owe", "tax owe",
    "tax liability", "compute my tax", "figure my tax", "calculate my tax",
    "what tax do i owe", "what will i owe",
}
DEDUCTION_TRIGGERS = {"standard deduction"}


def detect_filing_status(question: str):
    q = question.lower()
    if "married" in q and ("joint" in q or re.search(r"\bmfj\b", q)):
        return "mfj"
    if "married" in q and ("separat" in q or re.search(r"\bmfs\b", q)):
        return "mfs"
    if "head of household" in q or re.search(r"\bhoh\b", q):
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
