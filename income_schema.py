"""Schema for the CA income tax (FTB) domain -- Ring 2, Phase 0 groundwork.

Deliberately NOT added to product_rules: that table is sales-tax-shaped
(taxable BOOLEAN + one flat rate NUMERIC) and cannot hold progressive
brackets, filing status, or credit phase-outs (see the design note this
followed). Follows the same "separate table per concept" precedent as
local_rates.py / fees.py / cannabis_local.py rather than one flexible/JSONB
table -- the prior-generation SQL system in this repo tried JSONB-facts and
it was explicitly rejected as unauditable.

No loader/population scripts exist yet (Phase 0 is schema-only groundwork,
ahead of any crawled FTB content) -- each table will likely get its own
dedicated loader file later (mirroring load_product_rules.py / local_rates.py
/ fees.py), at which point its schema block can move out of this shared file,
same as how rule_embeddings' schema lives in embed_rules.py rather than here.

Usage:
  python income_schema.py create   # create all 4 tables (idempotent)
  python income_schema.py status   # row counts
"""
import sys

import config
import income_db as db

SCHEMA = f"""
-- Mirrors product_rules' shape for the parts that transfer (a topic-level
-- taxability/treatment determination with a citation), swapping
-- product_key/product_label for topic_key/topic_label since "product" doesn't
-- apply to income tax. `taxable` is nullable and only meaningful for
-- genuinely boolean questions ("is unemployment income taxable in CA").
-- `treatment` is free text for genuinely prose-only/informational topics
-- ONLY (tier='administrative' equivalent) -- anything a computed number
-- depends on must get its own typed column or table (see
-- ca_income_tax_brackets/ca_income_credits), never this column, to avoid
-- reintroducing the opacity problem the old JSONB-facts system had.
CREATE TABLE IF NOT EXISTS income_tax_topics (
    id            SERIAL PRIMARY KEY,
    topic_key     TEXT NOT NULL,
    topic_label   TEXT,
    taxable       BOOLEAN,
    treatment     TEXT,
    filing_status TEXT,
    tax_year      INTEGER,
    citation      TEXT,
    condition     TEXT,
    summary       TEXT,
    source_url    TEXT,
    tier          TEXT NOT NULL DEFAULT 'consumer',
    status        TEXT NOT NULL DEFAULT 'ai_drafted',
    confidence    REAL,
    model_used    TEXT,
    drafted_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (topic_key, tax_year)
);

-- Hand-verified reference data (NOT LLM-drafted), same spirit as local_rates.
-- bracket_type distinguishes the standard progressive brackets (which vary by
-- filing_status) from the Mental Health Services Tax's 1% surtax on income
-- over $1M, which does NOT vary by filing status and is NOT doubled for
-- joint filers -- modeled as its own bracket_type='mhs_surtax' row
-- (filing_status=NULL meaning "applies regardless") rather than silently
-- folded into the standard per-filing-status rows, where it would be wrong
-- for exactly the high-income joint-filer case most likely to check it.
CREATE TABLE IF NOT EXISTS ca_income_tax_brackets (
    id               SERIAL PRIMARY KEY,
    tax_year         INTEGER NOT NULL,
    filing_status    TEXT,              -- NULL = applies regardless of filing status
    bracket_type     TEXT NOT NULL DEFAULT 'standard',  -- 'standard' | 'mhs_surtax'
    bracket_floor    NUMERIC NOT NULL,
    bracket_ceiling  NUMERIC,           -- NULL = no upper bound
    rate             NUMERIC NOT NULL,
    citation         TEXT,
    source_url       TEXT,
    as_of            DATE,
    UNIQUE (tax_year, filing_status, bracket_type, bracket_floor)
);

-- FTB's own Rate Schedules publish the CUMULATIVE tax at each bracket floor
-- directly (e.g. Schedule Y 2025: "$3,974.82 + 8.00% of the amount over
-- $115,084") -- store that verbatim number rather than re-deriving it via our
-- own segment summation, so the stored figures match the official PDF
-- byte-for-byte and carry no independent rounding risk. Idempotent, additive
-- (Phase 0/2's income_schema.py predates this column).
ALTER TABLE ca_income_tax_brackets ADD COLUMN IF NOT EXISTS base_amount NUMERIC NOT NULL DEFAULT 0;

-- Standard deduction is its own concept (a single verified number per
-- tax_year x filing_status that a downstream computation depends on), not a
-- bracket or a credit -- same "separate table per concept" precedent as
-- local_rates/ca_income_tax_brackets rather than folding it into either.
CREATE TABLE IF NOT EXISTS ca_standard_deduction (
    id            SERIAL PRIMARY KEY,
    tax_year      INTEGER NOT NULL,
    filing_status TEXT NOT NULL,
    amount        NUMERIC NOT NULL,
    citation      TEXT,
    source_url    TEXT,
    as_of         DATE,
    UNIQUE (tax_year, filing_status)
);

-- Reference numbers for CA income tax credits (CalEITC, YCTC, renter's
-- credit, dependent exemption credits...). Eligibility/phase-out MATCHING
-- logic belongs in a future income_credits.py applicable() function
-- mirroring fees.py's term-triggered pattern -- this table holds only the
-- verified numeric truth that function would look up, not the matching logic
-- itself.
CREATE TABLE IF NOT EXISTS ca_income_credits (
    id                SERIAL PRIMARY KEY,
    credit_key        TEXT NOT NULL,
    credit_label      TEXT,
    tax_year          INTEGER NOT NULL,
    filing_status     TEXT,
    max_amount        NUMERIC,
    phase_out_start   NUMERIC,
    phase_out_end     NUMERIC,
    refundable        BOOLEAN,
    citation          TEXT,
    source_url        TEXT,
    as_of             DATE,
    UNIQUE (credit_key, tax_year, filing_status)
);

-- The phase-out RATE (e.g. YCTC's "$21.71 per $100 of excess income") is
-- itself verified reference data, same as max_amount/phase_out_start/end --
-- store it here rather than hardcoding it in income_credits.py's compute
-- logic, so every verified number stays in one place, auditable the same
-- way as every other table in this file. Idempotent, additive.
ALTER TABLE ca_income_credits ADD COLUMN IF NOT EXISTS phase_out_rate NUMERIC;

-- The CalEITC amount itself is NOT a flat-amount-with-phase-out shape like
-- YCTC/FYTC (which fit ca_income_credits above) -- it's FTB's own official
-- "2025 Earned Income Tax Credit Table" (FTB 3514 Booklet, hand-extracted
-- and verified from the primary-source PDF, not derived/approximated by us:
-- 658 $50-income-band rows x 4 qualifying-child columns, 2025 max credit
-- $3,756 for 3+ children matches FTB's own advertised headline number
-- exactly). A genuinely different fact shape gets its own table, same
-- "separate table per concept" precedent as ca_income_tax_brackets vs
-- ca_standard_deduction.
CREATE TABLE IF NOT EXISTS ca_eitc_table (
    id              SERIAL PRIMARY KEY,
    tax_year        INTEGER NOT NULL,
    income_floor    NUMERIC NOT NULL,
    income_ceiling  NUMERIC NOT NULL,
    credit_0        NUMERIC NOT NULL,  -- 0 qualifying children
    credit_1        NUMERIC NOT NULL,  -- 1 qualifying child
    credit_2        NUMERIC NOT NULL,  -- 2 qualifying children
    credit_3        NUMERIC NOT NULL,  -- 3 OR MORE qualifying children
    citation        TEXT,
    source_url      TEXT,
    as_of           DATE,
    UNIQUE (tax_year, income_floor)
);

-- Mirrors rule_embeddings exactly (see embed_rules.py), own vector space so
-- income-domain candidates are never compared against sales-domain distances
-- as if they mean the same thing (see the Phase 2 domain-gate design note:
-- each domain is routed independently against its own calibrated threshold,
-- not merged into one nearest-neighbor search).
CREATE TABLE IF NOT EXISTS income_rule_embeddings (
    topic_key TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    text      TEXT NOT NULL,
    embedding vector({config.EMBED_DIM})
);
"""


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    print("income domain schema created (income_tax_topics, ca_income_tax_brackets, "
          "ca_standard_deduction, ca_income_credits, ca_eitc_table, income_rule_embeddings)")
    status()


def status():
    conn = db.get_conn()
    for tbl in ("income_tax_topics", "ca_income_tax_brackets", "ca_standard_deduction",
                "ca_income_credits", "ca_eitc_table", "income_rule_embeddings"):
        n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:26} {n} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "create":
        create()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
