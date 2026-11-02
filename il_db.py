"""Connection to the ILLINOIS DEPARTMENT OF REVENUE (income tax) database --
physically separate from every California database (one database per agency,
per state; see states.py). Mirrors employer_db.py's get_conn().

Run `python il_db.py` once to verify the connection.
"""
import psycopg
from pgvector.psycopg import register_vector

import config


def get_conn():
    """Open a connection to the Illinois revenue database with pgvector registered."""
    url = config.require("IL_REVENUE_DATABASE_URL", config.IL_REVENUE_DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    register_vector(conn)
    return conn


if __name__ == "__main__":
    conn = get_conn()
    conn.execute("SELECT 1")
    print("Illinois revenue DB connection OK")
    conn.close()
