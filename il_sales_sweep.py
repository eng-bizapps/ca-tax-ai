"""Illinois sales and use tax sweep: grids of cities x amounts x phrasings, item taxability, grocery,
use tax, and negative cases.

Expected values come from an independent reader of IDOR's raw fixed-width files below (column
slices written from IDOR's published record layouts), plus the PIO-101 matrix facts written out by
hand -- NOT from il_sales.py or the database. The engine's date is pinned to the data's retrieval date
so a January/July refresh of IDOR's files can't silently change expectations.

  python il_sales_sweep.py        # failures only
  python il_sales_sweep.py -v     # every case
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
GROC_FILE = "il_sales_data/grocerymache-current-2026-11-17.txt"


def q2(x):
    return D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def usd(x):
    return f"${q2(x):,.2f}"


def pct(r):
    p = (D(str(r)) * 100).quantize(D("0.01"))
    return f"{p}%"


# ------------------------------------------------------------------ independent oracle (raw column slices)
def _d(t):
    return datetime.date(int(t[0:4]), int(t[4:6]), int(t[6:8]))


def raw_locations():
    out = {}
    for line in open(LOC_FILE, encoding="latin-1"):
        line = line.rstrip("\n")
        if len(line) != 211:
            continue
        rec = {"id": line[0:10], "name": line[10:35].strip(), "county": line[35:60].strip(), "addr": line[60] == "Y",
               "eff": _d(line[61:69]), "gm": D(int(line[69:74])) / 100000, "dm": D(int(line[74:79])) / 100000,
               "gm_lo": D(int(line[79:84])) / 100000, "rflag": line[89] == "Y",
               "peff": _d(line[132:140]), "pend": _d(line[140:148]), "pgm": D(int(line[148:153])) / 100000,
               "pdm": D(int(line[153:158])) / 100000}
        out[rec["id"]] = rec          # a later duplicate (Marion) wins, as in the loader
    return out


def raw_grocery():
    out = {}
    for line in open(GROC_FILE, encoding="latin-1"):
        line = line.rstrip("\n")
        if len(line) == 106:
            out[line[0:10]] = {"eff": _d(line[60:68]), "high": D(int(line[68:73])) / 100000,
                               "low": D(int(line[73:78])) / 100000, "peff": _d(line[79:87]), "pend": _d(line[87:95]),
                               "phigh": D(int(line[95:100])) / 100000}
    return out


LOCS, GROC = raw_locations(), raw_grocery()


def gm_rate(loc_id, as_of=AS_OF):
    r = LOCS[loc_id]
    return r["gm"] if as_of >= r["eff"] else r["pgm"]


def dm_rate(loc_id, as_of=AS_OF):
    r = LOCS[loc_id]
    return r["dm"] if as_of >= r["eff"] else r["pdm"]


def groc_rate(loc_id, as_of=AS_OF):
    g = GROC[loc_id]
    return g["high"] if as_of >= g["eff"] else g["phigh"]


# well-formed cities: one record, no address/receipts flag (looked up by id in IDOR's file)
CITIES = {"Chicago": "016-0001-1", "Evanston": "016-0018-4", "Champaign": "010-0005-5", "Urbana": "010-0001-2",
          "Bloomington": "057-0001-9", "Normal": "057-0023-1", "Decatur": "058-0001-3", "Rockford": "101-0001-6"}
for _c, _i in CITIES.items():
    assert not LOCS[_i]["addr"] and not LOCS[_i]["rflag"], _c

CASES = []   # (question, category, needles, bucket)


def add(bucket, question, category, *needles):
    CASES.append((question, category, needles, bucket))


# ------------------------------------------------------------------ tax on a purchase (general merchandise)
TPL = [
    "How much sales tax on a {a} couch in {c}?",
    "What is the sales tax on a {a} purchase in {c}, Illinois?",
    "I'm buying a {a} bookcase in {c}. How much is the tax?",
    "sales tax on {a} in {c}",
    "How much would I pay in sales tax for a {a} lamp in {c}?",
    "What's the total with tax for a {a} chair in {c}?",
]
AMTS = [10, 49.99, 100, 250, 1234.56, 5000]
i = 0
for city, lid in CITIES.items():
    for amt in AMTS:
        rate = gm_rate(lid)
        tax = q2(D(str(amt)) * rate)
        a = f"${amt:,.2f}" if amt != int(amt) else f"${int(amt):,}"
        tpl = TPL[i % len(TPL)]
        i += 1
        if "total with tax" in tpl:
            add("tax-general", tpl.format(a=a, c=city), "il_sales_tax", usd(D(str(amt)) + tax), pct(rate))
        else:
            add("tax-general", tpl.format(a=a, c=city), "il_sales_tax", usd(tax), pct(rate))

# ------------------------------------------------------------------ drugs / medical appliances, groceries
for city in ("Chicago", "Evanston", "Champaign", "Decatur"):
    lid = CITIES[city]
    for item, amt in (("ibuprofen", 20), ("a wheelchair", 800), ("insulin", 150)):
        add("tax-drug", f"How much sales tax on {'$' + str(amt)} of {item} in {city}?" if not item.startswith("a ")
            else f"How much sales tax on a ${amt} wheelchair in {city}?", "il_sales_tax",
            usd(q2(D(amt) * dm_rate(lid))), pct(dm_rate(lid)))
    for amt in (100, 87.5):
        g = groc_rate(lid)
        add("tax-grocery", f"How much sales tax on ${amt} of groceries in {city}?", "il_sales_tax", usd(q2(D(str(amt)) * g)))
add("tax-grocery", "What's the tax on $60 of groceries in Chicago?", "il_sales_tax", usd(q2(D(60) * groc_rate("016-0001-1"))), "1.50%")
add("tax-grocery", "Is there sales tax on groceries in Chicago?", "il_sales_item", "1.50%")
add("tax-grocery", "Is there sales tax on groceries in Illinois?", "il_sales_item", "ended January 1, 2026")

# ------------------------------------------------------------------ plain rate questions
for city, lid in CITIES.items():
    add("rate", f"What is the sales tax rate in {city}?", "il_sales_rate", pct(gm_rate(lid)), pct(dm_rate(lid)),
        pct(groc_rate(lid)))
    add("rate", f"sales tax in {city}, IL", "il_sales_rate", pct(gm_rate(lid)))
add("rate", "What is the sales tax rate in Cook County?", "il_sales_rate", pct(gm_rate("016-5000-1")), "unincorporated")
add("rate", "What is the sales tax rate in Madison County?", "il_sales_rate", "6.60% to 6.85%", "Metro East")

# ------------------------------------------------------------------ explicit dates (current vs prior period)
for city in ("Chicago", "Evanston"):
    lid = CITIES[city]
    before = datetime.date(2026, 7, 1)
    add("dates", f"What was the sales tax rate in {city} on July 1, 2026?", "il_sales_rate", pct(gm_rate(lid, before)))
    add("dates", f"How much sales tax on a $100 coat in {city} on August 15, 2026?", "il_sales_tax",
        usd(q2(D(100) * gm_rate(lid, datetime.date(2026, 8, 15)))))
add("dates", "What is the sales tax rate in Chicago in 2019?", "il_sales_unsupported_date", "exact date")
add("dates", "What was the sales tax rate in Chicago on March 3, 2010?", "il_sales_unsupported_date", "outside both")

# ------------------------------------------------------------------ county seat / county hints / ambiguity
add("ambiguous", "What is the sales tax rate in Aurora?", "il_sales_ambiguous_place", "Which county")
add("ambiguous", "How much sales tax on $100 in Elgin?", "il_sales_ambiguous_place", "Which county")
add("ambiguous", "sales tax in Schaumburg", "il_sales_ambiguous_place", "Which county")
add("ambiguous", "What is the sales tax rate in Aurora in Kane County?", "il_sales_rate", pct(gm_rate("045-0002-4")))
add("ambiguous", "How much sales tax on $200 in Elgin, Kane County?", "il_sales_tax", usd(q2(D(200) * gm_rate("045-0009-1"))))
add("ambiguous", "How much sales tax on $200 in Schaumburg in DuPage County?", "il_sales_tax",
    usd(q2(D(200) * gm_rate("022-0066-1"))))
add("ambiguous", "How much sales tax on a $40 shirt in Rockford?", "il_sales_tax", usd(q2(D(40) * gm_rate("101-0001-6"))),
    "Ogle County")
add("ambiguous", "How much sales tax on a $40 shirt in Rockford in Ogle County?", "il_sales_tax",
    usd(q2(D(40) * gm_rate("071-0038-8"))))
add("ambiguous", "What's the sales tax from Chicago to Evanston?", "il_sales_multiple_places", "one place at a time")

# ------------------------------------------------------------------ address-specific locations are not guessed
for city in ("Springfield", "Peoria", "Joliet", "Naperville"):
    add("address", f"What is the sales tax rate in {city}?" if city in ("Springfield", "Peoria") else
        f"How much sales tax on $100 in {city}, Will County?" if city == "Joliet" else
        f"How much sales tax on $100 in {city}, DuPage County?", "il_sales_unresolved_address", "Tax Rate Finder")
add("address", "How much sales tax on a $100 jacket in Springfield?", "il_sales_unresolved_address", "6.25%")

# ------------------------------------------------------------------ item taxability (state level, no city)
ITEMS_TAXABLE = [("candy", "Candy"), ("soda", "Soft drinks"), ("clothing", "Clothing"), ("cigarettes", "Cigarettes"),
                 ("canned software", "Computer software"), ("books", "Books"), ("pet dogs", "Pets")]
for word, name in ITEMS_TAXABLE:
    add("item", f"Is {word} taxable in Illinois?", "il_sales_item", "general merchandise rate", "6.25%")
for word in ("ibuprofen", "a wheelchair", "insulin", "diabetic test strips"):
    add("item", f"Is {word} taxed in Illinois?" if word != "diabetic test strips" else "Is there sales tax on glucose test strips in Illinois?",
        "il_sales_item", "low rate", "1%")
for word in ("milk", "vitamins", "infant formula", "bread"):
    add("item", f"Is {word} subject to sales tax in Illinois?", "il_sales_item", "no state tax")
for word in ("tampons", "breast pumps"):
    add("item", f"Are {word} taxable in Illinois?", "il_sales_item", "exempt")
for word in ("ebooks", "gift cards", "newspapers", "garage sale items"):
    add("item", f"Is sales tax charged on {word} in Illinois?", "il_sales_item", "not subject to Illinois sales tax")
add("item", "Is sales tax charged on a car in Illinois?", "il_vehicle_ask_source", "who sold it")
add("item", "Do I pay sales tax on dry cleaning in Illinois?", "il_sales_item_conditional", "Dry cleaning",
    "not subject to sales tax")
add("item", "Is a haircut subject to sales tax in Illinois?", "il_sales_services", "service")
add("item", "Is flowers taxable in Illinois?", "il_sales_item_ambiguous", "Tell me which")

# item + city
add("item", "Is candy taxable in Chicago and what's the rate?", "il_sales_item", "10.50%")
add("item", "Is soda taxed in Evanston?", "il_sales_item", pct(gm_rate("016-0018-4")))
add("item", "Is milk taxed in Chicago?", "il_sales_item", pct(groc_rate("016-0001-1")))
add("item", "Is soda taxable in Illinois, and how much on a $5 six-pack in Peoria?", "il_sales_unresolved_address",
    "Soft drinks are taxable")

# ------------------------------------------------------------------ use tax
for price, paid, expect in [(2000, 0, 125), (500, 0, 31.25), (2000, 50, 75), (500, 40, 0), (1000, 62.5, 0)]:
    q = (f"I bought a ${price:,} laptop online from a seller that charged no Illinois tax. How much Illinois use tax do I owe?"
         if not paid else f"I bought a ${price:,} laptop in Missouri and paid ${paid} in sales tax there. How much Illinois use "
         f"tax do I owe?")
    add("use-tax", q, "il_sales_use_tax", usd(expect))
add("use-tax", "I bought $100 of ibuprofen from an out-of-state website with no tax. How much Illinois use tax do I owe?",
    "il_sales_use_tax", "$1.00")
add("use-tax", "Do I owe Illinois use tax on groceries I bought out of state?", "il_sales_use_tax", "no state use tax")
add("use-tax", "What is Illinois use tax?", "il_sales_use_tax_info", "6.25%")
add("use-tax", "Is there local use tax in Illinois?", "il_sales_use_tax_info", "no local use tax")

# ------------------------------------------------------------------ messy phrasing
add("wild", "sales tax on $100 chicago", "il_sales_tax", usd(q2(D(100) * gm_rate("016-0001-1"))))
add("wild", "how much is tax on 250 dollars of stuff in evanston", "il_sales_tax", usd(q2(D(250) * gm_rate("016-0018-4"))))
add("wild", "illinois sales tax rate chicago", "il_sales_rate", "10.50%")
add("wild", "Chicago sales tax rate", "il_sales_rate", "10.50%")
add("wild", "whats the tax rate in champaign il", "il_sales_rate", pct(gm_rate("010-0005-5")))
add("wild", "tax on $1,000 TV in Bloomington", "il_sales_tax", usd(q2(D(1000) * gm_rate("057-0001-9"))))
add("wild", "If I buy a $80 pair of shoes in Normal, IL what's the tax?", "il_sales_tax", usd(q2(D(80) * gm_rate("057-0023-1"))))

# ------------------------------------------------------------------ negatives: must never produce a figure
NEGATIVE = [
    "How much sales tax on a $500 couch in Atlantis?",
    "How much sales tax on $100?",
    "What is the sales tax?",
    "Is a couch taxable in Illinois?",
    "What is the sales tax in Los Angeles?",
    "How much sales tax on a $500 couch in California?",
    "How much sales tax on $100 in Mars?",
    "How much sales tax do I charge on my consulting services in Chicago?",
    "Do I pay sales tax on Netflix in Illinois?",
    "What is the Illinois property tax rate in Cook County?",
    "How do I register to collect Illinois sales tax?",
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
            print(f"     want [{category}] {needles}\n     got  [{r.get('category')}] {(r.get('answer_text') or '')[:260]!r}")
    neg_bad = 0
    for question in NEGATIVE:
        r = engine.answer(question, state="IL")
        ok = r.get("tax") is None and r.get("amount") is None and r.get("status") == "needs_review"
        neg_bad += not ok
        if verbose or not ok:
            print(("ok   " if ok else "FAIL ") + f"[negative] {question} -> [{r.get('category')}] {(r.get('answer_text') or '')[:120]!r}")
    total = len(CASES) + len(NEGATIVE)
    print("\nby bucket:", ", ".join(f"{b} {g}/{g + w}" for b, (g, w) in by_bucket.items()), f"| negatives {len(NEGATIVE) - neg_bad}/{len(NEGATIVE)}")
    print(f"{total - fails - neg_bad} correct, {fails + neg_bad} wrong of {total}")


if __name__ == "__main__":
    main()
