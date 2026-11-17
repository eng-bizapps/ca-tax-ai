"""Schema + loader for the Illinois sales and use tax domain (Illinois Department of Revenue) --
its own physically-separate database (see il_sales_db.py).

Sources, all from tax.illinois.gov and stored under il_sales_data/ so the load is reproducible:
  * Retailers' Occupation Tax county/municipality rate file (current + prior period per location)
  * Grocery-tax locations and rates file (effective January 1, 2026)
  * PIO-101 Illinois Sales & Use Tax Matrix (R-01/26): item-level taxability
Tables:
  il_sales_locations    one row per IDOR location id: sales (receipts) and use (purchases) rates, auto rates, flags
  il_grocery_locations  local grocery-tax rates (the state's 1% grocery rate ended 2026-01-01)
  il_sales_matrix_items PIO-101 rows: description, taxable/exempt, high/low rate, explanation, references
  il_sales_params       state-level rates with an effective date
  il_sales_sources      which file/version each table was loaded from
  il_sales_inventory    completeness ledger (built | partial | deferred | out_of_scope)

Usage:
  python il_sales_schema.py create
  python il_sales_schema.py seed
  python il_sales_schema.py status
"""
import sys

import il_sales_db as db
import il_sales_parse as parse

DATA = "il_sales_data/"
LOCATION_FILE = DATA + "ordmache-current-2026-11-17.txt"
GROCERY_FILE = DATA + "grocerymache-current-2026-11-17.txt"
MATRIX_FILE = DATA + "pio-101-R-01-26.pdf"
BASE = "https://tax.illinois.gov"
LOCATION_URL = BASE + "/content/dam/soi/en/web/tax/research/taxrates/documents/salestaxrates/ordmache-current.txt"
GROCERY_URL = BASE + "/content/dam/soi/en/web/tax/research/taxrates/documents/salestaxrates/grocerymache-current.txt"
MATRIX_URL = BASE + "/content/dam/soi/en/web/tax/research/taxinformation/sales/documents/pio-101-illinois-tax-matrix.pdf"

_PERIODS = ("", "prior_")
_LOC_COLS = ["location_id", "name", "county", "address_flag", "effective", "receipts_gm_high", "receipts_dm_high",
             "receipts_gm_low", "receipts_dm_low", "receipts_flag", "purchases_gm_high", "purchases_dm_high",
             "purchases_gm_low", "purchases_dm_low", "purchases_flag", "auto_high", "auto_low", "auto_med_fee_pct",
             "auto_max_med_fee", "auto_flag", "prior_effective", "prior_end", "prior_receipts_gm_high",
             "prior_receipts_dm_high", "prior_receipts_gm_low", "prior_receipts_dm_low", "prior_receipts_flag",
             "prior_purchases_gm_high", "prior_purchases_dm_high", "prior_purchases_gm_low", "prior_purchases_dm_low",
             "prior_purchases_flag", "prior_auto_high", "prior_auto_low", "prior_auto_med_fee_pct",
             "prior_auto_max_med_fee", "prior_auto_flag"]
_GROC_COLS = ["location_id", "name", "county", "effective", "grocery_high", "grocery_low", "flag", "prior_effective",
              "prior_end", "prior_grocery_high", "prior_grocery_low", "prior_flag"]


