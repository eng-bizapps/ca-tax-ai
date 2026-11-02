"""Schema + seed data for the Illinois income-tax domain (Illinois Department
of Revenue) -- its own physically-separate database (see il_db.py).

Every figure was read from the IDOR 2025 IL-1040 instructions / schedules on
2026-11-01 (URLs stored per row). Tables:
  - il_income_params   -- one row per (tax_year, param_key): rate, exemption
    amounts, AGI cutoffs, credit percentages and caps.
  - il_use_tax_table   -- the IL-1040 Use Tax (UT) Table (AGI bands).
  - il_eitc_max        -- IDOR's published maximum Illinois EITC by children.
  - il_income_inventory -- the completeness ledger (same shape as the
    California ledgers). status: built | partial | deferred | out_of_scope.

Usage:
  python il_schema.py create
  python il_schema.py seed
  python il_schema.py status
"""
import sys

import il_db as db

INSTR = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-instr.pdf"
ICR = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-schedule-icr.pdf"
EIC = "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-schedule-il-e-eic.pdf"
EXEMPT_QA = "https://tax.illinois.gov/questionsandanswers/answer.851.html"
EIC_MAX = "https://tax.illinois.gov/programs/eitc/max-credit.html"

SCHEMA = """
CREATE TABLE IF NOT EXISTS il_income_params (
    id          SERIAL PRIMARY KEY,
    tax_year    INTEGER NOT NULL,
    param_key   TEXT NOT NULL,
    value       NUMERIC NOT NULL,
    description TEXT,
    citation    TEXT,
    source_url  TEXT,
    UNIQUE (tax_year, param_key)
);

CREATE TABLE IF NOT EXISTS il_use_tax_table (
    id       SERIAL PRIMARY KEY,
    tax_year INTEGER NOT NULL,
    agi_min  NUMERIC NOT NULL,
    agi_max  NUMERIC,
    use_tax  NUMERIC,
    pct_of_agi NUMERIC,
    UNIQUE (tax_year, agi_min)
);

CREATE TABLE IF NOT EXISTS il_eitc_max (
    id       SERIAL PRIMARY KEY,
    tax_year INTEGER NOT NULL,
    children INTEGER NOT NULL,
    max_credit NUMERIC NOT NULL,
    UNIQUE (tax_year, children)
);

CREATE TABLE IF NOT EXISTS il_income_inventory (
    id         SERIAL PRIMARY KEY,
    part       TEXT NOT NULL,
    line_ref   TEXT NOT NULL,
    item_label TEXT NOT NULL,
    status     TEXT NOT NULL,
    topic_key  TEXT,
    notes      TEXT,
    UNIQUE (part, line_ref)
);
"""

