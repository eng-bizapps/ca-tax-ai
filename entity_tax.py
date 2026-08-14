"""Ring 3, business entities Phase A -- California ENTITY-LEVEL annual/
minimum tax for C-corps, S-corps, LLCs, and partnerships (LPs/LLPs/general
partnerships). The first genuinely new compute engine for the business-
entity domain -- unlike anything built so far, this taxes the ENTITY
itself, not an individual's personal return.

Verified directly against FTB's business-entity pages (ftb.ca.gov), not
derived from general tax knowledge -- see load_entity_tax_data.py's module
docstring for every quoted figure and its source URL.

CORE MECHANIC, by entity type (2025 tax year):
  general_partnership: $0 annual tax. Not required to register with the CA
    Secretary of State, so it owes no CA franchise/annual tax at all.
  lp / llp: $800 flat annual tax, regardless of income. No first-year
    waiver (the AB 85 waiver expired for tax years beginning 2024+).
  llc: $800 flat annual tax PLUS a tiered fee based on total CA income
    (see llc_fee_for_income) -- applies even to single-member/disregarded
    LLCs, which California does NOT treat differently for this purpose.
    No first-year waiver (same AB 85 expiration as LP/LLP).
  s_corp / s_corp_financial: $800 annual tax (WAIVED in the entity's first
    taxable year -- a PERMANENT rule for entities formed/qualified on or
    after 2020-01-01, still active for 2025) PLUS 1.5% (3.5% for financial
    S-corps) of net CA income -- that income tax portion applies even in
    the waived first year, only the $800 floor is waived.
  c_corp / c_corp_financial (added 2026-08-13, same session): SAME $800
    annual tax / first-year-waiver mechanic as S-corps -- re-verified via
    FTB's general "Corporations" page (not the S-corp-specific page) that
    the waiver is a GENERAL CORPORATION rule, not S-corp-only as originally
    assumed when this module was first built -- PLUS 8.84% (10.84% for
    banks/financials) of net CA income instead of the S-corp rate.

This is a lookup-plus-flat-rate shape, structurally identical to the LLC
fee tiers / CA income tax brackets / CalEITC phase-out precedents already
in this codebase -- reference data lives in entity_annual_tax_rules /
llc_fee_brackets (income_schema.py), not hardcoded here, so it survives
annual figure changes the same way as every other verified-numbers table.

SCOPE (deliberately narrow, "build the default case, defer the rest"
discipline used throughout this project): single-state California-only
entities. Explicitly NOT modeled, disclosed rather than silently ignored:
  - Multi-state apportionment (Schedule R) -- an entity operating in
    multiple states needs to apportion income before this math applies;
    this module assumes the stated income IS the CA-source/apportioned
    figure already, same "trust the input" precedent used throughout this
    project's compute paths.
  - Combined/unitary group returns.
  - The 6.65% Alternative Minimum Tax (AMT) that can apply to corporations.
  - The 15-day short-period exception (CA tax year <=15 days + no CA
    business activity).
  - How this income flows to an INDIVIDUAL owner's personal return via
    Schedule K-1 -- a deliberately SEPARATE feature (Phase B, built the
    same session), since it's a genuinely different compute problem
    (personal bracket tax on pass-through income, not entity-level tax).
"""
import re

from income_brackets import DEFAULT_TAX_YEAR

ENTITY_TYPE_LABELS = {
    "general_partnership": "general partnership",
    "lp": "limited partnership",
    "llp": "limited liability partnership (LLP)",
    "llc": "LLC",
    "s_corp": "S-corporation",
    "s_corp_financial": "financial S-corporation",
    "c_corp": "C-corporation",
    "c_corp_financial": "financial C-corporation",
}

# Entities whose total CA tax depends on a stated income figure -- LLC's
# fee tier and S-corp/C-corp's flat income tax rate both need it; general
# partnerships/LPs/LLPs owe a flat amount regardless of income (LPs/LLPs
# still owe $800, general partnerships still owe $0), so no figure is
# required for those types.
INCOME_REQUIRED_TYPES = {"llc", "s_corp", "s_corp_financial", "c_corp", "c_corp_financial"}

