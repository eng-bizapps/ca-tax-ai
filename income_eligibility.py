"""Ring 2 extension: deterministic CHECKLIST-based eligibility
determinations -- Head of Household filing status (and, in future passes,
the 4 personal-history credits) -- a genuinely different fact shape from
bracket math (income_brackets.py) or income-based credit phase-outs
(income_credits.py): these require SEVERAL INDEPENDENT boolean facts to
all be true, not a dollar amount or a single income figure. Mirrors this
project's "separate module per concept" precedent -- engine.py imports and
calls this, it does not reimplement the checklist logic inline.

WHY THIS EXISTS (reclassified from "Bucket 1 -- structurally impossible"):
HOH determination was originally excluded on the theory that it needs
multi-turn conversation. That was wrong -- if a user states ALL the
required facts in ONE question, the determination is fully deterministic,
the same "trust the input" principle as every other compute path here. The
real barrier isn't multi-turn intake, it's that a vague question doesn't
state enough facts -- which is the same "missing input, ask specifically"
shape as filing-status/children-count detection elsewhere in this project.

SCOPE DISCIPLINE (same "narrowest safe default, defer the rest" rule as
every other compute path): verified against FTB Form 3532 (2025)
Instructions -- Head of Household Filing Status Schedule -- before
building. The FULL test has real depth this v1 does NOT attempt:
  - The "considered unmarried" branch for taxpayers who WERE married/RDP
    as of Dec 31 (or whose spouse/RDP died during the year) but lived
    apart for the last 6 months -- its own multi-part sub-test.
  - The qualifying-RELATIVE branch (as opposed to qualifying CHILD) --
    adds a gross-income ceiling ($5,200 for 2025, the federal exemption
    amount) with a community-property wrinkle if the relative is married.
  - The explicit support test ("you paid more than half the qualifying
    person's total support"), joint-return test, and citizenship test --
    referenced by FTB's own instructions but not spelled out on the
    CA-specific page; they defer to federal IRC Section 152 dependency
    rules (federal Pub. 501).
This module computes ONLY the narrowest core case: unmarried the ENTIRE
year (no separation/divorce-during-year complexity), the qualifying person
is the taxpayer's OWN child (birth/step/adopted/foster -- not a relative),
the child lived with the taxpayer more than half the year (183+ days),
the taxpayer paid more than half the home costs, and the child meets the
simple age/full-time-student test. Anything outside that -- defers, never
guesses. The support/joint-return/citizenship sub-tests are DISCLOSED as
assumed in the answer text, not silently ignored.

DETECTION IS NECESSARILY DIFFERENT from every other module here: instead
of extracting a dollar amount, it requires the question to explicitly
assert EACH required fact via recognized phrasing -- a genuine checklist,
not open-ended natural-language understanding. If the required facts
aren't ALL recognized as stated, the determination declines with a
specific checklist message (mirroring detect_compute_missing_filing_status
elsewhere) rather than guessing, or a plain "no" for the one case where
a single hard-gate fact (marital status) is clearly, explicitly false.
"""
import re

HOH_CITATION = "FTB 2025 Instructions for Form FTB 3532, Head of Household Filing Status Schedule -- Part I/II/III"
HOH_SOURCE_URL = "https://www.ftb.ca.gov/forms/2025/2025-3532-instructions.html"

HOH_TRIGGER_TERMS = {"head of household", "head-of-household", "hoh"}
HOH_QUESTION_TERMS = {"qualify", "qualifies", "qualified", "eligible", "eligibility",
                       "do i qualify", "can i file", "am i eligible"}

# --- Fact 1: unmarried the ENTIRE year (the simple prong -- NOT the
# "considered unmarried" separated-but-still-married sub-test, which is
# real complexity this v1 doesn't attempt). ---
UNMARRIED_TERMS = {
    "unmarried all year", "unmarried the entire year", "unmarried for the whole year",
    "unmarried the whole year", "i was unmarried", "i am unmarried", "was unmarried",
    "am unmarried", "not married", "wasn't married", "was not married", "never married",
    "single all year", "single the entire year", "single the whole year",
    "not in a registered domestic partnership", "not a registered domestic partner",
    "not an rdp",
}
# hints the taxpayer might actually need the "considered unmarried"
# sub-test (married/RDP as of Dec 31 or spouse died, living apart) --
# genuinely different and more complex; must defer, not assume the simple
# unmarried-all-year case applies.
MARITAL_COMPLEXITY_TERMS = {
    "separated", "separation", "divorced during", "divorce during",
    "considered unmarried", "spouse died", "rdp died", "domestic partner died",
    "living apart", "lived apart",
}
# a clean, explicit "I was married" statement -- the one negative case
# simple enough to answer definitively without the full checklist. Uses
# regex with a \b word boundary before "married" specifically -- a plain
# substring check would also match inside "unmarried" (the literal text
# "married all year" IS a substring of "unmarried all year"), which
# caused a real bug found via testing: the positive case wrongly hit this
# negative branch first and never reached the real checklist below.
_MARRIED_ALL_YEAR_RE = re.compile(
    r"\bmarried (?:all year|the entire year|the whole year)\b|"
    r"\bi (?:was|am) married\b")

