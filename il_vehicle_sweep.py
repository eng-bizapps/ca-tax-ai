"""Illinois vehicle tax sweep: private party (RUT-50), dealer / out-of-state purchases (ST-556 / RUT-25),
moving into Illinois, and the cases that must be refused or clarified.

Expected values come from IDOR's 2026 RUT-5 / RUT-6 tables written out below by hand, and from an
independent reader of the raw rate file's auto-rate columns -- NOT from il_vehicle.py or the database.
The engine's date is pinned (il_sales.AS_OF_OVERRIDE) so a January/July rate refresh can't move expectations.

  python il_vehicle_sweep.py        # failures only
  python il_vehicle_sweep.py -v     # every case
"""
import datetime
import sys
from decimal import Decimal, ROUND_HALF_UP

import engine
import il_sales

D = Decimal
AS_OF = datetime.date(2026, 11, 17)
il_sales.AS_OF_OVERRIDE = AS_OF
LOC_FILE = "il_sales_data/ordmache-current-2026-11-17.txt"


def usd(x):
    return f"${D(str(x)).quantize(D('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def whole(x):
    return D(str(x)).quantize(D("1"), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ 2026 tables, typed from RUT-5 / RUT-6
def table_a(age):
    return {0: 465, 1: 465, 2: 365, 3: 290, 4: 240, 5: 190, 6: 165, 7: 155, 8: 140, 9: 125, 10: 115}.get(age, 100)


def table_b(price):
    for lo, tax in ((1000000, 10100), (100000, 5100), (50000, 2600), (30000, 1600), (25000, 1350), (20000, 1100), (15000, 850)):
        if price >= lo:
            return tax
    raise ValueError(price)


def state_pp(price, age):
    return table_a(age) if price < 15000 else table_b(price)


def chicago_pp(age, cat="standard"):
    if cat == "motorcycle":
        return 15
    if cat == "exception":
        return 15 if age <= 14 else 0
    return 80 if age <= 3 else 65 if age <= 8 else 50 if age <= 14 else 0


def cook_pp(age, cat="standard"):
    if cat == "motorcycle":
        return 90
    if cat == "exception":
        return 25
    return 225 if age <= 3 else 175 if age <= 8 else 90


# ------------------------------------------------------------------ independent raw reader: auto rates
def raw_auto():
    out = {}
    for line in open(LOC_FILE, encoding="latin-1"):
        line = line.rstrip("\n")
        if len(line) != 211:
            continue
        eff = datetime.date(int(line[61:65]), int(line[65:67]), int(line[67:69]))
        out[line[0:10]] = {"name": line[10:35].strip(), "county": line[35:60].strip(), "eff": eff,
                           "high": D(int(line[111:116])) / 100000, "low": D(int(line[116:121])) / 100000,
                           "phigh": D(int(line[190:195])) / 100000, "plow": D(int(line[195:200])) / 100000}
    return out


RAW = raw_auto()


def auto_rate(loc_id, as_of=AS_OF):
    r = RAW[loc_id]
    hi, lo = (r["high"], r["low"]) if as_of >= r["eff"] else (r["phigh"], r["plow"])
    return hi if r["name"] == "CHICAGO" else lo


PLACES = {"Chicago": "016-0001-1", "Evanston": "016-0018-4", "Champaign": "010-0005-5", "Rockford": "101-0001-6",
          "Bloomington": "057-0001-9", "Decatur": "058-0001-3", "Waukegan": "049-0001-4", "Woodstock": "056-0001-4",
          "Wheaton": "022-0001-5"}

CASES = []


def add(bucket, question, category, *needles):
    CASES.append((question, category, needles, bucket))


# ------------------------------------------------------------------ private party: state tax grid (no place given)
MAKES = ["Honda Civic", "Toyota Camry", "Ford F-150", "Subaru Outback", "Chevy Silverado", "Tesla Model 3", "Mazda CX-5"]
TPL = [
    "I bought a {y} {m} from a private seller for {p}. How much Illinois vehicle tax do I owe?",
    "I bought a {y} {m} from a neighbor for {p}. What tax do I owe the state?",
    "I bought a used {y} {m} on Craigslist for {p}. How much tax do I have to pay?",
]
i = 0
for model_year in (2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2012):
    price = 9000 + (i % 5) * 1200
    age = 2026 - model_year
    add("pp-state", TPL[i % 3].format(y=model_year, m=MAKES[i % len(MAKES)], p=f"${price:,}"), "il_vehicle_private_party",
        f"state tax {usd(state_pp(price, age))}")
    i += 1
for price in (15000, 19999, 20000, 24999, 25000, 29999, 30000, 49999, 50000, 99999, 100000, 999999, 1000000, 1200000):
    add("pp-state", TPL[i % 3].format(y=2021, m=MAKES[i % len(MAKES)], p=f"${price:,}"), "il_vehicle_private_party",
        f"state tax {usd(table_b(price))}")
    i += 1
add("pp-state", "I bought a 2020 Honda Civic from a private seller for $14,999. How much Illinois vehicle tax do I owe?",
    "il_vehicle_private_party", "state tax $165.00")

# ------------------------------------------------------------------ private party with Chicago / Cook / elsewhere
for model_year, price in ((2023, 14000), (2019, 12000), (2016, 9000), (2008, 4000), (2022, 28000)):
    age = 2026 - model_year
    state = state_pp(price, age)
    add("pp-chicago", f"I bought a {model_year} Honda Accord from a private seller for ${price:,} and I live in Chicago. What "
        f"vehicle tax do I owe?", "il_vehicle_private_party", usd(state + chicago_pp(age) + cook_pp(age)),
        f"Chicago private party vehicle tax: {usd(chicago_pp(age))}")
    add("pp-cook", f"I bought a {model_year} Honda Accord from a private seller for ${price:,} and I live in Evanston. What "
        f"vehicle tax do I owe?", "il_vehicle_private_party", usd(state + cook_pp(age)))
    add("pp-other", f"I bought a {model_year} Honda Accord from a private seller for ${price:,} and I live in Champaign. What "
        f"vehicle tax do I owe?", "il_vehicle_private_party", usd(state), "no local private party vehicle tax")

# ------------------------------------------------------------------ exceptions, gifts, motorcycles
add("pp-exception", "I bought a 2018 Toyota Camry from my brother for $8,000. What vehicle tax do I owe?",
    "il_vehicle_private_party", "state tax $15.00")
add("pp-exception", "My dad gave me his 2019 Ford F-150 worth $22,000. What tax do I owe?", "il_vehicle_private_party",
    "state tax $15.00")
add("pp-exception", "My mom sold me her 2021 Honda CR-V for $18,000 and I live in Evanston. What tax do I owe?",
    "il_vehicle_private_party", usd(15 + cook_pp(5, "exception")))
add("pp-exception", "My dad gave me his 2021 Honda CR-V and I live in Chicago. What tax do I owe on this car?",
    "il_vehicle_private_party", usd(15 + chicago_pp(5, "exception") + cook_pp(5, "exception")))
add("pp-exception", "My dad gave me his 2008 Honda Accord and I live in Chicago. What tax do I owe on this car?",
    "il_vehicle_private_party", usd(15 + chicago_pp(18, "exception") + cook_pp(18, "exception")))
add("pp-exception", "I bought a 2018 Toyota Camry from my stepdad for $8,000. What vehicle tax do I owe?",
    "il_vehicle_private_party", usd(state_pp(8000, 8)))
add("pp-exception", "I bought a 2018 Toyota Camry from my grandfather for $8,000. What vehicle tax do I owe?",
    "il_vehicle_private_party", usd(state_pp(8000, 8)))
add("pp-exception", "A friend gave me a 2017 Ford Focus worth $9,000. What tax do I owe?", "il_vehicle_private_party",
    usd(state_pp(9000, 9)))
add("pp-exception", "A neighbor gave me a 2019 Ford F-150 worth $22,000. What tax do I owe?", "il_vehicle_private_party",
    usd(table_b(22000)))
add("pp-exception", "I inherited a 2015 Buick from my aunt's estate. What vehicle tax do I owe?", "il_vehicle_private_party",
    "state tax $15.00")
add("pp-exception", "I inherited my husband's car through his estate; I'm the surviving spouse. What vehicle tax do I owe?",
    "il_vehicle_private_party", "$0.00")
add("pp-motorcycle", "I bought a motorcycle from a private seller for $5,000. What vehicle tax do I owe?",
    "il_vehicle_private_party", "state tax $25.00")
add("pp-motorcycle", "I bought a motorcycle from a private seller for $5,000 and I live in Chicago. What vehicle tax do I owe?",
    "il_vehicle_private_party", usd(25 + chicago_pp(0, "motorcycle") + cook_pp(0, "motorcycle")))
add("pp-motorcycle", "I bought an ATV from a private seller for $3,000 and I live in Champaign. What tax do I owe?",
    "il_vehicle_private_party", "state tax $25.00")

# ------------------------------------------------------------------ private party: clarifying / refusing
add("pp-missing", "I bought a car from a private seller. What tax do I owe?", "il_vehicle_missing_info", "price")
add("pp-missing", "I bought a car from a private seller for $9,000. What tax do I owe?", "il_vehicle_missing_info", "model year")
add("pp-missing", "I bought a 2021 car from a private seller for $9,000 and I live in Chicago. What tax do I owe?",
    "il_vehicle_private_party", usd(state_pp(9000, 5) + chicago_pp(5) + cook_pp(5)))
add("pp-missing", "On January 5, 2027 I bought a 2024 Honda Civic from a private seller for $14,000. What vehicle tax do I owe?",
    "il_sales_unsupported_date", "future")
add("pp-missing", "I bought a 2023 Honda Civic from a private seller for $14,000 on March 3, 2025. What vehicle tax do I owe?",
    "il_vehicle_unsupported_year", "2026")

# ------------------------------------------------------------------ dealer purchases: rate by registration place
DTPL = [
    "I bought a {p} car from a dealer and will register it in {c}. How much tax do I owe?",
    "How much tax on a {p} vehicle from a dealership if it's titled in {c}?",
    "I'm buying a {p} truck at a dealer. I live in {c}. What is the tax?",
]
i = 0
for city, lid in PLACES.items():
    for price in (10000, 23450, 30000, 52999):
        add("dealer", DTPL[i % 3].format(p=f"${price:,}", c=city), "il_vehicle_dealer_tax",
            usd(whole(D(price) * auto_rate(lid))), f"{auto_rate(lid) * 100:.2f}%")
        i += 1
    for price, trade in ((30000, 5000), (45000, 12500)):
        add("dealer-trade", f"I bought a ${price:,} car from a dealer with a ${trade:,} trade-in and will register it in {city}. "
            f"How much tax do I owe?", "il_vehicle_dealer_tax", usd(whole(D(price - trade) * auto_rate(lid))))
add("dealer", "I bought a $12,345 car from a dealer and will register it in Champaign. How much tax do I owe?",
    "il_vehicle_dealer_tax", "$772.00")
add("dealer", "I bought a $30,000 car from a dealer and will register it in Chicago. How much tax do I owe?",
    "il_vehicle_dealer_tax", "$2,625.00", "Chicago Home Rule")
add("dealer", "I bought a $20,000 car from a dealer and will register it in Alton. How much tax do I owe?",
    "il_vehicle_dealer_tax", "between $1,250.00 and $1,300.00", "Metro East")
add("dealer-dates", "On July 1, 2026 I bought a $30,000 car from a dealer and will register it in Chicago. How much tax do I owe?",
    "il_vehicle_dealer_tax", usd(whole(D(30000) * auto_rate("016-0001-1", datetime.date(2026, 7, 1)))))
add("dealer-dates", "On July 1, 2026 I bought a $30,000 car from a dealer and will register it in Evanston. How much tax do I owe?",
    "il_vehicle_dealer_tax", usd(whole(D(30000) * auto_rate("016-0018-4", datetime.date(2026, 7, 1)))))
add("dealer-missing", "I bought a $30,000 car from a dealer. How much tax do I owe?", "il_vehicle_missing_place", "8.75%")
add("dealer-missing", "I bought a car from a dealer and will register it in Chicago. How much tax do I owe?",
    "il_vehicle_missing_info", "purchase price")
add("dealer-missing", "I bought a $30,000 car from a dealer and will register it in Aurora. How much tax do I owe?",
    "il_vehicle_ambiguous_place", "Which county")

# ------------------------------------------------------------------ out-of-state retailer (RUT-25) with credit
for price, paid, city, lid in ((25000, 1750, "Champaign", "010-0005-5"), (25000, 1750, "Evanston", "016-0018-4"),
                                (25000, 500, "Evanston", "016-0018-4"), (40000, 2400, "Chicago", "016-0001-1"),
                                (18000, 0, "Rockford", "101-0001-6")):
    tax = whole(D(price) * auto_rate(lid))
    due = max(D(0), tax - D(paid))
    q = (f"I bought a ${price:,} truck from a dealer in Indiana"
         + (f", paid ${paid:,} in sales tax there," if paid else "") + f" and I'll register it in {city}. How much Illinois use tax "
         f"do I owe?")
    add("rut25", q, "il_vehicle_dealer_tax", usd(tax), "RUT-25", f"{usd(due)} is due" if paid else usd(tax))

# ------------------------------------------------------------------ moving into Illinois
MOVE = "I moved to Illinois with a car I bought in Ohio {m} months ago for {p}{paid}, registering in Champaign. How much use tax do I owe?"
for months, paid in ((1, 0), (2, 0), (2, 1300), (1, 100)):
    price = 20000
    dep = whole(D(price) * D("0.0167") * months)
    tax = whole((D(price) - dep) * D("0.0625"))
    due = max(D(0), tax - D(paid))
    add("moving", MOVE.format(m=months, p=f"${price:,}", paid=f" and paid ${paid:,} in sales tax there" if paid else ""),
        "il_vehicle_moving_in", usd(dep), usd(due))
add("moving", MOVE.format(m=3, p="$20,000", paid=""), "il_vehicle_moving_in", "No Illinois use tax")
add("moving", MOVE.format(m=8, p="$20,000", paid=" and paid $1,300 in sales tax there"), "il_vehicle_moving_in", "No Illinois use tax")

# ------------------------------------------------------------------ clarify / refuse
add("ask", "How much tax on a $40,000 car?", "il_vehicle_ask_source", "Which applies to you")
add("ask", "I bought a used car for $9,000. How much tax do I owe?", "il_vehicle_ask_source", "who sold it")
add("ask", "I bought a car from a dealer and a private seller. How much tax do I owe?", "il_vehicle_ambiguous_source", "Which was it")
add("refuse", "I leased a car. How much tax do I owe?", "il_vehicle_lease", "ST-556-LSE")
add("refuse", "How much tax on a $30,000 car lease in Chicago?", "il_vehicle_lease", "lease")
add("refuse", "I bought a boat from a dealer for $20,000. How much tax do I owe?", "il_vehicle_unsupported", "RUT-75")

NEGATIVE = [
    "Can I deduct my car on my Illinois income tax return?",
    "How much is car registration in Chicago?",
    "What is the Illinois gas tax per gallon?",
    "How much tax on a $30,000 car in California?",
    "What is the sales tax on a car wash in Chicago?",
]


def check(question, category, needles):
    r = engine.answer(question, state="IL")
    text = r.get("answer_text") or ""
    ok = r.get("category") == category and all(n in text for n in needles)
    if ok and r.get("status") != "needs_review" and r.get("citation") is None:
        ok = False
    return ok, r


def main():
    verbose = "-v" in sys.argv
    by_bucket, fails = {}, 0
    for question, category, needles, bucket in CASES:
        ok, r = check(question, category, needles)
        by_bucket.setdefault(bucket, [0, 0])[0 if ok else 1] += 1
        fails += not ok
        if verbose or not ok:
            print(("ok   " if ok else "FAIL ") + f"[{bucket}] {question}")
        if not ok:
            print(f"     want [{category}] {needles}\n     got  [{r.get('category')}] {(r.get('answer_text') or '')[:300]!r}")
    neg_bad = 0
    for question in NEGATIVE:
        r = engine.answer(question, state="IL")
        ok = r.get("tax") is None and r.get("amount") is None and not str(r.get("category", "")).startswith("il_vehicle_private") \
            and not str(r.get("category", "")).startswith("il_vehicle_dealer")
        neg_bad += not ok
        if verbose or not ok:
            print(("ok   " if ok else "FAIL ") + f"[negative] {question} -> [{r.get('category')}] {(r.get('answer_text') or '')[:110]!r}")
    total = len(CASES) + len(NEGATIVE)
    print("\nby bucket:", ", ".join(f"{b} {g}/{g + w}" for b, (g, w) in by_bucket.items()), f"| negatives {len(NEGATIVE) - neg_bad}/{len(NEGATIVE)}")
    print(f"{total - fails - neg_bad} correct, {fails + neg_bad} wrong of {total}")


if __name__ == "__main__":
    main()
