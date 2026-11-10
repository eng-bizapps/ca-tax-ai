"""Hand-computed sweep for the Illinois income-tax domain (tax year 2025).
Each case: (question, expected category, text that must appear). Expected
numbers were worked by hand from the IL-1040 rules (flat 4.95%, $2,850
exemptions, ...) and are NOT read back from the engine.

  python il_sweep.py
"""
import engine

CASES = [
    # tax = (AGI - exemptions) x 4.95%, rounded half-up to cents
    ("How much Illinois income tax do I owe on $60,000, single?", "il_income_compute", "$2,828.93"),
    ("How much Illinois income tax do I owe on $60,000 married filing jointly?", "il_income_compute", "$2,687.85"),
    ("How much Illinois income tax do I owe on $100,000, single with 2 dependents?", "il_income_compute", "$4,526.78"),
    ("How much Illinois income tax do I owe on $300,000, single?", "il_income_compute", "$14,850.00"),
    ("How much Illinois income tax do I owe on $250,001, single?", "il_income_compute", "$12,375.05"),
    ("How much Illinois income tax do I owe on $500,000 married filing jointly?", "il_income_compute", "$24,467.85"),
    ("How much Illinois income tax do I owe on $500,001 married filing jointly?", "il_income_compute", "$24,750.05"),
    ("How much Illinois income tax do I owe on $40,000, single and I am 65?", "il_income_compute", "$1,789.43"),
    ("I make $50,000 including $20,000 of Social Security, single. How much Illinois income tax do I owe?",
     "il_income_compute", "$1,343.93"),
    ("How much Illinois income tax do I owe on $75,000?", "il_compute_missing_status", "filing status"),
    ("How much Illinois income tax do I owe on $75,000 single for 2019?", "il_unsupported_year", "tax year 2025"),
    # property tax credit: 5%, limited to tax due
    ("I paid $6,000 in property tax on my home. What is my Illinois property tax credit?", "il_property_credit", "$300.00"),
    ("I paid $30,000 in property tax and earn $20,000, single. What is my Illinois property tax credit?",
     "il_property_credit", "limited to $848.93"),
    ("I paid $8,000 in property tax and earn $300,000, single. What is my Illinois property tax credit?",
     "il_property_credit", "No property tax credit"),
    # K-12: 25% of expenses over $250, max $750
    ("I paid $1,250 in tuition for my child's private school. What is the Illinois education expense credit?",
     "il_k12_credit", "$250.00"),
    ("I paid $5,000 in tuition for my child's private school. What is the Illinois education expense credit?",
     "il_k12_credit", "$750.00"),
    # EITC = 20% of federal, child credit = 40% of that
    ("My federal EITC is $2,000. What is my Illinois EITC?", "il_eitc", "$400.00"),
    ("My federal earned income credit is $2,000 and I have a child under 12. What is my Illinois EITC and child tax credit?",
     "il_eitc", "$560.00"),
    ("How much is the Illinois earned income credit?", "il_eitc_info", "$866.00"),
    # use tax table
    ("What is my Illinois use tax if my AGI is $45,000?", "il_use_tax", "$23.00"),
    ("What is my Illinois use tax if my AGI is $120,000?", "il_use_tax", "$60.00"),
    # informational
    ("What is the Illinois exemption allowance?", "il_exemption", "$2,850.00 per exemption"),
    ("What is the Illinois exemption for 2026?", "il_exemption", "$2,925.00"),
    ("What is the Illinois income tax rate?", "il_rate", "4.95%"),
    ("Is Social Security taxable in Illinois?", "il_retirement_income", "Line 5"),
    # Schedule CR: credit = lesser of tax paid and IL tax x (non-IL income / base income)
    ("I live in Illinois, single, with $80,000 of income, of which $30,000 was earned in Indiana, and I paid $1,500 in "
     "income tax to Indiana. What is my Illinois credit for tax paid to other states?", "il_other_state_credit", "$1,432.10"),
    ("I live in Illinois, single, with $80,000 of income, of which $30,000 was earned in Indiana, and I paid $1,000 in "
     "income tax to Indiana. What is my Illinois credit for tax paid to other states?", "il_other_state_credit", "$1,000.00"),
    ("I live in Illinois and work in Wisconsin, earning $60,000 in wages, and paid $2,000 in income tax to Wisconsin. "
     "Can I claim the Illinois credit for tax paid to other states?", "il_other_state_credit_reciprocal", "reciprocal agreement"),
    ("I paid $2,000 in income tax to Canada. Can I get the Illinois credit for tax paid to other states?",
     "il_other_state_credit_rules", "foreign country"),
    ("As a nonresident, can I take the Illinois credit for tax paid to other states?", "il_other_state_credit_rules",
     "nonresident of Illinois cannot"),
    ("What is the Illinois credit for tax paid to another state?", "il_other_state_credit_missing", "I still need"),
    # Schedule NR: decimal = IL income / total income; exemption prorated by it
    ("I'm a nonresident of Illinois with $80,000 of total income, $20,000 of it earned in Illinois, single. How much "
     "Illinois income tax do I owe?", "il_nonresident", "$954.73"),
    ("I'm a part-year resident of Illinois with $90,000 of total income, $45,000 of it Illinois income, married filing "
     "jointly. How much Illinois income tax do I owe?", "il_nonresident", "$2,086.43"),
    ("I live in Wisconsin and work in Illinois, earning $50,000 in wages. Do I owe Illinois income tax?",
     "il_nonresident_reciprocal", "No Illinois income tax"),
    ("I'm a nonresident, how much Illinois income tax do I owe on $50,000?", "il_nonresident_missing", "I still need"),
    # IL-2210 penalties: installment = min(90% x tax, prior-year tax) / 4, minus withholding/4; 10% if 31+ days late
    ("My 2025 Illinois tax after credits is $10,000, last year's tax was $8,000, and I had $2,000 withheld. I made no "
     "estimated payments. What is my underpayment penalty?", "il_penalty_underpayment", "$600."),
    ("My 2025 Illinois tax after credits is $10,000, last year's tax was $8,000, and I had $2,000 withheld. I paid $4,000 "
     "in estimated tax on January 10, 2026. What is my underpayment penalty?", "il_penalty_underpayment", "$500."),
    ("My 2025 Illinois tax after credits is $10,000, last year's tax was $20,000, and I had $8,500 withheld. I made no "
     "estimated payments. What is my underpayment penalty?", "il_penalty_underpayment", "$50."),
    ("My 2025 Illinois tax after credits is $3,000, last year's tax was $2,500, and I had $2,200 withheld. Do I owe an "
     "underpayment penalty?", "il_penalty_underpayment", "$1,000 or less"),
    ("My 2025 Illinois tax after credits is $10,000 and last year's tax was $0. Do I owe an underpayment penalty?",
     "il_penalty_underpayment", "was zero"),
    ("I'm a farmer with two-thirds of my income from farming. Do I owe the Illinois underpayment penalty on $20,000 of tax?",
     "il_penalty_underpayment", "a farmer"),
    ("I owe $3,000 on my 2025 Illinois return and paid it on May 20, 2026. What is the late payment penalty?",
     "il_penalty_late_payment", "$300.00"),
    ("I owe $3,000 on my 2025 Illinois return and paid it on May 1, 2026. What is the late payment penalty?",
     "il_penalty_late_payment", "$60.00"),
    ("I filed my 2025 Illinois return on November 20, 2026 and $5,000 of tax was unpaid on April 15. What is the late filing "
     "penalty?", "il_penalty_late_filing", "$100.00"),
    ("I filed my 2025 Illinois return on November 20, 2026 and $20,000 of tax was unpaid on April 15. What is the late "
     "filing penalty?", "il_penalty_late_filing", "$250.00"),
    ("I filed my 2025 Illinois return on October 1, 2026 and $5,000 of tax was unpaid on April 15. What is the late filing "
     "penalty?", "il_penalty_late_filing", "No Illinois late-filing penalty"),
    ("What are the Illinois penalties for underpaying estimated tax?", "il_penalty_rules", "2% if paid 1-30 days late"),
    # part-year Schedule CR: decimal .333; exemption 2,850 x .600 = 1,710; tax (60,000-1,710) x 4.95% = 2,885.36 (.355 rounds up)
    ("I was an Illinois part-year resident in 2025. My total income was $100,000, of which $60,000 was earned while I lived "
     "in Illinois, and $20,000 of that was earned in Indiana. I paid $1,200 in income tax to Indiana. I'm single. What is my "
     "Illinois credit for tax paid to other states?", "il_other_state_credit_part_year", "$960.82"),
    ("I was an Illinois part-year resident in 2025. My total income was $100,000, of which $60,000 was earned while I lived "
     "in Illinois, and $20,000 of that was earned in Indiana. I paid $800 in income tax to Indiana. I'm single. What is my "
     "Illinois credit for tax paid to other states?", "il_other_state_credit_part_year", "$800.00"),
    ("As a part-year resident, what is my Illinois credit for tax paid to another state?",
     "il_other_state_credit_part_year_missing", "I still need"),
    # IL-W-4 withholding (automated method): 4.95% x (wages - (L1 x 2,925 + L2 x 1,000)/periods) + extra
    ("I get paid $800 weekly with 2 allowances on IL-W-4 Line 1 and 2 additional allowances. What is my Illinois "
     "withholding per paycheck?", "il_withholding", "$32.13"),
    ("I get paid $2,000 biweekly with 1 allowance on my IL-W-4. What is my Illinois withholding per paycheck?",
     "il_withholding", "$93.43"),
    ("I get paid $5,000 monthly with 2 allowances and 1 additional allowance on my IL-W-4. What is my Illinois withholding "
     "per paycheck?", "il_withholding", "$219.24"),
    ("I get paid $800 weekly with 2 allowances and 2 additional allowances on my IL-W-4 and ask for an extra $10 withheld. "
     "What is my Illinois withholding per paycheck?", "il_withholding", "$42.13"),
    ("I earn a $100,000 salary paid biweekly with 1 allowance on my IL-W-4. What is my Illinois withholding per paycheck?",
     "il_withholding", "$184.82"),
    ("I get paid $50 weekly with 1 allowance on my IL-W-4. What is my Illinois withholding per paycheck?",
     "il_withholding", "$0.00"),
    ("What is my Illinois withholding per paycheck?", "il_withholding_missing", "I need the pay amount"),
    # other domains are refused, not answered
    ("What is the sales tax in Chicago?", "domain_not_covered", "not covered yet"),
    ("How do I register for Illinois unemployment insurance as an employer?", "domain_not_covered", "not covered yet"),
]

