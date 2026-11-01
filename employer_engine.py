"""Chat answers for the CA employer payroll-tax (EDD) domain -- Ring 5.

Deterministic and keyword-gated, same shape as engine._answer_property: each
sub-feature has a tight signal detector (employer context AND a payroll-tax
term) so it never intercepts a sales or income question, and returns a result
dict in the shared answer shape or None. Numbers come from employer_payroll
(which reads the Ring 5 database); no LLM composes a figure.

Sub-features: employer UI/ETT/SDI cost, deposit schedule + due date,
supplemental-wage PIT withholding, late-payment penalty and interest.
"""
import datetime
import re

import employer_db
import employer_payroll as ep

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"),
    re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
               re.IGNORECASE),
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)")
_AMOUNT_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)\s*(k\b)?|(?<![\d,.])([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?)")


def _has(q, *terms):
    return any(t in q for t in terms)


def _word(q, *words):
    return any(re.search(r"\b" + re.escape(w) + r"\b", q) for w in words)


def _dates(question):
    """[(date, start, end)] for ISO, MM/DD/YYYY and 'Month D, YYYY' dates."""
    out = []
    for i, pat in enumerate(_DATE_PATTERNS):
        for m in pat.finditer(question):
            try:
                if i == 0:
                    d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif i == 1:
                    y = int(m.group(3))
                    d = datetime.date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
                else:
                    d = datetime.date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            except ValueError:
                continue
            out.append((d, m.start(), m.end()))
    return sorted(out, key=lambda x: x[1])


def _mask(question, spans):
    chars = list(question)
    for s, e in spans:
        for i in range(s, e):
            chars[i] = " "
    return "".join(chars)


def _amounts(question):
    """Dollar amounts ($-prefixed, or comma-grouped) with dates and percents masked out."""
    spans = [(s, e) for _, s, e in _dates(question)] + [m.span() for m in _PERCENT_RE.finditer(question)]
    masked = _mask(question, spans)
    out = []
    for m in _AMOUNT_RE.finditer(masked):
        raw = m.group(1) or m.group(3)
        v = float(raw.replace(",", ""))
        if m.group(2):
            v *= 1000
        out.append((v, m.start(), m.end()))
    return out


def _base():
    return {"category": None, "taxable": None, "rate": None, "amount": None, "tax": None,
            "citation": None, "source_url": None, "location": None, "rate_basis": None,
            "branches": [], "fees": [], "info": None, "city_cannabis_tax": None,
            "route_dist": None, "rerank_v2_key": None, "domain": "employer"}


# ---------------------------------------------------------------- signals
_EMPLOYER_CTX = ("employer", "my employee", "my employees", "i employ", "payroll", "hire ", "hiring", "my staff",
                 "edd", "de 88", "de 9")


def detect_cost_signal(q):
    q = q.lower()
    terms = _has(q, "unemployment insurance", "employment training tax", "ui tax", "sui ") or _word(q, "ett", "ui")
    return terms and _has(q, *_EMPLOYER_CTX) and "unemployment benefit" not in q and "claim" not in q


def detect_deposit_signal(q):
    q = q.lower()
    return ("deposit" in q and _has(q, "pit", "withholding", "withheld", "sdi", "de 88", "edd", "payroll")
            and _has(q, "employer", "payroll", "edd", "de 88", "my employees", "semi-weekly", "semiweekly",
                     "next-day", "next day"))


def detect_supplemental_signal(q):
    q = q.lower()
    if "bonus depreciation" in q:
        return False
    wage = _has(q, "supplemental wage", "bonus", "severance", "commission", "stock option", "overtime pay",
                "vacation pay")
    return wage and _has(q, "withhold", "withholding") and _has(q, "california", "pit", "edd", "state")


def detect_late_signal(q):
    q = q.lower()
    late = _has(q, "late", "delinquent", "overdue", "past due", "missed")
    pay = _has(q, "payroll tax", "payroll deposit", "edd", "de 88", "de 9", "withholding deposit", "ui tax",
               "sdi deposit", "payroll")
    return late and pay and _has(q, "penalty", "interest", "owe", "how much")


def detect_futa_signal(q):
    q = q.lower()
    return _has(q, "futa", "federal unemployment tax", "form 940") or (
        "credit reduction" in q and _has(q, "unemployment", "employer", "940"))


def has_any_signal(question):
    for check in (detect_futa_signal, detect_cost_signal, detect_deposit_signal, detect_supplemental_signal, detect_late_signal):
        try:
            if check(question):
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------- answers
def _result(base, text, calc, category, amount=None, tax=None, status="answered"):
    return {**base, "status": status, "category": category, "amount": amount, "tax": tax,
            "citation": calc.get("citation") if calc else ep.CITATION,
            "source_url": calc.get("source_url") if calc else ep.SOURCE_URL, "answer_text": text}


def _futa_text(f, include_intro=True):
    """FUTA sentence(s) shared by the cost answer and the standalone answer.
    A non-final (preliminary) credit reduction is shown as a range."""
    year = f["tax_year"]
    if f["reduction_status"] == "final":
        text = (f"Federal unemployment tax (FUTA) for {year}: {f['gross_rate']:.1%} on the first "
                f"${f['wage_limit']:,.0f} per employee, less the {f['max_credit']:.1%} credit for state UI paid on time, "
                f"is {f['net_rate']:.1%} = ${f['base_each']:,.2f} per employee. California is a credit-reduction state: "
                f"the {year} reduction was {f['reduction_rate']:.1%}, adding ${f['reduction_each']:,.2f}, so FUTA is "
                f"${f['total_each']:,.2f} per employee (${f['total']:,.2f} total).")
    else:
        text = (f"Federal unemployment tax (FUTA) for {year}: {f['gross_rate']:.1%} on the first "
                f"${f['wage_limit']:,.0f} per employee, less the {f['max_credit']:.1%} credit for state UI paid on time, "
                f"is {f['net_rate']:.1%} = ${f['base_each']:,.2f} per employee before any credit reduction. California "
                f"is a credit-reduction state and the {year} rate is NOT final -- DOL sets it on November 10, {year}. "
                f"Its preliminary figure is {f['reduction_rate']:.1%} (+${f['reduction_each']:,.2f}, for "
                f"${f['total_each']:,.2f} per employee, ${f['total']:,.2f} total), and it could be as high as "
                f"{f['reduction_rate_max']:.1%} if a benefit-cost-rate add-on applies (+${f['reduction_each_max']:,.2f}, "
                f"${f['total_each_max']:,.2f} per employee, ${f['total_max']:,.2f} total).")
    text += (" The credit reduction is reported on Schedule A of Form 940 and paid with your fourth-quarter FUTA "
             "deposit; FUTA is deposited quarterly once you owe more than $500. The full credit assumes you pay "
             "your state UI on time -- late state payments reduce it.")
    return text


def _cost_answer(question, base):
    if not detect_cost_signal(question):
        return None
    q = question.lower()
    n_match = re.search(r"\b(\d+)\s+(?:full[- ]time\s+|part[- ]time\s+)?(?:employees|workers|staff|people)\b", q)
    n = int(n_match.group(1)) if n_match else 1
    amounts = [a for a in _amounts(question)
               if not (n_match and abs(a[1] - n_match.start()) < 3)]
    rate = None
    for m in _PERCENT_RE.finditer(q):
        if re.search(r"\b(ui|unemployment|sui|rate)\b", q[max(0, m.start() - 40):m.end() + 25]):
            rate = float(m.group(1)) / 100
            break
    total_payroll = _has(q, "total payroll", "in total", "combined", "altogether", "total wages")
    with employer_db.get_conn() as conn:
        if not amounts:
            calc = ep.compute_employer_costs(conn, 7000, 1, rate)
            if not calc:
                return None
            text = (f"California charges employers Unemployment Insurance (UI) and Employment Training Tax (ETT) "
                    f"on only the first ${calc['taxable_wage_limit']:,.0f} of each employee's wages per year. "
                    f"A new employer's UI rate is {ep.get_rate(conn, 'ui_new_employer')[0]:.1%} for two to three "
                    f"years (experience-rated {ep.get_rate(conn, 'ui_min')[0]:.1%}-"
                    f"{ep.get_rate(conn, 'ui_max')[0]:.1%} after) and ETT is {calc['ett_rate']:.1%}, so the most "
                    f"an employee can cost you at the new-employer rate is ${calc['employer_cost_each']:,.2f} a "
                    f"year (${calc['ui_each']:,.2f} UI + ${calc['ett_each']:,.2f} ETT). SDI "
                    f"({calc['sdi_rate']:.1%}) is withheld from the employee's pay, not an employer cost, and has "
                    f"no wage limit. Tell me the annual wages (and the number of employees) for an exact figure. "
                    f"({calc['citation']})")
            return _result(base, text, calc, "employer_payroll_cost_info", tax=calc["employer_cost_each"])
        wages = amounts[0][0]
        if total_payroll and n > 1:
            wages = wages / n
        calc = ep.compute_employer_costs(conn, wages, n, rate)
        if not calc:
            if rate is not None:
                lo, hi = ep.get_rate(conn, "ui_min")[0], ep.get_rate(conn, "ui_max")[0]
                return _result(base, f"A UI rate of {rate:.2%} is outside California's {lo:.1%}-{hi:.1%} range "
                                     f"for 2026, so I can't use it; check the rate on your EDD notice.",
                               None, "employer_payroll_cost_bad_rate", status="needs_review")
            return None
    each = "each employee" if n > 1 else "the employee"
    text = (f"For {n} employee{'s' if n > 1 else ''} paid ${calc['annual_wages']:,.2f} a year: UI and ETT apply to "
            f"only the first ${calc['taxable_wage_limit']:,.0f} of wages, so ${calc['taxable_wages_each']:,.2f} is "
            f"taxable for {each}. UI at {calc['ui_rate']:.2%} = ${calc['ui_each']:,.2f}; ETT at "
            f"{calc['ett_rate']:.1%} = ${calc['ett_each']:,.2f}. Your cost is ${calc['employer_cost_each']:,.2f} per "
            f"employee, ${calc['employer_cost_total']:,.2f} total. Separately, {calc['sdi_rate']:.1%} SDI of "
            f"${calc['sdi_withheld_each']:,.2f} per employee (${calc['sdi_withheld_total']:,.2f} total) is withheld "
            f"from their pay on all wages -- that is the employee's cost, which you remit.")
    if calc["ui_rate_is_default_new_employer"]:
        text += (f" I used the {calc['ui_rate']:.1%} new-employer UI rate; if you have an experience-rated rate "
                 f"(shown in e-Services for Business), tell me and I'll use it.")
    if total_payroll and n > 1:
        text += " I treated the dollar figure as total payroll and split it evenly across employees."
    text += (f" Not included above: the ETT exemption cases under CUIC 977(c). ({calc['citation']})")
    with employer_db.get_conn() as conn:
        futa = ep.compute_futa(conn, wages, n)
    if futa:
        text += " " + _futa_text(futa) + " FUTA is on top of the UI and ETT above."
    return _result(base, text, calc, "employer_payroll_cost", amount=calc["annual_wages"],
                   tax=calc["employer_cost_total"])


def _futa_answer(question, base):
    if not detect_futa_signal(question):
        return None
    q = question.lower()
    year_match = re.search(r"\b((?:19|20)\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else ep.PAYROLL_TAX_YEAR
    n_match = re.search(r"\b(\d+)\s+(?:full[- ]time\s+|part[- ]time\s+)?(?:employees|workers|staff|people)\b", q)
    n = int(n_match.group(1)) if n_match else 1
    amounts = [a for a in _amounts(question)
               if not (n_match and abs(a[1] - n_match.start()) < 3)
               and not (year_match and a[1] <= year_match.start() < a[2])]
    wages = amounts[0][0] if amounts else 7000.0
    if amounts and n > 1 and _has(q, "total payroll", "in total", "combined", "altogether", "total wages"):
        wages = wages / n
    with employer_db.get_conn() as conn:
        f = ep.compute_futa(conn, wages, n, year)
    if not f:
        return _result(base, f"I only have FUTA and California credit-reduction figures for 2025 and 2026, not {year}; "
                             "check the IRS Form 940 instructions and DOL's credit reduction page for that year.",
                       None, "employer_futa_unsupported_year", status="needs_review")
    text = _futa_text(f)
    if not amounts:
        text += (" That is for an employee earning $7,000 or more; give me the wages and number of employees for "
                 "your exact figure.")
    elif wages < 7000:
        text += f" FUTA applies to the ${f['taxable_wages_each']:,.2f} of wages paid, below the $7,000 limit."
    return _result(base, text, f, "employer_futa", amount=wages, tax=f["total"])


_FED_SCHEDULE_TERMS = (
    ("semi_weekly", ("semi-weekly", "semiweekly", "semi weekly")),
    ("next_day", ("next-day", "next day", "$100,000")),
    ("quarterly_or_annual", ("quarterly", "annual")),
    ("monthly", ("monthly",)),
)
_SCHEDULE_LABELS = {"next_day": "next-day", "semi_weekly": "semi-weekly", "monthly": "monthly",
                    "quarterly_or_annual": "quarterly or annual"}
_CA_SCHEDULE_TEXT = {
    "quarterly": "quarterly (due by each quarter's delinquent date: April 30, July 31, November 2, February 1 for 2026)",
    "monthly": "monthly (by the 15th of the following month)",
    "semi_weekly": "semi-weekly (Wednesday, Thursday or Friday paydays by the following Wednesday; Saturday through "
                   "Tuesday paydays by the following Friday)",
    "next_day": "next-day (the next banking day after payday)",
}


def _deposit_answer(question, base):
    if not detect_deposit_signal(question):
        return None
    q = question.lower()
    fed = next((key for key, terms in _FED_SCHEDULE_TERMS if _has(q, *terms)), None)
    dates = _dates(question)
    amounts = _amounts(question)
    accumulated = amounts[0][0] if amounts else None
    payday = dates[0][0] if dates else None
    if fed is None or accumulated is None:
        text = ("California PIT withholding and SDI deposits follow your federal deposit schedule and the amount you "
                "have accumulated: under $350 you deposit quarterly; $350 to $400 monthly (except a federal "
                "monthly/quarterly/annual depositor, who deposits monthly at $350 or more); over $400 follows "
                "the federal schedule -- next-day or semi-weekly. To tell you your schedule and due date I need "
                "your federal deposit schedule (next-day, semi-weekly, monthly, or quarterly/annual) and the PIT "
                f"withholding accumulated, plus the payday if you want a due date. ({ep.CITATION})")
        return _result(base, text, None, "employer_deposit_info", status="needs_review")
    with employer_db.get_conn() as conn:
        calc = ep.compute_deposit_schedule(conn, fed, accumulated, payday)
    if not calc:
        return None
    text = (f"As a {_SCHEDULE_LABELS[fed]} federal depositor with ${accumulated:,.2f} of accumulated PIT "
            f"withholding, your California deposit schedule is {_CA_SCHEDULE_TEXT[calc['ca_schedule']]}.")
    if calc["due_date"]:
        text += (f" For a payday of {payday:%B %d, %Y}, the deposit is due {calc['due_date']:%A, %B %d, %Y}.")
        text += (" This moves a due date that lands on a weekend to the next business day but does not account for "
                 "legal holidays, which also extend it; electronic deposits must settle by the due date.")
    elif payday is None:
        text += " Give me the payday and I can compute the due date."
    text += f" Deposits cover PIT withholding and SDI together; SDI is deposited on the same schedule. ({calc['citation']})"
    return _result(base, text, calc, "employer_deposit_schedule", amount=accumulated)


def _supplemental_answer(question, base):
    if not detect_supplemental_signal(question):
        return None
    q = question.lower()
    amounts = _amounts(question)
    if not amounts:
        return None
    amount = amounts[0][0]
    bonus = _has(q, "bonus", "stock option")
    other = _has(q, "severance", "commission", "overtime", "vacation", "sales award")
    if bonus and other:
        return None
    wage_type = "bonus" if bonus else "other"
    with employer_db.get_conn() as conn:
        calc = ep.compute_supplemental_withholding(conn, amount, wage_type)
    if not calc:
        return None
    kind = "a bonus or stock option" if bonus else "other supplemental wages (overtime, commissions, severance, vacation pay)"
    text = (f"For ${amount:,.2f} of {kind} paid separately from regular wages, you may withhold California PIT at the "
            f"flat {calc['rate']:.2%} rate with no allowances: ${calc['withholding']:,.2f}. If it is paid at the same "
            "time as regular wages, add it to the regular pay and use the normal withholding schedules instead; "
            "you may also use the aggregate method (withholding on regular plus supplemental pay, minus what was "
            "withheld from the regular pay). Federal supplemental withholding and SDI (1.3%, no wage limit) are "
            f"separate. ({calc['citation']})")
    return _result(base, text, calc, "employer_supplemental_withholding", amount=amount, tax=calc["withholding"])


def _late_answer(question, base):
    if not detect_late_signal(question):
        return None
    q = question.lower()
    dates = _dates(question)
    amounts = _amounts(question)
    if not amounts:
        return None
    tax = amounts[0][0]
    days_match = re.search(r"\b(\d+)\s+days?\b", q)
    with employer_db.get_conn() as conn:
        if len(dates) >= 2:
            calc = ep.compute_late_payment(conn, tax, dates[0][0], dates[1][0])
            basis = f"due {dates[0][0]:%B %d, %Y}, paid {dates[1][0]:%B %d, %Y}"
        elif days_match:
            paid = datetime.date.today()
            calc = ep.compute_late_payment(conn, tax, paid - datetime.timedelta(days=int(days_match.group(1))), paid)
            basis = f"{days_match.group(1)} days late, using today's interest rate period"
        else:
            pen = ep.get_rate(conn, "late_penalty")
            if not pen:
                return None
            text = (f"EDD charges a {pen[0]:.0%} penalty on late payroll tax payments: ${tax * pen[0]:,.2f} on "
                    f"${tax:,.2f}, plus interest compounded daily at EDD's current rate (7% a year for July-December "
                    "2026), which depends on how many days late. Give me the due date and the date you paid (or the "
                    f"number of days late) and I'll add the interest. ({ep.CITATION})")
            return _result(base, text, None, "employer_late_payment_penalty_only", amount=tax, tax=round(tax * pen[0], 2))
    if not calc:
        return None
    text = (f"On ${tax:,.2f} of payroll tax {basis} ({calc['days_late']} days late): the {calc['penalty_rate']:.0%} "
            f"penalty is ${calc['penalty']:,.2f} and interest is about ${calc['interest']:,.2f}, for a total of about "
            f"${calc['total']:,.2f}. Interest compounds daily at EDD's rate for each period (rate changes every "
            "January and July). I applied interest to the tax plus the penalty; EDD says interest applies to unpaid "
            "tax, interest and certain penalties, so the exact EDD figure on your Statement of Account (DE 2176) "
            f"can differ slightly. Interest cannot be waived. ({calc['citation']})")
    return _result(base, text, calc, "employer_late_payment", amount=tax, tax=calc["total"])


def answer_employer(question):
    base = _base()
    for fn in (_late_answer, _supplemental_answer, _deposit_answer, _cost_answer, _futa_answer):
        result = fn(question, base)
        if result:
            return result
    return None
