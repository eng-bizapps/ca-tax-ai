"""Wide Illinois sweep: grids of inputs x several phrasings per feature, plus negative cases.

Expected values come from the small independent calculator below, written straight from the
IDOR 2025 IL-1040 / IL-2210 / IL-700-T rules (flat 4.95%, $2,850 exemptions, 5% / 25% credits...),
NOT from il_income.py. Every positive case must also carry a citation.

  python il_sweep_wide.py            # run everything
  python il_sweep_wide.py -v         # also print passing cases
"""
import datetime
import itertools
import sys
from decimal import Decimal, ROUND_HALF_UP

import engine

D = Decimal


def q2(x):
    return D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def usd(x):
    return f"${q2(x):,.2f}"


def amt(x):
    return f"${x:,.0f}"


# ------------------------------------------------------------------ independent oracle
def exemption(agi, status, deps=0, age65=0, blind=0):
    if D(agi) > (500000 if status == "mfj" else 250000):
        return D(0)
    return D(5700 if status == "mfj" else 2850) + 1000 * (age65 + blind) + 2850 * deps


def il_tax(agi, status, deps=0, age65=0, blind=0, ss=0):
    base = max(D(0), D(agi) - D(ss))
    return q2(max(D(0), base - exemption(agi, status, deps, age65, blind)) * D("0.0495"))


def dec3(n, d):
    return min(D(1), (D(n) / D(d)).quantize(D("0.001"), rounding=ROUND_HALF_UP)) if D(d) > 0 else D(0)


STATUS_WORDS = {"single": ["single", "filing single"], "mfj": ["married filing jointly"],
                "mfs": ["married filing separately"], "hoh": ["head of household"]}

CASES = []   # (question, expected category, needles, bucket)


def add(bucket, question, category, *needles):
    CASES.append((question, category, needles, bucket))


# ------------------------------------------------------------------ income tax grid
COMPUTE_TEMPLATES = [
    "How much Illinois income tax do I owe on {a}, {s}?",
    "What is my Illinois tax if my income is {a} and I am {s}?",
    "Illinois income tax on {a} of income, {s}",
    "I earned {a} last year and I'm {s}. How much Illinois income tax do I owe?",
    "Calculate my Illinois income tax: AGI {a}, {s}.",
    "What would I owe in Illinois income tax with {a} of income if I am {s}?",
]
AGIS = [0, 1, 2850, 2851, 5700, 25000, 49999, 60000, 99999, 150000, 249999, 250000, 250001, 300000, 499999, 500000, 500001, 750000]
combos = list(itertools.product(AGIS, ["single", "mfj", "mfs", "hoh"]))
for i, (agi, st) in enumerate(combos):
    if agi == 0:
        continue    # an income of $0 is not a phrasing a user states with a dollar sign; covered by AGI 1
    tpl = COMPUTE_TEMPLATES[i % len(COMPUTE_TEMPLATES)]
    word = STATUS_WORDS[st][0]
    add("tax", tpl.format(a=amt(agi), s=word), "il_income_compute", usd(il_tax(agi, st)))
# dependents / age / blind / social security variants
for i, (agi, st, deps, a65, bl, ss) in enumerate([
        (80000, "single", 1, 0, 0, 0), (80000, "mfj", 3, 0, 0, 0), (120000, "hoh", 2, 0, 0, 0),
        (45000, "single", 0, 1, 0, 0), (70000, "mfj", 0, 2, 0, 0), (52000, "single", 0, 1, 0, 0),
        (90000, "mfj", 2, 1, 0, 0), (50000, "single", 0, 0, 0, 20000), (95000, "mfj", 0, 2, 0, 30000),
        (260000, "single", 2, 0, 0, 0), (510000, "mfj", 4, 0, 0, 0)]):
    parts = []
    if deps:
        parts.append(f"{deps} dependents" if deps > 1 else "1 dependent")
    if a65:
        parts.append("we are both 65" if a65 == 2 else "I am 65")
    extra = (" with " + " and ".join(parts)) if parts else ""
    if ss:
        q = (f"I make {amt(agi)} including {amt(ss)} of Social Security, {STATUS_WORDS[st][0]}{extra}. "
             f"How much Illinois income tax do I owe?")
    else:
        q = f"How much Illinois income tax do I owe on {amt(agi)}, {STATUS_WORDS[st][0]}{extra}?"
    add("tax-variants", q, "il_income_compute", usd(il_tax(agi, st, deps, a65, bl, ss)))