def _col_type(c):
    if c in ("location_id", "name", "county"):
        return "TEXT NOT NULL" if c != "county" else "TEXT"
    if c.endswith("flag"):
        return "BOOLEAN NOT NULL DEFAULT FALSE"
    if c.endswith("effective") or c.endswith("end"):
        return "DATE"
    return "NUMERIC"


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS il_sales_locations (\n    "
    + ",\n    ".join(f"{c} {_col_type(c)}" + (" PRIMARY KEY" if c == "location_id" else "") for c in _LOC_COLS)
    + "\n);\n\nCREATE INDEX IF NOT EXISTS il_sales_locations_name ON il_sales_locations (upper(name));\n\n"
    "CREATE TABLE IF NOT EXISTS il_grocery_locations (\n    "
    + ",\n    ".join(f"{c} {_col_type(c)}" + (" PRIMARY KEY" if c == "location_id" else "") for c in _GROC_COLS)
    + "\n);\n"
    """
CREATE TABLE IF NOT EXISTS il_sales_matrix_items (
    item_id     TEXT PRIMARY KEY,
    page        INTEGER,
    description TEXT NOT NULL,
    taxable     TEXT,
    rate        TEXT,
    explanation TEXT,
    "references" TEXT,
    source      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS il_vehicle_pp_table_a (
    tax_year INTEGER NOT NULL,
    age_min  INTEGER NOT NULL,
    age_max  INTEGER,
    tax      NUMERIC NOT NULL,
    PRIMARY KEY (tax_year, age_min)
);

CREATE TABLE IF NOT EXISTS il_vehicle_pp_table_b (
    tax_year  INTEGER NOT NULL,
    price_min NUMERIC NOT NULL,
    price_max NUMERIC,
    tax       NUMERIC NOT NULL,
    PRIMARY KEY (tax_year, price_min)
);

CREATE TABLE IF NOT EXISTS il_vehicle_pp_local (
    jurisdiction TEXT NOT NULL,
    category     TEXT NOT NULL,
    age_min      INTEGER NOT NULL,
    age_max      INTEGER,
    tax          NUMERIC NOT NULL,
    PRIMARY KEY (jurisdiction, category, age_min)
);

CREATE TABLE IF NOT EXISTS il_sales_item_aliases (
    alias   TEXT NOT NULL,
    item_id TEXT NOT NULL,
    note    TEXT,
    PRIMARY KEY (alias, item_id)
);

CREATE TABLE IF NOT EXISTS il_sales_params (
    id             SERIAL PRIMARY KEY,
    param_key      TEXT NOT NULL,
    effective_from DATE NOT NULL,
    value          NUMERIC NOT NULL,
    description    TEXT,
    citation       TEXT,
    source_url     TEXT,
    UNIQUE (param_key, effective_from)
);

CREATE TABLE IF NOT EXISTS il_sales_sources (
    source_key TEXT PRIMARY KEY,
    file_name  TEXT NOT NULL,
    url        TEXT NOT NULL,
    retrieved  DATE NOT NULL,
    row_count  INTEGER NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS il_sales_inventory (
    id         SERIAL PRIMARY KEY,
    part       TEXT NOT NULL,
    line_ref   TEXT NOT NULL,
    item_label TEXT NOT NULL,
    status     TEXT NOT NULL,
    topic_key  TEXT,
    notes      TEXT,
    UNIQUE (part, line_ref)
);
""")

_PIO = "IDOR PIO-101 Illinois Sales & Use Tax Matrix (R-01/26)"
PARAMS = [  # (key, effective_from, value, description, citation, url)
    ("general_rate", "1990-01-01", 0.0625, "State rate on general merchandise (high rate)", _PIO + ", p. 3", MATRIX_URL),
    ("low_rate", "1990-01-01", 0.01, "State rate on qualifying drugs and medical appliances (low rate)", _PIO + ", p. 3", MATRIX_URL),
    ("state_grocery_rate", "1990-01-01", 0.01, "State rate on groceries before 2026", "IDOR FY Bulletin 2026-03",
     BASE + "/content/dam/soi/en/web/tax/research/publications/bulletins/documents/2026/fy-2026-03.pdf"),
    ("state_grocery_rate", "2026-01-01", 0.0, "State grocery rate eliminated by P.A. 103-0781", "IDOR FY Bulletin 2026-03 / " + _PIO + ", Key Dates",
     BASE + "/content/dam/soi/en/web/tax/research/publications/bulletins/documents/2026/fy-2026-03.pdf"),
    ("local_grocery_rate", "2026-01-01", 0.01, "Local grocery tax a municipality or county may impose by ordinance (exactly 1%)",
     "IDOR FY Bulletin 2026-03 / " + _PIO + ", Key Dates", BASE + "/content/dam/soi/en/web/tax/research/publications/bulletins/documents/2026/fy-2026-03.pdf"),
    ("use_tax_general_rate", "1990-01-01", 0.0625, "State use tax on general merchandise", _PIO + ", Introduction", MATRIX_URL),
    ("use_tax_low_rate", "1990-01-01", 0.01, "State use tax on qualifying drugs and medical appliances", _PIO + ", p. 3", MATRIX_URL),
]

