"""State registry -- the single place that knows which states exist, which tax
domains each one covers, and which database env var backs each (state, agency).

engine.answer(state=...) consults this module before any domain routing:
  * the default state (CA) runs the existing engine unchanged;
  * any other state is handed to that state's engine module, and a domain the
    state does not cover yet gets an explicit "not covered" answer -- it never
    falls through to another state's rules;
  * a question that names a different state than the one selected gets a
    "switch the state selector" answer instead of a silent wrong-state result.

Adding a state = one entry in STATES plus its engine module.
"""
import re

DEFAULT_STATE = "CA"

# domain keys match the sidebar tax_type hints used by engine.answer().
STATES = {
    "CA": {
        "name": "California",
        "domains": {"sales", "income", "property", "employer"},
        "db_env": {"sales": "DATABASE_URL", "income": "INCOME_DATABASE_URL",
                   "property": "PROPERTY_DATABASE_URL", "employer": "EMPLOYER_DATABASE_URL"},
        "engine": None,   # California is served by engine.py itself
    },
    "IL": {
        "name": "Illinois",
        "domains": {"income"},
        "db_env": {"income": "IL_REVENUE_DATABASE_URL"},
        "engine": "il_engine",
    },
}

DOMAIN_LABELS = {"sales": "sales and use tax", "income": "income tax",
                 "property": "property tax", "employer": "employer payroll tax"}

# A question about credit for tax paid to another state, residency changes, etc.
# legitimately names a second state, so these phrases switch the mismatch check off.
_MULTI_STATE_PHRASES = (
    "other state", "another state", "out of state", "out-of-state", "nonresident", "non-resident",
    "part-year", "part year", "moved from", "moved to", "reciprocal", "resident of", "paid to",
    "tax paid to", "credit for taxes paid",
)


def state_name(code):
    return STATES[code]["name"]


def is_supported(code):
    return code in STATES


def _base(code, domain="multi_state"):
    return {"category": None, "taxable": None, "rate": None, "amount": None, "tax": None,
            "citation": None, "source_url": None, "location": None, "rate_basis": None,
            "branches": [], "fees": [], "info": None, "city_cannabis_tax": None,
            "route_dist": None, "rerank_v2_key": None, "domain": domain, "state": code}


def _needs_review(code, text, category):
    base = _base(code)
    base.update({"status": "needs_review", "answer_text": text, "category": category})
    return base


def mentioned_other_states(question, code):
    """State codes other than `code` whose full name appears in the question."""
    q = question.lower()
    return [c for c, s in STATES.items()
            if c != code and re.search(r"\b" + re.escape(s["name"].lower()) + r"\b", q)]


def mismatch_answer(question, code):
    """A 'switch the state selector' answer when the question names another
    supported state (and is not a legitimate multi-state question), else None."""
    q = question.lower()
    others = mentioned_other_states(question, code)
    if not others:
        return None
    own = re.search(r"\b" + re.escape(state_name(code).lower()) + r"\b", q) is not None
    if own and any(p in q for p in _MULTI_STATE_PHRASES):
        return None   # names both states and asks a multi-state question (credit for tax paid to another state)
    other = state_name(others[0])
    text = (f"Your question mentions {other}, but the state selector is set to {state_name(code)}. "
            f"I only apply one state's rules at a time so I never answer with the wrong state's law. "
            f"Switch the state selector to {other} and ask again. If you meant your {state_name(code)} return "
            f"(for example a credit for tax paid to {other}), say {state_name(code)} in the question.")
    return _needs_review(code, text, "state_mismatch")


def answer_other_state(question, code, tax_type=None):
    """Answer for any non-default state. Unsupported domains are refused explicitly."""
    mismatch = mismatch_answer(question, code)
    if mismatch:
        return mismatch
    s = STATES[code]
    if tax_type and tax_type not in s["domains"]:
        text = (f"{DOMAIN_LABELS.get(tax_type, tax_type).capitalize()} is not covered yet for {s['name']}. "
                f"Currently covered for {s['name']}: "
                f"{', '.join(DOMAIN_LABELS[d] for d in sorted(s['domains']))}.")
        return _needs_review(code, text, "domain_not_covered")
    import importlib
    return importlib.import_module(s["engine"]).answer_state(question, tax_type)