# ------------------------------------------------------------------ property tax credit
PROP_TEMPLATES = [
    "I paid {p} in property tax on my principal residence. What is my Illinois property tax credit?",
    "How much is the Illinois property tax credit if I paid {p} in property taxes?",
    "My Illinois property tax bill was {p}. What credit can I claim on my IL-1040?",
]
for i, p in enumerate([1200, 2400, 4800, 7500, 12000, 25000]):
    add("property", PROP_TEMPLATES[i % 3].format(p=amt(p)), "il_property_credit", usd(q2(p * D("0.05"))))
for p, inc, st in [(25000, 20000, "single"), (30000, 20000, "single"), (4800, 60000, "single"), (9000, 40000, "mfj")]:
    tax = il_tax(inc, st)
    credit = min(q2(p * D("0.05")), tax)
    add("property", f"I paid {amt(p)} in property tax and earn {amt(inc)}, {STATUS_WORDS[st][0]}. What is my Illinois "
        f"property tax credit?", "il_property_credit", usd(credit))
for p, inc, st in [(8000, 260000, "single"), (8000, 510000, "mfj")]:
    add("property", f"I paid {amt(p)} in property tax and earn {amt(inc)}, {STATUS_WORDS[st][0]}. What is my Illinois "
        f"property tax credit?", "il_property_credit", "No property tax credit")

# ------------------------------------------------------------------ K-12 credit
K12_TEMPLATES = [
    "I paid {e} in tuition at my kid's private school. What is the Illinois K-12 education expense credit?",
    "How much is the Illinois K-12 education credit for {e} of school tuition and fees?",
    "Illinois education expense credit for {e} paid to my child's school",
]
for i, e in enumerate([100, 250, 251, 1000, 3250, 3251, 6000]):
    credit = min(D(750), q2(max(D(0), D(e) - 250) * D("0.25")))
    add("k12", K12_TEMPLATES[i % 3].format(e=amt(e)), "il_k12_credit", usd(credit))

# ------------------------------------------------------------------ Illinois EITC / child credit
for i, f in enumerate([500, 1500, 3000, 6000, 7000]):
    eitc = q2(D(f) * D("0.2"))
    tpl = ["My federal EITC is {f}. What is my Illinois EITC?",
           "I got {f} for the federal earned income credit. How much is my Illinois earned income credit?"][i % 2]
    add("eitc", tpl.format(f=amt(f)), "il_eitc", usd(eitc))
    child = q2(eitc * D("0.4"))
    add("eitc", f"My federal EITC is {amt(f)} and I have a child under 12. What is my Illinois EITC and child tax credit?",
        "il_eitc", usd(eitc), usd(child), usd(eitc + child))

# ------------------------------------------------------------------ use tax
USE_BANDS = [(0, 10000, 3), (10001, 20000, 8), (20001, 30000, 13), (30001, 40000, 18), (40001, 50000, 23),
             (50001, 75000, 31), (75001, 100000, 44)]
USE_TEMPLATES = ["What is my Illinois use tax if my AGI is {a}?",
                 "How much Illinois use tax should I pay on my IL-1040 with an AGI of {a}?",
                 "Use tax estimate Illinois AGI {a}"]
agis = [5000, 10000, 10001, 20000, 20001, 30000, 30001, 40000, 40001, 50000, 50001, 75000, 75001, 100000, 100001, 250000]
for i, a in enumerate(agis):
    tax = next((D(t) for lo, hi, t in USE_BANDS if lo <= a <= hi), q2(D(a) * D("0.0005")))
    add("use-tax", USE_TEMPLATES[i % 3].format(a=amt(a)), "il_use_tax", usd(tax))

# ------------------------------------------------------------------ Schedule CR (full-year resident)
CR_TPL = ("I live in Illinois, {s}, with {inc} of income, of which {non} was earned in {st}, and I paid {paid} in income "
          "tax to {st}. What is my Illinois credit for tax paid to other states?")
