"""Illinois individual income-tax computations (IL-1040), numbers read from the
Illinois income database (il_schema.py). Pure functions over a connection:
no chat parsing here (see il_engine.py).

Only what the IL-1040 instructions fully define is computed. Federal AGI,
the federal EITC and Schedule M items are inputs the user supplies.
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def _d(x):
    return Decimal(str(x))


def money(x):
    return _d(x).quantize(CENT, rounding=ROUND_HALF_UP)


def param(conn, tax_year, key):
    row = conn.execute(
        "SELECT value FROM il_income_params WHERE tax_year=%s AND param_key=%s", (tax_year, key)).fetchone()
    if row is None:
        raise KeyError(f"Illinois parameter {key} for {tax_year} not loaded")
    return _d(row[0])


def has_param(conn, tax_year, key):
    return conn.execute(
        "SELECT 1 FROM il_income_params WHERE tax_year=%s AND param_key=%s", (tax_year, key)).fetchone() is not None


def agi_limit(conn, tax_year, filing_status):
    return param(conn, tax_year, "agi_limit_joint" if filing_status == "mfj" else "agi_limit_other")


def compute_exemption(conn, tax_year, filing_status, agi, base_income, dependents=0, age65=0, blind=0,
                      claimed_as_dependent=False):
    """Step 4 Line 10: base allowance chart + 65/blind boxes + dependents; zero above the AGI cutoff.
    Returns dict(total, base, extras, dependents, cutoff_applied)."""
    if _d(agi) > agi_limit(conn, tax_year, filing_status):
        return {"total": Decimal(0), "base": Decimal(0), "extras": Decimal(0), "dependents": Decimal(0),
                "cutoff_applied": True}
    per = param(conn, tax_year, "exemption_per_person")
    if filing_status == "mfj":
        base = per * 2
    else:
        base = per
        if claimed_as_dependent and _d(base_income) > per:
            base = Decimal(0)
    extras = param(conn, tax_year, "exemption_65_blind_each") * (age65 + blind)
    dep_amt = param(conn, tax_year, "dependent_exemption") * dependents
    return {"total": base + extras + dep_amt, "base": base, "extras": extras, "dependents": dep_amt,
            "cutoff_applied": False}


def compute_income_tax(conn, tax_year, filing_status, agi, additions=0, ss_retirement=0, other_subtractions=0,
                       dependents=0, age65=0, blind=0, claimed_as_dependent=False):
    """Illinois resident return, Lines 1-12. Returns a dict with every intermediate figure."""
    agi = _d(agi)
    base_income = max(Decimal(0), agi + _d(additions) - _d(ss_retirement) - _d(other_subtractions))
    ex = compute_exemption(conn, tax_year, filing_status, agi, base_income, dependents, age65, blind,
                           claimed_as_dependent)
    net_income = max(Decimal(0), base_income - ex["total"])
    rate = param(conn, tax_year, "rate")
    tax = money(net_income * rate)
    return {"tax_year": tax_year, "agi": agi, "base_income": base_income, "exemption": ex, "net_income": net_income,
            "rate": rate, "tax": tax}


def compute_property_credit(conn, tax_year, filing_status, agi, property_tax, tax_available, business_portion=0):
    """Schedule ICR Section A: 5% of qualifying property tax, limited to tax due; none above the AGI cutoff."""
    if _d(agi) > agi_limit(conn, tax_year, filing_status):
        return {"credit": Decimal(0), "cutoff_applied": True, "computed": Decimal(0)}
    computed = money((_d(property_tax) - _d(business_portion)) * param(conn, tax_year, "property_credit_rate"))
    credit = min(computed, _d(tax_available)) if tax_available is not None else computed
    return {"credit": credit, "cutoff_applied": False, "computed": computed}


def compute_k12_credit(conn, tax_year, filing_status, agi, expenses, tax_available=None):
    """Schedule ICR Section B: 25% of expenses over $250, capped at $750; none above the AGI cutoff."""
    if _d(agi) > agi_limit(conn, tax_year, filing_status):
        return {"credit": Decimal(0), "cutoff_applied": True, "computed": Decimal(0)}
    over = max(Decimal(0), _d(expenses) - param(conn, tax_year, "k12_threshold"))
    computed = min(money(over * param(conn, tax_year, "k12_rate")), param(conn, tax_year, "k12_max"))
    credit = min(computed, _d(tax_available)) if tax_available is not None else computed
    return {"credit": credit, "cutoff_applied": False, "computed": computed}


def compute_eitc(conn, tax_year, federal_eitc, child_under_12=False):
    """Schedule IL-E/EITC Lines 7-12 for an Illinois resident (decimal on Line 8 = 1.0)."""
    eitc = money(_d(federal_eitc) * param(conn, tax_year, "eitc_rate"))
    child = money(eitc * param(conn, tax_year, "child_credit_rate")) if child_under_12 else Decimal(0)
    return {"eitc": eitc, "child_credit": child}


def eitc_max(conn, tax_year, children):
    row = conn.execute("SELECT max_credit FROM il_eitc_max WHERE tax_year=%s AND children=%s",
                       (tax_year, min(children, 3))).fetchone()
    return _d(row[0]) if row else None


def compute_use_tax(conn, tax_year, agi):
    """IL-1040 Use Tax (UT) Table by AGI."""
    agi = _d(agi)
    if agi > 100000:
        return money(agi * param(conn, tax_year, "use_tax_table_pct_over_100k"))
    row = conn.execute(
        "SELECT use_tax FROM il_use_tax_table WHERE tax_year=%s AND agi_min <= %s AND agi_max >= %s",
        (tax_year, int(agi), int(agi))).fetchone()
    return _d(row[0]) if row else None


def decimal3(numerator, denominator):
    """Schedule CR / NR decimal: ratio rounded to three places, capped at 1.000."""
    if _d(denominator) <= 0:
        return Decimal(0)
    return min(Decimal(1), (_d(numerator) / _d(denominator)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def reciprocal_states(conn):
    return {r[0] for r in conn.execute("SELECT state_name FROM il_reciprocal_states").fetchall()}


def compute_other_state_credit(conn, tax_year, il_tax, base_income, non_il_income, other_state_tax):
    """Schedule CR Steps 4 and 6 (full-year resident): lesser of other-state tax paid (Line 51) and
    Illinois tax x decimal (Line 54), where decimal = non-Illinois / total base income."""
    dec = decimal3(non_il_income, base_income)
    limit = money(_d(il_tax) * dec)
    credit = min(money(other_state_tax), limit)
    return {"decimal": dec, "limit": limit, "credit": credit}


def compute_nonresident_tax(conn, tax_year, filing_status, total_agi, il_portion, dependents=0, age65=0, blind=0):
    """Schedule NR Step 5: Illinois base income = the Illinois portion; decimal = Illinois / total base
    income; the exemption allowance computed as a full-year resident is prorated by that decimal."""
    total_base = max(Decimal(0), _d(total_agi))
    il_base = max(Decimal(0), _d(il_portion))
    ex = compute_exemption(conn, tax_year, filing_status, total_agi, total_base, dependents, age65, blind)
    dec = decimal3(il_base, total_base)
    ex_il = money(ex["total"] * dec)
    net = max(Decimal(0), il_base - ex_il)
    tax = money(net * param(conn, tax_year, "rate"))
    return {"il_base": il_base, "total_base": total_base, "decimal": dec, "full_exemption": ex["total"],
            "exemption": ex_il, "net_income": net, "tax": tax, "cutoff_applied": ex["cutoff_applied"]}


# ---------------------------------------------------------------- penalties (Form IL-2210)
def filing_date(conn, tax_year, key):
    row = conn.execute("SELECT due_date FROM il_filing_dates WHERE tax_year=%s AND date_key=%s",
                       (tax_year, key)).fetchone()
    if row is None:
        raise KeyError(f"Illinois date {key} for {tax_year} not loaded")
    return row[0]


def _late_rate(conn, tax_year, days_late):
    """IL-2210 penalty rates: none if on time, 2% for 1-30 days, 10% for 31 or more."""
    if days_late <= 0:
        return Decimal(0)
    if days_late <= int(param(conn, tax_year, "lp_short_days")):
        return param(conn, tax_year, "lp_rate_short")
    return param(conn, tax_year, "lp_rate_long")


def compute_underpayment_penalty(conn, tax_year, tax_now, tax_prior, withholding, payments=(), exempt=False):
    """IL-2210 Steps 2-4 (regular method, equal installments). tax_now / tax_prior are Line 3 of each year (tax
    plus surcharges less credits); withholding is total Illinois withholding (treated as paid evenly); payments is
    [(date, amount)] of estimated payments. Returns every intermediate figure and the penalty in whole dollars."""
    tax_now, tax_prior, withholding = _d(tax_now), _d(tax_prior), _d(withholding)
    line5 = tax_now - withholding
    out = {"line3_now": tax_now, "line3_prior": tax_prior, "line5": line5, "required": Decimal(0),
           "installment": Decimal(0), "quarters": [], "penalty": Decimal(0), "reason": None}
    if exempt:
        out["reason"] = "exempt"
        return out
    if line5 <= param(conn, tax_year, "estimated_tax_threshold"):
        out["reason"] = "under_threshold"
        return out
    line6 = tax_now * param(conn, tax_year, "safe_harbor_current")
    line7 = min(line6, tax_prior)
    out["line6"] = line6
    if line7 <= 0:
        out["reason"] = "prior_year_zero"
        return out
    installment = line7 / 4
    out.update({"required": line7, "installment": installment})
    pool = [[d, _d(a)] for d, a in sorted(payments, key=lambda x: x[0])]
    carry = Decimal(0)
    total = Decimal(0)
    for n in (1, 2, 3, 4):
        due = filing_date(conn, tax_year, f"q{n}")
        line11 = installment - withholding / 4
        line13 = line11 - carry
        unpaid = max(Decimal(0), line13)
        carry = max(Decimal(0), -line13)
        pieces, q_pen = [], Decimal(0)
        for item in pool:
            if unpaid <= 0:
                break
            if item[1] <= 0:
                continue
            applied = min(item[1], unpaid)
            days = (item[0] - due).days
            rate = _late_rate(conn, tax_year, days)
            q_pen += applied * rate
            pieces.append((item[0], applied, days, rate))
            item[1] -= applied
            unpaid -= applied
        if unpaid > 0:
            rate = param(conn, tax_year, "lp_rate_long")
            q_pen += unpaid * rate
            pieces.append((None, unpaid, None, rate))
        out["quarters"].append({"quarter": n, "due": due, "unpaid": max(Decimal(0), line13), "penalty": q_pen,
                                "pieces": pieces})
        total += q_pen
    out["penalty"] = total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    out["reason"] = "computed"
    return out


def compute_unpaid_tax_penalty(conn, tax_year, unpaid, paid_date=None):
    """IL-2210 Penalty Worksheet 2: tax still owing after the original due date. paid_date=None means never paid."""
    due = filing_date(conn, tax_year, "original_due")
    if paid_date is None:
        days, rate = None, param(conn, tax_year, "lp_rate_long")
    else:
        days = (paid_date - due).days
        rate = _late_rate(conn, tax_year, days)
    return {"due": due, "days": days, "rate": rate, "penalty": money(_d(unpaid) * rate)}


def compute_late_filing_penalty(conn, tax_year, unpaid_at_original_due, filed_date):
    """IL-2210 Step 5: only if filed after the extended due date AND tax was not paid by the original due date."""
    ext = filing_date(conn, tax_year, "extended_due")
    unpaid = _d(unpaid_at_original_due)
    if filed_date <= ext or unpaid <= 0:
        return {"extended_due": ext, "two_percent": Decimal(0), "penalty": Decimal(0)}
    two = money(unpaid * param(conn, tax_year, "late_filing_rate"))
    return {"extended_due": ext, "two_percent": two, "penalty": min(two, param(conn, tax_year, "late_filing_cap"))}


# ---------------------------------------------------------------- employee withholding (IL-W-4)
WH_PERIODS = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}


def compute_il_withholding(conn, wages_this_period, pay_frequency, basic_allowances=0, additional_allowances=0,
                           extra_withholding=0, exempt=False, tax_year=2026):
    """IDOR 2026 Booklet IL-700-T automated payroll method:
    withhold = 4.95% x (wages - (L1 x $2,925 + L2 x $1,000) / pay periods) + IL-W-4 Line 3 extra. Returns None for an
    unsupported pay frequency (never a guessed number)."""
    periods = WH_PERIODS.get(pay_frequency)
    if not periods:
        return None
    wages = _d(wages_this_period)
    extra = _d(extra_withholding)
    if exempt:
        return {"withholding": money(extra), "exemption_this_period": Decimal(0), "taxable": Decimal(0), "periods": periods}
    annual_ex = (_d(basic_allowances) * param(conn, tax_year, "wh_allowance_basic")
                 + _d(additional_allowances) * param(conn, tax_year, "wh_allowance_additional"))
    ex_period = annual_ex / periods
    taxable = max(Decimal(0), wages - ex_period)
    tax = money(taxable * param(conn, tax_year, "wh_rate") + extra)
    return {"withholding": tax, "exemption_this_period": ex_period, "taxable": taxable, "periods": periods}


# ---------------------------------------------------------------- Schedule CR, part-year residents
def compute_part_year_other_state_credit(conn, tax_year, filing_status, total_base, resident_base, non_il_income,
                                         other_state_tax, dependents=0, age65=0, blind=0):
    """Schedule CR Steps 4-6 for a part-year resident. resident_base = base income received while an Illinois
    resident (Line 42 Column A); total_base = base income on Form IL-1040 Line 9. Step 5 prorates the exemption by
    resident_base / total_base to get the Illinois tax due, then the decimal and lesser-of test apply."""
    dec_cr = decimal3(non_il_income, resident_base)
    dec_ex = decimal3(resident_base, total_base)
    ex = compute_exemption(conn, tax_year, filing_status, total_base, total_base, dependents, age65, blind)
    ex_il = money(ex["total"] * dec_ex)
    line48 = max(Decimal(0), _d(resident_base) - ex_il)
    il_tax = money(line48 * param(conn, tax_year, "rate"))
    limit = money(il_tax * dec_cr)
    credit = min(money(other_state_tax), limit)
    return {"decimal_credit": dec_cr, "decimal_exemption": dec_ex, "full_exemption": ex["total"], "exemption": ex_il,
            "line48": line48, "il_tax": il_tax, "limit": limit, "credit": credit}
