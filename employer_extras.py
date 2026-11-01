"""Remaining EDD topics for the employer domain -- Ring 5: household employers,
voluntary UI contributions, which rate schedule applies (UI Fund ratio), and
worker-side UI / disability / paid-family-leave benefit amounts.

Sources (all EDD):
  - Household employers: edd.ca.gov Household Employer page.
  - Voluntary UI contributions and the schedule thresholds: DE 231Z Rev. 17
    (6-22); CUIC 976.5 and 977.
  - UI weekly benefit: Unemployment Insurance Benefit Table, DE 1101BT5, for new
    claims beginning January 2, 2005 or after (411 bands, parsed and
    integrity-checked: contiguous, monotone, +$1 at most per band, $40 to $450,
    and the $450 maximum agrees with DE 44 Rev. 52).
  - DI / paid-family-leave weekly benefit: EDD's DI benefit-calculation page and
    EDD Quick Statistics ($1,765 maximum for CY2026). EDD words the tiers in
    terms of "weekly wages"; the arithmetic ties them to highest-quarter wages
    / 13 (90% of $16,279.90/13 = $1,127.07, the flat tier above it; 70% of
    $20,931.31/13 = $1,127.07; 90% of $722.50/13 = $50.02, the $50 minimum),
    which is how this module applies them.

Usage:
  python employer_extras.py create
  python employer_extras.py load
  python employer_extras.py status
"""
import json
import os
import sys

import employer_db as db

HERE = os.path.dirname(os.path.abspath(__file__))
UI_TABLE_FILE = os.path.join(HERE, "ui_benefit_table_2005.json")

DE231Z = "EDD DE 231Z, California System of Experience Rating (Rev. 17, 6-22); CUIC 976.5, 977"
DE231Z_URL = "https://edd.ca.gov/pdf_pub_ctr/de231z.pdf"
HOUSEHOLD = "EDD Household Employer page (edd.ca.gov/en/payroll_taxes/household_employer)"
HOUSEHOLD_URL = "https://edd.ca.gov/en/payroll_taxes/household_employer/"
UI_BENEFIT = "EDD Unemployment Insurance Benefit Table, DE 1101BT5 (claims beginning Jan. 2, 2005 or after)"
UI_BENEFIT_URL = "https://edd.ca.gov/siteassets/files/pdf_pub_ctr/de1101bt5.pdf"
DI_BENEFIT = "EDD DI benefit calculation page; EDD State Disability Insurance Quick Statistics (CY2026)"
DI_BENEFIT_URL = "https://edd.ca.gov/en/disability/calculating_di_benefit_payment_amounts/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ca_ui_benefit_table (
    hq_min NUMERIC PRIMARY KEY,
    hq_max NUMERIC,
    weekly_benefit NUMERIC NOT NULL
);

