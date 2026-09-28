"""Ring 4 extension: deterministic CHECKLIST-based eligibility
determination for Proposition 19's parent-child (and grandparent-
grandchild) exclusion from reassessment -- the same "several independent
boolean facts, not a dollar amount" shape as income_eligibility.py's Head
of Household determination, just in the property-tax domain. A separate
module rather than added to income_eligibility.py: that module's own
docstring frames itself as a Ring 2 (income-domain) extension, and
property_tax_inventory.py states Ring 4 is a separate database by design
-- this checklist has zero income-domain dependency and belongs alongside
property_tax.py instead, mirroring how engine.py already treats
_answer_property as a structurally separate dispatcher from _answer_income.
Once this module returns True, property_tax.compute_parent_child_
exclusion_value computes the dollar consequence (the value-cap formula) --
a genuinely separate compute step this module does NOT perform.

Scope, verified against R&TC Sec. 63.2, Cal. Const. Art. XIII A Sec.
2.1(c), and BOE Letter To Assessors 2026/026 (read directly, not a
secondary tax-prep source) -- see property_tax_inventory.py for the full
ledger entry. This v1 models:
  - TWO independent categories per parcel: "family home" (the transferor's
    principal residence -- requires the transferee to occupy it AND file
    for the Homeowners'/Disabled Veterans' Exemption, both within 1 year
    of transfer, "no exceptions" per the statute) and "family farm" (land
    under cultivation/pasture/producing an agricultural commodity -- NO
    occupancy or filing requirement at all, a genuinely separate gate).
  - "Child" relationships: biological, stepchild/in-law (deemed to exist
    until the underlying marriage ends by divorce, or by death followed by
    the surviving stepparent's remarriage -- this v1 only detects the
    divorce-ended case explicitly; a remarriage-after-death fact pattern
    is rare enough in a single question that it's left to defer to None
    rather than guessing), formally adopted, and foster.
  - Grandparent-grandchild: same relationship rules one generation down,
    PLUS a single additional gate -- ALL of the grandchild's parents who
    themselves qualify as the grandparent's "children" must be deceased as
    of the transfer date (a stepparent link in the middle generation does
    NOT need to be deceased, only the grandparent's own child does; this
    v1 does not attempt that stepparent-vs-own-child distinction within
    the deceased-gate check itself -- it treats "my parents are deceased"
    as one flat fact, same "trust the stated figures" precedent as every
    other compute path in this codebase).
  - A narrow hard-decline gate: a transfer explicitly stated as more than
    3 years ago AND explicitly stated as already resold to a third party
    is categorically ineligible for a retroactive exclusion (the 3-year-
    or-before-resale claim deadline, whichever is earlier, has passed with
    no possibility of prospective-only relief helping a prior owner). This
    is the ONE case worth a hard gate rather than a defer: a wrong "maybe
    you still qualify" here produces a wildly-wrong full-reassessment
    number, exactly the failure mode this codebase's design exists to
    prevent -- see property_tax_inventory.py's own note on this item.

Deliberately DEFERRED (returns None, never guessed): the biological-child-
given-up-for-adoption-by-someone-else exception; any multi-generational
step/in-law nuance inside the grandparent deceased-gate beyond the single
flat fact above; a parcel where family-home and family-farm categories
would produce DIFFERENT verdicts for different portions of the same
parcel (this v1 answers at the parcel level, not the sub-portion level);
and the transferor's OWN prior Homeowners'/Disabled Veterans' Exemption
eligibility on the family home, which is DISCLOSED as assumed (taken as
satisfied whenever a family-home category term is stated) rather than
independently re-verified -- the same disclosure precedent income_
eligibility.py uses for HOH's support/joint-return/citizenship sub-tests.

DETECTION mirrors income_eligibility.py's approach exactly: the question
must explicitly assert each required fact via recognized phrasing (a
genuine checklist, not open-ended understanding). If the facts aren't ALL
recognized as stated, the determination declines to None (or, for the
narrow hard-gate/unrelated-party/step-ended cases, a clean False) rather
than guessing.
"""
import re