# --- Fact 2: paid more than half the cost of keeping up the home. ---
HOME_COST_TERMS = {
    "more than half the cost", "more than half the costs",
    "over half the cost", "over half the costs",
    "half the cost of keeping up", "half the costs of keeping up",
    "half the cost of maintaining", "half the costs of maintaining",
}

# --- Fact 3: qualifying person is the taxpayer's OWN child (birth/step/
# adopted/foster) -- NOT a qualifying relative, which adds the gross-
# income-ceiling/community-property branch this v1 doesn't attempt.
# PROXIMITY-based (not a literal "my son" substring): a real question
# almost always inserts an age descriptor between the two ("my 10 YEAR
# OLD son"), which a literal-phrase check misses entirely -- found via
# testing, the exact same class of gap as the marital-status bug above,
# just in the opposite direction (a false NEGATIVE here, silently
# declining to compute a case it safely could have). Allows up to 30
# chars between "my" and the relationship word. ---
_OWN_CHILD_RE = re.compile(
    r"\bmy\b[\w\s\-']{0,30}?\b(son|daughter|child|children|stepson|stepdaughter|"
    r"stepchild|foster\s+child|foster\s+son|foster\s+daughter|"
    r"adopted\s+son|adopted\s+daughter|adopted\s+child)\b")
# any of these signal a qualifying-RELATIVE (not child) fact pattern, or a
# disability/other complexity this v1 explicitly excludes. Same proximity
# reasoning as above -- the DANGEROUS direction this time (a literal-
# substring check could miss "my elderly 70 year old MOTHER" the same way
# it missed "my 10 year old son", but here a miss means silently treating
# a qualifying-RELATIVE question as the simpler qualifying-CHILD case).
_QUALIFYING_RELATIVE_RE = re.compile(
    r"\bmy\b[\w\s\-']{0,30}?\b(parent|mother|father|mom|dad|"
    r"grandchild|grandson|granddaughter|"
    r"sibling|brother|sister|niece|nephew|cousin|in-law)\b")
QUALIFYING_PERSON_COMPLEXITY_TERMS = {"qualifying relative", "disabled", "disability"}


# --- Fact 4: the child lived with the taxpayer more than half the year
# (183+ days, matching FTB Form 3532's own explicit day count). ---
_LIVED_TERMS = ("lived", "living", "live")


def _residency_more_than_half_year(q: str) -> bool:
    # unambiguous in this checklist's context, safe to accept anywhere.
    if "more than half the year" in q or "more than half of the year" in q:
        return True
    # "all year"/"the entire year"/"the whole year" are AMBIGUOUS -- the
    # SAME phrasing is also used for the marital-status duration fact
    # ("unmarried ALL YEAR"), so only count these near "lived"/"living"/
    # "live" (the child's residency clause) -- found via testing: a
    # question correctly stating the child lived with the taxpayer for
    # only 6 months (should FAIL residency) still passed, because
    # "unmarried all year" elsewhere in the SAME sentence was wrongly
    # counted as satisfying the child's residency too.
    for phrase in ("all year", "the entire year", "the whole year", "all year round"):
        start = 0
        while True:
            idx = q.find(phrase, start)
            if idx == -1:
                break
            window = q[max(0, idx - 40):idx + 40]
            if any(t in window for t in _LIVED_TERMS):
                return True
            start = idx + 1
    m = re.search(r"\b(\d{1,2})\s*months?\b", q)
    if m and int(m.group(1)) >= 7:
        return True
    m = re.search(r"\b(\d{2,3})\s*days?\b", q)
    if m and int(m.group(1)) > 183:
        return True
    return False


