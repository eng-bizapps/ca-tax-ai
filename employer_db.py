"""Connection to the EMPLOYER-PAYROLL-TAX (EDD) database -- physically
separate from db.py's sales-tax database, income_db.py's income-tax database
and property_db.py's property-tax database, as of the Ring 5 database-split
decision (2026-10-31). Mirrors property_db.py's get_conn() byte-for-byte;
schema lives in employer_schema.py.

Run `python employer_db.py` once to verify the connection.
"""
import psycopg
from pgvector.psycopg import register_vector

import config


def get_conn():
    """Open a connection to the employer-payroll-tax database with pgvector registered."""
    url = config.require("EMPLOYER_DATABASE_URL", config.EMPLOYER_DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    register_vector(conn)
    return conn


if __name__ == "__main__":
    conn = get_conn()
    conn.execute("SELECT 1")
    print("employer DB connection OK")
    conn.close()