# (tax_year, key, value, description, citation, url)
_C25 = "IDOR 2025 IL-1040 Instructions"
PARAMS = [
    (2025, "rate", 0.0495, "Flat Illinois income tax rate", f"{_C25}, General Information", INSTR),
    (2026, "rate", 0.0495, "Flat Illinois income tax rate (2026 rate stated on IDOR exemption page)", "IDOR What's New / Q&A 851", EXEMPT_QA),
    (2025, "exemption_per_person", 2850, "Exemption allowance per exemption", f"{_C25}, Step 4 Line 10a", INSTR),
    (2026, "exemption_per_person", 2925, "Exemption allowance per exemption, 2026", "IDOR Q&A 851", EXEMPT_QA),
    (2025, "exemption_65_blind_each", 1000, "Added per box: age 65+ or legally blind (each taxpayer/spouse)", f"{_C25}, Lines 10b/10c", INSTR),
    (2025, "dependent_exemption", 2850, "Exemption per dependent (Schedule IL-E/EITC Step 2 Line 1)", "IDOR Schedule IL-E/EITC (R-12/25) Line 1", EIC),
    (2025, "agi_limit_joint", 500000, "Exemption/property/K-12 credits not allowed if AGI exceeds this (married filing jointly)", f"{_C25}, Income Exceptions", INSTR),
    (2025, "agi_limit_other", 250000, "Same cutoff, all other filing statuses", f"{_C25}, Income Exceptions", INSTR),
    (2025, "property_credit_rate", 0.05, "Property tax credit: 5% of qualifying property tax", "IDOR Schedule ICR (R-12/25) Line 4g", ICR),
    (2025, "k12_rate", 0.25, "K-12 education expense credit rate", "IDOR Schedule ICR Line 7d", ICR),
    (2025, "k12_threshold", 250, "First $250 of K-12 expenses earns no credit", "IDOR Schedule ICR Line 7b", ICR),
    (2025, "k12_max", 750, "K-12 education expense credit cap", "IDOR Schedule ICR Line 7d", ICR),
    (2025, "volunteer_worker_credit", 500, "Volunteer Emergency Worker credit per certificate holder", "IDOR Schedule ICR Line 10c", ICR),
    (2025, "eitc_rate", 0.20, "Illinois EITC = 20% of federal EITC", "IDOR Schedule IL-E/EITC Line 7", EIC),
    (2025, "child_credit_rate", 0.40, "Illinois Child Tax Credit = 40% of Illinois EITC (child under 12)", "IDOR Schedule IL-E/EITC Line 12", EIC),
    (2025, "child_credit_age_limit", 12, "Child must be under this age on Dec 31", "IDOR Schedule IL-E/EITC Step 5", EIC),
    (2025, "use_tax_general_rate", 0.0625, "Use tax on general merchandise", f"{_C25}, UT Worksheet Line 1b", INSTR),
    (2025, "use_tax_food_drug_rate", 0.01, "Use tax on qualifying food, non-prescription drugs, medical appliances", f"{_C25}, UT Worksheet Line 2b", INSTR),
    (2025, "use_tax_st44_single", 600, "Use tax above this must be filed on Form ST-44 instead of IL-1040", f"{_C25}, Line 21", INSTR),
    (2025, "use_tax_st44_joint", 1200, "Same threshold, married filing jointly", f"{_C25}, Line 21", INSTR),
    (2025, "use_tax_table_pct_over_100k", 0.0005, "UT Table: AGI over $100,000 x 0.05%", f"{_C25}, UT Table", INSTR),
    (2025, "estimated_tax_threshold", 1000, "Estimated payments may be required if liability after withholding/credits exceeds this", f"{_C25}, Line 26", INSTR),
]

USE_TAX = [  # (agi_min, agi_max, use_tax)
    (0, 10000, 3), (10001, 20000, 8), (20001, 30000, 13), (30001, 40000, 18),
    (40001, 50000, 23), (50001, 75000, 31), (75001, 100000, 44),
]

EITC_MAX = [(0, 130), (1, 866), (2, 1430), (3, 1609)]  # 3 means 3 or more

