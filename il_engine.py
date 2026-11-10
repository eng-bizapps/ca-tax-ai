"""Chat answers for the Illinois income-tax domain (Illinois Department of
Revenue, IL-1040). Deterministic and keyword-gated, same shape as
employer_engine: each answer function returns a result dict in the shared
shape or None. Every figure comes from il_income / the Illinois database; no
LLM composes a number.

Tax year: 2025 is the full computation year (the instructions read on
2026-11-01). 2026 is answered only for the facts IDOR has published (rate and
per-person exemption).
"""
import datetime
import re

import il_db
import il_income as ii
import income_brackets
import states

DEFAULT_YEAR = 2025
_AMOUNT_RE = re.compile(r"\$\s*([0-9](?:[0-9,]*[0-9])?(?:\.\d+)?)\s*(k\b)?|(?<![\d,.])([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?)")
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_STATUS_LABELS = {"single": "single", "mfj": "married filing jointly", "mfs": "married filing separately",
                  "hoh": "head of household", "qss": "qualifying surviving spouse"}


def _has(q, *terms):
    return any(t in q for t in terms)


def _result(text, category, tax=None, amount=None, status="answered", citation=None, source_url=None, info=None):
    base = states._base("IL", domain="income")
    base.update({"status": status, "answer_text": text, "category": category, "tax": tax, "amount": amount,
                 "citation": citation, "source_url": source_url, "info": info})
    return base


def _review(text, category):
    return _result(text, category, status="needs_review")


def _fmt(x):
    return f"${ii.money(x):,.2f}"


def _amounts(question):
    out = []
    for m in _AMOUNT_RE.finditer(question):
        raw = m.group(1) or m.group(3)
        v = float(raw.replace(",", ""))
        if m.group(2):
            v *= 1000
        out.append((v, m.start(), m.end()))
    return out


_ROLES = (
    ("ss", ("social security", "pension", "retirement", "ira", "401", "annuit")),
    ("property", ("property tax", "real estate tax")),
    ("education", ("education", "tuition", "school", "k-12", "k12")),
    ("eitc", ("eitc", "earned income")),
    ("income", ("income", "agi", "salary", "wages", "earn", "make", "made", "gross", "per year", "a year", "annually")),
)


def _role(q, start, end, roles=None):
    """What a dollar amount refers to: the nearest role keyword within 48 characters either side
    (the one right after the amount wins a tie)."""
    after, before = q[end:end + 48].lower(), q[max(0, start - 48):start].lower()
    after = re.split(r"[$,;.]|\band\b", after)[0]     # stop at the next amount or clause
    before = re.split(r"[$;,]|\band\b", before)[-1]    # start after the previous amount or clause
    best = None
    for role, words in (roles or _ROLES):
        for w in words:
            i = after.find(w)
            if i != -1 and (best is None or i < best[0]):
                best = (i, role)
            j = before.rfind(w)
            if j != -1:
                d = len(before) - (j + len(w)) + 0.5
                if best is None or d < best[0]:
                    best = (d, role)
    return best[1] if best else None


def _roles(question, roles=None):
    q = question.lower()
    found = {}
    unassigned = []
    for v, s, e in _amounts(question):
        role = _role(q, s, e, roles)
        if role and role not in found:
            found[role] = v
        else:
            unassigned.append(v)
    return found, unassigned


def _year(question):
    m = re.search(r"\b(20[0-9]{2})\b", question)
    return int(m.group(1)) if m else None


def _dependents(q):
    m = re.search(r"\b(\d+|an?|one|two|three|four|five|six)\s+(?:qualifying\s+)?(?:dependents?|children|kids|child)\b", q)
    if not m:
        return None
    t = m.group(1)
    return int(t) if t.isdigit() else _NUM_WORDS[t]


def _flags(q, filing_status):
    both = filing_status == "mfj" and _has(q, "both", "we are both", "my spouse and i are")
    age65 = (2 if both else 1) if re.search(r"\b65\b|\bsenior\b|\bover 65\b", q) else 0
    blind = (2 if both and "blind" in q else 1) if "blind" in q else 0
    return age65, blind


def _unsupported_year(year):
    return _review(f"I only have verified Illinois income-tax figures for tax year {DEFAULT_YEAR}, plus the 2026 rate and "
                   f"per-person exemption. I can't answer for {year} reliably.", "il_unsupported_year")


# ---------------------------------------------------------------- out of scope
_NOT_COVERED = (
    ("sales tax", "sales and use tax"), ("payroll tax", "employer payroll tax"), ("unemployment insurance", "employer payroll tax"),
    ("ides", "employer payroll tax"), ("withholding", "employer payroll tax"), ("property tax bill", "property tax"),
    ("assessed value", "property tax"), ("tax rate in chicago", "sales and use tax"),
)


def _not_covered_answer(question, q):
    if _has(q, "credit", "use tax", "nonresident", "non-resident", "part-year", "part year", "reciprocal",
            "penalty", "underpay", "estimated tax", "allowance", "il-w-4", "paycheck"):
        return None
    for term, domain in _NOT_COVERED:
        if re.search(r"\b" + re.escape(term) + r"\b", q):
            return _review(f"{domain.capitalize()} is not covered yet for Illinois. Currently covered: income tax.",
                           "domain_not_covered")
    return None


# ---------------------------------------------------------------- answers
def _use_tax_answer(conn, question, q):
    if "use tax" not in q:
        return None
    found, unassigned = _roles(question)
    agi = found.get("income") or (unassigned[0] if unassigned else None)
    if agi is None:
        return _review("To estimate Illinois use tax from the IL-1040 table, I need your adjusted gross income "
                       "(Line 1). With receipts, use the worksheet: 6.25% on general merchandise and 1% on "
                       "qualifying food, non-prescription drugs and medical appliances, less sales tax paid to "
                       "another state.", "il_use_tax_missing_agi")
    tax = ii.compute_use_tax(conn, DEFAULT_YEAR, agi)
    over = (f" Use tax over {_fmt(ii.param(conn, DEFAULT_YEAR, 'use_tax_st44_single'))} "
            f"({_fmt(ii.param(conn, DEFAULT_YEAR, 'use_tax_st44_joint'))} if married filing jointly) is reported on "
            f"Form ST-44 instead.")
    return _result(f"Estimated Illinois use tax for {_fmt(agi)} of adjusted gross income, from the IL-1040 Use Tax (UT) "
                   f"Table: {_fmt(tax)}, entered on Line 21. The table is for people with no major purchases and no "
                   f"receipts; with receipts, the worksheet (6.25% general, 1% food/drugs) is used instead.{over}",
                   "il_use_tax", tax=float(tax), citation="IDOR 2025 IL-1040 Instructions, Line 21 / UT Table",
                   source_url="https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-instr.pdf")