PARENT_CHILD_EXCLUSION_CITATION = ("Rev. & Tax. Code Sec. 63.2; Cal. Const. Art. XIII A Sec. "
                                    "2.1(c); BOE Letter To Assessors 2026/026")
PARENT_CHILD_EXCLUSION_SOURCE_URL = "https://www.boe.ca.gov/prop19/"

# Carries forward the original detect_parent_child_exclusion_out_of_scope
# term set verbatim (so the existing zero-fact sweep regression case keeps
# matching), plus additional phrasing this richer determination path
# should also recognize.
PARENT_CHILD_TRIGGER_TERMS = {
    "parent-child exclusion", "parent child exclusion", "inherited my parents",
    "inherited from my parents", "transferred from my parents", "grandparent-grandchild",
    "grandparent grandchild exclusion", "grandparent-grandchild exclusion",
    "reassessment exclusion", "excluded from reassessment", "avoid reassessment",
    "prop 19 exclusion", "proposition 19 exclusion",
}
PARENT_CHILD_QUESTION_TERMS = {
    "qualify", "qualifies", "qualified", "eligible", "eligibility",
    "do i qualify", "does it qualify", "am i eligible", "exclude", "excluded",
}

# --- Relationship (proximity regex, same shape/rationale as income_
# eligibility._OWN_CHILD_RE -- a real question usually inserts words
# between "my" and the relationship term, e.g. "my son who just turned
# 30", so a literal-substring check would miss it). ---
_CHILD_RELATIONSHIP_RE = re.compile(
    r"\bmy\b[\w\s\-']{0,30}?\b(son|daughter|child|children|stepson|stepdaughter|"
    r"stepchild|son-in-law|daughter-in-law|"
    r"adopted\s+son|adopted\s+daughter|adopted\s+child|"
    r"foster\s+son|foster\s+daughter|foster\s+child)\b")
_GRANDCHILD_RELATIONSHIP_RE = re.compile(
    r"\bmy\b[\w\s\-']{0,30}?\b(grandchild|grandson|granddaughter|"
    r"adopted\s+grandchild)\b")
# the relationship types whose statutory validity is conditioned on the
# underlying marriage NOT having ended by divorce (deemed to persist
# through death until the surviving stepparent remarries) -- distinct from
# biological/adopted/foster, which have no such condition.
_STEP_OR_INLAW_RE = re.compile(
    r"\bmy\b[\w\s\-']{0,30}?\b(stepson|stepdaughter|stepchild|son-in-law|daughter-in-law)\b")
STEP_LINK_ENDED_TERMS = {
    "ended by divorce", "the marriage ended in divorce", "divorced my stepparent",
    "since the divorce", "after the divorce", "we got divorced", "we are divorced",
    "is now divorced", "the marriage ended by divorce",
}
UNRELATED_PARTY_TERMS = {
    "unrelated party", "not related", "a friend", "not my child",
    "not my parent", "no relation", "not a relative", "not related to me",
}

# --- Category: family home (occupancy+filing gate applies) vs. family
# farm (no occupancy/filing requirement at all -- R&TC 63.2(e)(4)/(5)). ---
FAMILY_HOME_TERMS = {
    "family home", "principal residence", "primary residence",
    "primary home", "main residence",
}
FAMILY_FARM_TERMS = {
    "family farm", "farmland", "under cultivation", "pasture land",
    "grazing land", "agricultural commodity", "farming operation",
}

