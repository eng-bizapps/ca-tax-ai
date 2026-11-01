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
import employer_extras as ep_x
import employer_payment_types as ept
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


_ACRONYMS = frozenset({"edd", "ett", "sdi", "pit", "ui", "sui", "vui", "wba", "pfl", "di"})


def _has(q, *terms):
    """Any term appears in q. Acronyms (edd, ett, sdi, pit, ...) must match as whole words: as
    substrings they hide inside ordinary words ('wedding' contains 'edd', 'typesetting' contains
    'ett'). Other terms stay substring matches so stems like 'fail' still catch 'failed'."""
    for t in terms:
        if t in _ACRONYMS:
            if re.search(r"\b" + t + r"\b", q):
                return True
        elif t in q:
            return True
    return False


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


def detect_ui_rate_signal(q):
    q = q.lower()
    ui = _has(q, "unemployment", "edd", "de 2088", "experience rat") or _word(q, "ui")
    return ui and (_has(q, "reserve account", "reserve ratio", "reserve balance", "experience rating",
                        "experience-rated", "de 2088", "rate schedule", "schedule f"))


_INTENT_WORDS = ("intentional", "willful", "wilful", "conspir", "collu", "on purpose", "knowingly")


def detect_filing_penalty_signal(q):
    q = q.lower()
    strong = _has(q, "de 34", "de34", "de 542", "de542", "de 9c", "de9c", "wage item", "unreported wages")
    # generic worker/hire words only count alongside reporting or EDD wording, so "my new
    # employee is always late" is not a payroll-penalty question
    weak = (_has(q, "new hire", "new employee", "rehire", "independent contractor")
            and _has(q, "report", "edd")) or (_has(q, "w-2") and _has(q, "edd", "provide"))
    topic = (strong or weak
             or (_has(q, "paper") and _has(q, "de 9", "de 88", "return", "deposit", "filing"))
             or re.search(r"\bde 9\b", q) is not None)
    return bool(topic and _has(q, "penalty", "penalties", "late", "fail", "miss", "forgot", "didn't report",
                               "did not report", "never reported", "not reported", "how much"))


_PAYROLL_CTX_TERMS = ("sdi", "ett", "unemployment insurance", "disability insurance", "payroll tax", "edd",
                      "de 9c", "subject wage", "subject to withholding", "pit wage", "paid family leave")


def detect_wages_signal(q):
    """Which payments are subject wages. Needs payroll-tax wording so that an
    employee asking 'is my bonus taxable?' (an income-tax question) is not
    intercepted."""
    q = q.lower()
    payroll = _has(q, *_PAYROLL_CTX_TERMS) or _word(q, "ui")
    return payroll and _has(q, "subject", "taxable", "withhold", "wages", "report", "count as", "include")


def detect_registration_signal(q):
    q = q.lower()
    if _has(q, "seller", "permit", "cdtfa", "sales tax", "secretary of state", "business license", "dmv", "trademark"):
        return False
    return (_has(q, "register", "registration", "payroll tax account", "employer account", "subject employer",
                 "employer payroll tax account number")
            and _has(q, "employer", "edd", "employee", "payroll", "hire", "hiring", "wages"))


_HOUSEHOLD_WORKERS = ("nanny", "housekeeper", "babysitter", "baby-sitter", "caregiver", "gardener", "maid",
                     "home health aide", "domestic worker", "cook at home")


def detect_household_signal(q):
    q = q.lower()
    if _has(q, "household employer", "household employee", "de 3hw", "de 1hw", "de 3bhw", "de 8829", "de 89 "):
        return True
    if _has(q, "credit", "deduct", "dependent care", "taxable income", "tax return", "w-2 box"):
        return False
    return _has(q, *_HOUSEHOLD_WORKERS) and _has(q, "edd", "sdi", "ett", "ui", "payroll tax", "register",
                                                   "unemployment insurance", "withhold")


def detect_vui_signal(q):
    q = q.lower()
    return (_has(q, "vui", "de 2088a", "de 2088vu")
            or (_has(q, "voluntary") and _has(q, "contribution", "payment") and _has(q, "ui", "unemployment", "rate")))


def detect_schedule_signal(q):
    q = q.lower()
    return (_has(q, "ui fund", "unemployment fund", "fund balance", "fund ratio")
            and _has(q, "ratio", "schedule", "rate schedule", "percent", "%"))


