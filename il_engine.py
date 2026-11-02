"""Chat answers for the Illinois income-tax domain (Illinois Department of
Revenue, IL-1040). Deterministic and keyword-gated, same shape as
employer_engine: each answer function returns a result dict in the shared
shape or None. Every figure comes from il_income / the Illinois database; no
LLM composes a number.

Tax year: 2025 is the full computation year (the instructions read on
2026-11-01). 2026 is answered only for the facts IDOR has published (rate and
per-person exemption).
"""
import re

import il_db
import il_income as ii
import income_brackets
import states

DEFAULT_YEAR = 2025
_AMOUNT_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)\s*(k\b)?|(?<![\d,.])([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?)")
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


def _role(q, start, end):
    """What a dollar amount refers to: the nearest role keyword within 32 characters either side
    (the one right after the amount wins a tie)."""
    after, before = q[end:end + 32].lower(), q[max(0, start - 32):start].lower()
    best = None
    for role, words in _ROLES:
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


def _roles(question):
    q = question.lower()
    found = {}
    unassigned = []
    for v, s, e in _amounts(question):
        role = _role(q, s, e)
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
    if "credit" in q or "use tax" in q:
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


def answer_state(question, tax_type=None):
    q = question.lower()
    refusal = _not_covered_answer(question, q)
    if refusal:
        return refusal
    with il_db.get_conn() as conn:
        for fn in (_use_tax_answer, _eitc_answer, _k12_answer, _property_credit_answer, _exemption_answer,
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