# --- Fact 5: the child's age/full-time-student test. ---
_AGE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:years?\s*old|yo|y/o|yrs?\.?\s*old)\b|"
    r"\bis\s+(\d{1,2})\b|\baged?\s+(\d{1,2})\b")


def _is_full_time_student(q: str) -> bool:
    """Negation-aware: a plain substring check for "full-time student"
    would also match inside "NOT a full-time student" -- found via
    testing, the same false-positive-substring class as the marital-
    status bug above, just for a negated phrase this time."""
    for phrase in ("full-time student", "full time student", "fulltime student"):
        idx = q.find(phrase)
        if idx == -1:
            continue
        prefix = q[max(0, idx - 15):idx]
        if "not" in prefix or "n't" in prefix:
            continue
        return True
    return False


def _age_test(q: str):
    """Returns True (passes), False (fails), or None (age not stated).
    Tries an explicit "N years old"/"yo" pattern first, then falls back
    to bare "is N"/"aged N" (a real, common phrasing -- "he is 16" --
    found missing via testing)."""
    m = _AGE_RE.search(q)
    if not m:
        return None
    age = int(next(g for g in m.groups() if g is not None))
    if age < 19:
        return True
    if age <= 23 and _is_full_time_student(q):
        return True
    return False


def _hoh_question_ok(q: str) -> bool:
    if not any(t in q for t in HOH_TRIGGER_TERMS):
        return False
    return any(t in q for t in HOH_QUESTION_TERMS)


def detect_hoh_determination(question: str):
    """Returns True (qualifies), False (a clean, simple disqualification --
    explicitly married all year, the one negative case verifiable without
    the full checklist), or None (not a determination this v1 can make --
    either not enough facts stated, or a fact pattern this v1 deliberately
    defers, like the considered-unmarried or qualifying-relative branches)."""
    q = question.lower()
    if not _hoh_question_ok(q):
        return None

    if _MARRIED_ALL_YEAR_RE.search(q) and not any(
            t in q for t in MARITAL_COMPLEXITY_TERMS):
        return False

    if any(t in q for t in MARITAL_COMPLEXITY_TERMS):
        return None
    if not any(t in q for t in UNMARRIED_TERMS):
        return None
    if any(t in q for t in QUALIFYING_PERSON_COMPLEXITY_TERMS):
        return None
    if _QUALIFYING_RELATIVE_RE.search(q):
        return None
    if not _OWN_CHILD_RE.search(q):
        return None
    if not any(t in q for t in HOME_COST_TERMS):
        return None
    if not _residency_more_than_half_year(q):
        return None
    # unlike the checks above (missing/ambiguous -> defer), an EXPLICITLY
    # failing age test is itself a clean, independent "no" once every
    # other fact already checks out -- it doesn't depend on any of the
    # deferred complexity branches. Only a genuinely MISSING age defers.
    age_ok = _age_test(q)
    if age_ok is None:
        return None
    return age_ok


def _any_personal_fact_stated(q: str) -> bool:
    """True iff the question shows at least one attempt at stating a
    personal fact (marital status, a relationship word, a home-cost
    claim, an age, or a residency duration) -- distinguishes "I'm trying
    to describe my situation but missing something" (worth a specific
    checklist nudge) from a bare "what are the requirements"/"am I
    eligible" question with ZERO personal facts (which should keep
    falling through to the existing informational topic, same as before
    this feature existed -- found via testing: without this guard, every
    vague HOH question got downgraded from a helpful "answered"
    informational response to "needs_review", a real regression)."""
    return (any(t in q for t in UNMARRIED_TERMS)
            or any(t in q for t in MARITAL_COMPLEXITY_TERMS)
            or bool(_OWN_CHILD_RE.search(q))
            or bool(_QUALIFYING_RELATIVE_RE.search(q))
            or any(t in q for t in HOME_COST_TERMS)
            or _age_test(q) is not None
            or _residency_more_than_half_year(q))


def detect_hoh_checklist_incomplete(question: str) -> bool:
    """True iff this looks like someone attempting an HOH determination
    (at least one personal fact stated) but detect_hoh_determination
    couldn't reach a verdict -- lets the caller give a specific checklist
    instead of a generic defer. A bare "am I eligible for HOH" with no
    personal facts at all does NOT count -- that should keep falling
    through to the informational topic, not get intercepted here."""
    q = question.lower()
    if not _hoh_question_ok(q):
        return False
    if not _any_personal_fact_stated(q):
        return False
    return detect_hoh_determination(question) is None
