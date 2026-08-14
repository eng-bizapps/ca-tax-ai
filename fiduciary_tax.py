"""Ring 3, trust/estate Phase A -- California FIDUCIARY-level tax on
RETAINED (undistributed) trust/estate income (Form 541). Genuinely
different from trust/estate Phase B (income_brackets.py's K-1 section):
this computes what the TRUST/ESTATE ITSELF owes on income it keeps, not
what a beneficiary owes on income distributed to them.

Verified directly against FTB's Form 541 booklet (ftb.ca.gov), not derived
from general tax knowledge -- see load_fiduciary_tax_data.py's module
docstring for the exemption-credit figures and citation.

CORE MECHANIC: California's 541 Tax Rate Schedule is numerically IDENTICAL
to the individual Schedule X (Single/Married-filing-separately) schedule --
confirmed by direct comparison during scoping. This module therefore reuses
income_brackets.compute_ca_tax(conn, retained_income, "single", tax_year)
UNCHANGED for the bracket-math step -- zero new bracket data, the same
"zero new math, just a new use of existing math" pattern as several other
features this session. The ONLY new reference data is the EXEMPTION
CREDIT (Form 541 Line 22): $1 (trust) / $10 (estate) / $144 (qualified
disability trust), SUBTRACTED FROM the computed tax AFTER the bracket
step -- a CREDIT, not a standard-deduction-style reduction of taxable
income beforehand (confirmed via FTB's own "Line 22 -- Exemption credit"
wording; there is no standard deduction on Form 541 at all).

SCOPE (deliberately narrow, "build the default case, defer the rest"
discipline used throughout this project):

  GRANTOR TRUSTS are not handled by this module at all -- FTB's optional
  simplified reporting means grantor trust income is taxed DIRECTLY to the
  grantor on the grantor's own personal return, so a grantor trust never
  owes this fiduciary-level tax in the first place. See engine.py's
  _fiduciary_tax_grantor_redirect_answer (reuses
  income_brackets.GRANTOR_TRUST_TERMS, the same constant trust/estate
  Phase B's K-1 grantor-trust redirect uses).

  RESIDENCY: California's rules for whether a trust/estate's income is
  taxable here without apportionment are genuinely complex in general
  (Schedule G), but FTB provides an explicit escape hatch -- if ANY ONE of
  the following holds, ALL trust/estate income is CA-taxable with no
  apportionment needed: all trustees are California residents, OR all
  non-contingent beneficiaries are California residents, OR all of the
  trust/estate's income is California-source. This module ONLY handles
  that bail-out case -- the taxpayer must explicitly STATE one of these
  conditions (see CA_RESIDENT_ENTITY_TERMS; same "trust the input, don't
  derive it" precedent used throughout this project), never derived from
  trustee/beneficiary facts. Anything needing actual Schedule G
  apportionment (mixed CA/non-CA trustees or beneficiaries) is out of
  scope, deferred.

  DISTRIBUTION: a trust/estate that distributes ALL of its income to
  beneficiaries gets a distribution deduction that offsets its own taxable
  income entirely -- FTB: "The deduction is equal to the amounts paid,
  credited, or required to be distributed or the distributable net
  income, whichever is smaller." For the SIMPLEST case (full
  distribution), that means fiduciary-level tax is exactly $0 and the
  beneficiary is taxed instead via K-1 (Phase B). This module handles that
  shortcut directly (detect_full_distribution) rather than computing DNI.
  Full DNI computation with tax-exempt income, capital gains allocated to
  corpus, or the AMID interaction is NOT attempted -- genuinely complex
  algebra FTB's own booklet works through via a worksheet, out of scope
  for a "state your retained income directly" v1.

  NOT ATTEMPTED AT ALL (disclosed, not silently ignored): the accumulation-
  distribution throwback tax (Form 5870A, a real complexity computed at
  the trust level for distributions of PRIOR YEARS' accumulated income),
  ESBT (Electing Small Business Trust) bifurcated computation, QSST/QSF
  special regimes, and the estate-specific fiscal-year/2-year-estimated-
  tax-deferral facts (administrative, not needed for a tax-owed compute
  question).
"""
from income_brackets import DEFAULT_TAX_YEAR, K1_TRIGGERS, compute_ca_tax