# --- Family-home-only gate: transferee occupancy AND exemption filing,
# BOTH within 1 year of transfer -- "no exceptions" per the statute, so
# treated as a hard requirement, not a disclosed assumption. Negated term
# sets include both first-person ("have not filed") and third-person
# ("has not filed") forms -- found via testing: a third-person question
# ("my son... has not filed") wrongly fell through to None instead of the
# clean False it should have reached, because only the first-person form
# was originally covered. ---
OCCUPANCY_TERMS = {
    "moved into", "moved in", "made it my principal residence",
    "made it my own principal residence", "living there", "i live there",
    "occupied the property", "moved into the home", "moved into the house",
}
OCCUPANCY_NEGATED_TERMS = {
    "did not move in", "didn't move in", "never moved in",
    "have not moved in", "haven't moved in", "has not moved in",
    "hasn't moved in", "not living there",
    "don't live there", "do not live there", "renting it out",
    "rented out", "not occupying",
}
EXEMPTION_FILED_TERMS = {
    "filed for the homeowners", "filed for the homeowner's",
    "filed the homeowners exemption", "filed for the homeowners exemption",
    "applied for the homeowners exemption", "filed a homeowners exemption claim",
    "filed for the disabled veterans exemption", "filed for the disabled veteran's exemption",
}
FILING_NEGATED_TERMS = {
    "did not file", "didn't file", "never filed", "have not filed",
    "haven't filed", "has not filed", "hasn't filed", "failed to file",
}
WITHIN_ONE_YEAR_TERMS = {
    "within a year", "within one year", "within 1 year", "within the first year",
}

# --- Grandparent-grandchild extra gate: all of the grandchild's parents
# who themselves qualify as the grandparent's "children" must be deceased
# as of the transfer date. Treated as one flat fact (see module docstring)
# -- not a re-derivation of the middle generation's own step/in-law status.
GRANDPARENT_DECEASED_GATE_TERMS = {
    "is deceased", "are deceased", "passed away", "has passed away",
    "have passed away", "predeceased", "died before the transfer",
    "died before the transfer date",
}
GRANDPARENT_PARENT_ALIVE_TERMS = {
    "still alive", "still living", "is alive", "are alive",
}

# --- Narrow hard-decline gate: claim deadline (3 years from transfer, or
# before resale to a third party, whichever is earlier) already passed
# with no possibility of relief -- see module docstring for why this is a
# hard gate rather than a defer. ---
_OLD_TRANSFER_RE = re.compile(r"\b(?:more than|over)\s+(\d{1,2})\s+years?\s+ago\b")
RESOLD_THIRD_PARTY_TERMS = {
    "resold", "sold it to a third party", "since sold the property",
    "sold to someone else", "already sold it", "sold the property since",
}


def _parent_child_question_ok(q: str) -> bool:
    if not any(t in q for t in PARENT_CHILD_TRIGGER_TERMS):
        return False
    return any(t in q for t in PARENT_CHILD_QUESTION_TERMS)


def _claim_window_closed(q: str) -> bool:
    m = _OLD_TRANSFER_RE.search(q)
    if not m or int(m.group(1)) < 3:
        return False
    return any(t in q for t in RESOLD_THIRD_PARTY_TERMS)


def _step_link_ended(q: str) -> bool:
    if not _STEP_OR_INLAW_RE.search(q):
        return False
    return any(t in q for t in STEP_LINK_ENDED_TERMS)


def _grandparent_gate(q: str):
    """True (gate passes), False (explicitly contradicted -- a stated
    parent is still alive), or None (not confirmed either way)."""
    if any(t in q for t in GRANDPARENT_PARENT_ALIVE_TERMS):
        return False
    if any(t in q for t in GRANDPARENT_DECEASED_GATE_TERMS):
        return True
    return None


def _family_home_requirements_met(q: str):
    """True (occupied + filed + within a year, all affirmatively stated),
    False (occupancy or filing explicitly negated), or None (incomplete)."""
    if any(t in q for t in OCCUPANCY_NEGATED_TERMS) or any(t in q for t in FILING_NEGATED_TERMS):
        return False
    occupied = any(t in q for t in OCCUPANCY_TERMS)
    filed = any(t in q for t in EXEMPTION_FILED_TERMS)
    within_year = any(t in q for t in WITHIN_ONE_YEAR_TERMS)
    if occupied and filed and within_year:
        return True
    return None