def _eitc_answer(conn, question, q):
    if not (_has(q, "eitc", "earned income") or "child tax credit" in q):
        return None
    found, unassigned = _roles(question)
    fed = found.get("eitc")
    if fed is None and len(unassigned) == 1 and "federal" in q:
        fed = unassigned[0]
    kids = _dependents(q)
    under12 = bool(re.search(r"under (?:the age of )?12|under twelve", q))
    if fed is None:
        parts = [f"Illinois EITC is {int(ii.param(conn, DEFAULT_YEAR, 'eitc_rate') * 100)}% of your federal EITC. "
                 f"The 2025 maximums IDOR publishes are: no qualifying children {_fmt(ii.eitc_max(conn, DEFAULT_YEAR, 0))}, "
                 f"1 child {_fmt(ii.eitc_max(conn, DEFAULT_YEAR, 1))}, 2 children {_fmt(ii.eitc_max(conn, DEFAULT_YEAR, 2))}, "
                 f"3 or more {_fmt(ii.eitc_max(conn, DEFAULT_YEAR, 3))}. With a dependent child under 12 you also get the "
                 f"Illinois Child Tax Credit, {int(ii.param(conn, DEFAULT_YEAR, 'child_credit_rate') * 100)}% of the "
                 f"Illinois EITC. Both are refundable. Tell me your federal EITC amount (Form 1040, Line 27a) and I'll "
                 f"compute your Illinois credit."]
        return _result(" ".join(parts), "il_eitc_info", citation="IDOR Schedule IL-E/EITC; EITC maximum credit page",
                       source_url="https://tax.illinois.gov/programs/eitc/max-credit.html", status="answered")
    r = ii.compute_eitc(conn, DEFAULT_YEAR, fed, child_under_12=under12)
    text = (f"With a federal EITC of {_fmt(fed)}, your Illinois EITC is {_fmt(r['eitc'])} (20% of the federal credit, "
            f"Schedule IL-E/EITC Lines 7-9, entered on Form IL-1040 Line 29).")
    total = r["eitc"]
    if under12:
        text += (f" With a dependent child under 12 on December 31, the Illinois Child Tax Credit adds "
                 f"{_fmt(r['child_credit'])} (40% of the Illinois EITC, Line 30), for a total of "
                 f"{_fmt(r['eitc'] + r['child_credit'])}.")
        total = r["eitc"] + r["child_credit"]
    text += " Both credits are refundable. This assumes an Illinois resident for the full year."
    return _result(text, "il_eitc", amount=float(total), citation="IDOR 2025 Schedule IL-E/EITC",
                   source_url="https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-schedule-il-e-eic.pdf")


def _k12_answer(conn, question, q):
    if not (_has(q, "k-12", "k12", "education expense", "school tuition", "private school", "tuition")
            and _has(q, "credit", "expense")):
        return None
    found, unassigned = _roles(question)
    expenses = found.get("education") or (unassigned[0] if unassigned else None)
    status = income_brackets.detect_filing_status(question)
    agi = found.get("income")
    if expenses is None:
        return _review("The Illinois K-12 education expense credit is 25% of qualified expenses over the first $250, "
                       "capped at $750 per return. Tell me the amount you paid in qualified tuition, book-rental and "
                       "lab fees and I'll compute it.", "il_k12_missing_expenses")
    if agi is not None and agi > 250000 and status is None:
        return _review("The credit isn't allowed above $250,000 of AGI ($500,000 if married filing jointly). Tell me your "
                       "filing status.", "il_k12_missing_status")
    r = ii.compute_k12_credit(conn, DEFAULT_YEAR, status or "single", agi or 0, expenses)
    if r["cutoff_applied"]:
        return _result(f"No K-12 education expense credit: your AGI is over the limit "
                       f"({_fmt(ii.agi_limit(conn, DEFAULT_YEAR, status or 'single'))}).", "il_k12_credit", amount=0.0)
    over = max(0, expenses - 250)
    return _result(f"Illinois K-12 education expense credit on {_fmt(expenses)} of qualified expenses: "
                   f"25% x ({_fmt(expenses)} - $250.00) = {_fmt(ii.money(over * 0.25))}, capped at $750.00, so "
                   f"{_fmt(r['credit'])}. It is nonrefundable (limited to your tax due) and not allowed above "
                   f"$250,000 of AGI ($500,000 if married filing jointly). Only tuition, required book-rental and lab "
                   f"fees count; daycare, tutoring and after-school care do not.",
                   "il_k12_credit", amount=float(r["credit"]), citation="IDOR 2025 Schedule ICR Section B",
                   source_url="https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-schedule-icr.pdf")