LEDGER = [  # (part, line_ref, label, status, topic_key, notes)
    ("tax", "rate", "Flat 4.95% rate on net income", "built", "il_income_compute", "2025 and 2026 rate."),
    ("tax", "line-9-11", "Base income and net income (AGI + additions - subtractions - exemptions)", "built",
     "il_income_compute", "User supplies federal AGI, additions and subtractions; no federal return is computed."),
    ("tax", "line-5", "Social Security / retirement income subtraction", "partial", "il_income_compute",
     "User supplies the amount that is federally taxed; Pub. 120 detail on which plans qualify is not evaluated."),
    ("tax", "schedule-m-other", "Other Schedule M additions/subtractions", "partial", "il_income_compute",
     "A user-stated net figure is applied; individual Schedule M items (529, military pay, etc.) are not itemized."),
    ("exemptions", "line-10", "Exemption allowance: filing status, 65+/blind, dependents, AGI cutoffs", "built",
     "il_exemption", "2025 full; 2026 per-person amount stated only. Claimed-as-dependent rule: single filers only."),
    ("credits", "property-tax-credit", "Property tax credit (5%)", "built", "il_property_credit",
     "Principal residence, 2024 taxes paid in 2025; capped at tax due; AGI cutoff. Seller reimbursements/sale proration not computed."),
    ("credits", "k12-credit", "K-12 education expense credit (25% over $250, max $750)", "built", "il_k12_credit",
     "Qualified-expense rules stated, not itemized."),
    ("credits", "volunteer-worker", "Volunteer Emergency Worker credit ($500)", "partial", "il_credits",
     "Amount stated; award is by IDOR certificate only."),
    ("credits", "other-state-credit", "Credit for tax paid to other states (Schedule CR)", "deferred", None,
     "Schedule CR (Pub. 111) not loaded."),
    ("credits", "eitc", "Illinois EITC (20% of federal EITC) and Child Tax Credit (40% of IL EITC)", "partial",
     "il_eitc", "User supplies the federal EITC; the federal EITC and the Illinois Expanded EITC Worksheet are not computed."),
    ("other", "use-tax", "Individual use tax (UT Table / worksheet)", "built", "il_use_tax", "AGI table and 6.25%/1% worksheet."),
    ("other", "estimated-tax-penalty", "Underpayment of estimated tax penalty (IL-2210)", "deferred", None, "Not loaded."),
    ("residency", "schedule-nr", "Nonresident / part-year allocation (Schedule NR)", "deferred", None, "Not loaded."),
    ("withholding", "il-w-4", "Employee withholding (IL-W-4)", "deferred", None, "Paycheck calculator is California-only for now."),
]


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    print("created Illinois income tables")


def seed():
    with db.get_conn() as conn:
        for row in PARAMS:
            conn.execute(
                "INSERT INTO il_income_params (tax_year, param_key, value, description, citation, source_url) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tax_year, param_key) DO UPDATE SET "
                "value=EXCLUDED.value, description=EXCLUDED.description, citation=EXCLUDED.citation, "
                "source_url=EXCLUDED.source_url", row)
        for lo, hi, tax in USE_TAX:
            conn.execute(
                "INSERT INTO il_use_tax_table (tax_year, agi_min, agi_max, use_tax) VALUES (2025,%s,%s,%s) "
                "ON CONFLICT (tax_year, agi_min) DO UPDATE SET agi_max=EXCLUDED.agi_max, use_tax=EXCLUDED.use_tax",
                (lo, hi, tax))
        conn.execute(
            "INSERT INTO il_use_tax_table (tax_year, agi_min, pct_of_agi) VALUES (2025,100001,0.0005) "
            "ON CONFLICT (tax_year, agi_min) DO UPDATE SET pct_of_agi=EXCLUDED.pct_of_agi")
        for kids, mx in EITC_MAX:
            conn.execute(
                "INSERT INTO il_eitc_max (tax_year, children, max_credit) VALUES (2025,%s,%s) "
                "ON CONFLICT (tax_year, children) DO UPDATE SET max_credit=EXCLUDED.max_credit", (kids, mx))
        keep = {(p, l) for p, l, *_ in LEDGER}
        for part, ref, label, status, key, notes in LEDGER:
            conn.execute(
                "INSERT INTO il_income_inventory (part, line_ref, item_label, status, topic_key, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (part, line_ref) DO UPDATE SET item_label=EXCLUDED.item_label, "
                "status=EXCLUDED.status, topic_key=EXCLUDED.topic_key, notes=EXCLUDED.notes",
                (part, ref, label, status, key, notes))
        for pid, part, ref in conn.execute("SELECT id, part, line_ref FROM il_income_inventory").fetchall():
            if (part, ref) not in keep:
                conn.execute("DELETE FROM il_income_inventory WHERE id=%s", (pid,))
    print(f"seeded {len(PARAMS)} params, {len(USE_TAX) + 1} use-tax rows, {len(EITC_MAX)} EITC rows, {len(LEDGER)} ledger rows")


def status():
    with db.get_conn() as conn:
        for t in ("il_income_params", "il_use_tax_table", "il_eitc_max", "il_income_inventory"):
            print(t, conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
        for s, n in conn.execute("SELECT status, count(*) FROM il_income_inventory GROUP BY status ORDER BY status"):
            print(f"  ledger {s}: {n}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"create": create, "seed": seed, "status": status}[cmd]()
