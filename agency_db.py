"""Maps a corpus `agency` value to the database that owns its content --
'cdtfa' -> db.py's sales database, 'ftb' -> income_db.py's income database.
Used by the crawl/store/embed tooling (discover_corpus.py, store_manifest.py,
embed_docs.py) that is already parameterized by agency, so it writes to the
correct physical database after the Ring 2 database split.
"""
import db
import income_db


def get_conn(agency: str = "cdtfa"):
    return income_db.get_conn() if agency == "ftb" else db.get_conn()