SOURCES = [
    ("locations", "ordmache-current-2026-11-17.txt", LOCATION_URL, "2026-11-17",
     "County/municipality Retailers' Occupation Tax rates (current and prior period). Refreshed by IDOR each January and July."),
    ("grocery", "grocerymache-current-2026-11-17.txt", GROCERY_URL, "2026-11-17", "Local grocery tax locations and rates, layout effective 2026-01-01."),
    ("matrix", "pio-101-R-01-26.pdf", MATRIX_URL, "2026-11-17", "PIO-101 (R-01/26), tables extracted with pdfplumber."),
]


# Everyday words -> PIO-101 matrix item ids. This maps vocabulary only; every taxability fact still comes from the
# matrix row. An alias is added only when the matrix row itself names or defines the thing (e.g. 1004 soft drinks,
# 1020's list of grooming products, 1053 clothing). A word that fits several rows maps to all of them.
ALIASES = {
    "1001": ["farm machinery", "farm equipment", "tractor"],
    "1002": ["groceries", "grocery", "grocery items", "packaged food", "bread", "eggs", "cereal", "fruit", "vegetables",
             "produce", "meat", "chicken", "chips", "peanut butter", "rice", "pasta", "food"],
    "1002.1": ["restaurant meal", "restaurant", "takeout", "take-out", "hot food", "prepared food", "fast food",
               "meal at a restaurant", "dine in", "dine-in"],
    "1003": ["candy", "candy bar", "chocolate bar", "gummies", "lollipops"],
    "1004": ["soda", "soft drink", "soft drinks", "pop", "cola", "sweetened beverage"],
    "1007": ["occasional sale"],
    "1008": ["beer", "wine", "liquor", "alcohol", "vodka", "whiskey", "alcoholic beverages"],
    "1009": ["coffee", "tea", "milk"],
    "1010": ["medicine", "medication", "prescription drugs", "prescription", "ibuprofen", "aspirin", "tylenol",
             "cough syrup", "over-the-counter", "over the counter", "otc", "drugs"],
    "1011": ["vitamins", "vitamin", "supplements", "dietary supplements"],
    "1012": ["wheelchair", "crutches", "walker", "hearing aid", "hearing aids", "prosthetic", "medical appliance",
             "medical appliances", "cane"],
    "1014": ["insulin", "test strips", "glucose test strips", "syringes"],
    "1015": ["tampons", "menstrual cups", "feminine hygiene", "menstrual products", "menstrual pads"],
    "1016": ["diapers", "baby diapers", "infant diapers"],
    "1017": ["adult diapers"],
    "1018": ["infant formula", "baby formula"],
    "1019": ["breast pump", "breast pumps"],
    "1020": ["shampoo", "deodorant", "moisturizer", "shaving cream", "hair products", "lotion", "contact lens solution",
             "condoms"],
    "1023": ["antique", "antiques", "artwork", "painting"],
    "1031": ["canned software", "mobile app", "downloaded game", "video game download"],
    "1032": ["custom software"],
    "1033": ["licensed software"],
    "1037": ["flowers", "bouquet"],
    "1037.1": ["flowers", "bouquet"],
    "1038": ["pet", "dog", "cat", "puppy", "kitten", "horse"],
    "1049": ["lease", "leased", "rental"],
    "1050": ["lease", "leased", "rental"],
    "1053": ["clothes", "clothing", "shirt", "jacket", "jeans", "dress", "pants", "shoes"],
    "1055": ["gasoline", "motor fuel"],
    "1056": ["cigarettes"],
    "1057": ["cigars", "pipe tobacco", "tobacco"],
    "1067": ["carpet", "flooring", "floor coverings"],
    "1068": ["books", "book", "cd", "cds", "vinyl records", "sheet music"],
    "1069": ["ebook", "e-book", "digital book", "digital music", "mp3", "digital download", "downloaded music"],
    "1070": ["newspaper", "magazine"],
    "1083": ["gift card", "gift cards", "gift certificate"],
    "3015": ["trade-in", "traded in", "trade in"],
    "3017": ["installation"],
    "3018": ["installation"],
    "3027.1": ["active duty military"],
    "3031": ["home-delivered meals", "meals on wheels"],
    "3032": ["shipping", "delivery charge", "handling"],
    "3034": ["garage sale", "yard sale"],
    "3035": ["credit card fees", "credit card surcharge"],
    "4003": ["dry cleaning", "laundry"],
    "4004": ["car", "vehicle", "truck", "motorcycle", "motor vehicle"],
    "4009": ["boat", "watercraft", "airplane", "aircraft"],
}