# Checked in a specific ORDER (most-specific phrase first) since "limited
# liability partnership" contains "partnership" and "limited liability
# company"-adjacent LLC phrasing must not be confused with LLP. Bare
# "partnership" (no general/limited/liability qualifier) is deliberately
# NOT mapped to a type here -- see detect_entity_type -- since general
# partnerships owe $0 and LPs/LLPs owe $800, a genuinely different answer
# this module must not guess between. Covers both S-corp and C-corp
# financial phrasing (added 2026-08-13 alongside C-corp support) plus
# entity-type-agnostic terms ("financial corporation") since the same
# bank/financial distinction applies to either.
FINANCIAL_ENTITY_TERMS = {
    "financial corporation", "banking corporation", "bank corporation",
    "financial s corporation", "financial s-corp", "financial s corp",
    "bank s corporation", "bank s-corp",
    "financial c corporation", "financial c-corp", "financial c corp",
    "bank c corporation", "bank c-corp",
}

ENTITY_COMPUTE_TRIGGERS = {
    "how much tax", "how much franchise tax", "how much does my", "how much do i owe",
    "what tax does", "what is the tax", "what's the tax", "llc fee", "annual tax",
    "minimum tax", "minimum franchise tax", "franchise tax", "do i owe", "am i subject to",
}

# Defense-in-depth (mirrors income_brackets.COMPLEXITY_EXCLUDE's role): a
# question mentioning a K-1 is asking what the INDIVIDUAL owner owes on
# pass-through income (business entities Phase B), NOT what the entity
# itself owes (this module, Phase A) -- "I received a K-1 from my S-corp
# showing $50,000 in income, how much tax do I owe" would otherwise match
# both detect_entity_type (on "s-corp") and ENTITY_COMPUTE_TRIGGERS (on
# "how much tax"), wrongly computing the ENTITY's $800+1.5% tax instead of
# the taxpayer's personal bracket tax on that $50,000. Checked here (not
# just via engine.py call-site ordering) so this module can never fire on
# a K-1 question regardless of where it's called from.
K1_EXCLUDE_TERMS = {"k-1", "k1", "schedule k-1", "schedule k1"}

FIRST_YEAR_TERMS = {
    "first year", "first taxable year", "newly formed", "newly qualified",
    "just formed", "formed this year", "started this year", "new llc",
    "new s-corp", "new s corp", "new c-corp", "new c corp", "new corporation",
    "just started", "recently formed", "recently incorporated", "recently qualified",
}


def detect_entity_type(question: str):
    """Returns (entity_type, is_ambiguous). is_ambiguous=True means an
    entity signal is present but the SPECIFIC type couldn't be pinned
    down -- currently only bare "partnership" with no general/limited/
    liability qualifier, since general partnerships owe $0 and LPs/LLPs
    owe $800, a genuinely different answer. entity_type is None whenever
    is_ambiguous is True OR no entity signal is present at all."""
    q = question.lower()
    if re.search(r"\bllp\b", q) or "limited liability partnership" in q:
        return "llp", False
    if "limited partnership" in q:
        return "lp", False
    if "general partnership" in q:
        return "general_partnership", False
    if re.search(r"\bllc\b", q) or "limited liability company" in q:
        return "llc", False
    if "s-corp" in q or "s corp" in q or "s corporation" in q or "subchapter s" in q:
        if any(t in q for t in FINANCIAL_ENTITY_TERMS):
            return "s_corp_financial", False
        return "s_corp", False
    # C-corp is checked AFTER S-corp (already excluded above) and defaults
    # from bare "corporation"/"corp" too -- C-corp is the default tax
    # treatment for a corporation (S-corp requires an election the
    # taxpayer would name explicitly), so "my corporation" without an S
    # qualifier is read as a C-corp, not left ambiguous the way bare
    # "partnership" is (general vs. LP/LLP genuinely differ in whether
    # ANY tax is owed at all; C-corp is simply the unmarked default here).
    if ("c-corp" in q or "c corp" in q or "c corporation" in q
            or re.search(r"\bcorporation\b", q) or re.search(r"\bcorp\b", q)):
        if any(t in q for t in FINANCIAL_ENTITY_TERMS):
            return "c_corp_financial", False
        return "c_corp", False
    if re.search(r"\bpartnership\b", q):
        return None, True
    return None, False