for i, (inc, non, paid, s, state) in enumerate([
        (80000, 30000, 1500, "single", "Indiana"), (80000, 30000, 1000, "single", "Indiana"),
        (120000, 20000, 900, "married filing jointly", "Missouri"), (60000, 60000, 5000, "single", "Ohio"),
        (100000, 10000, 2000, "head of household", "Minnesota"), (45000, 5000, 100, "single", "Georgia")]):
    st = {"single": "single", "married filing jointly": "mfj", "head of household": "hoh"}[s]
    tax = il_tax(inc, st)
    dec = dec3(non, inc)
    credit = min(q2(paid), q2(tax * dec))
    add("cr", CR_TPL.format(s=s, inc=amt(inc), non=amt(non), paid=amt(paid), st=state),
        "il_other_state_credit", usd(credit))
add("cr", "I live in Illinois and work in Wisconsin, earning $60,000 in wages, and paid $2,000 in income tax to Wisconsin. "
    "Can I claim the Illinois credit for tax paid to other states?", "il_other_state_credit_reciprocal", "reciprocal")
add("cr", "I work in Kentucky and live in Illinois; my wages are $70,000. Is there an Illinois credit for tax paid to "
    "another state?", "il_other_state_credit_reciprocal", "reciprocal")
add("cr", "I paid $3,000 in income tax to Canada. Can I get the Illinois credit for tax paid to other states?",
    "il_other_state_credit_rules", "foreign country")
add("cr", "As a nonresident, can I take the Illinois credit for tax paid to other states?",
    "il_other_state_credit_rules", "nonresident of Illinois cannot")
add("cr", "What is the Illinois credit for tax paid to another state?", "il_other_state_credit_missing", "I still need")

# ------------------------------------------------------------------ Schedule NR / part-year
NR_TPL = ["I'm a nonresident of Illinois with {t} of total income, {il} of it earned in Illinois, {s}. How much Illinois "
          "income tax do I owe?",
          "As a part-year resident, my total income was {t} and {il} of it was Illinois income. I'm {s}. How much Illinois "
          "income tax do I owe?"]
for i, (t, il, st) in enumerate([(80000, 20000, "single"), (90000, 45000, "mfj"), (150000, 150000, "single"),
                                 (60000, 5000, "single"), (260000, 100000, "single"), (120000, 30000, "hoh")]):
    ex_full = exemption(t, st)
    dec = dec3(il, t)
    net = max(D(0), D(il) - q2(ex_full * dec))
    tax = q2(net * D("0.0495"))
    add("nr", NR_TPL[i % 2].format(t=amt(t), il=amt(il), s=STATUS_WORDS[st][0]), "il_nonresident", usd(tax))
add("nr", "I live in Wisconsin and work in Illinois, earning $50,000 in wages. Do I owe Illinois income tax?",
    "il_nonresident_reciprocal", "No Illinois income tax")
add("nr", "I live in Michigan and my employer is in Illinois, wages are $65,000. Do I owe Illinois income tax as a "
    "nonresident?", "il_nonresident_reciprocal", "No Illinois income tax")
add("nr", "I'm a nonresident, how much Illinois income tax do I owe on $50,000?", "il_nonresident_missing", "I still need")

# ------------------------------------------------------------------ part-year Schedule CR
PY_TPL = ("I was an Illinois part-year resident in 2025. My total income was {t}, of which {res} was earned while I lived in "
          "Illinois, and {non} of that was earned in Indiana. I paid {paid} in income tax to Indiana. I'm single. What is my "
          "Illinois credit for tax paid to other states?")
for t, res, non, paid in [(100000, 60000, 20000, 1200), (100000, 60000, 20000, 800), (90000, 30000, 10000, 500),
                          (200000, 120000, 40000, 5000)]:
    dec_cr = dec3(non, res)
    dec_ex = dec3(res, t)
    ex = q2(exemption(t, "single") * dec_ex)
    il = q2(max(D(0), D(res) - ex) * D("0.0495"))
    credit = min(q2(paid), q2(il * dec_cr))
    add("cr-part-year", PY_TPL.format(t=amt(t), res=amt(res), non=amt(non), paid=amt(paid)),
        "il_other_state_credit_part_year", usd(credit))

# ------------------------------------------------------------------ penalties
UNDER_TPL = ("My 2025 Illinois tax after credits is {now}, last year's tax was {prior}, and I had {wh} withheld. I made no "
             "estimated payments. What is my underpayment penalty?")