_RUT5 = "IDOR RUT-5 Private Party Vehicle Use Tax Chart for 2026 (R-12/25)"
_RUT50 = "IDOR RUT-50 Instructions (R-04/26)"
_RUT6 = "IDOR RUT-6 Form RUT-50 Reference Guide (R-03/24)"
_RUT25 = "IDOR RUT-25 Instructions (R-07/26)"
_ST556 = "IDOR ST-556 Instructions (R-12/24)"
_NITA = "IDOR Informational Bulletin FY 2026-34 (NITA rate change effective 2026-08-01)"
_FORMS = BASE + "/content/dam/soi/en/web/tax/forms/sales/documents/vehicleusetax/"

PP_TABLE_A_YEAR = 2026
PP_TABLE_A = [(0, 1, 465), (2, 2, 365), (3, 3, 290), (4, 4, 240), (5, 5, 190), (6, 6, 165), (7, 7, 155), (8, 8, 140),
              (9, 9, 125), (10, 10, 115), (11, None, 100)]      # (age_min, age_max, tax)
PP_TABLE_B = [(15000, 19999.99, 850), (20000, 24999.99, 1100), (25000, 29999.99, 1350), (30000, 49999.99, 1600),
              (50000, 99999.99, 2600), (100000, 999999.99, 5100), (1000000, None, 10100)]
PP_LOCAL = [  # (jurisdiction, category, age_min, age_max, tax)
    ("chicago", "standard", 0, 3, 80), ("chicago", "standard", 4, 8, 65), ("chicago", "standard", 9, 14, 50),
    ("chicago", "standard", 15, None, 0),
    ("chicago", "exception", 0, 14, 15), ("chicago", "exception", 15, None, 0),
    ("chicago", "motorcycle", 0, None, 15),
    ("cook", "standard", 0, 3, 225), ("cook", "standard", 4, 8, 175), ("cook", "standard", 9, None, 90),
    ("cook", "exception", 0, None, 25), ("cook", "motorcycle", 0, None, 90),
]
VEHICLE_PARAMS = [  # (key, effective_from, value, description, citation, url)
    ("veh_pp_exception_tax", "2026-01-01", 15, "Private party tax for family/estate/reorganization exceptions", _RUT5, _FORMS + "rut-5.pdf"),
    ("veh_pp_motorcycle_tax", "2026-01-01", 25, "Private party tax on a motorcycle, motor-driven cycle, ATV or motorized pedalcycle", _RUT5, _FORMS + "rut-5.pdf"),
    ("veh_pp_table_threshold", "2026-01-01", 15000, "Table A below this price/value, Table B at or above", _RUT50, _FORMS + "rut-50-instr.pdf"),
    ("veh_chicago_home_rule_use_tax", "1990-01-01", 0.0125, "Chicago Home Rule Use Tax added to titled items bought by a Chicago address", _ST556, _FORMS + "st-556-instr.pdf"),
    ("veh_depreciation_per_month", "1990-01-01", 0.0167, "RUT-25 depreciation per whole month of out-of-state use (motor vehicles)", _RUT25, _FORMS + "rut-25-instr.pdf"),
    ("veh_nonresident_exempt_months", "1990-01-01", 3, "Months of out-of-state use after which a nonresident individual owes no Illinois use tax", _RUT25, _FORMS + "rut-25-instr.pdf"),
    ("veh_med_fee_cap", "1990-01-01", 20, "Metro-East Mass Transit District fee cap on titled items", _ST556, _FORMS + "st-556-instr.pdf"),
]
VEHICLE_SOURCES = [
    ("rut5", "rut-5-2026-R-12-25.pdf", _FORMS + "rut-5.pdf", "2026-11-17", 1, "Private party vehicle use tax Tables A and B, 2026."),
    ("rut50", "rut-50-instr-R-04-26.pdf", _FORMS + "rut-50-instr.pdf", "2026-11-17", 1, "RUT-50 instructions: age rule, exemptions, exceptions."),
    ("rut6", "rut-6-R-03-24.pdf", _FORMS + "rut-6.pdf", "2026-11-17", 1, "Chicago and Cook County private party vehicle use tax (edition R-03/24, bundled with RUT-50 instructions)."),
    ("rut25", "rut-25-instr-R-07-26.pdf", _FORMS + "rut-25-instr.pdf", "2026-11-17", 1, "Vehicle use tax on out-of-state retailer purchases."),
    ("st556", "st-556-instr-R-12-24.pdf", _FORMS + "st-556-instr.pdf", "2026-11-17", 1, "Retailer transaction return for titled items (older edition)."),
    ("fy2026_34", "fy-2026-34-NITA.pdf", BASE + "/content/dam/soi/en/web/tax/research/publications/bulletins/documents/2026/fy-2026-34.pdf",
     "2026-11-17", 1, "NITA rate +0.25% on titled items from 2026-08-01 (Cook 1.25%, collar counties 1.00%)."),
]

