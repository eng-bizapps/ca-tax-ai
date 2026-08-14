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

-- Traceability from an answerable topic back to the specific Schedule CA
-- (540) line it implements -- additive, nullable (most non-conformity
-- topics, e.g. HOH determination, aren't Schedule CA line items at all).
ALTER TABLE income_tax_topics ADD COLUMN IF NOT EXISTS schedule_ca_line TEXT;

-- COMPLETENESS LEDGER for Schedule CA (540) -- mirrors corpus_manifest's
-- pending->stored->ruled precedent: a durable record of EVERY CA/federal
-- conformity line item found via primary-source research (FTB's 2025
-- Schedule CA (540) instructions, ~90 items), independent of whether it's
-- been built yet. Exists specifically because the original Phase 3 plan
-- flagged "full conformity coverage deserves its own completeness ledger,
-- not a bullet point" and then never built one -- this closes that gap so
-- future sessions can query "what's left" instead of re-researching the
-- whole form from scratch, which is exactly what had to happen this pass.
-- Populated by schedule_ca_inventory.py (hand-researched reference data,
-- not crawled -- same "verified truth lives in code, not scraped" pattern
-- as local_rates.py/fees.py).
CREATE TABLE IF NOT EXISTS schedule_ca_inventory (
    id              SERIAL PRIMARY KEY,
    tax_year        INTEGER NOT NULL,
    part            TEXT NOT NULL,        -- 'I' or 'II'
    section         TEXT,                 -- 'A' | 'B' | 'C' | NULL (Part II has no lettered sections)
    line_ref        TEXT NOT NULL,        -- e.g. '5a/5b', '8z-kincade', '19-22'
    item_label      TEXT NOT NULL,
    adjustment_type TEXT,                 -- 'addition' | 'subtraction' | 'both' | 'schedule_d' | 'worksheet'
    frequency       TEXT,                 -- 'common' | 'moderate' | 'narrow' | 'one_time'
    citation        TEXT,
    -- 'not_started' | 'pending' (targeted this pass) | 'built' |
    -- 'deferred_itemized_engine' (Tier 2 -- extends _income_itemized_answer,
    -- not a new topic) | 'deferred_new_engine' (Tier 3 -- needs history/
    -- basis-tracking no single Q&A fact can supply, same class as business
    -- entities) | 'not_applicable' (Tier 4 -- narrow/one-time, covered by a
    -- generic disclaimer instead of a dedicated rule)
    status          TEXT NOT NULL DEFAULT 'not_started',
    topic_key       TEXT,                 -- set once a income_tax_topics row exists for this item
    notes           TEXT,
    UNIQUE (tax_year, line_ref, item_label)
);

-- Ring 3, business entities Phase A -- ENTITY-LEVEL California annual/
-- minimum tax by entity type. Hand-verified reference data (NOT
-- LLM-drafted), same spirit as ca_income_tax_brackets/local_rates.
-- annual_tax is the flat $800 minimum franchise tax (0 for general
-- partnerships, which owe no CA annual tax at all -- confirmed via FTB:
-- "General partnerships do not pay annual tax; however, limited
-- partnerships are subject to the annual tax of $800"). income_tax_rate
-- is NULL for pure pass-through entities (partnerships/LLCs owe no
-- entity-level INCOME tax, only the flat annual tax/fee) and set only for
-- s_corp/s_corp_financial (1.5%/3.5% of net CA income). first_year_waiver
-- distinguishes S-corps (a PERMANENT waiver of the $800 floor for entities
-- formed/qualified on or after 2020-01-01 -- the income tax itself still
-- applies in year one) from LLCs/LPs/LLPs (the AB 85 first-year waiver
-- EXPIRED for tax years beginning on/after 2024-01-01 -- a 2025-formed
-- LLC/LP/LLP owes the full $800 in year one, confirmed against FTB's own
-- page language, not assumed from AB 85's original passage).
CREATE TABLE IF NOT EXISTS entity_annual_tax_rules (
    id                SERIAL PRIMARY KEY,
    tax_year          INTEGER NOT NULL,
    entity_type       TEXT NOT NULL,   -- 'general_partnership'|'lp'|'llp'|'llc'|'s_corp'|'s_corp_financial'
    annual_tax        NUMERIC NOT NULL,
    income_tax_rate   NUMERIC,         -- NULL = no entity-level income tax (pure pass-through)
    first_year_waiver BOOLEAN NOT NULL DEFAULT FALSE,
    form_number       TEXT,
    citation          TEXT,
    source_url        TEXT,
    as_of             DATE,
    UNIQUE (tax_year, entity_type)
);

-- The LLC FEE (separate from and additional to the $800 annual tax above)
-- is its own tiered-by-CA-income schedule -- a genuinely different fact
-- shape from the flat per-entity-type rule above, same "separate table
-- per concept" precedent as ca_income_tax_brackets vs ca_standard_deduction.
-- Applies to ALL LLCs including single-member/disregarded ones (confirmed:
-- "We require an SMLLC to file Form 568... subject to the annual tax, LLC
-- fee and credit limitations" -- California does not follow federal
-- disregarded-entity treatment for this purpose).
CREATE TABLE IF NOT EXISTS llc_fee_brackets (
    id              SERIAL PRIMARY KEY,
    tax_year        INTEGER NOT NULL,
    income_floor    NUMERIC NOT NULL,
    income_ceiling  NUMERIC,           -- NULL = no upper bound
    fee_amount      NUMERIC NOT NULL,
    citation        TEXT,
    source_url      TEXT,
    as_of           DATE,
    UNIQUE (tax_year, income_floor)
);

-- Ring 3, trust/estate Phase A -- FIDUCIARY-level tax on RETAINED
-- (undistributed) trust/estate income (Form 541). Deliberately NOT a new
-- bracket table -- confirmed via FTB research that the 541 Tax Rate
-- Schedule is numerically IDENTICAL to individual Schedule X (Single/
-- MFS), so fiduciary tax reuses the EXISTING ca_income_tax_brackets table
-- (filing_status='single') directly, no new bracket data needed. Only the
-- EXEMPTION CREDIT (Form 541 Line 22, subtracted from tax AFTER the
-- bracket computation -- a CREDIT, not a standard-deduction-style
-- reduction of taxable income beforehand) is genuinely new reference
-- data: "An estate is allowed an exemption credit of $10. A trust is
-- allowed an exemption credit of $1. A qualified disability trust is
-- allowed an exemption credit of $144."
CREATE TABLE IF NOT EXISTS fiduciary_exemption_credit (
    id           SERIAL PRIMARY KEY,
    tax_year     INTEGER NOT NULL,
    entity_type  TEXT NOT NULL,   -- 'trust' | 'estate' | 'qualified_disability_trust'
    amount       NUMERIC NOT NULL,
    citation     TEXT,
    source_url   TEXT,
    as_of        DATE,
    UNIQUE (tax_year, entity_type)
);
"""


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    print("income domain schema created (income_tax_topics, ca_income_tax_brackets, "
          "ca_standard_deduction, ca_income_credits, ca_eitc_table, income_rule_embeddings, "
          "entity_annual_tax_rules, llc_fee_brackets, fiduciary_exemption_credit)")
    status()


def status():
    conn = db.get_conn()
    for tbl in ("income_tax_topics", "ca_income_tax_brackets", "ca_standard_deduction",
                "ca_income_credits", "ca_eitc_table", "income_rule_embeddings",
                "schedule_ca_inventory", "entity_annual_tax_rules", "llc_fee_brackets",
                "fiduciary_exemption_credit"):
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