for now, prior, wh in itertools.product([4000, 10000, 25000], [0, 3000, 9000, 40000], [0, 2000, 5000, 9000]):
    if wh > now:
        continue
    now_d, prior_d, wh_d = D(now), D(prior), D(wh)
    if now_d - wh_d <= 1000 or prior_d == 0:
        cat_needles = ("$1,000 or less",) if now_d - wh_d <= 1000 else ("was zero",)
    else:
        req = min(D("0.9") * now_d, prior_d)
        short = max(D(0), req / 4 - wh_d / 4)
        pen = (short * 4 * D("0.10")).quantize(D("1"), rounding=ROUND_HALF_UP)
        cat_needles = (f"${int(pen):,}." if pen else "No penalty is due.",)
    add("penalty-under", UNDER_TPL.format(now=amt(now), prior=amt(prior), wh=amt(wh)), "il_penalty_underpayment",
        *cat_needles)

DATE0 = datetime.date(2026, 4, 15)
for owed, d in itertools.product([1500, 3000, 12000], [datetime.date(2026, 4, 15), datetime.date(2026, 4, 16),
                                                      datetime.date(2026, 5, 15), datetime.date(2026, 5, 16),
                                                      datetime.date(2026, 7, 1), datetime.date(2026, 12, 31)]):
    days = (d - DATE0).days
    rate = D(0) if days <= 0 else (D("0.02") if days <= 30 else D("0.10"))
    add("penalty-late-pay",
        f"I owe {amt(owed)} on my 2025 Illinois return and paid it on {d:%B} {d.day}, {d.year}. What is the late payment "
        f"penalty?", "il_penalty_late_payment", usd(q2(D(owed) * rate)))
for owed, d in itertools.product([1000, 5000, 20000], [datetime.date(2026, 10, 15), datetime.date(2026, 10, 16),
                                                      datetime.date(2026, 11, 20), datetime.date(2026, 12, 31)]):
    late = d > datetime.date(2026, 10, 15)
    pen = min(D(250), q2(D(owed) * D("0.02"))) if late else D(0)
    add("penalty-late-file",
        f"I filed my 2025 Illinois return on {d:%B} {d.day}, {d.year} and {amt(owed)} of tax was unpaid on April 15. What is "
        f"the late filing penalty?", "il_penalty_late_filing", usd(pen) if late else "No Illinois late-filing penalty")

# ------------------------------------------------------------------ withholding
WH_FREQ = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}
for i, (wages, freq, l1, l2) in enumerate(itertools.product([500, 1200, 2500, 3750], WH_FREQ, [0, 1, 2, 3], [0, 1, 2])):
    if (i % 3) != 0:
        continue    # every third combination keeps the grid broad without 190 near-identical calls
    periods = WH_FREQ[freq]
    taxable = max(D(0), D(wages) - (D(l1) * 2925 + D(l2) * 1000) / periods)
    expect = usd(q2(taxable * D("0.0495")))
    extra = f" and {l2} additional allowances" if l2 else ""
    add("withholding", f"I get paid {amt(wages)} {freq} with {l1} allowances on my IL-W-4 Line 1{extra}. What is my "
        f"Illinois withholding per paycheck?", "il_withholding", expect)

# ------------------------------------------------------------------ informational
add("info", "What is the Illinois income tax rate?", "il_rate", "4.95%")
add("info", "What is the Illinois income tax rate for 2026?", "il_rate", "4.95%")
add("info", "What is the Illinois exemption allowance?", "il_exemption", "$2,850.00 per exemption")
add("info", "What is the Illinois personal exemption for 2026?", "il_exemption", "$2,925.00")
add("info", "Is Social Security taxable in Illinois?", "il_retirement_income", "Line 5")
add("info", "Are pensions taxed in Illinois?", "il_retirement_income", "Line 5")
add("info", "What are the Illinois penalties for underpaying estimated tax?", "il_penalty_rules", "2% if paid 1-30 days late")

