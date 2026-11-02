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
    # other domains are refused, not answered
    ("What is the sales tax in Chicago?", "domain_not_covered", "not covered yet"),
    ("How do I register for Illinois unemployment insurance as an employer?", "domain_not_covered", "not covered yet"),
]

NEGATIVE = [  # must NOT produce a computed answer
    "What is the weather in Chicago?",
    "Is a wedding video taxable?",
]


def main():
    ok = bad = 0
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
