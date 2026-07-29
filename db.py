"""Postgres + pgvector layer: connection, schema, and the two stores.

  rules     -> structured truth, looked up by exact category match
  documents -> legal text, searched by vector similarity (for citations)

Run `python db.py` once to create the tables and verify the connection.
"""
import psycopg
from pgvector.psycopg import register_vector

import config

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rules (
    id              SERIAL PRIMARY KEY,
    category        TEXT UNIQUE NOT NULL,
    taxable         BOOLEAN NOT NULL,
    rate            NUMERIC NOT NULL DEFAULT 0,
    citation        TEXT NOT NULL,
    summary         TEXT,
    source_url      TEXT,
    effective_date  DATE,
    status          TEXT NOT NULL DEFAULT 'approved'
);

CREATE TABLE IF NOT EXISTS documents (
    id          SERIAL PRIMARY KEY,
    category    TEXT,
    text        TEXT NOT NULL,
    source_url  TEXT,
    date        DATE,
    embedding   vector({config.EMBED_DIM})
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS reg TEXT;

-- The pipeline writes AI-drafted rules here (the "factory" output), keyed by
-- regulation number. They are promoted into the serving `rules` table only
-- after auto-verify + triage. This keeps unverified drafts out of serving.
CREATE TABLE IF NOT EXISTS rule_drafts (
    reg                TEXT PRIMARY KEY,
    category           TEXT,
    is_taxability_rule BOOLEAN,
    taxable            BOOLEAN,
    rate               NUMERIC DEFAULT 0,
    citation           TEXT,
    summary            TEXT,
    source_url         TEXT,
    status             TEXT NOT NULL DEFAULT 'ai_drafted',
    confidence         REAL,
    check_results      JSONB,
    model_used         TEXT,
    drafted_at         TIMESTAMPTZ DEFAULT now()
);

-- Fine-grained, condition-aware rules: one row per product/condition (a single
-- regulation produces many). This is what gives real coverage depth -- e.g.
-- Reg 1602 alone yields ~23 rules (soda taxable, candy exempt, pet food taxable).
CREATE TABLE IF NOT EXISTS product_rules (
    id            SERIAL PRIMARY KEY,
    reg           TEXT,
    product_key   TEXT,
    product_label TEXT,
    taxable       BOOLEAN NOT NULL,
    rate          NUMERIC NOT NULL DEFAULT 0,
    citation      TEXT,
    condition     TEXT,
    summary       TEXT,
    source_url    TEXT,
    status        TEXT NOT NULL DEFAULT 'ai_drafted',
    confidence    REAL,
    model_used    TEXT,
    drafted_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (reg, product_key)
);

-- Jurisdiction the rule's taxable/rate/citation facts apply to. 'CA' for every
-- row today (product_key/product_label/condition are the state-agnostic part;
-- taxable/rate/citation/summary are the state-specific facts) -- added ahead of
-- any second-state work so that expansion doesn't require reworking the PK.
ALTER TABLE product_rules ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'CA';

-- Usage log: every engine.answer() call, for mining real coverage gaps and
-- low-confidence answers instead of guessing what to test next. `source`
-- distinguishes real usage ('live', the default) from internal test-script
-- traffic (item_sweep/coverage/smoke_test), which already has known-correct
-- verdicts and isn't useful signal for "what are people actually asking."
CREATE TABLE IF NOT EXISTS query_log (
    id           SERIAL PRIMARY KEY,
    question     TEXT NOT NULL,
    status       TEXT NOT NULL,
    product_key  TEXT,
    route_dist   NUMERIC,
    branched     BOOLEAN NOT NULL DEFAULT FALSE,
    source       TEXT NOT NULL DEFAULT 'live',
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Shadow-mode column: what the DF-weighted _rerank_v2 candidate would have
-- picked, computed alongside the real (live) _rerank pick at zero extra API
-- cost (same routing candidates, just re-scored). NULL until a routed
-- question actually reaches _rerank (disambig hits / needs_review don't set
-- it). Never used to decide a live answer -- purely for measuring
-- agreement/disagreement before deciding whether to ship it.
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS rerank_v2_key TEXT;

-- Which domain answered ('sales' | 'income') -- lets query_log mining (Phase
-- 4's cross-domain regression) distinguish real usage patterns per domain and
-- catch a domain answering a question that plainly belongs to the other one.
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS domain TEXT NOT NULL DEFAULT 'sales';
"""


def get_conn():
    """Open a connection with the pgvector type registered."""
    url = config.require("DATABASE_URL", config.DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    register_vector(conn)
    return conn


def create_schema():
    with get_conn() as conn:
        conn.execute(SCHEMA)


def vector_search(conn, query_embedding, k: int = 1, reg: str = None, agency: str = "cdtfa"):
    """Return [(reg/category, text, source_url), ...] nearest to the query embedding.

    Prefers the chunked deep-read corpus (doc_chunks); falls back to the older
    whole-document embeddings when no chunks are embedded yet. When `reg` is given,
    the passage is drawn from that regulation (so it matches the answer's citation);
    if that reg has no embedded chunk, it falls back to an unconstrained search.

    `agency` scopes doc_chunks to one source (default 'cdtfa', the sales-tax
    engine's only domain) so a future second agency's stored content (e.g. FTB)
    can never leak into a sales-tax citation lookup -- doc_chunks.reg is not
    itself agency-qualified, so this filter is required, not cosmetic.
    """
    emb = "[" + ",".join(str(float(x)) for x in query_embedding) + "]"
    try:
        if reg:
            rows = conn.execute(
                "SELECT reg, text, source_url FROM doc_chunks "
                "WHERE embedding IS NOT NULL AND reg = %s AND agency = %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s", (reg, agency, emb, k)).fetchall()
            if rows:
                return rows
        rows = conn.execute(
            "SELECT reg, text, source_url FROM doc_chunks "
            "WHERE embedding IS NOT NULL AND agency = %s "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (agency, emb, k),
        ).fetchall()
        if rows:
            return rows
    except Exception:
        pass  # doc_chunks not present yet -> fall back
    return conn.execute(
        "SELECT category, text, source_url FROM documents "
        "WHERE embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s",
        (emb, k),
    ).fetchall()


if __name__ == "__main__":
    create_schema()
    with get_conn() as conn:
        n_rules = conn.execute("SELECT count(*) FROM rules;").fetchone()[0]
        n_docs = conn.execute("SELECT count(*) FROM documents;").fetchone()[0]
    print(f"DB OK. rules={n_rules}  documents={n_docs}")
