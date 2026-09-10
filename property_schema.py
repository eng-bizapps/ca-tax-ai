"""Schema for the CA property tax (county-assessor/BOE) domain -- Ring 4
database split (own physically-separate Postgres/pgvector database,
mirroring the Ring 2 income-DB split). See property_db.py for the
connection.

Deliberately NOT one JSONB-facts table -- same "separate table per concept,
never JSONB-facts" precedent as income_schema.py, which explicitly rejected
that shape as unauditable (the prior-generation SQL system in this repo
tried it).

Three genuinely different fact shapes:
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

The Homeowners' Exemption ($7,000) is NOT a table here -- it's fixed
directly in the CA Constitution (Art. XIII Sec. 3(k)), not annually
republished data the way the DV exemption is. Lives as a Python constant
in property_tax.py, same class as CASUALTY_LOSS_AGI_FLOOR_RATE in
income_brackets.py. If ever amended, promote to a tax_year-keyed table then.

Usage:
  python property_schema.py create   # create all 3 tables (idempotent)
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
          "property_rule_embeddings, property_tax_inventory)")
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
    for tbl in ("ca_disabled_veterans_exemption", "property_rule_embeddings", "property_tax_inventory"):
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
