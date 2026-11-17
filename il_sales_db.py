"""Connection to the ILLINOIS sales and use tax database (Retailers' Occupation Tax, Use Tax;
Illinois Department of Revenue) -- physically separate from the Illinois income-tax database
(il_db.py) and from every California database. Mirrors il_db.py's get_conn().

Run `python il_sales_db.py` once to verify the connection.
"""
import psycopg
from pgvector.psycopg import register_vector

import config


def get_conn():
    """Open a connection to the Illinois sales-tax database with pgvector registered."""
    url = config.require("IL_SALES_DATABASE_URL", config.IL_SALES_DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    register_vector(conn)
    return conn


if __name__ == "__main__":
    conn = get_conn()
    conn.execute("SELECT 1")
    print("Illinois sales-tax DB connection OK")
    conn.close()