def _property_credit_answer(conn, question, q):
    if not ("property tax" in q and "credit" in q):
        return None
    found, unassigned = _roles(question)
    prop = found.get("property")
    agi = found.get("income")
    status = income_brackets.detect_filing_status(question)
    if prop is None:
        return _review("The Illinois property tax credit is 5% of the property tax you paid on your Illinois principal "
                       "residence. Tell me the amount of property tax and I'll compute it.", "il_property_missing_tax")
    if agi is not None and agi > 250000 and status is None:
        return _review("The credit isn't allowed above $250,000 of AGI ($500,000 if married filing jointly). Tell me your "
                       "filing status.", "il_property_missing_status")
    tax_avail = None
    note = " It is nonrefundable, so it can't exceed your Illinois tax due."
    if agi is not None and status is not None:
        t = ii.compute_income_tax(conn, DEFAULT_YEAR, status, agi, dependents=_dependents(q) or 0,
                                  age65=_flags(q, status)[0], blind=_flags(q, status)[1])
        tax_avail = t["tax"]
        note = f" Your Illinois tax before credits is {_fmt(tax_avail)}, so the credit is limited to that."
    r = ii.compute_property_credit(conn, DEFAULT_YEAR, status or "single", agi or 0, prop, tax_avail)
    if r["cutoff_applied"]:
        return _result("No property tax credit: your AGI is over the limit.", "il_property_credit", amount=0.0)
    return _result(f"Illinois property tax credit: 5% x {_fmt(prop)} = {_fmt(r['computed'])}"
                   + (f", limited to {_fmt(r['credit'])}." if r["credit"] != r["computed"] else ".")
                   + note + " It applies to the principal residence only (not a vacation home or rental), for property tax "
                   "paid in 2025 that covers the time you owned and lived there during 2024, and isn't allowed above "
                   "$250,000 of AGI ($500,000 if married filing jointly).",
                   "il_property_credit", amount=float(r["credit"]), citation="IDOR 2025 Schedule ICR Section A",
                   source_url="https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-schedule-icr.pdf")


def _exemption_answer(conn, question, q):
    if "exemption" not in q or _has(q, "how much tax", "tax do i owe", "tax would i owe", "what is my tax") or _amounts(question):
        return None
    year = _year(question) or DEFAULT_YEAR
    if year not in (2025, 2026):
        return _unsupported_year(year)
    per = ii.param(conn, year, "exemption_per_person")
    text = (f"For tax year {year} the Illinois exemption allowance is {_fmt(per)} per exemption "
            f"({_fmt(per * 2)} on a joint return)")
    if year == 2025:
        text += (f", plus $1,000 for each of you and your spouse who is 65 or older or legally blind, plus "
                 f"{_fmt(ii.param(conn, 2025, 'dependent_exemption'))} per dependent.")
    else:
        text += ", plus $1,000 for each of you and your spouse who is 65 or older or legally blind."
    text += (" It is not allowed if federal AGI is over $250,000 ($500,000 if married filing jointly). "
             "If someone else can claim you as a dependent, your allowance is the base amount only when your Illinois "
             "income is no more than that amount, otherwise $0.")
    return _result(text, "il_exemption", amount=float(per), citation="IDOR 2025 IL-1040 Instructions, Step 4",
                   source_url="https://tax.illinois.gov/questionsandanswers/answer.851.html")


def _rate_answer(conn, question, q):
    if not ("rate" in q and "tax" in q) or _amounts(question) or _has(q, "use tax", "sales", "property"):
        return None
    year = _year(question) or DEFAULT_YEAR
    if year not in (2025, 2026):
        return _unsupported_year(year)
    rate = ii.param(conn, year, "rate")
    return _result(f"Illinois taxes individual net income at a flat {rate * 100:.2f}% for tax year {year}, with no "
                   f"brackets. Net income is your federal AGI adjusted by Illinois additions and subtractions, less the "
                   f"exemption allowance.", "il_rate", citation="IDOR 2025 IL-1040 Instructions",
                   source_url="https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-instr.pdf")


def _retirement_info_answer(question, q):
    if _amounts(question) or not _has(q, "social security", "pension", "retirement", "ira", "401"):
        return None
    if not _has(q, "taxable", "tax ", "taxed", "subtract", "exempt"):
        return None
    return _result("Illinois lets you subtract federally taxed Social Security and railroad retirement benefits and "
                   "qualified retirement income (qualified employer plans including 401(k), IRAs and self-employed "
                   "plans, and government retirement and deferred-compensation plans) from your income on Form IL-1040, "
                   "Line 5, so they aren't taxed by Illinois. Publication 120 lists exactly which plans qualify. If you "
                   "give me the amount and the rest of your income I can compute the tax.",
                   "il_retirement_income", citation="IDOR 2025 IL-1040 Instructions, Line 5",
                   source_url="https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-instr.pdf")


def _compute_answer(conn, question, q):
    if not (_has(q, "income tax", "tax do i owe", "tax would i owe", "how much tax", "tax on", "what is my tax", "owe")
            and _amounts(question)):
        return None
    year = _year(question)
    if year is not None and year != DEFAULT_YEAR:
        return _unsupported_year(year)
    found, unassigned = _roles(question)
    agi = found.get("income")
    if agi is None and unassigned and not found:
        agi = unassigned[0]
    if agi is None and unassigned:
        agi = unassigned[0]
    status = income_brackets.detect_filing_status(question)
    if agi is None:
        return _review("Tell me your federal adjusted gross income (or total income) and I'll compute your Illinois tax.",
                       "il_compute_missing_income")
    if status is None:
        return _review("I need your filing status (single, married filing jointly, married filing separately, head of "
                       "household) to compute your Illinois tax, because the exemption allowance depends on it.",
                       "il_compute_missing_status")
    dependent_of_other = bool(re.search(r"(claimed|claims?) (?:me )?as a dependent|someone (?:else )?can claim me", q))
    if dependent_of_other and status == "mfj":
        return _review("A joint return where one or both spouses can be claimed as a dependent uses a different exemption "
                       "chart that I don't compute yet.", "il_compute_unsupported")
    deps = _dependents(q) or 0
    age65, blind = _flags(q, status)
    ss = found.get("ss", 0)
    r = ii.compute_income_tax(conn, DEFAULT_YEAR, status, agi, ss_retirement=ss, dependents=deps, age65=age65,
                              blind=blind, claimed_as_dependent=dependent_of_other)
    ex = r["exemption"]
    lines = [f"Illinois income tax for {DEFAULT_YEAR}, {_STATUS_LABELS[status]}, federal AGI {_fmt(agi)}:"]
    if ss:
        lines.append(f"- Less Social Security/retirement income subtracted (Line 5): {_fmt(ss)}")
    lines.append(f"- Base income: {_fmt(r['base_income'])}")
    if ex["cutoff_applied"]:
        lines.append(f"- Exemption allowance: $0.00 (AGI is over {_fmt(ii.agi_limit(conn, DEFAULT_YEAR, status))})")
    else:
        parts = [f"{_fmt(ex['base'])} base"]
        if ex["extras"]:
            parts.append(f"{_fmt(ex['extras'])} for age 65+/blind")
        if ex["dependents"]:
            parts.append(f"{_fmt(ex['dependents'])} for {deps} dependent(s)")
        lines.append(f"- Exemption allowance: {_fmt(ex['total'])} ({', '.join(parts)})")
    lines.append(f"- Net income: {_fmt(r['net_income'])}")
    lines.append(f"- Tax at {r['rate'] * 100:.2f}%: {_fmt(r['tax'])}")
    lines.append("This assumes an Illinois resident for the full year and no other Schedule M additions or subtractions, "
                 "and is before credits (property tax, K-12, other-state) and payments.")
    return _result("\n".join(lines), "il_income_compute", tax=float(r["tax"]),
                   citation="IDOR 2025 IL-1040 Instructions, Steps 2-5",
                   source_url="https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-instr.pdf")



