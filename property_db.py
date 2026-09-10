"""Connection to the PROPERTY-TAX (county assessor/BOE) database --
physically separate from db.py's sales-tax database and income_db.py's
income-tax database, as of the Ring 4 database-split decision (2026-09-07).
Mirrors income_db.py's get_conn() byte-for-byte; schema lives in
property_schema.py, exactly as income_db.py owns the income schema.

Run `python property_db.py` once to verify the connection.
"""
import psycopg
from pgvector.psycopg import register_vector

import config


def get_conn():
    """Open a connection to the property-tax database with pgvector registered."""
    url = config.require("PROPERTY_DATABASE_URL", config.PROPERTY_DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    register_vector(conn)
    return conn


if __name__ == "__main__":
    conn = get_conn()
    conn.execute("SELECT 1")
    print("property DB connection OK")
    conn.close()