def detect_entity_compute_signal(question: str) -> bool:
    q = question.lower()
    if any(t in q for t in K1_EXCLUDE_TERMS):
        return False
    return any(t in q for t in ENTITY_COMPUTE_TRIGGERS)


def detect_first_year(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in FIRST_YEAR_TERMS)


def get_entity_rule(conn, entity_type: str, tax_year: int = DEFAULT_TAX_YEAR):
    r = conn.execute(
        "SELECT annual_tax, income_tax_rate, first_year_waiver, form_number, citation, source_url "
        "FROM entity_annual_tax_rules WHERE tax_year=%s AND entity_type=%s",
        (tax_year, entity_type)).fetchone()
    if not r:
        return None
    return {"annual_tax": float(r[0]), "income_tax_rate": float(r[1]) if r[1] is not None else None,
            "first_year_waiver": r[2], "form_number": r[3], "citation": r[4], "source_url": r[5]}


def llc_fee_for_income(conn, ca_income: float, tax_year: int = DEFAULT_TAX_YEAR):
    """Segment lookup, same style as income_brackets.compute_ca_tax --
    default to the top (unbounded) tier if income exceeds every listed
    ceiling. Returns None if there's no fee data for this tax_year, or if
    ca_income is missing/negative."""
    if ca_income is None or ca_income < 0:
        return None
    rows = conn.execute(
        "SELECT income_floor, income_ceiling, fee_amount, citation, source_url "
        "FROM llc_fee_brackets WHERE tax_year=%s ORDER BY income_floor",
        (tax_year,)).fetchall()
    if not rows:
        return None
    seg = rows[-1]
    for floor, ceiling, fee, citation, source_url in rows:
        if ca_income >= float(floor) and (ceiling is None or ca_income <= float(ceiling)):
            seg = (floor, ceiling, fee, citation, source_url)
            break
    _, _, fee, citation, source_url = seg
    return {"fee_amount": float(fee), "citation": citation, "source_url": source_url}


def compute_entity_tax(conn, entity_type: str, ca_income, is_first_year: bool = False,
                        tax_year: int = DEFAULT_TAX_YEAR):
    """ca_income is the entity's CA-source income -- for LLCs, TOTAL CA
    income (used only for the fee tier, no entity-level income tax); for
    S-corps, NET CA income (the 1.5%/3.5% tax base). May be None for
    entity types in INCOME_REQUIRED_TYPES's complement (general
    partnership, LP, LLP) since their tax doesn't depend on income at all.
    Returns None if the entity type is unknown, or if an income-dependent
    type is missing/has an invalid income figure."""
    rule = get_entity_rule(conn, entity_type, tax_year)
    if not rule:
        return None

    annual_tax = rule["annual_tax"]
    waived = bool(is_first_year and rule["first_year_waiver"])
    if waived:
        annual_tax = 0.0

    income_tax = 0.0
    fee_amount = 0.0
    fee_citation = None
    if entity_type in INCOME_REQUIRED_TYPES:
        if ca_income is None or ca_income < 0:
            return None
        if rule["income_tax_rate"] is not None:
            income_tax = round(ca_income * rule["income_tax_rate"], 2)
        if entity_type == "llc":
            fee = llc_fee_for_income(conn, ca_income, tax_year)
            if not fee:
                return None
            fee_amount = fee["fee_amount"]
            fee_citation = fee["citation"]

    total_tax = round(annual_tax + income_tax + fee_amount, 2)
    return {
        "annual_tax": annual_tax, "annual_tax_waived": waived, "income_tax": income_tax,
        "fee_amount": fee_amount, "total_tax": total_tax, "form_number": rule["form_number"],
        "income_tax_rate": rule["income_tax_rate"], "citation": rule["citation"],
        "source_url": rule["source_url"], "fee_citation": fee_citation,
    }