# ---------------------------------------------------------------- Schedule CR / Schedule NR
_STATE_NAMES = ("alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
                "georgia", "hawaii", "idaho", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
                "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
                "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
                "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee",
                "texas", "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming")
_FOREIGN = ("canada", "mexico", "united kingdom", "england", "germany", "france", "india", "china", "japan", "australia",
            "ireland", "italy", "spain", "brazil", "philippines", "israel")

_CR_ROLES = (
    ("tax", ("income tax to", "tax to ", "taxes to ", "in income tax", "in state tax", "in tax", "tax paid", "paid tax")),
    ("nonil", ("earned in", "earned there", "earned out", "other state income", "out-of-state", "out of state",
               "wages in ", "working in ", "worked in ")),
    ("income", ("agi", "total income", "salary", "income of", "of income", "gross", "make", "made", "earn")),
)
_NR_ROLES = (
    ("il", ("in illinois", "from illinois", "illinois income", "illinois wages", "illinois source", "illinois-source",
            "earned in illinois", "worked in illinois", "illinois portion")),
    ("total", ("total income", "total", "agi", "all sources", "overall", "federal")),
)
_CR_URL = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-schedule-cr-instr.pdf"
_NR_URL = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-schedule-nr-instr.pdf"


def _named_states(q):
    return [n for n in _STATE_NAMES if re.search(r"\b" + n + r"\b", q)]


def _mentions_foreign(q):
    return [c for c in _FOREIGN if re.search(r"\b" + c + r"\b", q)]


def _cr_answer(conn, question, q):
    if not ("schedule cr" in q or ("credit" in q and _has(
            q, "other state", "another state", "tax paid to", "taxes paid to", "paid tax to", "out-of-state", "out of state"))):
        return None
    if _has(q, "nonresident", "non-resident"):
        return _result("A nonresident of Illinois cannot take Schedule CR (credit for tax paid to other states); it is only "
                       "for Illinois residents and part-year residents, for income earned while an Illinois resident.",
                       "il_other_state_credit_rules", citation="IDOR 2025 Schedule CR Instructions", source_url=_CR_URL)
    foreign = _mentions_foreign(q)
    states_named = _named_states(q)
    if foreign and not states_named:
        return _result("No Illinois credit is allowed for income tax paid to a foreign country (or to the federal "
                       "government). Schedule CR covers only taxes paid to another U.S. state, D.C., Puerto Rico, a "
                       "U.S. territory, or their local governments. (Foreign tax may be creditable on your federal "
                       "return instead.)", "il_other_state_credit_rules", amount=0.0,
                       citation="IDOR 2025 Schedule CR Instructions", source_url=_CR_URL)
    wages = _has(q, "wage", "salary", "salaries", "paycheck", "employer", "working", "worked", "work in", "tips")
    recip = ii.reciprocal_states(conn)
    if states_named and all(n in recip for n in states_named) and wages and "city" not in q and "local" not in q:
        names = ", ".join(n.title() for n in states_named)
        return _result(f"No credit: Illinois has a reciprocal agreement with {names}, so wages and other employee "
                       f"compensation you earn there while an Illinois resident are not taxed by that state. If tax was "
                       f"withheld by that state in error, you claim a refund from that state, not a credit on Schedule CR. "
                       f"(A city or county tax on those wages can still be credited, and so can tax on other kinds of "
                       f"income such as rent or business income.)", "il_other_state_credit_reciprocal", amount=0.0,
                       citation="IDOR 2025 Schedule CR Instructions", source_url=_CR_URL)
    if _has(q, "part-year", "part year", "moved out of illinois", "moved to illinois", "moved into illinois"):
        return _cr_part_year(conn, question, q)
    found, unassigned = _roles(question, _CR_ROLES)
    agi, nonil, paid = found.get("income"), found.get("nonil"), found.get("tax")
    status = income_brackets.detect_filing_status(question)
    missing = [label for label, v in (("your total income (federal AGI)", agi),
                                      ("how much of that income was earned in the other state", nonil),
                                      ("the income tax you paid to the other state", paid),
                                      ("your filing status", status)) if v is None]
    if missing:
        return _review("The Illinois credit for tax paid to other states is the smaller of (a) the tax you paid the other "
                       "state and (b) your Illinois tax multiplied by the share of your income taxed by the other state "
                       "(non-Illinois income divided by base income, to three decimals). To compute it I still need: "
                       + "; ".join(missing) + ". Interest, dividends, pensions and IRA distributions are Illinois "
                       "income, so only wages, out-of-state rent, business income and similar count as non-Illinois income. "
                       "No credit for foreign tax; wages from Iowa, Kentucky, Michigan or Wisconsin are covered by a "
                       "reciprocal agreement instead.", "il_other_state_credit_missing")
    deps = _dependents(q) or 0
    age65, blind = _flags(q, status)
    t = ii.compute_income_tax(conn, DEFAULT_YEAR, status, agi, dependents=deps, age65=age65, blind=blind)
    r = ii.compute_other_state_credit(conn, DEFAULT_YEAR, t["tax"], t["base_income"], nonil, paid)
    after = t["tax"] - r["credit"]
    return _result(
        f"Illinois credit for tax paid to other states (Schedule CR), {_STATUS_LABELS[status]}, full-year Illinois resident:\n"
        f"- Illinois tax before credits: {_fmt(t['tax'])} (base income {_fmt(t['base_income'])})\n"
        f"- Decimal: {_fmt(nonil)} non-Illinois income / {_fmt(t['base_income'])} = {r['decimal']}\n"
        f"- Limit: {_fmt(t['tax'])} x {r['decimal']} = {_fmt(r['limit'])}\n"
        f"- Tax paid to the other state: {_fmt(paid)}\n"
        f"- Credit (the lesser, Form IL-1040 Line 15): {_fmt(r['credit'])}\n"
        f"Your Illinois tax after this credit would be {_fmt(after)}, before other credits. You must have filed the other "
        f"state's required return; use the tax shown on that return, not the amount withheld on your W-2.",
        "il_other_state_credit", amount=float(r["credit"]), tax=float(after),
        citation="IDOR 2025 Schedule CR (R-12/25) Steps 4 and 6", source_url=_CR_URL.replace("-instr", ""))