FIDUCIARY_TYPE_LABELS = {
    "trust": "trust",
    "estate": "estate",
    "qualified_disability_trust": "qualified disability trust",
}

FIDUCIARY_COMPUTE_TRIGGERS = {
    "how much tax", "how much does my", "how much do i owe", "what tax does",
    "what is the tax", "what's the tax", "does it owe", "do i owe", "am i subject to",
    "how much franchise tax",
}

# The 3 explicit FTB bail-out conditions for skipping Schedule G
# apportionment (any ONE, not all, is sufficient) -- stated directly by
# the taxpayer, never derived from separately-stated trustee/beneficiary
# facts (this project's standing "trust the input" precedent).
CA_RESIDENT_ENTITY_TERMS = {
    "california resident trust", "california resident estate",
    "all trustees are california residents", "all trustees are residents of california",
    "trustee is a california resident", "sole trustee is a california resident",
    "all beneficiaries are california residents",
    "all non-contingent beneficiaries are california residents",
    "all trust income is california source", "all trust income is california-source",
    "all estate income is california source", "all estate income is california-source",
    "all income is california source", "all income is california-source",
}

FULL_DISTRIBUTION_TERMS = {
    "distributed all of its income", "distributed all its income",
    "distributed all income to beneficiaries", "distributed all of the income",
    "fully distributed", "distributed everything to beneficiaries",
    "distributed all income to its beneficiaries", "distributed all of its income to beneficiaries",
}


def detect_fiduciary_type(question: str):
    """Checked most-specific-first. "estate" is a substring of common
    unrelated phrases ("real estate") -- accepted as low-risk here (mirrors
    the same accepted tradeoff in income_brackets.detect_trust_estate_k1)
    since a misclassification only affects which small exemption credit
    ($1 vs $10) is used, not whether the question is answered at all."""
    q = question.lower()
    if "qualified disability trust" in q:
        return "qualified_disability_trust"
    if "estate" in q:
        return "estate"
    if "trust" in q:
        return "trust"
    return None


def detect_fiduciary_compute_signal(question: str) -> bool:
    """Defense-in-depth against trust/estate Phase B's K-1 path (mirrors
    entity_tax.py's K1_EXCLUDE_TERMS defense): a question mentioning K-1
    language is asking what the BENEFICIARY owes, not what the trust/
    estate itself owes, and must never be answered here regardless of
    call-site ordering."""
    q = question.lower()
    if any(t in q for t in K1_TRIGGERS):
        return False
    return any(t in q for t in FIDUCIARY_COMPUTE_TRIGGERS)


def detect_ca_resident_entity_assertion(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in CA_RESIDENT_ENTITY_TERMS)


def detect_full_distribution(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in FULL_DISTRIBUTION_TERMS)


def get_exemption_credit(conn, entity_type: str, tax_year: int = DEFAULT_TAX_YEAR):
    r = conn.execute(
        "SELECT amount, citation, source_url FROM fiduciary_exemption_credit "
        "WHERE tax_year=%s AND entity_type=%s", (tax_year, entity_type)).fetchone()
    if not r:
        return None
    return {"amount": float(r[0]), "citation": r[1], "source_url": r[2]}


def compute_fiduciary_tax(conn, retained_income: float, entity_type: str,
                           tax_year: int = DEFAULT_TAX_YEAR):
    """retained_income is the trust/estate's OWN taxable (undistributed)
    income -- NOT reduced by a standard deduction (Form 541 has none).
    Reuses compute_ca_tax with filing_status='single' (Schedule X) for the
    bracket step, then subtracts the exemption credit -- see module
    docstring. Returns None if retained_income is missing/negative, or if
    there's no exemption-credit/bracket data for this (tax_year,
    entity_type)."""
    if retained_income is None or retained_income < 0:
        return None
    credit = get_exemption_credit(conn, entity_type, tax_year)
    if not credit:
        return None
    calc = compute_ca_tax(conn, retained_income, "single", tax_year)
    if not calc:
        return None
    total_tax = max(0.0, round(calc["total_tax"] - credit["amount"], 2))
    return {
        "tax_before_credit": calc["total_tax"], "exemption_credit": credit["amount"],
        "total_tax": total_tax, "marginal_rate": calc["marginal_rate"],
        "surtax": calc["surtax"], "citation": credit["citation"], "source_url": credit["source_url"],
    }
