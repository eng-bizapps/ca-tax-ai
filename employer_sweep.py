"""Regression sweep for the employer payroll-tax (EDD) domain -- Ring 5.

Every expected figure is worked by hand from the DE 44 rules, not copied from
the engine's own output. Runs employer_engine directly (no embedding calls),
so it is fast. `negative` cases must NOT trigger the employer domain.

  python employer_sweep.py
"""
import re
import sys

import employer_engine as ee

# (question, expected category, substring that must appear in answer_text)
POSITIVE = [
    # 3 x min($50,000, $7,000) x 3.4% = 714 UI; ETT 3 x 7 = 21 -> 735; SDI 3 x 650
    ("I'm an employer with 3 employees each paid $50,000 a year. What UI and ETT tax do I owe?",
     "employer_payroll_cost", "$735.00 total"),
    # $4,000 wages < $7,000 cap: UI 4,000 x 3.4% = 136, ETT 4; SDI 52
    ("As an employer, what is the UI and ETT on one employee paid $4,000 this year?",
     "employer_payroll_cost", "$140.00 per employee"),
    # experience rate 2.5%: 7,000 x 2.5% = 175 + 7 = 182
    ("Employer with one employee at $80,000 and a UI rate of 2.5%: UI and ETT cost?",
     "employer_payroll_cost", "$182.00 per employee"),
    # rate outside 1.5%-6.2%
    ("Employer with one employee at $80,000 and a UI rate of 9%: UI and ETT cost?",
     "employer_payroll_cost_bad_rate", "outside California's"),
    # no wages -> max per employee 7,000 x 3.4% + 7 = 245
    ("How much does an employee cost me in UI tax and ETT as an employer?",
     "employer_payroll_cost_info", "$245.00"),
    # Wed 11/4/2026 semi-weekly, > $400 -> following Wednesday 11/11
    ("I'm a semi-weekly depositor with $1,200 of PIT withholding accumulated, payday November 4, 2026. When is my EDD deposit due?",
     "employer_deposit_schedule", "Wednesday, November 11, 2026"),
    # Sat 11/7 payday -> following Friday 11/13
    ("Semi-weekly depositor, $900 of PIT withholding accumulated, payday 11/07/2026: when is the EDD payroll deposit due?",
     "employer_deposit_schedule", "Friday, November 13, 2026"),
    # 3 x ($42 + 7,000 x 1.5% = 105) = 441 ; high 3 x (42 + 371) = 1,239
    ("I'm an employer with 3 employees each paid $50,000 a year. What UI and ETT tax do I owe?",
     "employer_payroll_cost", "$147.00 per employee, $441.00 total"),
    ("I'm an employer with 3 employees each paid $50,000 a year. What UI and ETT tax do I owe?",
     "employer_payroll_cost", "$1,239.00 total"),
    # 2025 final: 7,000 x 0.6% = 42 + 7,000 x 1.2% = 84 -> 126
    ("What FUTA do I owe for one employee paid $50,000 in 2025?", "employer_futa", "$126.00 per employee"),
    # $4,000 wages in 2025: 24 + 48 = 72
    ("FUTA for an employee paid $4,000 in 2025 with the California credit reduction?", "employer_futa", "$72.00 per employee"),
    # 2026 preliminary range, 3 employees
    ("How much FUTA for 3 employees at $50,000 each?", "employer_futa", "November 10, 2026"),
    ("How much FUTA for 3 employees at $50,000 each?", "employer_futa", "$413.00 per employee"),
    ("What is the FUTA credit reduction for California in 2021?", "employer_futa_unsupported_year", "only have FUTA"),
    # ratio 22,000/400,000 = 0.055 -> F+ band [0.05, 0.06) = 4.7% ; 7,000 x 4.7% = 329 ; + ETT 7 = 336
    ("My UI reserve account balance is $22,000 and my average base payroll is $400,000. What's my UI rate?",
     "employer_ui_rate", "pays a UI rate of 4.7%"),
    ("My UI reserve account balance is $22,000 and my average base payroll is $400,000. What's my UI rate?",
     "employer_ui_rate", "$336.00 per employee"),
    # ratio -0.05 -> [-0.05, -0.04) = 6.2% ; 7,000 x 6.2% = 434 ; negative balance -> ETT 0.0%
    ("My UI reserve account balance is negative $10,000 with average taxable payroll of $200,000. What UI rate will I pay?",
     "employer_ui_rate", "$434.00 per employee"),
    ("My UI reserve account balance is negative $10,000 with average taxable payroll of $200,000. What UI rate will I pay?",
     "employer_ui_rate", "ETT is 0.0%"),
    # ratio 0.30 -> 0.20 or more = 1.5% in 2027 (F+ both years)
    ("EDD says my reserve ratio is 0.30. What is my UI rate for 2027?", "employer_ui_rate", "in effect for 2027"),
    # ratio 0 -> [0.00, 0.01) = 5.9% ; sits on a band edge -> flagged
    ("My UI reserve account balance is $0 and average base payroll is $200,000. UI rate?", "employer_ui_rate", "5.9%"),
    ("My UI reserve account balance is $20,000 and average base payroll is $400,000. UI rate?",
     "employer_ui_rate", "within 0.0005 of a band edge"),
    ("How is my UI rate determined by experience rating?", "employer_ui_rate_info", "Schedule F+"),
    ("My UI reserve account balance is $60,000 and average base payroll is $200,000 in 2021, UI rate?",
     "employer_ui_rate_unsupported", "2026 and 2027"),
    # exactly $400 is the monthly band for a semi-weekly depositor, due 12/15/2026 for a Nov payday
    ("Semi-weekly depositor with $400 of PIT withholding accumulated, payday November 4, 2026, EDD deposit due date?",
     "employer_deposit_schedule", "Tuesday, December 15, 2026"),
    # monthly federal depositor, $300 accumulated -> quarterly; Q3 payday -> delinquent Nov 2, 2026
    ("Monthly federal depositor with $300 of PIT withholding accumulated, payday August 3, 2026: EDD payroll deposit due?",
     "employer_deposit_schedule", "Monday, November 02, 2026"),
    # missing schedule/amount -> asks
    ("When is my EDD payroll deposit due?", "employer_deposit_info", "federal deposit schedule"),
    # 10,000 x 10.23% = 1,023.00 ; 10,000 x 6.6% = 660.00
    ("How much California withholding on a $10,000 bonus paid separately from regular wages?",
     "employer_supplemental_withholding", "$1,023.00"),
    ("California withholding on $10,000 of commissions paid separately from regular pay?",
     "employer_supplemental_withholding", "$660.00"),
    # 5,000 x 15% = 750; 61 days compounded daily at 7% (0.000192) on 5,750 = 67.73
    ("I paid $5,000 of payroll tax to EDD late, due October 15, 2026 and paid December 15, 2026. How much penalty and interest do I owe?",
     "employer_late_payment", "$750.00"),
    ("I paid $5,000 of payroll tax to EDD late, due October 15, 2026 and paid December 15, 2026. How much penalty and interest do I owe?",
     "employer_late_payment", "$67.73"),
    ("My payroll tax payment of $2,000 to EDD is late, what is the penalty?",
     "employer_late_payment_penalty_only", "$300.00"),
]