_CR_PY_ROLES = (
    ("tax", _CR_ROLES[0][1]),
    ("nonil", _CR_ROLES[1][1]),
    ("resident", ("while i lived in illinois", "while an illinois resident", "while a resident", "as an illinois resident",
                  "while i was a resident", "resident period", "lived in illinois")),
    ("income", ("agi", "total income", "income of", "of income", "gross", "make", "made")),
)


def _cr_part_year(conn, question, q):
    found, unassigned = _roles(question, _CR_PY_ROLES)
    total, resident, nonil, paid = found.get("income"), found.get("resident"), found.get("nonil"), found.get("tax")
    status = income_brackets.detect_filing_status(question)
    missing = [label for label, v in (("your total income for the whole year (federal AGI)", total),
                                      ("how much income you received while an Illinois resident", resident),
                                      ("how much of that was earned in the other state", nonil),
                                      ("the income tax you paid to the other state", paid),
                                      ("your filing status", status)) if v is None]
    if missing:
        return _review("A part-year resident's credit for tax paid to other states only covers income received while an "
                       "Illinois resident. To compute it I still need: " + "; ".join(missing) + ".",
                       "il_other_state_credit_part_year_missing")
    deps = _dependents(q) or 0
    age65, blind = _flags(q, status)
    r = ii.compute_part_year_other_state_credit(conn, DEFAULT_YEAR, status, total, resident, nonil, paid, deps, age65, blind)
    return _result(
        f"Illinois credit for tax paid to other states (Schedule CR), part-year resident, {_STATUS_LABELS[status]}:\n"
        f"- Decimal (Step 4): {_fmt(nonil)} non-Illinois / {_fmt(resident)} resident-period income = {r['decimal_credit']}\n"
        f"- Exemption allowance prorated (Step 5): {_fmt(r['full_exemption'])} x ({_fmt(resident)} / {_fmt(total)} = "
        f"{r['decimal_exemption']}) = {_fmt(r['exemption'])}\n"
        f"- Illinois tax on resident-period income: ({_fmt(resident)} - {_fmt(r['exemption'])}) x 4.95% = {_fmt(r['il_tax'])}\n"
        f"- Limit: {_fmt(r['il_tax'])} x {r['decimal_credit']} = {_fmt(r['limit'])}\n"
        f"- Tax paid to the other state: {_fmt(paid)}\n"
        f"- Credit (the lesser, Form IL-1040 Line 15): {_fmt(r['credit'])}\n"
        f"Only income received while you were an Illinois resident counts, and you must have filed the other state's "
        f"required return.", "il_other_state_credit_part_year", amount=float(r["credit"]),
        citation="IDOR 2025 Schedule CR (R-12/25) Steps 4-6", source_url=_CR_URL.replace("-instr", ""))


# ---------------------------------------------------------------- employee withholding (IL-W-4)
_WH_FREQ = (("biweekly", ("biweekly", "bi-weekly", "every two weeks", "every other week")),
            ("semimonthly", ("semimonthly", "semi-monthly", "twice a month", "twice monthly")),
            ("weekly", ("weekly", "every week", "each week", "a week", "per week")),
            ("monthly", ("monthly", "every month", "each month", "a month", "per month")))
_WH_URL = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/withholding/documents/currentyear/il-700-t.pdf"


def _wh_count(q, pattern):
    m = re.search(pattern, q)
    if not m:
        return None
    t = m.group(1)
    return int(t) if t.isdigit() else _NUM_WORDS.get(t, 0)


