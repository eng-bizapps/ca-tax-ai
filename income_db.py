"""Connection to the INCOME-TAX (FTB) database -- physically separate from
db.py's sales-tax database as of the Ring 2 database-split decision
(2026-07-20). Mirrors db.py's get_conn() byte-for-byte; schema lives in
income_schema.py (+ doc_chunks/corpus_manifest schema reused from
embed_docs.py/discover_corpus.py for the ftb-agency content), exactly as
db.py owns the sales schema.

Run `python income_db.py` once to verify the connection.
"""
import psycopg
from pgvector.psycopg import register_vector

import config


def get_conn():
    """Open a connection to the income-tax database with pgvector registered."""
    url = config.require("INCOME_DATABASE_URL", config.INCOME_DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    register_vector(conn)
    return conn


if __name__ == "__main__":
    conn = get_conn()
    conn.execute("SELECT 1")
    print("income DB connection OK")
    conn.close()