NEGATIVE = [
    "How are unemployment benefits calculated for a laid-off worker?",
    "How much should a landlord keep in a reserve account for repairs?",
    "Are federal unemployment benefits taxable in California?",
    "What is the sales tax on a $500 couch in Fresno?",
    "Is bonus depreciation allowed in California?",
    "my employer withheld SDI from my paycheck, is that right?",
    "How much California income tax on a $100,000 salary, single?",
    "I got a $10,000 bonus, how is it taxed?",
    "California property tax on a $600,000 house bought in 2020?",
]


def main():
    wrong = 0
    for q, cat, needle in POSITIVE:
        r = ee.answer_employer(q) if ee.has_any_signal(q) else None
        ok = r is not None and r["category"] == cat and needle in r["answer_text"]
        wrong += not ok
        print(("ok    " if ok else "WRONG ") + q[:78] + ("" if ok else f"\n      got {r and r['category']}: {r and r['answer_text'][:200]}"))
    for q in NEGATIVE:
        hit = ee.has_any_signal(q)
        wrong += hit
        print(("ok    " if not hit else "WRONG ") + "[negative] " + q[:66])
    print(f"\n{len(POSITIVE) + len(NEGATIVE) - wrong} correct, {wrong} wrong of {len(POSITIVE) + len(NEGATIVE)}")
    sys.exit(1 if wrong else 0)


if __name__ == "__main__":
    main()