def _withholding_answer(conn, question, q):
    if not (_has(q, "withhold", "withheld", "il-w-4") and _has(q, "allowance", "il-w-4", "paycheck", "per pay", "each pay")):
        return None
    freq = next((f for f, words in _WH_FREQ if _has(q, *words)), None)
    word = r"(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten)"
    additional = _wh_count(q, word + r"\s+additional\s+allowances?")
    q_basic = re.sub(word + r"\s+additional\s+allowances?", " ", q)
    basic = _wh_count(q_basic, word + r"\s+(?:basic\s+)?allowances?")
    extra_m = re.search(r"(?:extra|additional)\s+\$\s*([0-9][0-9,]*(?:\.\d+)?)", q)
    extra = float(extra_m.group(1).replace(",", "")) if extra_m else 0.0
    exempt = bool(re.search(r"\bexempt\b", q)) and "withholding" in q
    amts = [a for a in _amounts(question) if not (extra_m and abs(a[0] - extra) < 1e-9 and a[1] >= extra_m.start())]
    if exempt:
        r = ii.compute_il_withholding(conn, 0, freq or "weekly", exempt=True, extra_withholding=extra)
        return _result("If you claim exempt on Form IL-W-4 (only for residents of Iowa, Kentucky, Michigan or Wisconsin "
                       "via IL-W-5-NR, or who otherwise qualify), no Illinois income tax is withheld from your pay "
                       f"(only any extra amount you request: {_fmt(r['withholding'])}).", "il_withholding",
                       amount=float(r["withholding"]), citation="IDOR IL-W-4 instructions", source_url=_WH_URL)
    if freq is None or not amts:
        return _review("To compute Illinois withholding from a paycheck I need the pay amount, how often you're paid "
                       "(weekly, biweekly, semimonthly or monthly) and the allowances on your Form IL-W-4 (Line 1 basic, "
                       "Line 2 additional). The formula is 4.95% x (pay - (Line 1 x $2,925 + Line 2 x $1,000) / pay "
                       "periods per year) plus any extra amount on Line 3.", "il_withholding_missing")
    periods = ii.WH_PERIODS[freq]
    gross, a_s, a_e = amts[0]
    window = q[a_e:a_e + 25]
    annual = bool(re.search(r"a year|per year|annual|salary|yearly", window)) or bool(
        re.search(r"salary|annual", q[max(0, a_s - 25):a_s]))
    wages = gross / periods if annual else gross
    r = ii.compute_il_withholding(conn, wages, freq, basic or 0, additional or 0, extra)
    lines = [f"Illinois withholding, 2026, {freq} pay: wages {_fmt(wages)}" + (f" (from {_fmt(gross)} a year)" if annual else "")
             + f", Line 1 allowances {basic or 0}, Line 2 allowances {additional or 0}:",
             f"- Exemption per paycheck: ({basic or 0} x $2,925 + {additional or 0} x $1,000) / {periods} = "
             f"{_fmt(r['exemption_this_period'])}",
             f"- Taxable wages: {_fmt(r['taxable'])}",
             f"- Withholding at 4.95%" + (f" plus {_fmt(extra)} extra" if extra else "") + f": {_fmt(r['withholding'])}",
             "This is IDOR's automated payroll method; its printed tables use wage-band midpoints, so a table lookup can "
             "differ by a few cents."]
    return _result("\n".join(lines), "il_withholding", amount=float(r["withholding"]),
                   citation="IDOR 2026 Booklet IL-700-T (automated payroll method)", source_url=_WH_URL)


def _nr_answer(conn, question, q):
    if not (_has(q, "nonresident", "non-resident", "part-year", "part year", "schedule nr", "work in illinois",
                 "works in illinois", "commute to illinois")
            or re.search(r"\blive in (?:" + "|".join(_STATE_NAMES) + r")", q)
            or re.search(r"\b(?:moved|moving) (?:out of|to|from|into) illinois", q)):
        return None
    if not _has(q, "tax", "owe", "file"):
        return None
    states_named = _named_states(q)
    recip = ii.reciprocal_states(conn)
    lives_recip = re.search(r"\blive in (iowa|kentucky|michigan|wisconsin)\b", q) or (
        _has(q, "nonresident", "non-resident") and any(n in recip for n in states_named))
    wages_only = _has(q, "wage", "salary", "salaries", "paycheck", "employer", "work in illinois", "works in illinois")
    if lives_recip and wages_only and not _has(q, "rent", "business", "self-employ"):
        return _result("No Illinois income tax on those wages. Illinois has a reciprocal agreement with Iowa, Kentucky, "
                       "Michigan and Wisconsin: if you live in one of those states and work in Illinois, your "
                       "salary, wages, tips and other employee compensation are exempt from Illinois income tax. Give your "
                       "employer Form IL-W-5-NR so Illinois tax isn't withheld; if it already was, file Form IL-1040 with "
                       "Schedule NR to get it back. Other Illinois-source income (such as Illinois rental or business income) "
                       "is still taxed.", "il_nonresident_reciprocal", amount=0.0, tax=0.0,
                       citation="IDOR 2025 Schedule NR Instructions", source_url=_NR_URL)
    found, unassigned = _roles(question, _NR_ROLES)
    total, il_part = found.get("total"), found.get("il")
    if total is None and il_part is not None and len(unassigned) == 1:
        total = unassigned[0]
    if il_part is None and total is not None and len(unassigned) == 1:
        il_part = unassigned[0]
    status = income_brackets.detect_filing_status(question)
    missing = [label for label, v in (("your total income (federal AGI)", total),
                                      ("how much of it is Illinois income (earned while an Illinois resident, plus "
                                       "Illinois-source income while a nonresident)", il_part),
                                      ("your filing status", status)) if v is None]
    if missing:
        return _review("Illinois taxes a nonresident only on Illinois-source income, and a part-year resident on everything "
                       "earned while a resident plus Illinois-source income after. The tax is 4.95% of the Illinois income "
                       "less your exemption allowance scaled by (Illinois income / total income). To compute it I still need: "
                       + "; ".join(missing) + ". Nonresidents are not taxed on interest, dividends, pensions, IRA "
                       "distributions or Social Security (unless from an Illinois business), and wages count only for work "
                       "performed in Illinois.", "il_nonresident_missing")
    deps = _dependents(q) or 0
    age65, blind = _flags(q, status)
    r = ii.compute_nonresident_tax(conn, DEFAULT_YEAR, status, total, il_part, dependents=deps, age65=age65, blind=blind)
    lines = [f"Illinois income tax (Schedule NR) for {DEFAULT_YEAR}, {_STATUS_LABELS[status]}, total income "
             f"{_fmt(total)}, of which Illinois income {_fmt(il_part)}:",
             f"- Illinois base income: {_fmt(r['il_base'])}",
             f"- Decimal: {_fmt(r['il_base'])} / {_fmt(r['total_base'])} = {r['decimal']}"]
    if r["cutoff_applied"]:
        lines.append("- Exemption allowance: $0.00 (total AGI is over the cutoff)")
    else:
        lines.append(f"- Exemption allowance: {_fmt(r['full_exemption'])} x {r['decimal']} = {_fmt(r['exemption'])}")
    lines += [f"- Illinois net income: {_fmt(r['net_income'])}",
              f"- Tax at 4.95%: {_fmt(r['tax'])}",
              "This uses the Illinois income figure you gave; it does not re-source each line of your return (wages by days "
              "worked in Illinois, the business apportionment worksheet, partnership K-1s), and assumes no Schedule M "
              "adjustments. Illinois withholding already paid is not subtracted."]
    return _result("\n".join(lines), "il_nonresident", tax=float(r["tax"]),
                   citation="IDOR 2025 Schedule NR (R-12/25) Step 5", source_url=_NR_URL.replace("-instr", ""))


