"""Embed the rule catalog so routing can be done by local vector similarity
instead of a Gemini generation call (which is capped at 20/day and doesn't scale
to hundreds of rules in one prompt).

One row per lookup key: the fine product_rules (by product_key) plus the coarse
rules (by category, only where the key isn't already a product rule) -- matching
engine._catalog / _lookup. The embedded text is label + condition + summary so a
short user question matches the rule's vocabulary.

Quota-aware: embedding is one call per rule, rate-limited per minute; embeds in
resumable batches with backoff (already-embedded rows are skipped).

Usage:
  python embed_rules.py build      # offline: (re)load catalog text into rule_embeddings
  python embed_rules.py embed [N]  # embed up to N un-embedded rules (default 600)
  python embed_rules.py status
  python embed_rules.py reset
"""
import sys
import time

import google.generativeai as genai

import config
import db

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS rule_embeddings (
    product_key TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector({config.EMBED_DIM})
);
"""


def build():
    conn = db.get_conn()
    conn.execute(SCHEMA)
    seen = set()
    n = 0
    for pk, label, cond, summ, tier in conn.execute(
            "SELECT product_key, product_label, condition, summary, "
            "COALESCE(tier,'consumer') FROM product_rules").fetchall():
        # consumer rules power the router (kind='product'); administrative rules
        # are embedded too but as kind='admin' -- searchable by the engine's
        # informational tier, EXCLUDED from consumer routing (_route_candidates
        # filters kind <> 'admin') so they can never produce a taxability verdict.
        kind = "product" if tier == "consumer" else "admin"
        text = ". ".join(x for x in (label, cond, summ) if x)
        conn.execute(
            "INSERT INTO rule_embeddings (product_key, kind, text) VALUES (%s,%s,%s) "
            "ON CONFLICT (product_key) DO UPDATE SET text=EXCLUDED.text, kind=EXCLUDED.kind, "
            "embedding = CASE WHEN rule_embeddings.text <> EXCLUDED.text THEN NULL "
            "ELSE rule_embeddings.embedding END",
            (pk, kind, text))   # column order is (product_key, kind, text)
        seen.add(pk)
        n += 1
    for cat, summ in conn.execute("SELECT category, summary FROM rules").fetchall():
        if cat in seen:
            continue
        text = ". ".join(x for x in (cat, summ) if x)
        conn.execute(
            "INSERT INTO rule_embeddings (product_key, kind, text) VALUES (%s,'coarse',%s) "
            "ON CONFLICT (product_key) DO UPDATE SET text=EXCLUDED.text, kind='coarse', "
            "embedding = CASE WHEN rule_embeddings.text <> EXCLUDED.text THEN NULL "
            "ELSE rule_embeddings.embedding END",
            (cat, text))
        n += 1
    tot = conn.execute("SELECT count(*) FROM rule_embeddings").fetchone()[0]
    print(f"build: {n} catalog entries processed; rule_embeddings has {tot} rows")
    conn.close()


def embed(limit=600):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT product_key, text FROM rule_embeddings WHERE embedding IS NULL "
        "ORDER BY product_key LIMIT %s", (limit,)).fetchall()
    done = 0
    for pk, text in rows:
        v = None
        for _ in range(6):
            try:
                v = genai.embed_content(model=config.EMBED_MODEL, content=text,
                                        output_dimensionality=config.EMBED_DIM)["embedding"]
                break
            except Exception as e:
                msg = str(e).lower()
                if any(w in msg for w in ("429", "quota", "rate", "resource")):
                    time.sleep(25)
                    continue
                print(f"STOP after {done}: {type(e).__name__}: {str(e)[:140]}")
                v = "FATAL"
                break
        if v in (None, "FATAL"):
            if v is None:
                print(f"STOP after {done}: retries exhausted")
            break
        emb = "[" + ",".join(str(float(x)) for x in v) + "]"
        conn.execute("UPDATE rule_embeddings SET embedding=%s::vector WHERE product_key=%s", (emb, pk))
        done += 1
        time.sleep(0.4)
    print(f"embedded {done} this run")
    status(conn)
    conn.close()


def status(conn=None):
    own = conn is None
    conn = conn or db.get_conn()
    tot = conn.execute("SELECT count(*) FROM rule_embeddings").fetchone()[0]
    emb = conn.execute("SELECT count(*) FROM rule_embeddings WHERE embedding IS NOT NULL").fetchone()[0]
    print(f"rule_embeddings: {tot} rules | embedded {emb} | remaining {tot-emb}")
    if own:
        conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "build":
        build()
    elif cmd == "embed":
        embed(int(sys.argv[2]) if len(sys.argv) > 2 else 600)
    elif cmd == "status":
        status()
    elif cmd == "reset":
        c = db.get_conn(); c.execute("DROP TABLE IF EXISTS rule_embeddings"); c.close()
        print("rule_embeddings dropped")
    else:
        print(__doc__)