def detect_benefit_signal(q):
    q = q.lower()
    benefit = (re.search(r"weekly.{0,30}benefit|benefit.{0,30}weekly|benefit amount|per week|a week"
                         r"|how much (?:will|would|do|can) i (?:get|receive|collect)", q) is not None
               or _has(q, "wba"))
    kind = _has(q, "unemployment", "disability", "sdi", "pfl", "paid family leave", "di benefit", "ui benefit")
    return benefit and kind and not _has(q, "taxable", "tax return", "1099-g")


def detect_reimbursable_signal(q):
    q = q.lower()
    if _has(q, "reimbursable method", "reimbursable employer", "cost of benefits", "cost-of-benefits",
            "reimburse the state", "reimburse edd", "de 1snp"):
        return True
    return (_has(q, "nonprofit", "501(c)(3)", "public entity", "school district")
            and _has(q, "unemployment insurance", "ui") and _has(q, "reimburs", "elect"))


def has_any_signal(question):
    for check in (detect_household_signal, detect_vui_signal, detect_schedule_signal, detect_benefit_signal,
                  detect_reimbursable_signal, detect_wages_signal, detect_registration_signal, detect_filing_penalty_signal, detect_ui_rate_signal, detect_futa_signal, detect_cost_signal, detect_deposit_signal, detect_supplemental_signal, detect_late_signal):
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