# ---------------------------------------------------------------- Penalties (Form IL-2210)
_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
           "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
           "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
    re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
               re.IGNORECASE),
)
_PEN_ROLES = (
    ("prior", ("last year", "prior year", "2024")),
    ("withheld", ("withheld", "withholding")),
    ("now", ("this year", "2025", "tax after credits", "my tax", "tax liability", "after credits")),
)
_IL2210_URL = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-2210.pdf"


def _dates(question):
    """[(date, start, end)] for ISO, MM/DD/YYYY and 'Month D, YYYY' dates."""
    out = []
    for i, pat in enumerate(_DATE_PATTERNS):
        for m in pat.finditer(question):
            try:
                if i == 0:
                    d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif i == 1:
                    d = datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                else:
                    d = datetime.date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            except ValueError:
                continue
            out.append((d, m.start(), m.end()))
    return sorted(out, key=lambda x: x[1])


def _mask(question, spans):
    chars = list(question)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def _payments(question):
    """Estimated payments written as '$4,000 ... on <date>': [(date, amount)] plus the spans to mask."""
    pays, spans = [], []
    amts = _amounts(question)
    for d, ds, de in _dates(question):
        prior = [a for a in amts if a[2] <= ds and ds - a[2] <= 45]
        if prior:
            v, a_s, a_e = prior[-1]
            pays.append((d, v))
            spans.append((a_s, de))
    return pays, spans


def _penalty_rules_answer():
    return _result(
        "Illinois penalties on Form IL-2210: (1) Underpayment of estimated tax. You owe none if your tax after withholding "
        "and credits is $1,000 or less, if last year's tax was zero, if you weren't required to file last year, if you are a "
        "farmer (two-thirds of gross income from farming), or if you are 65+ and live permanently in a nursing home. "
        "Otherwise the penalty is waived if you timely paid the lesser of 100% of last year's tax or 90% of this year's, in "
        "four equal installments (April 15, June 15, September 15 and January 15). Late installments are charged 2% if "
        "paid 1-30 days late and 10% if 31 or more days late or never paid. (2) Late payment of the tax you owe at filing: "
        "same 2% / 10% rates, counted from the original due date (April 15, 2026 for 2025). An extension to file does not "
        "extend the time to pay. (3) Late filing: only if you file after the extended due date (October 15, 2026) and the "
        "tax wasn't paid by April 15; the lesser of $250 or 2% of the unpaid tax. You can leave Line 34 blank and let IDOR "
        "figure the penalty and bill you. Give me your 2025 tax after credits, last year's tax, your withholding and any "
        "estimated payments (with dates) and I'll compute the underpayment penalty.",
        "il_penalty_rules", citation="IDOR 2025 Form IL-2210 Instructions", source_url=_IL2210_URL)


