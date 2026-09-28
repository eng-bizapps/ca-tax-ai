"""Schema for the CA property tax (county-assessor/BOE) domain -- Ring 4
database split (own physically-separate Postgres/pgvector database,
mirroring the Ring 2 income-DB split). See property_db.py for the
connection.

Deliberately NOT one JSONB-facts table -- same "separate table per concept,
never JSONB-facts" precedent as income_schema.py, which explicitly rejected
that shape as unauditable (the prior-generation SQL system in this repo
tried it).

Five genuinely different fact shapes:
  - ca_disabled_veterans_exemption -- inflation-indexed, republished
    annually by BOE Letter To Assessors (R&TC 205.5), same "one row per
    tax_year" shape as income_schema.py's ca_standard_deduction. NOT keyed
    by filing_status -- property tax is parcel-based, not tied to the
    owner's income-tax filing status.
  - property_rule_embeddings -- mirrors income_rule_embeddings exactly, own
    vector space so property-domain candidates are never compared against
    sales/income-domain distances as if they mean the same thing. 0 rows at
    launch (schema-only groundwork, same state income_rule_embeddings
    itself started in) -- not populated or queried by the first build.
  - property_tax_inventory -- the completeness ledger, same generic shape
    as schedule_ca_inventory (tax_year, part, section, line_ref,
    item_label, adjustment_type, frequency, citation, status, topic_key,
    notes). A genuinely NEW table here (not a literal reuse of
    schedule_ca_inventory) because that table physically lives in the
    INCOME database -- form540_inventory.py's cross-part reuse trick only
    works within one database, and Ring 4 is a separate database by design.
  - county_override_rates -- a MANY-rows-per-tax_year table (unlike
    ca_disabled_veterans_exemption's one-row-per-year shape), one row per
    CA county's voter-approved local ad-valorem override rate on top of
    the 1% base, sourced from the CA State Controller's "CA Property Tax
    Data" portal. Seeded by the separate load_county_override_rates.py
    (mirrors load_payroll_withholding_data.py's own "large externally-
    sourced dataset gets its own loader file" precedent), not seed()
    below. See property_tax.compute_county_override_rate for the lookup
    (deliberately NOT an exact tax_year match -- see that function's own
    docstring for why).
  - tra_rates -- exact per-Tax-Rate-Area (TRA) combined ad-valorem rate,
    a NARROWLY-SCOPED PILOT for exactly ONE county (Kern) -- NOT a general
    58-county solution (that stays deferred, see property_tax_inventory.py's
    'local-tra-rate' item; this is a separate 'tra-rate-kern-pilot' item).
    Stores the FULL combined rate directly (1% base + all overrides
    already summed by Kern's own rate book), unlike county_override_rates'
    override-only figure -- a simpler representation matching what the
    source actually publishes. Seeded by load_kern_tra_rates.py from a CSV
    produced by extract_kern_tra_rates.py (a real, hands-on-validated PDF
    extraction -- see that script's own docstring for the extraction
    algorithm and the two real bugs found and fixed while building it).

The Homeowners' Exemption ($7,000) is NOT a table here -- it's fixed
directly in the CA Constitution (Art. XIII Sec. 3(k)), not annually
republished data the way the DV exemption is. Lives as a Python constant
in property_tax.py, same class as CASUALTY_LOSS_AGI_FLOOR_RATE in
income_brackets.py. If ever amended, promote to a tax_year-keyed table then.

Usage:
  python property_schema.py create   # create all 5 tables (idempotent)
  python property_schema.py seed     # insert verified DV-exemption + ledger rows
  python property_schema.py status   # row counts
"""
import sys

import config
import property_db as db

