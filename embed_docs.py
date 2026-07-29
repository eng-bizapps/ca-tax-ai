"""Chunk the deep-read regulation texts and embed them for citation retrieval.

Why: the old `documents` table capped text at 6000 chars and only 20 rows were
embedded, so the responder could not retrieve citations for most regs. This builds
a `doc_chunks` table from the full reg*_full.txt deep-read texts (chunked so each
piece fits the embedding token limit), then embeds chunks in resumable batches.

Quota-aware: embedding is one Gemini call per chunk; already-embedded chunks are
skipped, so you can embed a batch per run and resume later (or all at once with
billing on).

Usage:
  python embed_docs.py build            # offline: chunk all reg*_full.txt into doc_chunks (cdtfa only)
  python embed_docs.py embed [N] [agency]  # embed up to N un-embedded chunks (default 150)
  python embed_docs.py status [agency]     # chunks total / embedded / remaining
  python embed_docs.py reset [agency]      # drop the doc_chunks table
"""
import glob
import os
import sys
import time

import google.generativeai as genai

import agency_db
import config
import db

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))

CHUNK_SIZE = 5500      # chars (~1400 tokens, safely under the 2048-token embed limit)
OVERLAP = 400
URL = "https://cdtfa.ca.gov/lawguides/vol1/sutr/{}.html"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS doc_chunks (
    id         SERIAL PRIMARY KEY,
    reg        TEXT NOT NULL,
    chunk_idx  INTEGER NOT NULL,
    text       TEXT NOT NULL,
    source_url TEXT,
    embedding  vector({config.EMBED_DIM}),
    UNIQUE (reg, chunk_idx)
);

-- `reg` doubles as a plain CDTFA reg number (ruled tier) AND a sanitized-URL
-- doc key (stored tier, see store_manifest.py doc_key()) -- neither is agency-
-- qualified, so a second agency's site could collide with an existing key and
-- silently fail to store (ON CONFLICT DO NOTHING), or worse, a citation
-- lookup by reg could return the wrong agency's text. Widen uniqueness to
-- (agency, reg, chunk_idx) before any second agency's content is stored.
-- Idempotent: safe to re-run.
ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS agency TEXT NOT NULL DEFAULT 'cdtfa';
ALTER TABLE doc_chunks DROP CONSTRAINT IF EXISTS doc_chunks_reg_chunk_idx_key;
ALTER TABLE doc_chunks DROP CONSTRAINT IF EXISTS doc_chunks_agency_reg_chunk_idx_key;
ALTER TABLE doc_chunks ADD CONSTRAINT doc_chunks_agency_reg_chunk_idx_key UNIQUE (agency, reg, chunk_idx);
"""


def chunk_text(text, size=CHUNK_SIZE, overlap=OVERLAP):
    text = " ".join(text.split())          # normalize whitespace
    out, i, n = [], 0, len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:                         # break on a space, not mid-word
            sp = text.rfind(" ", i + size - overlap, end)
            if sp > i:
                end = sp
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return out


def build():
    conn = db.get_conn()
    conn.execute(SCHEMA)
    files = sorted(glob.glob("reg*_full.txt"))
    total = 0
    for f in files:
        reg = os.path.basename(f)[3:7]
        text = open(f, encoding="utf-8").read()
        chunks = chunk_text(text)
        for idx, piece in enumerate(chunks):
            conn.execute(
                "INSERT INTO doc_chunks (reg, chunk_idx, text, source_url) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (agency, reg, chunk_idx) DO NOTHING",
                (reg, idx, piece, URL.format(reg)),
            )
        total += len(chunks)
    n = conn.execute("SELECT count(*) FROM doc_chunks").fetchone()[0]
    print(f"build: {len(files)} docs -> {total} chunks generated; doc_chunks now has {n} rows")
    conn.close()


def embed(limit=150, agency="cdtfa"):
    conn = agency_db.get_conn(agency)
    rows = conn.execute(
        "SELECT id, text FROM doc_chunks WHERE embedding IS NULL ORDER BY id LIMIT %s",
        (limit,),
    ).fetchall()
    done = 0
    for cid, text in rows:
        v = None
        for attempt in range(6):                 # ride out per-minute rate limits
            try:
                v = genai.embed_content(
                    model=config.EMBED_MODEL, content=text,
                    output_dimensionality=config.EMBED_DIM,
                )["embedding"]
                break
            except Exception as e:
                msg = str(e).lower()
                if any(w in msg for w in ("429", "quota", "rate", "resource")):
                    time.sleep(25)               # rate-limit window; wait and retry
                    continue
                print(f"STOP after {done}: {type(e).__name__}: {str(e)[:160]}")
                v = "FATAL"
                break
        if v == "FATAL":
            break
        if v is None:                            # exhausted retries
            print(f"STOP after {done}: retries exhausted (rate limit persists)")
            break
        emb = "[" + ",".join(str(float(x)) for x in v) + "]"
        conn.execute("UPDATE doc_chunks SET embedding=%s::vector WHERE id=%s", (emb, cid))
        done += 1
        time.sleep(0.4)                          # gentle pacing under the RPM cap
    print(f"embedded {done} this run")
    status(conn)
    conn.close()


def status(conn=None, agency="cdtfa"):
    own = conn is None
    conn = conn or agency_db.get_conn(agency)
    tot = conn.execute("SELECT count(*) FROM doc_chunks").fetchone()[0]
    emb = conn.execute("SELECT count(*) FROM doc_chunks WHERE embedding IS NOT NULL").fetchone()[0]
    regs = conn.execute("SELECT count(distinct reg) FROM doc_chunks").fetchone()[0]
    print(f"doc_chunks: {tot} chunks across {regs} regs | embedded {emb} | remaining {tot-emb}")
    if own:
        conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "build":
        build()
    elif cmd == "embed":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 150
        ag = sys.argv[3] if len(sys.argv) > 3 else "cdtfa"
        embed(n, ag)
    elif cmd == "status":
        status(agency=sys.argv[2] if len(sys.argv) > 2 else "cdtfa")
    elif cmd == "reset":
        ag = sys.argv[2] if len(sys.argv) > 2 else "cdtfa"
        c = agency_db.get_conn(ag); c.execute("DROP TABLE IF EXISTS doc_chunks"); c.close()
        print(f"doc_chunks dropped ({ag})")
    else:
        print(__doc__)