def _ui_rate_answer(question, base):
    if not detect_ui_rate_signal(question):
        return None
    q = question.lower()
    year_match = re.search(r"\b((?:19|20)\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else ep.PAYROLL_TAX_YEAR
    amounts = [a for a in _amounts(question) if not (year_match and a[1] <= year_match.start() < a[2])]

    def near(words, window=60):
        best = None
        for m in re.finditer("|".join(words), q):
            for a in amounts:
                if 0 <= a[1] - m.end() <= window and (best is None or a[1] - m.end() < best[1] - m.end()):
                    best = a
        return best

    balance_amt = near(("balance", "reserve account"))
    payroll_amt = near(("payroll",))
    ratio_match = re.search(r"reserve ratio[^0-9-]{0,25}(-?\d*\.?\d+)", q)
    if balance_amt and payroll_amt and balance_amt[1] == payroll_amt[1]:
        balance_amt = None
    if payroll_amt and balance_amt is None:
        rest = [a for a in amounts if a[1] != payroll_amt[1]]
        balance_amt = rest[0] if len(rest) == 1 else None
    if ratio_match:
        balance, payroll = float(ratio_match.group(1)), 1.0
    elif balance_amt and payroll_amt:
        balance, payroll = balance_amt[0], payroll_amt[0]
        pre = q[max(0, balance_amt[1] - 25):balance_amt[1] + 1]
        if _has(pre, "negative", "deficit", "overdrawn") or re.search(r"[-−]\s*\$?\s*$", pre):
            balance = -balance
    else:
        text = ("Your UI rate comes from your reserve ratio: your reserve account balance (credits minus benefit "
                "charges, as of July 31) divided by your average base payroll (the average of your taxable "
                "payroll for the three calendar years before June 30). EDD puts one of seven schedules in effect "
                "each year -- Schedule F+ for both 2026 and 2027 -- and reads your rate off it: from 1.5% for a "
                "strongly positive reserve (ratio 0.20 or more) up to 6.2% for a negative one. New employers "
                "pay 3.4% for two to three years. Give me your reserve account balance and average base "
                "payroll (both are on your Notice of Contribution Rates, DE 2088, mailed each December) and I'll "
                "work out the rate, or look it up in e-Services for Business. (EDD DE 231Z; DE 44)")
        return _result(base, text, None, "employer_ui_rate_info", status="needs_review")
    with employer_db.get_conn() as conn:
        calc = ep.compute_ui_rate(conn, balance, payroll, year)
        ett = ep.get_rate(conn, "ett")
    if not calc:
        return _result(base, f"I can't work out a UI rate for {year} from those figures; I have EDD's rate "
                             "schedule for 2026 and 2027, and need a positive average base payroll. Your DE 2088 "
                             "shows your actual rate.", None, "employer_ui_rate_unsupported", status="needs_review")
    lo, hi = calc["band_min"], calc["band_max"]
    band = (f"below {hi:.2f}" if lo is None else f"{lo:.2f} or more" if hi is None else f"{lo:.2f} up to (not incl.) {hi:.2f}")
    rate = calc["ui_rate"]
    limit = 7000.0
    ett_each = limit * ett[0] if calc["ett_applies"] else 0.0
    if ratio_match:
        basis = f"Your reserve ratio is {calc['reserve_ratio']:.4f}"
    else:
        bal = calc["reserve_balance"]
        bal_text = f"-${-bal:,.2f}" if bal < 0 else f"${bal:,.2f}"
        basis = (f"A reserve balance of {bal_text} against average base payroll of "
                 f"${calc['avg_base_payroll']:,.2f} gives a reserve ratio of {calc['reserve_ratio']:.4f}")
    text = (f"{basis}. On Schedule {calc['schedule']} (the schedule EDD has in effect for {year}), the band {band} "
            f"pays a UI rate of {rate:.1%}. On the first ${limit:,.0f} of an employee's wages that is "
            f"${limit * rate:,.2f} of UI")
    if calc["ett_applies"]:
        text += f" plus ${ett_each:,.2f} of ETT ({ett[0]:.1%}), or ${limit * rate + ett_each:,.2f} per employee."
    else:
        text += (" and, because the reserve balance is negative, ETT is 0.0%, so the cost is "
                 f"${limit * rate:,.2f} per employee.")
    if calc["near_boundary"]:
        text += (" Your ratio sits within 0.0005 of a band edge, where how EDD rounds the ratio could move you "
                 "one band, so check your DE 2088.")
    text += (" Your actual rate is the one on your Notice of Contribution Rates (DE 2088), which can differ if "
             "reserve transfers, cancellations or other adjustments apply; you can protest it within 60 days of "
             f"the notice date. ({calc['citation']})")
    return _result(base, text, calc, "employer_ui_rate", amount=calc["avg_base_payroll"], tax=round(limit * rate, 2))


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
    year_match = re.search(r"\b((?:19|20)\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else ep.PAYROLL_TAX_YEAR
    if year not in (2026, 2027):
        return _result(base, f"I only have California employer payroll tax rates for 2026 and 2027, not {year}; "
                             "check EDD's Rates and Withholding page or the DE 44 for that year.",
                       None, "employer_payroll_cost_unsupported_year", status="needs_review")
    with employer_db.get_conn() as conn:
        if not amounts:
            calc = ep.compute_employer_costs(conn, 7000, 1, rate, year)
            if not calc:
                return None
            text = (f"California charges employers Unemployment Insurance (UI) and Employment Training Tax (ETT) "
                    f"on only the first ${calc['taxable_wage_limit']:,.0f} of each employee's wages per year. "
                    f"A new employer's UI rate is {ep.get_rate(conn, 'ui_new_employer', year)[0]:.1%} for two to three "
                    f"years (experience-rated {ep.get_rate(conn, 'ui_min', year)[0]:.1%}-"
                    f"{ep.get_rate(conn, 'ui_max', year)[0]:.1%} after) and ETT is {calc['ett_rate']:.1%}, so the most "
                    f"an employee can cost you at the new-employer rate is ${calc['employer_cost_each']:,.2f} a "
                    f"year (${calc['ui_each']:,.2f} UI + ${calc['ett_each']:,.2f} ETT). SDI "
                    f"({calc['sdi_rate']:.1%}) is withheld from the employee's pay, not an employer cost, and has "
                    f"no wage limit. Tell me the annual wages (and the number of employees) for an exact figure. "
                    f"({calc['citation']})")
            return _result(base, text, calc, "employer_payroll_cost_info", tax=calc["employer_cost_each"])
        wages = amounts[0][0]
        if total_payroll and n > 1:
            wages = wages / n
        calc = ep.compute_employer_costs(conn, wages, n, rate, year)
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
        futa = ep.compute_futa(conn, wages, n, year)
    if futa:
        text += " " + _futa_text(futa) + " FUTA is on top of the UI and ETT above."
    else:
        text += (f" I don't have {year} federal unemployment tax (FUTA) figures yet: DOL hasn't published a "
                 f"{year} California credit reduction. The base FUTA rate is 0.6% on the first $7,000 "
                 "($42 per employee) before any reduction.")
    text += f" These are {year} rates."
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
        text += (f" For a payday of {payday:%B %d, %Y}, the deposit is due {calc['due_date']:%A, %B %d, %Y}")
        if calc.get("holiday_shift"):
            text += f" (after skipping {calc['holiday_shift']}, a California state holiday)"
        text += "."
        if calc["ca_schedule"] == "quarterly":
            text += " That is the delinquency date EDD publishes for the quarter."
        elif calc.get("holidays_modeled"):
            text += (" Due dates move past weekends and California state holidays (the Secretary of State's list; "
                     "EDD does not publish its own definition of 'legal holiday').")
        else:
            text += (" This moves a weekend due date to the next business day, but I don't have the California "
                     f"state holiday list for {calc['due_date'].year} yet, so a holiday could extend it further.")
        if calc.get("federal_holiday_note"):
            text += (f" Note that date is {calc['federal_holiday_note']}, a federal bank holiday: it is not a "
                     "California state holiday, but an electronic payment may not settle that day, so pay earlier "
                     "to be safe.")
        text += " Electronic deposits must settle by the due date."
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


_TREATMENT_WORDS = {"subject": "subject", "not subject": "not subject", "reportable": "reportable",
                    "not reportable": "not reportable", "conditional": "depends on the facts (see below)"}


def _year_in(q, default=ep.PAYROLL_TAX_YEAR):
    m = re.search(r"\b((?:19|20)\d{2})\b", q)
    return int(m.group(1)) if m else default


def _household_answer(question, base):
    if not detect_household_signal(question):
        return None
    q = question.lower()
    amounts = _amounts(question)
    quarter = amounts[0][0] if amounts else None
    annual = amounts[1][0] if len(amounts) > 1 else None
    text = ("A household employer is a private household (or a college club or sorority/fraternity chapter) paying "
            "workers such as baby-sitters, caretakers, cooks, gardeners, housekeepers and maids. Different thresholds "
            "from other employers apply: once you pay cash wages of $750 or more in a calendar quarter (checks and "
            "cash count; meals and lodging do not) you must register with EDD within 15 days and report; from $750 to "
            "$999.99 you withhold State Disability Insurance (SDI) from the employee; from $1,000 a quarter you also "
            "pay Unemployment Insurance (UI) and Employment Training Tax (ETT). Withholding personal income tax is "
            "optional (you must still report PIT wages, and if you and the employee agree to withhold you must send it "
            "in). If annual wages are $20,000 or less you may elect annual filing (DE 89): file the DE 3BHW wage "
            "report each quarter and the DE 3HW return with payment once a year; above $20,000 file quarterly (DE 9 "
            "and DE 9C with electronic deposits). Register online or on the DE 1HW, and file even in a quarter with "
            "no payroll. Workers supplied by an independent business or leased from an agency are not your "
            "household employees; carpenters, electricians and tutors are not household workers.")
    calc = None
    if quarter is not None:
        with employer_db.get_conn() as conn:
            calc = ep_x.compute_household(conn, quarter, annual)
    if calc:
        parts = []
        if not calc["must_register"]:
            parts.append(f"At ${quarter:,.2f} of cash wages in a quarter you are under the $750 threshold, so you are "
                         "not yet required to register or withhold.")
        else:
            parts.append(f"At ${quarter:,.2f} of cash wages in a quarter you must register and report and withhold "
                         "SDI" + (", and you also owe UI and ETT." if calc["pay_ui_ett"] else
                                  "; UI and ETT start at $1,000 a quarter, so not yet."))
        if calc["annual_filing_eligible"] is not None:
            parts.append("Your annual wages " + ("are within" if calc["annual_filing_eligible"] else "exceed")
                         + " the $20,000 limit for electing annual filing.")
        text = " ".join(parts) + " " + text
    text += f" ({ep_x.HOUSEHOLD})"
    return _result(base, text, calc or {"citation": ep_x.HOUSEHOLD, "source_url": ep_x.HOUSEHOLD_URL},
                   "employer_household", amount=quarter)


def _vui_answer(question, base):
    if not detect_vui_signal(question):
        return None
    q = question.lower()
    year = _year_in(q)
    rate = None
    for m in _PERCENT_RE.finditer(q):
        rate = float(m.group(1)) / 100
        break
    negative = _has(q, "negative reserve", "negative balance", "negative account")
    with employer_db.get_conn() as conn:
        calc = ep_x.voluntary_ui_availability(conn, year, -1.0 if negative else None, rate)
    if not calc:
        return _result(base, f"I only have the UI rate schedule for 2026 and 2027, not {year}. In general a "
                             "voluntary UI contribution is possible only in years when schedules AA through D are in "
                             "effect (DE 231Z).", None, "employer_vui_unsupported_year", status="needs_review")
    if calc["available"]:
        head = (f"Schedule {calc['schedule']} is in effect for {year}, so a voluntary UI contribution is allowed if you "
                "are eligible.")
    else:
        head = (f"No: a voluntary UI contribution is not available to you for {year}. " + "; ".join(calc["reasons"])
                + ".")
    text = (head + " How it works when it is available: eligible employers get a Voluntary UI Contribution Notice "
            "(DE 2088A) showing what to pay for a lower rate, and must pay by the last working day of March -- "
            "there is no late payment for good cause and no extension. You are not eligible if your required rate "
            "is 3.4%, your reserve account balance is negative, you already have the lowest possible rate, or EDD "
            "notified you before September 1 of an unpaid amount that is not under a timely petition. The payment "
            "is not a prepayment of tax, is not deductible, is not refundable, and the lower rate applies for the "
            "full calendar year; a lower rate may not reduce your annual UI cost, so use the DE 2088VU worksheet "
            f"before paying. ({calc['citation']})")
    return _result(base, text, calc, "employer_vui", status="answered")


def _schedule_answer(question, base):
    if not detect_schedule_signal(question):
        return None
    q = question.lower()
    m = _PERCENT_RE.search(q)
    ratio = None
    if m:
        ratio = float(m.group(1))
    else:
        r = re.search(r"ratio[^0-9]{0,20}(\d+(?:\.\d+)?)", q)
        ratio = float(r.group(1)) if r else None
    if ratio is None:
        text = ("EDD sets the UI rate schedule each year from the UI Fund balance on September 30 divided by the gross "
                "wages all employers reported in the fiscal year ending June 30, as a percent: 1.8 or more is "
                "Schedule AA; 1.6 up to 1.8 is A; 1.4 up to 1.6 is B; 1.2 up to 1.4 is C; 1.0 up to 1.2 is D; 0.8 up "
                "to 1.0 is E; 0.6 up to 0.8 is F; under 0.6 puts the emergency solvency surcharge in effect "
                "(Schedule F+, 1.15 times the Schedule F rate). EDD has Schedule F+ in effect for 2026 and 2027, with "
                "rates from 1.5% to 6.2%. Give me a fund ratio and I'll tell you the schedule. "
                f"({ep_x.DE231Z})")
        return _result(base, text, None, "employer_schedule_info", status="needs_review")
    with employer_db.get_conn() as conn:
        calc = ep_x.schedule_from_fund_ratio(conn, ratio)
    if not calc:
        return None
    text = f"A UI Fund ratio of {ratio}% puts Schedule {calc['schedule']} in effect."
    if calc["schedule"] == "F+":
        text += (" Under 0.6% the emergency solvency surcharge applies: 1.15 times the Schedule F rate, rounded to the "
                 "nearest tenth.")
    text += (" Voluntary UI contributions are " + ("allowed" if calc["voluntary_contributions_allowed"] else "not allowed")
             + " under this schedule. EDD publishes the schedule actually in effect (Schedule F+ for 2026 and 2027), "
             f"and that is what governs. ({calc['citation']})")
    return _result(base, text, calc, "employer_schedule", amount=ratio)


def _benefit_answer(question, base):
    if not detect_benefit_signal(question):
        return None
    q = question.lower()
    amounts = _amounts(question)
    di = _has(q, "disability", "sdi", "pfl", "paid family leave", "di benefit")
    ui = _has(q, "unemployment", "ui benefit")
    if not amounts:
        text = ("Both benefits are based on your highest quarter of wages in the base period (the wages paid 5 to 18 "
                "months before the claim start date). Unemployment (UI) weekly benefits run from $40 up to a "
                "$450 maximum, found on EDD's benefit table. Disability insurance (DI) and paid family leave (PFL) "
                "pay an estimated 70% to 90% of weekly wages, from a $50 minimum up to a $1,765 maximum for 2026. "
                "Give me your highest-quarter wages and I'll estimate the weekly amount. EDD confirms your actual "
                "amount when your claim is approved. (EDD DE 44 Rev. 52 (4-26); EDD Quick Statistics)")
        return _result(base, text, None, "employer_benefit_info", status="needs_review")
    hq = amounts[0][0]
    if not _has(q, "quarter") and _has(q, "year", "annual", "salary", "per month", "a month", "monthly"):
        text = ("Benefits are figured from your highest single quarter of wages in the base period, not from annual "
                "pay, so I need that quarter's total (for level pay it is roughly a quarter of the year's wages, but "
                "overtime or a bonus in one quarter changes it). Tell me your highest-quarter wages and I'll "
                "estimate the weekly benefit.")
        return _result(base, text, None, "employer_benefit_need_quarter", status="needs_review")
    parts, calc_all = [], None
    with employer_db.get_conn() as conn:
        if ui or not di:
            u = ep_x.compute_ui_benefit(conn, hq)
            if u:
                parts.append(f"Unemployment insurance: highest-quarter wages of ${hq:,.2f} fall at a weekly benefit of "
                             f"${u['weekly_benefit']:,.0f} on EDD's benefit table (maximum ${u['maximum']:,.0f})"
                             + (", the cap" if u["at_maximum"] else "") + ". Whether you qualify depends on EDD's "
                             "base-period earnings and other eligibility rules, which I haven't evaluated.")
                calc_all = u
            else:
                parts.append(f"Unemployment insurance: ${hq:,.2f} in your highest quarter is below the $900 where EDD's "
                             "benefit table begins, so the table lists no weekly benefit.")
        if di or not ui:
            d = ep_x.compute_di_benefit(conn, hq)
            if d:
                if not d["eligible"]:
                    parts.append(f"Disability insurance: ${hq:,.2f} in your highest quarter is under the $300 "
                                 "minimum, so EDD lists you as not eligible.")
                else:
                    how = {"flat": "the flat tier amount",
                           "pct_of_weekly": f"{(d['rate'] or 0):.0%} of weekly wages (${d['weekly_wages']:,.2f})"}[d["kind"]]
                    parts.append(f"Disability insurance: about ${d['weekly_benefit']:,.2f} a week -- {how}, "
                                 f"within the 2026 range of $50 to ${d['maximum']:,.0f}. This is an estimate; EDD "
                                 "confirms the amount when your claim is approved."
                                 + (" EDD's page describes disability insurance; paid family leave has the same "
                                    "$1,765 maximum, and I applied the same tiers to it." if _has(q, "pfl", "paid family leave")
                                    else ""))
                calc_all = calc_all or d
    if not parts:
        return None
    text = " ".join(parts) + " (" + (calc_all["citation"] if calc_all else "EDD") + ")"
    return _result(base, text, calc_all, "employer_benefit", amount=hq)


def _reimbursable_answer(question, base):
    if not detect_reimbursable_signal(question):
        return None
    text = ("Most employers pay UI tax under the experience rating method. Two groups may instead choose the "
            "reimbursable (cost-of-benefits) method, paying the UI Fund dollar for dollar for the benefits paid to "
            "their former employees and charged to their account, with no UI tax rate: nonprofit organizations with "
            "an IRC 501(c)(3) exemption, and public entities (the State, counties, cities, districts, public "
            "agencies and authorities, school and community college districts). A nonprofit elects it on the DE 1SNP "
            "when it registers (a later election takes effect the first day of the quarter in which it is filed). "
            "Reimbursable employers are billed quarterly and payment is due within 30 days of the statement date. "
            "The election must stay in effect for two complete calendar years for a public entity and five for a "
            "nonprofit, and after you end it you remain liable for benefits paid to former employees for three "
            "calendar years. California school districts, community colleges and charter schools can alternatively "
            "join the School Employees Fund. A nonprofit still withholds SDI and PIT, and a public entity must "
            "withhold PIT (SDI is optional for a public entity). Employers who elect the reimbursable method are "
            "not required to pay ETT. (EDD DE 44 Rev. 52 (4-26); EDD DE 231NP Rev. 5 (9-19))")
    return _result(base, text, None, "employer_reimbursable", status="answered")


def _wages_answer(question, base):
    if not detect_wages_signal(question):
        return None
    with employer_db.get_conn() as conn:
        rows = ept.find_payment_types(conn, question)
    if not rows:
        if _has(question.lower(), "gift card", "gift certificate", "gift cards"):
            text = ("EDD's Types of Payments sheet (DE 231TP) excludes only gifts of nominal value given as an "
                    "expression of goodwill, not based on pay or length or degree of service. It does not address gift "
                    "cards. Because wages are all compensation for services, including the cash value of noncash "
                    "payments, and a gift card is a cash equivalent, expect it to be treated as wages subject to UI, "
                    "ETT, SDI and PIT withholding unless EDD says otherwise -- this is my reading, not an EDD "
                    "statement, so confirm with the Taxpayer Assistance Center (1-888-745-3886) before relying on it. "
                    "(EDD DE 231TP, Rev. 1, 6-16; DE 44 Rev. 52 (4-26))")
            return _result(base, text, None, "employer_wages_gift_card", status="needs_review")
        if _has(question.lower(), "what are wages", "what counts as wages", "which payments", "types of payments",
                "what payments", "what is subject", "subject wages", "what is considered wages"):
            text = ("Wages are all compensation for an employee's personal services, whether paid by check, cash, "
                    "electronic payment or the reasonable cash value of noncash payments such as meals and lodging; "
                    "bonuses, overtime, sales awards, commissions and vacation pay are included. Subject wages (the "
                    "full amount, not just the first $7,000) are what you report on the DE 9C, and some payments are "
                    "excluded by statute. Subject wages and PIT wages are usually the same, but differ for employee "
                    "salary-reduction contributions to a qualified retirement plan (subject, not PIT wages), "
                    "family-employee wages (PIT wages only) and church employees. Ask me about a specific payment "
                    "(for example tips, severance, a 401(k) deferral, stock options or expense reimbursements) and "
                    "I'll give the treatment for UI/ETT, SDI and PIT, or see EDD's Types of Payments sheet "
                    "(DE 231TP). (EDD DE 44 Employer's Guide, Rev. 52 (4-26))")
            return _result(base, text, None, "employer_wages_subject_info", status="needs_review")
        return None
    top = rows[:3]
    parts = []
    for r in top:
        t = lambda v: _TREATMENT_WORDS.get(v, v)
        parts.append(
            f"{r['label']}: UI and ETT {t(r['ui_ett'])}; SDI {t(r['sdi'])}; PIT withholding {t(r['pit_withholding'])}; "
            f"PIT wages {t(r['pit_wages'])} ({r['code_refs']})."
            + (f" {r['conditions']}" if r['conditions'] else ""))
    text = " ".join(parts)
    text += (f" This follows EDD's Types of Payments sheet (DE 231TP, Rev. 1, June 2016) and agrees with the 2026 "
             "Employer's Guide (DE 44). The sheet is an older edition and how a particular payment is treated can "
             "turn on its facts, so for a large or unusual payment confirm with EDD's Taxpayer Assistance Center "
             "(1-888-745-3886).")
    return _result(base, text, {"citation": top[0]["citation"], "source_url": top[0]["source_url"]},
                   "employer_wages_subject", status="answered")


def _registration_answer(question, base):
    if not detect_registration_signal(question):
        return None
    amounts = [a for a in _amounts(question)]
    dates = _dates(question)
    wages = amounts[0][0] if amounts else None
    wage_date = dates[0][0] if dates else None
    calc = None
    if wages is not None:
        with employer_db.get_conn() as conn:
            calc = ep.compute_subject_employer(conn, wages, wage_date)
    text = ("You become a subject employer when you pay wages of over $100 in any calendar quarter, and you must then "
            "register with EDD within 15 days of paying those wages. Register online for an employer payroll tax "
            "account number at e-Services for Business (or send the paper form for your type of employer: DE 1 "
            "commercial, DE 1HW household, DE 1AG agriculture, DE 1GS government and schools, DE 1NP nonprofits, DE 1P "
            "if you only withhold personal income tax). EDD assigns an eight-digit account number to put on every "
            "deposit, return and letter. Once registered: report each new or rehired employee on a DE 34 within 20 "
            "calendar days of the start-of-work date (a rehire after a separation of 60 consecutive days or more counts "
            "as new), report an independent contractor on a DE 542 within 20 days of paying or contracting for $600 or "
            "more, give employees the DE 4 and EDD's benefit pamphlets, post the employee notice (DE 1857A), deposit "
            "with a DE 88, and file the DE 9 and DE 9C every quarter, even a quarter with no payroll.")
    if calc:
        if calc["subject_employer"]:
            text = (f"Yes: ${calc['quarterly_wages']:,.2f} of wages in a quarter is over the $100 threshold, so you are "
                    "a subject employer. " + text)
            if calc["register_by"]:
                text += (f" Counting 15 days from {wage_date:%B %d, %Y}, the registration deadline is "
                         f"{calc['register_by']:%B %d, %Y}; I have not extended it for a weekend or holiday, so register "
                         "before it.")
        else:
            text = (f"Not yet: ${calc['quarterly_wages']:,.2f} of wages in a quarter does not exceed the $100 threshold, "
                    "so you are not a subject employer for that quarter. " + text)
    text += (" If you are buying an existing business, you may be a successor employer. A household employer "
             "(someone paying workers in their own home) has different thresholds -- ask me about household employers. "
             f"({ep.CITATION})")
    return _result(base, text, calc, "employer_registration", amount=wages)


def _filing_penalty_answer(question, base):
    if not detect_filing_penalty_signal(question):
        return None
    q = question.lower()
    amounts = _amounts(question)
    count_match = re.search(r"\b(\d+)\s+(?:new\s+|late\s+|unreported\s+|missed\s+)?(?:employees|hires|workers|wage items|"
                            r"wage lines|contractors|independent contractors|reports|returns|w-2s|failures)\b", q)
    count = int(count_match.group(1)) if count_match else 1
    intentional = _has(q, *_INTENT_WORDS)
    paper = _has(q, "paper")
    if paper and _has(q, "de 88", "deposit") and amounts:
        with employer_db.get_conn() as conn:
            row = conn.execute("SELECT amount, citation, source_url FROM ca_edd_penalties "
                               "WHERE penalty_key='paper_de88'").fetchone()
        pct = float(row[0])
        text = (f"A payroll tax deposit sent on paper (DE 88) instead of electronically carries a {pct:.0%} penalty: "
                f"${amounts[0][0] * pct:,.2f} on ${amounts[0][0]:,.2f}, unless you have an approved e-file/e-pay mandate "
                f"waiver. ({row[1]})")
        return _result(base, text, {"citation": row[1], "source_url": row[2]}, "employer_filing_penalty",
                       amount=amounts[0][0], tax=round(amounts[0][0] * pct, 2))
    if paper and _has(q, "de 9c", "de9c", "wage"):
        kind = "paper_de9c"
    elif paper:
        kind = "paper_de9"
    elif _has(q, "de 542", "de542", "independent contractor"):
        kind = "de542"
    elif _has(q, "de 34", "de34", "new hire", "new employee", "rehire"):
        kind = "de34"
    elif _has(q, "de 9c", "de9c", "wage item", "unreported wages"):
        kind = "de9c"
    elif _has(q, "w-2") and _has(q, "fail", "not provide", "didn't provide", "did not provide"):
        kind = "w2"
    elif re.search(r"\bde 9\b", q) and not amounts:
        text = ("A late DE 9 quarterly return carries a penalty of 15% of the tax that is paid late plus interest "
                "(ask me with the tax amount and the due and paid dates for the figure), and a $20 penalty per "
                "employee wage item for wages reported late or not at all on the DE 9C. Filing the DE 9 on paper "
                "instead of electronically adds $50 per return, and even a no-wages return must be filed. You can "
                "request a waiver of penalty for good cause, and EDD may grant an extension of up to two months "
                f"when the governor declares an emergency that directly affects you. ({ep.CITATION})")
        return _result(base, text, None, "employer_filing_penalty_info", status="needs_review")
    else:
        return None
    with employer_db.get_conn() as conn:
        calc = ep.compute_filing_penalty(conn, kind, count, intentional)
    if not calc:
        return None
    unit, units = {"per_failure": ("failure to report", "failures to report"), "per_wage_item": ("wage item", "wage items"),
                   "per_return": ("return", "returns")}[calc["basis"]]
    n = f"{count} {units if count > 1 else unit}"
    text = (f"EDD may assess ${calc['per_unit']:,.0f} for each {unit}: {n} = ${calc['total']:,.2f}. "
            f"({calc['description']}.)")
    if kind in ("de34", "de542"):
        if intentional:
            text += " I used the $490 intentional rate."
        else:
            text += (" The penalty is $490 per failure instead if the failure is intentional (an agreement between "
                     "you and the worker not to report, or a false or incomplete report).")
        deadline = ("within 20 calendar days of the employee's start-of-work date" if kind == "de34" else
                    "within 20 calendar days of paying or contracting for $600 or more")
        text += f" The report is due {deadline}."
    if kind == "de9c":
        text += " That is on top of the 15% penalty and interest on any tax that was also paid late."
    text += (" These are the most EDD may charge, not a guaranteed assessment, and you can ask EDD to waive a penalty "
             "by showing good cause (Waiver of Penalty Policy, DE 231J). Interest and the 15% late-tax penalty are "
             f"separate. ({calc['citation']})")
    return _result(base, text, calc, "employer_filing_penalty", amount=float(count), tax=calc["total"])


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
    for fn in (_wages_answer, _household_answer, _reimbursable_answer, _vui_answer, _schedule_answer, _benefit_answer,
               _registration_answer, _filing_penalty_answer, _late_answer, _supplemental_answer, _deposit_answer, _ui_rate_answer, _cost_answer, _futa_answer):
        result = fn(question, base)
        if result:
            return result
    return None