LEDGER = [  # (part, line_ref, label, status, topic_key, notes)
    ("rates", "location-rates", "General-merchandise and drug/medical sales-tax rate by municipality or county", "built",
     "il_sales_rate", "From IDOR's county/municipality file; date-aware (current vs prior period). 200 of 1,596 locations "
     "carry an address override flag and 18 a receipts override flag: those are not guessed, the answer asks for the street "
     "address / explains the range."),
    ("rates", "grocery-local", "Local grocery tax (state 1% ended 2026-01-01)", "built", "il_sales_grocery",
     "From IDOR's grocery file; RTA/Metro-East district grocery taxes remain in the file's rates."),
    ("rates", "use-tax", "Use tax on out-of-state purchases", "partial", "il_sales_use_tax",
     "State use tax 6.25% (1% drugs/medical); there are no local use taxes on general merchandise administered by IDOR. "
     "Credit for tax paid to another state and remote-seller sourcing rules are stated, not computed per address."),
    ("rates", "address-rates", "Street-address (business district / destination) rates", "deferred", None,
     "IDOR's address-specific file is ~9 GB uncompressed; not loaded. Locations with an address flag are reported as such."),
    ("taxability", "matrix-items", "Item taxability from the PIO-101 matrix (taxable, exempt, rate)", "partial", "il_sales_item",
     "145 matrix rows loaded and matched by description; continuation text outside the matrix tables and use-based "
     "exemption certificates are not evaluated."),
    ("vehicles", "private-party", "Private party vehicle use tax (Form RUT-50, 2026 Tables A/B, family/estate exceptions, "
     "motorcycles/ATVs, Chicago and Cook County local amounts)", "built", "il_vehicle_private_party",
     "Age = title-application year minus vehicle year. Chicago/Cook amounts are from RUT-6 (R-03/24): IDOR says local rates "
     "can change each January/July, so the answer says which edition it used. Tax year 2026 only."),
    ("vehicles", "dealer-purchase", "Vehicle bought from a dealer (ST-556) or out-of-state retailer (RUT-25): rate by "
     "registration address, trade-in credit, credit for tax paid elsewhere", "partial", "il_vehicle_dealer",
     "Rates from IDOR's auto rate columns (Chicago 8.75% / other Cook 7.50% / DuPage-Kane-Lake-McHenry-Will 7.25% / Madison and "
     "St. Clair 6.25%-6.50% / elsewhere 6.25%, from 2026-08-01), tax rounded to the dollar as the returns do. Metro-East "
     "transit fee, off-site sales, nonresident purchases and remote-seller sourcing are stated, not computed."),
    ("vehicles", "moving-in", "Moving into Illinois with a vehicle (RUT-25: 3-month exemption, 1.67%/month depreciation)",
     "partial", "il_vehicle_moving_in", "Individuals only; business relocations and military home-of-record cases are stated, not computed."),
    ("vehicles", "leases", "Vehicle leases (ST-556-LSE / RUT-25-LSE)", "deferred", None, "Not loaded."),
    ("vehicles", "boats-aircraft", "Watercraft and aircraft (RUT-75)", "deferred", None, "Not loaded."),
    ("vehicles", "registration-fees", "Title and registration fees (Secretary of State)", "out_of_scope", None,
     "Not a tax administered by IDOR."),
    ("taxability", "cannabis", "Adult-use cannabis taxes", "deferred", None, "Separate cannabis rate files not loaded."),
    ("taxability", "services", "Service occupation tax / sales of services", "deferred", None, "Not loaded."),
    ("compliance", "registration-filing", "Retailer registration, ST-1 filing, remote retailers and marketplace facilitators",
     "deferred", None, "Informational answers not built."),
    ("other-taxes", "local-excise", "Chicago amusement / lease / soft-drink and other local taxes outside ROT", "out_of_scope", None,
     "Administered by the city, not IDOR's sales tax."),
]