def detect_parent_child_exclusion_qualifies(question: str):
    """Returns True (qualifies), False (a clean, simple disqualification),
    or None (not a determination this v1 can make -- missing facts, or a
    fact pattern deliberately deferred). Sequential decline chain, never
    guesses past an unresolved fact -- same discipline as income_
    eligibility.detect_hoh_determination."""
    q = question.lower()
    if not _parent_child_question_ok(q):
        return None

    if _claim_window_closed(q):
        return False

    is_grandchild = bool(_GRANDCHILD_RELATIONSHIP_RE.search(q))
    is_child = bool(_CHILD_RELATIONSHIP_RE.search(q))
    if any(t in q for t in UNRELATED_PARTY_TERMS):
        return False
    if not is_grandchild and not is_child:
        return None
    if _step_link_ended(q):
        return False

    if is_grandchild:
        gate = _grandparent_gate(q)
        if gate is False:
            return False
        if gate is None:
            return None

    is_family_home = any(t in q for t in FAMILY_HOME_TERMS)
    is_family_farm = any(t in q for t in FAMILY_FARM_TERMS)
    if not is_family_home and not is_family_farm:
        return None

    if is_family_home:
        met = _family_home_requirements_met(q)
        if met is False:
            return False
        if met is None:
            return None

    return True


def _any_personal_fact_stated(q: str) -> bool:
    """True iff the question shows at least one attempt at stating a
    personal fact -- distinguishes "trying to describe my situation but
    missing something" from a bare "what are the requirements" question
    with zero personal facts, mirroring income_eligibility._any_personal_
    fact_stated's own found-via-testing rationale (without this guard, a
    vague informational question would wrongly get downgraded to
    needs_review instead of falling through to a general explanation)."""
    return (bool(_CHILD_RELATIONSHIP_RE.search(q))
            or bool(_GRANDCHILD_RELATIONSHIP_RE.search(q))
            or any(t in q for t in UNRELATED_PARTY_TERMS)
            or any(t in q for t in FAMILY_HOME_TERMS)
            or any(t in q for t in FAMILY_FARM_TERMS)
            or any(t in q for t in OCCUPANCY_TERMS)
            or any(t in q for t in OCCUPANCY_NEGATED_TERMS)
            or any(t in q for t in EXEMPTION_FILED_TERMS)
            or any(t in q for t in FILING_NEGATED_TERMS)
            or any(t in q for t in GRANDPARENT_DECEASED_GATE_TERMS)
            or any(t in q for t in GRANDPARENT_PARENT_ALIVE_TERMS))


def detect_parent_child_exclusion_checklist_incomplete(question: str) -> bool:
    """True iff this looks like someone attempting a parent-child-
    exclusion determination (at least one personal fact stated) but
    detect_parent_child_exclusion_qualifies couldn't reach a verdict --
    mirrors income_eligibility.detect_hoh_checklist_incomplete exactly."""
    q = question.lower()
    if not _parent_child_question_ok(q):
        return False
    if not _any_personal_fact_stated(q):
        return False
    return detect_parent_child_exclusion_qualifies(question) is None


def parent_child_exclusion_false_reason(question: str):
    """Only meaningful when detect_parent_child_exclusion_qualifies already
    returned False -- re-walks the same decline chain to name WHICH clean
    negative applied, so the caller can give a specific message instead of
    one generic "no". Mirrors compute_prop19_base_year_transfer's own
    "reason" key on its ineligible-dict shape."""
    q = question.lower()
    if _claim_window_closed(q):
        return "claim_window_closed"
    if any(t in q for t in UNRELATED_PARTY_TERMS):
        return "unrelated_party"
    if _step_link_ended(q):
        return "step_link_ended"
    if bool(_GRANDCHILD_RELATIONSHIP_RE.search(q)) and _grandparent_gate(q) is False:
        return "grandparent_still_alive"
    if any(t in q for t in FAMILY_HOME_TERMS) and _family_home_requirements_met(q) is False:
        return "occupancy_or_filing_negated"
    return None