SCHEMA = f"""
-- Disabled Veterans' Exemption -- INFLATION-INDEXED, published annually by
-- BOE Letter To Assessors (R&TC 205.5). Two tiers: basic_exemption applies
-- with NO income limit; low_income_exemption is the HIGHER tier, available
-- only when household income is under income_limit.
CREATE TABLE IF NOT EXISTS ca_disabled_veterans_exemption (
    id                    SERIAL PRIMARY KEY,
    tax_year              INTEGER NOT NULL,
    basic_exemption       NUMERIC NOT NULL,
    low_income_exemption  NUMERIC NOT NULL,
    income_limit          NUMERIC NOT NULL,
    citation              TEXT,
    source_url            TEXT,
    as_of                 DATE,
    UNIQUE (tax_year)
);

-- Mirrors income_rule_embeddings exactly -- own vector space, per the
-- established "each domain routed independently against its own
-- calibrated threshold, never merged" precedent. 0 rows at launch.
CREATE TABLE IF NOT EXISTS property_rule_embeddings (
    topic_key TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    text      TEXT NOT NULL,
    embedding vector({config.EMBED_DIM})
);

-- COMPLETENESS LEDGER -- same generic shape as schedule_ca_inventory, but
-- a NEW table here (not a reuse) since that table lives in a different
-- physical database. `part` values: 'core' (Prop 13 estimate + Homeowners'
-- Exemption), 'dv_exemption', 'prop19', 'excluded' (confirmed out of scope
-- items, not tied to one slice). Populated by property_tax_inventory.py.
CREATE TABLE IF NOT EXISTS property_tax_inventory (
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

-- County-average voter-approved local ad-valorem OVERRIDE rate (on top of
-- the 1% base) -- CA State Controller's Office "CA Property Tax Data"
-- portal (Allocations + Levies by county). MANY rows per tax_year (one
-- per county), unlike ca_disabled_veterans_exemption above -- tax_year is
-- the LIEN YEAR the underlying fiscal-year data represents (property_tax.
-- DEFAULT_LIEN_YEAR's own convention), looked up via "most recent
-- tax_year <= requested" (property_tax.compute_county_override_rate),
-- not an exact match -- county bond resolutions are adopted Aug-Sept and
-- this data is aggregated afterward, so it naturally lags
-- DEFAULT_LIEN_YEAR by about a fiscal year.
CREATE TABLE IF NOT EXISTS county_override_rates (
    id            SERIAL PRIMARY KEY,
    tax_year      INTEGER NOT NULL,
    county        TEXT NOT NULL,
    override_rate NUMERIC NOT NULL,
    citation      TEXT,
    source_url    TEXT,
    as_of         DATE,
    UNIQUE (tax_year, county)
);

-- Exact per-TRA combined ad-valorem rate -- a NARROW PILOT for exactly ONE
-- county (Kern), not a general 58-county solution (see property_tax_
-- inventory.py's separate 'local-tra-rate' vs. 'tra-rate-kern-pilot'
-- items). total_rate is the FULL combined rate (1% base + all overrides
-- already summed by the source), unlike county_override_rates.
-- override_rate above -- a simpler representation matching what Kern's
-- own rate book actually publishes. Same "most recent tax_year <=
-- requested" lookup design as county_override_rates, same reason (only
-- knowable in arrears relative to DEFAULT_LIEN_YEAR).
CREATE TABLE IF NOT EXISTS tra_rates (
    id          SERIAL PRIMARY KEY,
    tax_year    INTEGER NOT NULL,
    county      TEXT NOT NULL,
    tra_number  TEXT NOT NULL,
    area_name   TEXT,
    total_rate  NUMERIC NOT NULL,
    citation    TEXT,
    source_url  TEXT,
    as_of       DATE,
    UNIQUE (tax_year, county, tra_number)
);
"""

# Verified directly against BOE Letter To Assessors 2025/014 (2025-05-21)
# and the 2025 base figures it compounds forward from -- NOT secondary
# sources. R&TC Sec. 205.5's own inflation-indexing mechanic (CA CPI,
# Feb-to-Feb change) means these figures move every year; seed only years
# actually confirmed against a primary BOE publication, never guessed
# forward.
DV_EXEMPTION_CITATION = "Rev. & Tax. Code Sec. 205.5; BOE Letter To Assessors 2025/014"
DV_EXEMPTION_SOURCE_URL = "https://www.boe.ca.gov/proptaxes/dv_exemption.htm"
DV_EXEMPTION_ROWS = [
    # (tax_year, basic_exemption, low_income_exemption, income_limit)
    (2025, 175298.0, 262950.0, 78718.0),
    (2026, 180671.0, 271009.0, 81131.0),
]


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    print("property domain schema created (ca_disabled_veterans_exemption, "
          "property_rule_embeddings, property_tax_inventory, county_override_rates, tra_rates)")
    status()


def seed():
    with db.get_conn() as conn:
        for tax_year, basic, low_income, income_limit in DV_EXEMPTION_ROWS:
            conn.execute(
                "INSERT INTO ca_disabled_veterans_exemption "
                "(tax_year, basic_exemption, low_income_exemption, income_limit, citation, source_url) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tax_year) DO UPDATE SET "
                "basic_exemption=EXCLUDED.basic_exemption, "
                "low_income_exemption=EXCLUDED.low_income_exemption, "
                "income_limit=EXCLUDED.income_limit, "
                "citation=EXCLUDED.citation, source_url=EXCLUDED.source_url",
                (tax_year, basic, low_income, income_limit, DV_EXEMPTION_CITATION, DV_EXEMPTION_SOURCE_URL))
    print(f"seeded {len(DV_EXEMPTION_ROWS)} ca_disabled_veterans_exemption rows")
    status()


def status():
    conn = db.get_conn()
    for tbl in ("ca_disabled_veterans_exemption", "property_rule_embeddings", "property_tax_inventory",
                "county_override_rates", "tra_rates"):
        n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:32} {n} rows")
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