CREATE TABLE IF NOT EXISTS ca_di_benefit_tiers (
    tier        INTEGER PRIMARY KEY,
    hq_min      NUMERIC NOT NULL,
    hq_max      NUMERIC,
    kind        TEXT NOT NULL,
    value       NUMERIC,
    weekly_cap  NUMERIC,
    tax_year    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ca_ui_schedule_thresholds (
    schedule  TEXT PRIMARY KEY,
    ratio_min NUMERIC,
    ratio_max NUMERIC,
    notes     TEXT
);
"""

# DI/PFL tiers for 2026. kind: none (ineligible), flat, pct_of_weekly (weekly wages = HQ / 13).
DI_TIERS_2026 = [
    (1, 0, 299.99, "none", None, None),
    (2, 300, 722.49, "flat", 50, None),
    (3, 722.50, 16279.90, "pct_of_weekly", 0.90, None),
    (4, 16279.91, 20931.30, "flat", 1127, None),
    (5, 20931.31, None, "pct_of_weekly", 0.70, 1765),
]

# UI Fund balance ratio (percent) -> schedule, DE 231Z. Bands [min, max).
SCHEDULE_THRESHOLDS = [
    ("AA", 1.8, None, None),
    ("A", 1.6, 1.8, None),
    ("B", 1.4, 1.6, None),
    ("C", 1.2, 1.4, None),
    ("D", 1.0, 1.2, None),
    ("E", 0.8, 1.0, None),
    ("F", 0.6, 0.8, None),
    ("F+", None, 0.6, "Emergency solvency surcharge: 1.15 times the Schedule F rate, rounded to the nearest tenth"),
]

# (rule_key, value, unit, description, citation, url)
HOUSEHOLD_RULES = [
    ("household_cash_wages_threshold", 750, "dollars",
     "Household employer: register and report once you pay cash wages of $750 or more in a calendar quarter (meals and lodging do not count)",
     HOUSEHOLD, HOUSEHOLD_URL),
    ("household_ui_ett_threshold", 1000, "dollars",
     "Household employer: pay UI and ETT (in addition to withholding SDI) once cash wages are $1,000 or more in a quarter",
     HOUSEHOLD, HOUSEHOLD_URL),
    ("household_annual_filing_max", 20000, "dollars",
     "Household employer may elect annual filing (DE 89; DE 3HW) if annual wages are $20,000 or less; above that, quarterly",
     HOUSEHOLD, HOUSEHOLD_URL),
]


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    status()


def load():
    rows = json.load(open(UI_TABLE_FILE, encoding="utf-8"))
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
        conn.execute("DELETE FROM ca_ui_benefit_table")
        for lo, hi, wba in rows:
            conn.execute("INSERT INTO ca_ui_benefit_table (hq_min, hq_max, weekly_benefit) VALUES (%s,%s,%s)",
                         (lo, hi, wba))
        conn.execute("DELETE FROM ca_di_benefit_tiers WHERE tax_year=2026")
        for tier, lo, hi, kind, value, cap in DI_TIERS_2026:
            conn.execute("INSERT INTO ca_di_benefit_tiers (tier, hq_min, hq_max, kind, value, weekly_cap, tax_year) "
                         "VALUES (%s,%s,%s,%s,%s,%s,2026)", (tier, lo, hi, kind, value, cap))
        conn.execute("DELETE FROM ca_ui_schedule_thresholds")
        for sched, lo, hi, notes in SCHEDULE_THRESHOLDS:
            conn.execute("INSERT INTO ca_ui_schedule_thresholds (schedule, ratio_min, ratio_max, notes) "
                         "VALUES (%s,%s,%s,%s)", (sched, lo, hi, notes))
        for key, value, unit, desc, cite, url in HOUSEHOLD_RULES:
            conn.execute(
                "INSERT INTO ca_employer_rules (rule_key, value, unit, description, citation, source_url) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (rule_key) DO UPDATE SET value=EXCLUDED.value, "
                "unit=EXCLUDED.unit, description=EXCLUDED.description, citation=EXCLUDED.citation, "
                "source_url=EXCLUDED.source_url", (key, value, unit, desc, cite, url))
    status()


def status():
    conn = db.get_conn()
    for tbl in ("ca_ui_benefit_table", "ca_di_benefit_tiers", "ca_ui_schedule_thresholds"):
        try:
            n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        except Exception:
            n = "(not created)"
        print(f"  {tbl:28} {n} rows")
    conn.close()


# ----------------------------------------------------------------- compute
def compute_ui_benefit(conn, highest_quarter_wages):
    """Weekly UI benefit from the DE 1101BT5 table (claims beginning Jan 2,
    2005 or after). Returns None below the table's $900 floor or for bad input.
    Eligibility (base-period earnings tests, work search, etc.) is NOT evaluated."""
    if highest_quarter_wages is None or highest_quarter_wages < 0:
        return None
    row = conn.execute(
        "SELECT weekly_benefit, hq_min, hq_max FROM ca_ui_benefit_table WHERE hq_min <= %s "
        "AND (hq_max IS NULL OR %s <= hq_max) ORDER BY hq_min DESC LIMIT 1",
        (highest_quarter_wages, highest_quarter_wages)).fetchone()
    if not row:
        # between a band's max and the next band's min can only be a sub-cent gap
        row = conn.execute("SELECT weekly_benefit, hq_min, hq_max FROM ca_ui_benefit_table WHERE hq_min <= %s "
                           "ORDER BY hq_min DESC LIMIT 1", (highest_quarter_wages,)).fetchone()
        if not row or highest_quarter_wages < 900:
            return None
    maximum = conn.execute("SELECT max(weekly_benefit) FROM ca_ui_benefit_table").fetchone()[0]
    return {"highest_quarter_wages": highest_quarter_wages, "weekly_benefit": float(row[0]),
            "at_maximum": float(row[0]) >= float(maximum), "maximum": float(maximum),
            "citation": UI_BENEFIT, "source_url": UI_BENEFIT_URL}


def compute_di_benefit(conn, highest_quarter_wages, tax_year=2026):
    """Estimated weekly disability / paid-family-leave benefit from EDD's tiers:
    under $300 not eligible, $300-722.49 the $50 minimum, 90% of weekly wages up
    to $16,279.90, a flat $1,127 to $20,931.30, then 70% of weekly wages up to
    the $1,765 maximum. Weekly wages = highest-quarter wages / 13 (see module
    docstring). An estimate: EDD confirms the actual amount when the claim is
    approved."""
    if highest_quarter_wages is None or highest_quarter_wages < 0:
        return None
    row = conn.execute(
        "SELECT kind, value, weekly_cap, tier FROM ca_di_benefit_tiers WHERE tax_year=%s AND hq_min <= %s "
        "ORDER BY hq_min DESC LIMIT 1", (tax_year, highest_quarter_wages)).fetchone()
    if not row:
        return None
    kind, value, cap, tier = row[0], row[1], row[2], row[3]
    weekly_wages = highest_quarter_wages / 13.0
    if kind == "none":
        wba = 0.0
    elif kind == "flat":
        wba = float(value)
    else:
        wba = weekly_wages * float(value)
        if cap is not None:
            wba = min(wba, float(cap))
    maximum = conn.execute("SELECT max(weekly_cap) FROM ca_di_benefit_tiers WHERE tax_year=%s", (tax_year,)).fetchone()[0]
    return {"highest_quarter_wages": highest_quarter_wages, "weekly_wages": round(weekly_wages, 2), "tier": tier,
            "kind": kind, "rate": float(value) if kind == "pct_of_weekly" else None,
            "weekly_benefit": round(wba, 2), "eligible": kind != "none", "maximum": float(maximum),
            "citation": DI_BENEFIT, "source_url": DI_BENEFIT_URL}


def schedule_from_fund_ratio(conn, fund_ratio_pct):
    """Which UI contribution rate schedule applies, from the UI Fund balance on
    Sept 30 divided by gross wages reported by all employers in the fiscal year
    ending June 30, as a percent (DE 231Z). A ratio under 0.6 puts the
    emergency solvency surcharge (Schedule F+) in effect."""
    if fund_ratio_pct is None:
        return None
    row = conn.execute(
        "SELECT schedule, ratio_min, ratio_max, notes FROM ca_ui_schedule_thresholds "
        "WHERE (ratio_min IS NULL OR ratio_min <= %s) AND (ratio_max IS NULL OR %s < ratio_max)",
        (fund_ratio_pct, fund_ratio_pct)).fetchone()
    if not row:
        return None
    return {"fund_ratio_pct": fund_ratio_pct, "schedule": row[0], "notes": row[3],
            "voluntary_contributions_allowed": row[0] in ("AA", "A", "B", "C", "D"),
            "citation": DE231Z, "source_url": DE231Z_URL}


def compute_household(conn, cash_wages_quarter, annual_wages=None):
    """Household-employer obligations by quarterly cash wages: $750 or more =
    register (within 15 days) and report and withhold SDI; $1,000 or more also
    UI and ETT; PIT withholding optional. Annual filing may be elected when
    annual wages are $20,000 or less."""
    if cash_wages_quarter is None or cash_wages_quarter < 0:
        return None
    rules = {k: conn.execute("SELECT value FROM ca_employer_rules WHERE rule_key=%s", (k,)).fetchone()
             for k in ("household_cash_wages_threshold", "household_ui_ett_threshold", "household_annual_filing_max")}
    if any(v is None for v in rules.values()):
        return None
    t_reg = float(rules["household_cash_wages_threshold"][0])
    t_ui = float(rules["household_ui_ett_threshold"][0])
    t_annual = float(rules["household_annual_filing_max"][0])
    out = {"cash_wages_quarter": cash_wages_quarter, "must_register": cash_wages_quarter >= t_reg,
           "withhold_sdi": cash_wages_quarter >= t_reg, "pay_ui_ett": cash_wages_quarter >= t_ui,
           "threshold_register": t_reg, "threshold_ui_ett": t_ui, "annual_filing_max": t_annual,
           "annual_filing_eligible": None, "citation": HOUSEHOLD, "source_url": HOUSEHOLD_URL}
    if annual_wages is not None:
        out["annual_filing_eligible"] = annual_wages <= t_annual
    return out


def voluntary_ui_availability(conn, tax_year, reserve_balance=None, ui_rate=None):
    """Whether a voluntary UI contribution (VUI) can lower the rate. DE 231Z:
    allowed only when schedules AA through D are in effect (CUIC 976.5 bars E, F
    and any year with the emergency surcharge, F+); not for an employer at the
    3.4% required rate, with a negative reserve balance, or already at the
    lowest rate. Returns None for a year with no stored schedule."""
    row = conn.execute("SELECT schedule FROM ca_ui_schedule_years WHERE tax_year=%s", (tax_year,)).fetchone()
    if not row:
        return None
    schedule = row[0]
    reasons = []
    if schedule not in ("AA", "A", "B", "C", "D"):
        reasons.append(f"Schedule {schedule} is in effect for {tax_year}; voluntary contributions are prohibited "
                       "under schedules E, F and F+ (CUIC 976.5)")
    if reserve_balance is not None and reserve_balance < 0:
        reasons.append("a negative reserve account balance makes you ineligible")
    if ui_rate is not None and abs(ui_rate - 0.034) < 1e-9:
        reasons.append("a required rate of 3.4% makes you ineligible")
    return {"tax_year": tax_year, "schedule": schedule, "available": not reasons, "reasons": reasons,
            "citation": DE231Z, "source_url": DE231Z_URL}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"create": create, "load": load}.get(cmd, status)()
