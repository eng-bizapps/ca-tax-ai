"""Schema for the CA employer payroll-tax (EDD) domain -- Ring 5 database
split (own physically-separate Postgres/pgvector database, mirroring the
Ring 2 income and Ring 4 property splits). See employer_db.py for the
connection.

The EMPLOYEE-side paycheck calculator (payroll_withholding.py) stays in the
income database; this domain is the EMPLOYER side: UI / ETT / SDI,
deposit schedules and due dates, supplemental-wage withholding, late-
payment penalty and interest. All numbers come from EDD's 2026 Employer's
Guide (DE 44 Rev. 52, 4-26) and EDD's interest-rate page.

Same "separate table per concept, never JSONB-facts" precedent as the other
schemas. Tables:
  - ca_employer_payroll_rates -- one row per (tax_year, rate_key): rate,
    taxable wage limit (NULL = none), who bears it.
  - ca_edd_deposit_rules -- the DE 44 "California Deposit Requirements"
    table: federal deposit schedule x accumulated PIT withholding band ->
    California deposit schedule.
  - ca_edd_due_dates -- DE 9 / DE 9C quarterly filing and delinquency dates.
  - ca_edd_interest_rates -- EDD's semi-annual interest rate on overdue
    taxes (compounds daily).
  - employer_rule_embeddings -- own vector space, 0 rows at launch.
  - employer_tax_inventory -- the completeness ledger (same shape as
    property_tax_inventory).

Usage:
  python employer_schema.py create
  python employer_schema.py seed
  python employer_schema.py status
"""
import sys

import config
import employer_db as db

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ca_employer_payroll_rates (
    id          SERIAL PRIMARY KEY,
    tax_year    INTEGER NOT NULL,
    rate_key    TEXT NOT NULL,
    rate        NUMERIC NOT NULL,
    wage_limit  NUMERIC,
    paid_by     TEXT NOT NULL,
    description TEXT,
    citation    TEXT,
    source_url  TEXT,
    UNIQUE (tax_year, rate_key)
);

CREATE TABLE IF NOT EXISTS ca_edd_deposit_rules (
    id               SERIAL PRIMARY KEY,
    tax_year         INTEGER NOT NULL,
    federal_schedule TEXT NOT NULL,
    min_accumulated  NUMERIC NOT NULL,
    max_accumulated  NUMERIC,
    ca_schedule      TEXT NOT NULL,
    due_rule         TEXT NOT NULL,
    citation         TEXT,
    UNIQUE (tax_year, federal_schedule, min_accumulated)
);

CREATE TABLE IF NOT EXISTS ca_edd_due_dates (
    id              SERIAL PRIMARY KEY,
    tax_year        INTEGER NOT NULL,
    quarter         INTEGER NOT NULL,
    due_date        DATE NOT NULL,
    delinquent_date DATE NOT NULL,
    citation        TEXT,
    UNIQUE (tax_year, quarter)
);

CREATE TABLE IF NOT EXISTS ca_edd_interest_rates (
    id            SERIAL PRIMARY KEY,
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    annual_rate   NUMERIC NOT NULL,
    daily_factor  NUMERIC NOT NULL,
    source_url    TEXT,
    UNIQUE (period_start)
);

-- FUTA credit reduction for California (DOL/ETA). status 'final' = the rate
-- DOL set for that calendar year; 'potential' = preliminary, not set until
-- November 10 of the year (potential_max_rate includes the BCR add-on, which
-- can be waived and replaced).
CREATE TABLE IF NOT EXISTS ca_futa_credit_reductions (
    id                SERIAL PRIMARY KEY,
    tax_year          INTEGER NOT NULL,
    reduction_rate    NUMERIC NOT NULL,
    potential_max_rate NUMERIC,
    status            TEXT NOT NULL,
    notes             TEXT,
    source_url        TEXT,
    as_of             DATE,
    UNIQUE (tax_year)
);

-- UI contribution rate by schedule and reserve ratio (DE 231Z). Bands are
-- [ratio_min, ratio_max): NULL ratio_min = no lower bound, NULL ratio_max = no
-- upper bound. rate is stored as a fraction (0.047 = 4.7%).
CREATE TABLE IF NOT EXISTS ca_ui_rate_schedule (
    id        SERIAL PRIMARY KEY,
    schedule  TEXT NOT NULL,
    ratio_min NUMERIC,
    ratio_max NUMERIC,
    rate      NUMERIC NOT NULL,
    citation  TEXT
);