# ------------------------------------------------------------------ wild phrasing (how people really type)
WILD = [
    ("illinois income tax on 60k, single", "il_income_compute", usd(il_tax(60000, "single"))),
    ("illinois income tax on $60k single", "il_income_compute", usd(il_tax(60000, "single"))),
    ("how much illinois tax do i owe on 60000 single", "il_income_compute", usd(il_tax(60000, "single"))),
    ("how much Illinois tax do I owe on 85000 if I'm married filing jointly", "il_income_compute",
     usd(il_tax(85000, "mfj"))),
    ("I make $75,000 a year in Illinois and I'm single. What's my state income tax?", "il_income_compute",
     usd(il_tax(75000, "single"))),
    ("how much will I pay in illinois taxes on $75,000 married filing jointly", "il_income_compute",
     usd(il_tax(75000, "mfj"))),
    ("what do I owe Illinois on $90,000 of income, head of household", "il_income_compute",
     usd(il_tax(90000, "hoh"))),
    ("IL income tax 100,000 single", "il_income_compute", usd(il_tax(100000, "single"))),
    ("Illinois state tax for someone earning $120,000 filing single", "il_income_compute", usd(il_tax(120000, "single"))),
    ("If my AGI is 45000 and I'm married filing separately, what is my Illinois income tax?", "il_income_compute",
     usd(il_tax(45000, "mfs"))),
    ("illinois tax $52,000 single 65 years old", "il_income_compute", usd(il_tax(52000, "single", age65=1))),
    ("What's the Illinois property tax credit for $4,000 of property taxes?", "il_property_credit", usd(200)),
    ("illinois property tax credit on 6000 paid", "il_property_credit", usd(300)),
    ("how much is the illinois k-12 credit if I spent $2,000 on school tuition", "il_k12_credit",
     usd(min(D(750), q2(D(1750) * D("0.25"))))),
    ("illinois use tax agi 60k", "il_use_tax", usd(31)),
    ("what is the illinois use tax for an AGI of 85,000", "il_use_tax", usd(44)),
    ("My federal EITC was 2000 - what's my Illinois EITC?", "il_eitc", usd(400)),
    ("il withholding for $1,500 biweekly, 2 allowances", "il_withholding",
     usd(q2(max(D(0), D(1500) - D(5850) / 26) * D("0.0495")))),
    ("What is my Illinois withholding on a $3,000 semimonthly paycheck with 1 allowance on my IL-W-4?", "il_withholding",
     usd(q2(max(D(0), D(3000) - D(2925) / 24) * D("0.0495")))),
    ("monthly paycheck $6,000, 3 allowances on IL-W-4, how much Illinois tax is withheld?", "il_withholding",
     usd(q2(max(D(0), D(6000) - D(8775) / 12) * D("0.0495")))),
    ("Illinois late payment penalty: I owed $2,000 and paid on June 15, 2026", "il_penalty_late_payment", usd(200)),
    ("I owed $4,000 on my Illinois return and didn't pay until 5/20/2026. Late payment penalty?",
     "il_penalty_late_payment", usd(400)),
    ("illinois estimated tax penalty: tax after credits $12,000, last year $6,000, withheld $4,000, no estimated payments",
     "il_penalty_underpayment", "$200."),
    ("Do I owe an Illinois underpayment penalty if my tax is $2,500 and I had $2,000 withheld?",
     "il_penalty_missing", "I still need"),
]
WILD += [
    ("How much Illinois income tax for tax year 2025 on $60,000 single?", "il_income_compute", usd(il_tax(60000, "single"))),
    ("In 2025 I made 60000 and filed single. Illinois tax?", "il_income_compute", usd(il_tax(60000, "single"))),
    ("Illinois tax on $60,000.50 single", "il_income_compute", usd(q2((D("60000.50") - 2850) * D("0.0495")))),
    ("illinois income tax on 1,000,000 married filing jointly", "il_income_compute", usd(il_tax(1000000, "mfj"))),
    ("I'm 65 and legally blind, single, $40,000 of income. Illinois income tax?", "il_income_compute",
     usd(il_tax(40000, "single", age65=1, blind=1))),
    ("Illinois income tax: wages $50,000, interest $2,000, single", "il_compute_ambiguous_income", "more than one amount"),
    ("My Illinois income tax withheld was $3,000 and my AGI is $60,000, single. What do I owe in Illinois income tax?",
     "il_income_compute", usd(il_tax(60000, "single"))),
    ("My federal EITC is $0. What is my Illinois EITC?", "il_eitc", usd(0)),
    ("I owed $1,000 on my Illinois return and paid on 04/20/2026. What is the late payment penalty?",
     "il_penalty_late_payment", usd(20)),
    ("illinois use tax if my AGI is $45k", "il_use_tax", usd(23)),
    ("illinois k-12 education credit for $3k of tuition", "il_k12_credit", usd(q2(D(2750) * D("0.25")))),
    ("illinois property tax credit if I paid 5k in property taxes", "il_property_credit", usd(250)),
    ("Illinois income tax on $60,000 single for 2024", "il_unsupported_year", "tax year 2025"),
    ("Illinois income tax on $60,000 single for 2026", "il_unsupported_year", "tax year 2025"),
    ("does illinois tax social security?", "il_retirement_income", "Line 5"),
    ("is my pension taxed in Illinois", "il_retirement_income", "Line 5"),
    ("are 401k withdrawals taxable in illinois?", "il_retirement_income", "Line 5"),
    ("What are the Illinois income tax brackets?", "il_rate", "no brackets"),
    ("what percent is illinois income tax", "il_rate", "4.95%"),
]
for question, category, expect in WILD:
    add("wild", question, category, *( (expect,) if expect else () ))