def _penalty_answer(conn, question, q):
    if "penalt" not in q and "il-2210" not in q:
        return None
    late_filing = _has(q, "late filing", "late-filing", "filed late", "file late", "filing late", "after the extended")
    late_pay = _has(q, "late payment", "late-payment", "paid late", "pay late", "paying late", "paid it on", "paid on")
    under = _has(q, "underpay", "estimated tax", "il-2210", "estimated payment")
    dates = _dates(question)
    found = _amounts(question)
    if not found and not dates:
        return _penalty_rules_answer()
    pays, spans = _payments(question)

    if late_filing and not under:
        filed = dates[0][0] if dates else None
        masked = _mask(question, [(a, b) for _, a, b in dates])
        amts = _amounts(masked)
        if filed is None or not amts:
            return _review("To compute the Illinois late-filing penalty I need the date you filed and how much tax was still "
                           "unpaid after the original due date (April 15, 2026 for 2025). The penalty applies only if you "
                           "file after the extended due date (October 15, 2026): the lesser of $250 or 2% of that unpaid "
                           "tax.", "il_penalty_missing")
        r = ii.compute_late_filing_penalty(conn, DEFAULT_YEAR, amts[0][0], filed)
        if r["penalty"] == 0:
            return _result(f"No Illinois late-filing penalty: it applies only if you file after the extended due date "
                           f"({r['extended_due']:%B %d, %Y}) and the tax wasn't paid by the original due date. "
                           f"(Late payment of the tax is penalized separately.)", "il_penalty_late_filing", amount=0.0,
                           citation="IDOR 2025 Form IL-2210, Step 5", source_url=_IL2210_URL)
        return _result(f"Illinois late-filing penalty: 2% x {_fmt(amts[0][0])} unpaid tax = {_fmt(r['two_percent'])}; the "
                       f"penalty is the lesser of that and $250.00, so {_fmt(r['penalty'])}. It applies because you filed "
                       f"after the extended due date ({r['extended_due']:%B %d, %Y}) and the tax wasn't paid by April 15, "
                       f"2026. This does not include the late-payment penalty on the unpaid tax, or the additional penalty "
                       f"IDOR can assess if you don't file within 30 days of a notice.",
                       "il_penalty_late_filing", amount=float(r["penalty"]), citation="IDOR 2025 Form IL-2210, Step 5",
                       source_url=_IL2210_URL)

    if late_pay and not under:
        paid = dates[0][0] if dates else None
        masked = _mask(question, [(a, b) for _, a, b in dates])
        amts = _amounts(masked)
        if not amts:
            return _review("To compute the Illinois late-payment penalty on tax you owed at filing I need the unpaid amount "
                           "and the date you paid it. The rate is 2% if paid 1-30 days after April 15, 2026 and 10% if "
                           "31 or more days after.", "il_penalty_missing")
        r = ii.compute_unpaid_tax_penalty(conn, DEFAULT_YEAR, amts[0][0], paid)
        if paid is None:
            when = "never paid (10% applies)"
        elif r["days"] <= 0:
            when = f"paid on time ({paid:%B %d, %Y})"
        else:
            when = f"paid {r['days']} days after the original due date of {r['due']:%B %d, %Y} ({paid:%B %d, %Y})"
        return _result(f"Illinois late-payment penalty on {_fmt(amts[0][0])} of unpaid tax, {when}: rate "
                       f"{r['rate'] * 100:.0f}% (2% for 1-30 days late, 10% for 31 or more) = {_fmt(r['penalty'])}. An "
                       f"extension to file doesn't extend the time to pay. This is the Penalty Worksheet 2 penalty only.",
                       "il_penalty_late_payment", amount=float(r["penalty"]),
                       citation="IDOR 2025 Form IL-2210, Penalty Worksheet 2", source_url=_IL2210_URL)

    if not under and not (late_filing or late_pay):
        return _penalty_rules_answer()

    masked = _mask(question, spans + [(a, b) for _, a, b in dates])
    roles, unassigned = _roles(masked, _PEN_ROLES)
    now, prior, withheld = roles.get("now"), roles.get("prior"), roles.get("withheld")
    exempt = None
    if _has(q, "farmer", "two-thirds", "farming income"):
        exempt = "a farmer (at least two-thirds of gross income from farming)"
    elif "nursing home" in q:
        exempt = "65 or older and permanently living in a nursing home"
    elif re.search(r"(?:not|n't) required to file (?:last year|for 2024|a 2024)", q):
        exempt = "not required to file an Illinois return last year"
    if exempt:
        return _result(f"No Illinois underpayment-of-estimated-tax penalty: you are {exempt}, which exempts you "
                       f"(Form IL-1040 Line 34a, 34b or 34d). Late payment of any tax you owe at filing is penalized "
                       f"separately.", "il_penalty_underpayment", amount=0.0, citation="IDOR 2025 Form IL-2210 Instructions",
                       source_url=_IL2210_URL)
    if withheld is None and "withh" not in q:
        withheld = 0.0
    missing = [label for label, v in (("your 2025 Illinois tax after credits", now), ("last year's (2024) tax after credits", prior))
               if v is None]
    if missing:
        return _review("To compute the Illinois underpayment-of-estimated-tax penalty I still need: " + "; ".join(missing)
                       + ". Also helpful: the Illinois tax withheld this year and any estimated payments with their dates.",
                       "il_penalty_missing")
    withheld = withheld or 0.0
    r = ii.compute_underpayment_penalty(conn, DEFAULT_YEAR, now, prior, withheld, pays)
    head = (f"Illinois underpayment-of-estimated-tax penalty, 2025 (IL-2210 regular method): this year's tax after credits "
            f"{_fmt(now)}, last year's {_fmt(prior)}, withholding {_fmt(withheld)}.")
    if r["reason"] == "under_threshold":
        return _result(f"{head}\nNo penalty: your tax after withholding ({_fmt(r['line5'])}) is $1,000 or less.",
                       "il_penalty_underpayment", amount=0.0, citation="IDOR 2025 Form IL-2210, Step 2 Line 7",
                       source_url=_IL2210_URL)
    if r["reason"] == "prior_year_zero":
        return _result(f"{head}\nNo penalty: last year's tax after credits was zero, so no installments were required.",
                       "il_penalty_underpayment", amount=0.0, citation="IDOR 2025 Form IL-2210 Instructions",
                       source_url=_IL2210_URL)
    lines = [head,
             f"- Required annual payment: the lesser of 90% of this year's tax ({_fmt(r['line6'])}) and last year's tax "
             f"({_fmt(prior)}) = {_fmt(r['required'])}, or {_fmt(r['installment'])} per quarter",
             f"- Withholding counts as paid evenly: {_fmt(withheld / 4)} per quarter"]
    for qd in r["quarters"]:
        if qd["unpaid"] <= 0:
            lines.append(f"- Q{qd['quarter']} (due {qd['due']:%b %d, %Y}): covered, no penalty")
            continue
        bits = []
        for d, amt, days, rate in qd["pieces"]:
            if d is None:
                bits.append(f"{_fmt(amt)} never paid at {rate * 100:.0f}%")
            elif days <= 0:
                bits.append(f"{_fmt(amt)} paid {d:%b %d, %Y} (on time)")
            else:
                bits.append(f"{_fmt(amt)} paid {d:%b %d, %Y}, {days} days late at {rate * 100:.0f}%")
        lines.append(f"- Q{qd['quarter']} (due {qd['due']:%b %d, %Y}): {_fmt(qd['unpaid'])} short; "
                     + "; ".join(bits) + f" = {_fmt(qd['penalty'])}")
    pen = r["penalty"]
    lines.append(f"Penalty (rounded to whole dollars): ${int(pen):,}." if pen else "No penalty is due.")
    lines.append("Assumes the only estimated payments are the ones you listed, withholding was paid evenly, and you use the "
                 "regular method (no annualized income, pass-through withholding or actual quarterly withholding). You can "
                 "also leave Line 34 blank and let IDOR figure and bill the penalty.")
    return _result("\n".join(lines), "il_penalty_underpayment", amount=float(pen),
                   citation="IDOR 2025 Form IL-2210, Steps 2-4", source_url=_IL2210_URL)


def answer_state(question, tax_type=None):
    q = question.lower()
    refusal = _not_covered_answer(question, q)
    if refusal:
        return refusal
    with il_db.get_conn() as conn:
        for fn in (_penalty_answer, _withholding_answer, _cr_answer, _nr_answer, _use_tax_answer, _eitc_answer, _k12_answer, _property_credit_answer, _exemption_answer,
                   _rate_answer):
            result = fn(conn, question, q)
            if result:
                return result
        result = _retirement_info_answer(question, q)
        if result:
            return result
        result = _compute_answer(conn, question, q)
        if result:
            return result
    return _review("I don't have a verified Illinois income-tax answer for that yet. Illinois currently covers: the flat "
                   "income tax computation, exemptions, the property tax and K-12 credits, the Illinois EITC and Child "
                   "Tax Credit, and use tax.", "il_no_answer")
