"""One-time migration: move every income-domain (FTB) row out of the shared
sales database and into the new, physically separate income database
(Ring 2 database-split decision, 2026-07-20). See the approved plan
(shiny-wandering-river.md) for the full design rationale.

Two separate, explicit steps -- never run together:
  `copy`    -- purely additive. Creates the income DB's schema, copies every
              income-domain row there, asserts row counts match exactly.
              Idempotent: safe to re-run after a partial failure (TRUNCATEs
              the income DB's own tables first; never touches the sales DB).
  `cleanup` -- destructive. Deletes the now-migrated rows from the OLD
              sales DB. Only run this after `copy` is verified AND the full
              regression suite (item_sweep.py + income_item_sweep.py) passes
              against the new split. Re-verifies counts immediately before
              deleting anything, as a guard against drift.

Usage:
  python migrate_to_income_db.py copy
  python migrate_to_income_db.py cleanup
"""
import sys

import db
import discover_corpus
import embed_docs
import income_db
import income_schema

# (table, non-id columns, extra WHERE clause on the SOURCE query, has_embedding)
INCOME_ONLY_TABLES = [
    ("income_tax_topics",
     ["topic_key", "topic_label", "taxable", "treatment", "filing_status",
      "tax_year", "citation", "condition", "summary", "source_url", "tier",
      "status", "confidence", "model_used", "drafted_at"], None, False),
    ("ca_income_tax_brackets",
     ["tax_year", "filing_status", "bracket_type", "bracket_floor",
      "bracket_ceiling", "rate", "citation", "source_url", "as_of",
      "base_amount"], None, False),
    ("ca_standard_deduction",
     ["tax_year", "filing_status", "amount", "citation", "source_url", "as_of"],
     None, False),
    ("ca_income_credits",
     ["credit_key", "credit_label", "tax_year", "filing_status", "max_amount",
      "phase_out_start", "phase_out_end", "refundable", "citation",
      "source_url", "as_of"], None, False),
    ("income_rule_embeddings", ["topic_key", "kind", "text"], None, True),
]

# Shared/mixed-agency tables -- only the agency='ftb' rows migrate; the
# cdtfa rows must stay in the sales DB untouched.
SHARED_TABLES = [
    ("doc_chunks", ["reg", "chunk_idx", "text", "source_url", "agency"],
     "agency='ftb'", True),
    ("corpus_manifest",
     ["url", "program", "section", "doc_type", "parent_url", "status",
      "discovered_at", "agency"], "agency='ftb'", False),
]

ALL_TABLES = INCOME_ONLY_TABLES + SHARED_TABLES


def _vec_str(v):
    """Same serialization convention used everywhere else in this codebase
    (embed_rules.py, store_manifest.py, load_income_content.py) -- explicit
    rather than relying on pgvector's automatic numpy-array adaptation."""
    if v is None:
        return None
    return "[" + ",".join(str(float(x)) for x in v) + "]"


def create_income_schema():
    with income_db.get_conn() as iconn:
        iconn.execute(income_schema.SCHEMA)
        iconn.execute(embed_docs.SCHEMA)
        iconn.execute(discover_corpus.SCHEMA)
    print("income DB schema created (income_schema + doc_chunks + corpus_manifest)")


def copy():
    create_income_schema()

    with income_db.get_conn() as iconn:
        table_names = ", ".join(t for t, *_ in ALL_TABLES)
        iconn.execute(f"TRUNCATE {table_names} RESTART IDENTITY")
        print(f"truncated income DB tables: {table_names}")

    source_counts, dest_counts = {}, {}
    with db.get_conn() as sconn, income_db.get_conn() as iconn:
        for table, cols, where, has_embedding in ALL_TABLES:
            select_cols = list(cols) + (["embedding"] if has_embedding else [])
            sql = f"SELECT {', '.join(select_cols)} FROM {table}"
            if where:
                sql += f" WHERE {where}"
            rows = sconn.execute(sql).fetchall()
            source_counts[table] = len(rows)

            insert_cols = list(cols) + (["embedding"] if has_embedding else [])
            placeholders = ["%s"] * len(cols) + (["%s::vector"] if has_embedding else [])
            insert_sql = (f"INSERT INTO {table} ({', '.join(insert_cols)}) "
                          f"VALUES ({', '.join(placeholders)})")

            written = 0
            for row in rows:
                values = list(row)
                if has_embedding:
                    values[-1] = _vec_str(values[-1])   # embedding is the last selected column
                iconn.execute(insert_sql, values)
                written += 1
            dest_counts[table] = written
            print(f"  {table:26} {written} rows copied"
                  + (f" (WHERE {where})" if where else ""))

    print()
    ok = True
    for table, *_ in ALL_TABLES:
        s, d = source_counts[table], dest_counts[table]
        status = "OK" if s == d else "MISMATCH"
        if s != d:
            ok = False
        print(f"  {table:26} source={s:4} dest={d:4}  {status}")
    if not ok:
        raise SystemExit("copy FAILED: row counts do not match, see MISMATCH above")
    print("\ncopy verified: all row counts match")


def cleanup():
    # Re-verify current sales-DB counts match what copy() migrated, in case
    # something wrote to the still-shared tables since copy() ran.
    with db.get_conn() as sconn, income_db.get_conn() as iconn:
        for table, cols, where, _emb in ALL_TABLES:
            sql = f"SELECT count(*) FROM {table}"
            if where:
                sql += f" WHERE {where}"
            s = sconn.execute(sql).fetchone()[0]
            d = iconn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if s != d:
                raise SystemExit(
                    f"cleanup ABORTED: {table} counts have drifted "
                    f"(sales DB now has {s}, income DB has {d} from the last "
                    f"copy) -- re-run `copy` before `cleanup`")
        print("pre-delete verification: counts still match copy() results")

        for table, _cols, _where, _emb in INCOME_ONLY_TABLES:
            sconn.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"  dropped {table} (sales DB)")
        for table, _cols, where, _emb in SHARED_TABLES:
            n = sconn.execute(f"DELETE FROM {table} WHERE {where}").rowcount
            print(f"  deleted {n} rows from {table} WHERE {where} (sales DB)")
    print("\ncleanup complete")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd == "copy":
        copy()
    elif cmd == "cleanup":
        cleanup()
    else:
        print(__doc__)