def fetch_missing():
    """The rate files are committed; the PDFs are not (the repository ignores *.pdf). Download any source file that is
    missing from il_sales_data/ so a fresh clone can seed. A re-download is IDOR's CURRENT edition, which may be newer than
    the one the rules in this file were verified against: re-check the figures if a file had to be fetched."""
    import os
    import requests
    wanted = [(LOCATION_FILE, LOCATION_URL), (GROCERY_FILE, GROCERY_URL), (MATRIX_FILE, MATRIX_URL)]
    wanted += [(DATA + fname, url) for _k, fname, url, _r, _n, _note in VEHICLE_SOURCES]
    for path, url in wanted:
        if os.path.exists(path):
            continue
        os.makedirs(DATA, exist_ok=True)
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)
        print(f"downloaded {path} from {url} (IDOR's current edition; re-verify the figures that came from it)")


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    print("created Illinois sales-tax tables")


def _insert(conn, table, cols, rows):
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})")
    for r in rows:
        conn.execute(sql, [r[c] for c in cols])


def seed():
    fetch_missing()
    locations = parse.parse_location_file(LOCATION_FILE)
    # IDOR's published file repeats one location id (Marion, Williamson County, two effective dates): keep the newest.
    by_id = {}
    for r in locations:
        if r["location_id"] not in by_id or (r["effective"] or "") >= (by_id[r["location_id"]]["effective"] or ""):
            by_id[r["location_id"]] = r
    grocery_by_id = {}
    for r in parse.parse_grocery_file(GROCERY_FILE):    # the grocery file repeats the same Marion record
        if r["location_id"] not in grocery_by_id or (r["effective"] or "") >= (grocery_by_id[r["location_id"]]["effective"] or ""):
            grocery_by_id[r["location_id"]] = r
    grocery = list(grocery_by_id.values())
    matrix = parse.parse_matrix(MATRIX_FILE)
    with db.get_conn() as conn:
        with conn.transaction():
            for t in ("il_sales_locations", "il_grocery_locations", "il_sales_matrix_items"):
                conn.execute(f"DELETE FROM {t}")
            _insert(conn, "il_sales_locations", _LOC_COLS, by_id.values())
            _insert(conn, "il_grocery_locations", _GROC_COLS, grocery)
            for item in matrix:
                conn.execute(
                    'INSERT INTO il_sales_matrix_items (item_id, page, description, taxable, rate, explanation, "references", source) '
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO NOTHING",
                    (item["item_id"], item["page"], item["description"], item["taxable"], item["rate"],
                     item["explanation"], item["references"], "PIO-101 R-01/26"))
            conn.execute("DELETE FROM il_sales_item_aliases")
            known = {i["item_id"] for i in matrix}
            n_alias = 0
            for item_id, words in ALIASES.items():
                if item_id not in known:
                    raise SystemExit(f"alias target {item_id} is not a PIO-101 matrix item")
                for w in words:
                    conn.execute("INSERT INTO il_sales_item_aliases (alias, item_id, note) VALUES (%s,%s,%s) "
                                 "ON CONFLICT (alias, item_id) DO NOTHING",
                                 (w, item_id, "curated vocabulary -> matrix row"))
                    n_alias += 1
            conn.execute("DELETE FROM il_vehicle_pp_table_a")
            conn.execute("DELETE FROM il_vehicle_pp_table_b")
            conn.execute("DELETE FROM il_vehicle_pp_local")
            for lo, hi, tax in PP_TABLE_A:
                conn.execute("INSERT INTO il_vehicle_pp_table_a (tax_year, age_min, age_max, tax) VALUES (%s,%s,%s,%s)",
                             (PP_TABLE_A_YEAR, lo, hi, tax))
            for lo, hi, tax in PP_TABLE_B:
                conn.execute("INSERT INTO il_vehicle_pp_table_b (tax_year, price_min, price_max, tax) VALUES (%s,%s,%s,%s)",
                             (PP_TABLE_A_YEAR, lo, hi, tax))
            for juris, cat, lo, hi, tax in PP_LOCAL:
                conn.execute("INSERT INTO il_vehicle_pp_local (jurisdiction, category, age_min, age_max, tax) "
                             "VALUES (%s,%s,%s,%s,%s)", (juris, cat, lo, hi, tax))
            for key, eff, val, desc, cite, url in PARAMS + VEHICLE_PARAMS:
                conn.execute(
                    "INSERT INTO il_sales_params (param_key, effective_from, value, description, citation, source_url) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (param_key, effective_from) DO UPDATE SET value=EXCLUDED.value, "
                    "description=EXCLUDED.description, citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
                    (key, eff, val, desc, cite, url))
            counts = {"locations": len(by_id), "grocery": len(grocery), "matrix": len(matrix)}
            counts.update({k: n for k, _f, _u, _r, n, _note in VEHICLE_SOURCES})
            for key, fname, url, retrieved, note in SOURCES + [(k, f, u, r, n_) for k, f, u, r, _n, n_ in VEHICLE_SOURCES]:
                conn.execute(
                    "INSERT INTO il_sales_sources (source_key, file_name, url, retrieved, row_count, note) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (source_key) DO UPDATE SET file_name=EXCLUDED.file_name, "
                    "url=EXCLUDED.url, retrieved=EXCLUDED.retrieved, row_count=EXCLUDED.row_count, note=EXCLUDED.note",
                    (key, fname, url, retrieved, counts[key], note))
            keep = {(p, l) for p, l, *_ in LEDGER}
            for part, ref, label, status, key, notes in LEDGER:
                conn.execute(
                    "INSERT INTO il_sales_inventory (part, line_ref, item_label, status, topic_key, notes) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (part, line_ref) DO UPDATE SET item_label=EXCLUDED.item_label, "
                    "status=EXCLUDED.status, topic_key=EXCLUDED.topic_key, notes=EXCLUDED.notes",
                    (part, ref, label, status, key, notes))
            for pid, part, ref in conn.execute("SELECT id, part, line_ref FROM il_sales_inventory").fetchall():
                if (part, ref) not in keep:
                    conn.execute("DELETE FROM il_sales_inventory WHERE id=%s", (pid,))
    print(f"seeded {n_alias} item aliases,")
    print(f"seeded {counts['locations']} locations, {counts['grocery']} grocery rows, {counts['matrix']} matrix items, "
          f"{len(PARAMS)} params, {len(LEDGER)} ledger rows")


def status():
    with db.get_conn() as conn:
        for t in ("il_sales_locations", "il_grocery_locations", "il_sales_matrix_items", "il_sales_item_aliases", "il_vehicle_pp_table_a", "il_vehicle_pp_table_b", "il_vehicle_pp_local", "il_sales_params",
                  "il_sales_sources", "il_sales_inventory"):
            print(t, conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
        for s, n in conn.execute("SELECT status, count(*) FROM il_sales_inventory GROUP BY status ORDER BY status"):
            print(f"  ledger {s}: {n}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"create": create, "seed": seed, "status": status, "fetch": fetch_missing}[cmd]()