NEGATIVE = [  # must NOT produce a computed answer
    "What is the weather in Chicago?",
    "Is a wedding video taxable?",
]


def paycheck_cases():
    """Illinois paycheck module: federal + FICA reuse the ADP-verified federal payroll code (weekly $1,923.08, single,
    2020+ W-4, no adjustments: federal $253.27, Social Security $119.23, Medicare $27.88); Illinois withholding is
    hand-computed: (1,923.08 - 2,925/52) x 4.95% = 92.41; take-home = 1,923.08 - 253.27 - 119.23 - 27.88 - 92.41 = 1,430.29."""
    import il_db
    import income_db
    import il_payroll
    with il_db.get_conn() as il_conn, income_db.get_conn() as inc_conn:
        r = il_payroll.compute_il_paycheck(il_conn, inc_conn, 1923.08, "weekly", "single", il_basic_allowances=1)
        exempt = il_payroll.compute_il_paycheck(il_conn, inc_conn, 1923.08, "weekly", "single", exempt_il=True)
        bad_freq = il_payroll.compute_il_paycheck(il_conn, inc_conn, 1923.08, "daily", "single")
    want = {"federal_withholding": 253.27, "social_security": 119.23, "medicare": 27.88, "il_withholding": 92.41,
            "take_home_pay": 1430.29}
    results = []
    for k, v in want.items():
        results.append((f"paycheck {k}", r is not None and abs(r[k] - v) < 0.005))
    results.append(("paycheck exempt Illinois = 0", exempt is not None and exempt["il_withholding"] == 0.0))
    results.append(("paycheck unsupported frequency -> None", bad_freq is None))
    return results


def main():
    ok = bad = 0
    for label, good in paycheck_cases():
        ok, bad = ok + good, bad + (not good)
        print(("ok   " if good else "FAIL ") + "[paycheck] " + label)
    for q, cat, needle in CASES:
        r = engine.answer(q, state="IL")
        good = r.get("category") == cat and needle in (r.get("answer_text") or "")
        ok, bad = ok + good, bad + (not good)
        print(("ok   " if good else "FAIL ") + q)
        if not good:
            print(f"     got [{r.get('category')}] {(r.get('answer_text') or '')[:200]!r}")
    for q in NEGATIVE:
        r = engine.answer(q, state="IL")
        good = r.get("status") == "needs_review" and r.get("tax") is None and r.get("amount") is None
        ok, bad = ok + good, bad + (not good)
        print(("ok   " if good else "FAIL ") + "[negative] " + q)
    print(f"{ok} correct, {bad} wrong of {ok + bad}")


if __name__ == "__main__":
    main()