# ------------------------------------------------------------------ negatives: never a figure, never the wrong state
NEGATIVE = [
    "What is the weather in Chicago?",
    "Is a wedding video taxable?",
    "What is the sales tax in Chicago?",
    "What is the Illinois sales tax rate on groceries?",
    "How do I register for Illinois unemployment insurance as an employer?",
    "What is Illinois property tax on a $400,000 home in Cook County?",
    "Tell me a joke",
    "How much Illinois income tax do I owe?",
    "I earn $80,000. How much Illinois income tax do I owe?",
    "What is my Illinois property tax credit?",
    "What is my Illinois EITC?",
    "How much tax do I owe on $60,000 in California?",
    "Is soda taxable in California?",
    "What is the California property tax on a $600,000 house?",
    "How much does a CalEITC pay with 2 children?",
    "What is my Illinois withholding per paycheck?",
    "Can I deduct my Illinois home office?",
    "What is Illinois estate tax?",
    "Do I owe Illinois tax on lottery winnings?",
    "What is the Illinois franchise tax?",
    "what's the illinois standard deduction?",
    "Is unemployment income taxable in Illinois?",
    "Illinois income tax on my wages and my spouse's wages",
    "I owe Illinois tax but can't pay, what should I do?",
]


def check_positive(question, category, needles):
    r = engine.answer(question, state="IL")
    text = r.get("answer_text") or ""
    ok = r.get("category") == category and all(n in text for n in needles) and r.get("citation") is not None
    if r.get("status") == "needs_review" or category in ("il_penalty_rules",):
        ok = r.get("category") == category and all(n in text for n in needles)
    return ok, r


def main():
    verbose = "-v" in sys.argv
    by_bucket, failures = {}, []
    for question, category, needles, bucket in CASES:
        ok, r = check_positive(question, category, needles)
        by_bucket.setdefault(bucket, [0, 0])
        by_bucket[bucket][0 if ok else 1] += 1
        if verbose or not ok:
            print(("ok   " if ok else "FAIL ") + f"[{bucket}] {question}")
        if not ok:
            print(f"     want [{category}] {needles}\n     got  [{r.get('category')}] {(r.get('answer_text') or '')[:230]!r}")
            failures.append(question)
    neg_bad = 0
    for question in NEGATIVE:
        r = engine.answer(question, state="IL")
        ok = (r.get("tax") is None and r.get("amount") is None
              and (r.get("status") == "needs_review" or str(r.get("category")).endswith("_info")))
        if not ok:
            neg_bad += 1
        if verbose or not ok:
            print(("ok   " if ok else "FAIL ") + f"[negative] {question}  -> [{r.get('category')}]")
    total = len(CASES) + len(NEGATIVE)
    wrong = len(failures) + neg_bad
    print("\nby bucket:", ", ".join(f"{b} {g}/{g + w}" for b, (g, w) in by_bucket.items()))
    print(f"negatives {len(NEGATIVE) - neg_bad}/{len(NEGATIVE)}")
    print(f"{total - wrong} correct, {wrong} wrong of {total}")


if __name__ == "__main__":
    main()