CREATE TABLE IF NOT EXISTS ca_ui_schedule_years (
    tax_year   INTEGER PRIMARY KEY,
    schedule   TEXT NOT NULL,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS employer_rule_embeddings (
    topic_key TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    text      TEXT NOT NULL,
    embedding vector({config.EMBED_DIM})
);

CREATE TABLE IF NOT EXISTS employer_tax_inventory (
    id              SERIAL PRIMARY KEY,
    tax_year        INTEGER NOT NULL,
    part            TEXT NOT NULL,
    section         TEXT,
    line_ref        TEXT NOT NULL,
    item_label      TEXT NOT NULL,
    adjustment_type TEXT,
    frequency       TEXT,
    citation        TEXT,
    status          TEXT NOT NULL DEFAULT 'not_started',
    topic_key       TEXT,
    notes           TEXT,
    UNIQUE (tax_year, line_ref, item_label)
);
"""

DE44 = "EDD DE 44 Employer's Guide, Rev. 52 (4-26)"
DE44_URL = "https://edd.ca.gov/pdf_pub_ctr/de44.pdf"
INTEREST_URL = "https://edd.ca.gov/en/payroll_taxes/interest_rate"

IRS940 = "IRS Instructions for Form 940 (2025); FUTA Sec. 3301-3302"
IRS940_URL = "https://www.irs.gov/instructions/i940"
DOL_URL = "https://oui.doleta.gov/unemploy/futa_credit.asp"

# FUTA rates (tax_year, rate_key, rate, wage_limit, description). 2025 is
# verified against the 2025 Form 940 instructions; 2026 assumes the statutory
# 6.0% / 5.4% / $7,000 is unchanged, which DOL's page (updated 2026-09-11)
# supports for the 0.6% net rate.
FUTA_ROWS = [
    (2025, "futa_gross", 0.06, 7000, "FUTA tax rate on first $7,000 per employee"),
    (2025, "futa_max_credit", 0.054, 7000, "Maximum credit for state unemployment tax paid on time"),
    (2026, "futa_gross", 0.06, 7000, "FUTA tax rate on first $7,000 per employee"),
    (2026, "futa_max_credit", 0.054, 7000, "Maximum credit for state unemployment tax paid on time"),
]

# California credit reduction by calendar year, from DOL's historical table
# (reduced_credit_states_2010-2025.xlsx) and potential-2026 file (2026-01-15).
# (tax_year, reduction_rate, potential_max_rate, status, notes)
FUTA_REDUCTION_ROWS = [
    (2022, 0.003, None, "final", "DOL historical table"),
    (2023, 0.006, None, "final", "DOL historical table"),
    (2024, 0.009, None, "final", "DOL historical table"),
    (2025, 0.012, None, "final", "DOL historical table; add-on relief received"),
    (2026, 0.015, 0.053, "potential",
     "Preliminary (DOL 2026-01-15): 1.5% for outstanding advance; estimated 3.8% BCR add-on could raise the total to "
     "5.3%. Final rate set Nov 10, 2026."),
]

UI_RATE_ROWS = [
    # (ratio_min inclusive, ratio_max exclusive, [AA, A, B, C, D, E, F, F+] as percents)
    (None, -0.2, [5.4, 5.4, 5.4, 5.4, 5.4, 5.4, 5.4, 6.2]),
    (-0.2, -0.18, [5.2, 5.3, 5.4, 5.4, 5.4, 5.4, 5.4, 6.2]),
    (-0.18, -0.16, [5.1, 5.2, 5.4, 5.4, 5.4, 5.4, 5.4, 6.2]),
    (-0.16, -0.14, [5.0, 5.1, 5.3, 5.4, 5.4, 5.4, 5.4, 6.2]),
    (-0.14, -0.12, [4.9, 5.0, 5.3, 5.4, 5.4, 5.4, 5.4, 6.2]),
    (-0.12, -0.11, [4.8, 4.9, 5.2, 5.4, 5.4, 5.4, 5.4, 6.2]),
    (-0.11, -0.1, [4.7, 4.8, 5.1, 5.3, 5.4, 5.4, 5.4, 6.2]),
    (-0.1, -0.09, [4.6, 4.7, 5.1, 5.3, 5.4, 5.4, 5.4, 6.2]),
    (-0.09, -0.08, [4.5, 4.6, 4.9, 5.2, 5.4, 5.4, 5.4, 6.2]),
    (-0.08, -0.07, [4.4, 4.5, 4.8, 5.1, 5.3, 5.4, 5.4, 6.2]),
    (-0.07, -0.06, [4.3, 4.4, 4.7, 5.0, 5.3, 5.4, 5.4, 6.2]),
    (-0.06, -0.05, [4.2, 4.3, 4.6, 4.9, 5.2, 5.4, 5.4, 6.2]),
    (-0.05, -0.04, [4.1, 4.2, 4.5, 4.8, 5.1, 5.3, 5.4, 6.2]),
    (-0.04, -0.03, [4.0, 4.1, 4.4, 4.7, 5.0, 5.3, 5.4, 6.2]),
    (-0.03, -0.02, [3.9, 4.0, 4.3, 4.6, 4.9, 5.2, 5.4, 6.2]),
    (-0.02, -0.01, [3.8, 3.9, 4.2, 4.5, 4.8, 5.1, 5.4, 6.2]),
    (-0.01, 0.0, [3.7, 3.8, 4.1, 4.4, 4.7, 5.0, 5.4, 6.2]),
    (0.0, 0.01, [3.4, 3.6, 3.9, 4.2, 4.5, 4.8, 5.1, 5.9]),
    (0.01, 0.02, [3.2, 3.4, 3.7, 4.0, 4.3, 4.6, 4.9, 5.6]),
    (0.02, 0.03, [3.0, 3.2, 3.5, 3.8, 4.1, 4.4, 4.7, 5.4]),
    (0.03, 0.04, [2.8, 3.0, 3.3, 3.6, 3.9, 4.2, 4.5, 5.2]),
    (0.04, 0.05, [2.6, 2.8, 3.1, 3.4, 3.7, 4.0, 4.3, 4.9]),
    (0.05, 0.06, [2.4, 2.6, 2.9, 3.2, 3.5, 3.8, 4.1, 4.7]),
    (0.06, 0.07, [2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.5]),
    (0.07, 0.08, [2.0, 2.2, 2.5, 2.8, 3.1, 3.4, 3.7, 4.3]),
    (0.08, 0.09, [1.8, 2.0, 2.3, 2.6, 2.9, 3.2, 3.5, 4.0]),
    (0.09, 0.1, [1.6, 1.8, 2.1, 2.4, 2.7, 3.0, 3.3, 3.8]),
    (0.1, 0.11, [1.4, 1.6, 1.9, 2.2, 2.5, 2.8, 3.1, 3.6]),
    (0.11, 0.12, [1.2, 1.4, 1.7, 2.0, 2.3, 2.6, 2.9, 3.3]),
    (0.12, 0.13, [1.0, 1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.1]),
    (0.13, 0.14, [0.8, 1.0, 1.3, 1.6, 1.9, 2.2, 2.5, 2.9]),
    (0.14, 0.15, [0.7, 0.9, 1.1, 1.4, 1.7, 2.0, 2.3, 2.6]),
    (0.15, 0.16, [0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.1, 2.4]),
    (0.16, 0.17, [0.5, 0.7, 0.9, 1.1, 1.3, 1.6, 1.9, 2.2]),
    (0.17, 0.18, [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.7, 2.0]),
    (0.18, 0.19, [0.3, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.7]),
    (0.19, 0.2, [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6]),
    (0.2, None, [0.1, 0.3, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5]),
]
UI_SCHEDULES = ["AA", "A", "B", "C", "D", "E", "F", "F+"]

# EDD (edd.ca.gov Rates and Withholding): "The UI rate schedule for 2026 is Schedule F+."
# and "for 2027 is Schedule F+."
UI_SCHEDULE_YEARS = [(2026, "F+"), (2027, "F+")]
DE231Z = "EDD DE 231Z, California System of Experience Rating (Rev. 17, 6-22); CUIC Sec. 977"
DE231Z_URL = "https://edd.ca.gov/pdf_pub_ctr/de231z.pdf"

# (rate_key, rate, wage_limit, paid_by, description)
RATE_ROWS_2026 = [
    ("ui_new_employer", 0.034, 7000, "employer", "UI rate for new employers, for two to three years"),
    ("ui_min", 0.015, 7000, "employer", "Lowest experience-rated UI rate (Schedule F+, 2026)"),
    ("ui_max", 0.062, 7000, "employer", "Highest experience-rated UI rate (Schedule F+, 2026)"),
    ("ett", 0.001, 7000, "employer", "Employment Training Tax; employers with a positive UI reserve balance"),
    ("sdi", 0.013, None, "employee", "SDI incl. Paid Family Leave, withheld from employee wages; no wage limit since 1/1/2024"),
    ("pit_supp_bonus", 0.1023, None, "employee", "PIT flat rate on bonuses and stock options"),
    ("pit_supp_other", 0.066, None, "employee", "PIT flat rate on other supplemental wages"),
    ("late_penalty", 0.15, None, "employer", "Penalty on late payroll tax payments"),
]

# (federal_schedule, min, max, ca_schedule, due_rule). Bands are half-open
# [min, max). "$350 to $400" includes $400 and "more than $400" excludes it, so
# that boundary is 400.005 (amounts are in cents).
# 2027 figures EDD has published (edd.ca.gov Rates and Withholding, seen 2026-11-01):
# SDI 1.4%, UI schedule F+ (1.5%-6.2%), new employer 3.4%, $7,000 limit, ETT 0.1%.
# Supplemental-wage rates, penalty, deposit and due-date tables for 2027 are NOT
# published yet (new DE 44 edition expected in January), so they are not seeded.
RATE_ROWS_2027 = [
    ("ui_new_employer", 0.034, 7000, "employer", "UI rate for new employers, for two to three years"),
    ("ui_min", 0.015, 7000, "employer", "Lowest experience-rated UI rate (Schedule F+, 2027)"),
    ("ui_max", 0.062, 7000, "employer", "Highest experience-rated UI rate (Schedule F+, 2027)"),
    ("ett", 0.001, 7000, "employer", "Employment Training Tax"),
    ("sdi", 0.014, None, "employee", "SDI incl. Paid Family Leave, withheld from employee wages; no wage limit"),
]
RATES_2027_URL = "https://edd.ca.gov/en/payroll_taxes/rates_and_withholding/"

DEPOSIT_ROWS_2026 = [
    ("next_day", 0, 350, "quarterly", "quarterly"),
    ("next_day", 350, 400.005, "monthly", "15th_following_month"),
    ("next_day", 400.005, None, "next_day", "next_day"),
    ("semi_weekly", 0, 350, "quarterly", "quarterly"),
    ("semi_weekly", 350, 400.005, "monthly", "15th_following_month"),
    ("semi_weekly", 400.005, None, "semi_weekly", "semi_weekly"),
    ("monthly", 0, 350, "quarterly", "quarterly"),
    ("monthly", 350, None, "monthly", "15th_following_month"),
    ("quarterly_or_annual", 0, 350, "quarterly", "quarterly"),
    ("quarterly_or_annual", 350, None, "monthly", "15th_following_month"),
]

# (quarter, due, delinquent)
DUE_DATE_ROWS_2026 = [
    (1, "2026-04-01", "2026-04-30"),
    (2, "2026-07-01", "2026-07-31"),
    (3, "2026-10-01", "2026-11-02"),
    (4, "2027-01-01", "2027-02-01"),
]

# (start, end, annual_rate, daily_factor)
INTEREST_ROWS = [
    ("2023-01-01", "2023-06-30", 0.05, 0.000137),
    ("2023-07-01", "2023-12-31", 0.07, 0.000192),
    ("2024-01-01", "2024-06-30", 0.07, 0.000191),
    ("2024-07-01", "2024-12-31", 0.08, 0.000219),
    ("2025-01-01", "2025-06-30", 0.08, 0.000219),
    ("2025-07-01", "2025-12-31", 0.07, 0.000192),
    ("2026-01-01", "2026-06-30", 0.07, 0.000192),
    ("2026-07-01", "2026-12-31", 0.07, 0.000192),
]


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    print("employer domain schema created")
    status()


def seed():
    with db.get_conn() as conn:
        for key, rate, limit, paid_by, desc in RATE_ROWS_2026:
            conn.execute(
                "INSERT INTO ca_employer_payroll_rates (tax_year, rate_key, rate, wage_limit, paid_by, "
                "description, citation, source_url) VALUES (2026, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tax_year, rate_key) DO UPDATE SET rate=EXCLUDED.rate, "
                "wage_limit=EXCLUDED.wage_limit, paid_by=EXCLUDED.paid_by, description=EXCLUDED.description",
                (key, rate, limit, paid_by, desc, DE44, DE44_URL))
        for key, rate, limit, paid_by, desc in RATE_ROWS_2027:
            conn.execute(
                "INSERT INTO ca_employer_payroll_rates (tax_year, rate_key, rate, wage_limit, paid_by, "
                "description, citation, source_url) VALUES (2027, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tax_year, rate_key) DO UPDATE SET rate=EXCLUDED.rate, "
                "wage_limit=EXCLUDED.wage_limit, paid_by=EXCLUDED.paid_by, description=EXCLUDED.description",
                (key, rate, limit, paid_by, desc, "EDD Rates and Withholding (2027 rates)", RATES_2027_URL))
        for ty, key, rate, limit, desc in FUTA_ROWS:
            conn.execute(
                "INSERT INTO ca_employer_payroll_rates (tax_year, rate_key, rate, wage_limit, paid_by, "
                "description, citation, source_url) VALUES (%s, %s, %s, %s, 'employer', %s, %s, %s) "
                "ON CONFLICT (tax_year, rate_key) DO UPDATE SET rate=EXCLUDED.rate, "
                "wage_limit=EXCLUDED.wage_limit, description=EXCLUDED.description, citation=EXCLUDED.citation, "
                "source_url=EXCLUDED.source_url",
                (ty, key, rate, limit, desc, IRS940, IRS940_URL))
        for ty, rate, pmax, st, notes in FUTA_REDUCTION_ROWS:
            conn.execute(
                "INSERT INTO ca_futa_credit_reductions (tax_year, reduction_rate, potential_max_rate, status, "
                "notes, source_url, as_of) VALUES (%s, %s, %s, %s, %s, %s, '2026-10-31') "
                "ON CONFLICT (tax_year) DO UPDATE SET reduction_rate=EXCLUDED.reduction_rate, "
                "potential_max_rate=EXCLUDED.potential_max_rate, status=EXCLUDED.status, notes=EXCLUDED.notes",
                (ty, rate, pmax, st, notes, DOL_URL))
        conn.execute("DELETE FROM ca_ui_rate_schedule")
        for lo, hi, rates in UI_RATE_ROWS:
            for sched, pct in zip(UI_SCHEDULES, rates):
                conn.execute(
                    "INSERT INTO ca_ui_rate_schedule (schedule, ratio_min, ratio_max, rate, citation) "
                    "VALUES (%s, %s, %s, %s, %s)", (sched, lo, hi, round(pct / 100, 4), DE231Z))
        for ty, sched in UI_SCHEDULE_YEARS:
            conn.execute(
                "INSERT INTO ca_ui_schedule_years (tax_year, schedule, source_url) VALUES (%s, %s, %s) "
                "ON CONFLICT (tax_year) DO UPDATE SET schedule=EXCLUDED.schedule",
                (ty, sched, "https://edd.ca.gov/en/payroll_taxes/rates_and_withholding/"))
        for fed, lo, hi, ca, due in DEPOSIT_ROWS_2026:
            conn.execute(
                "INSERT INTO ca_edd_deposit_rules (tax_year, federal_schedule, min_accumulated, "
                "max_accumulated, ca_schedule, due_rule, citation) VALUES (2026, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tax_year, federal_schedule, min_accumulated) DO UPDATE SET "
                "max_accumulated=EXCLUDED.max_accumulated, ca_schedule=EXCLUDED.ca_schedule, "
                "due_rule=EXCLUDED.due_rule",
                (fed, lo, hi, ca, due, DE44))
        for q, due, delinq in DUE_DATE_ROWS_2026:
            conn.execute(
                "INSERT INTO ca_edd_due_dates (tax_year, quarter, due_date, delinquent_date, citation) "
                "VALUES (2026, %s, %s, %s, %s) ON CONFLICT (tax_year, quarter) DO UPDATE SET "
                "due_date=EXCLUDED.due_date, delinquent_date=EXCLUDED.delinquent_date",
                (q, due, delinq, DE44))
        for start, end, rate, factor in INTEREST_ROWS:
            conn.execute(
                "INSERT INTO ca_edd_interest_rates (period_start, period_end, annual_rate, daily_factor, "
                "source_url) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (period_start) DO UPDATE SET "
                "period_end=EXCLUDED.period_end, annual_rate=EXCLUDED.annual_rate, "
                "daily_factor=EXCLUDED.daily_factor",
                (start, end, rate, factor, INTEREST_URL))
    print("seeded employer rate / deposit / due-date / interest rows")
    status()


def status():
    conn = db.get_conn()
    for tbl in ("ca_employer_payroll_rates", "ca_edd_deposit_rules", "ca_edd_due_dates",
                "ca_edd_interest_rates", "ca_futa_credit_reductions", "ca_ui_rate_schedule", "ca_ui_schedule_years", "employer_rule_embeddings", "employer_tax_inventory"):
        n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:30} {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "create":
        create()
    elif cmd == "seed":
        seed()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
