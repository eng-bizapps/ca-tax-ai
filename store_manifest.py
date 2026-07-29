"""Phase 4b - bulk STORE tier: fetch every remaining pending corpus_manifest doc
(annotations + industry/STF guide pages + misc lawguide pages), chunk, and embed
into doc_chunks for recall. No LLM reasoning needed here -- Phases 1-4 already
proved (260 regs, 0 consumer rules) that everything outside the SUT core is
administrative, so the goal is pure "nothing missed for recall", not new rules.

Resumable + automatable (HTTP fetch is free; only embedding is quota-limited).

Usage:
  python store_manifest.py crawl [N] [agency] [sections]  # fetch+chunk up to N pending manifest docs -> doc_chunks, mark 'stored'
                                                            # sections = comma-separated corpus_manifest.section values, optional
  python store_manifest.py embed [N] [agency]              # embed up to N un-embedded doc_chunks (default 150)
  python store_manifest.py status [agency]
"""
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

import agency_db
import config

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))

H = {"User-Agent": "Mozilla/5.0 (ca-tax-pipeline)"}
BASE = {"cdtfa": "https://www.cdtfa.ca.gov", "ftb": "https://www.ftb.ca.gov"}
CHUNK_SIZE = 5500
OVERLAP = 400

# Aggregate/TOC pages carry no new content beyond their per-item children.
SKIP_PAT = re.compile(r"(-all\.html$|-index\.html$|-toc\.html$|index-all\.html$)")


def doc_key(url):
    """Stable, globally-unique chunk key derived from the manifest URL."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", url.strip("/")).lower()[:120]


def fetch_clean(url):
    resp = requests.get(url, headers=H, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "form"]):
        t.decompose()
    main = soup.find("main") or soup.find(id="content") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(separator=" ")).strip()
    return text.replace("\x00", "")  # Postgres text columns reject NUL bytes


def chunk_text(text, size=CHUNK_SIZE, overlap=OVERLAP):
    text = " ".join(text.split())
    out, i, n = [], 0, len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
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


def crawl(limit=300, agency="cdtfa", sections=None):
    """`sections`, if given, restricts to those corpus_manifest.section values
    (e.g. ['personal-tax','business-tax']) -- lets a first pass target the
    highest consumer-value content before spending quota on procedural
    sections (pay/collections, filing-general)."""
    conn = agency_db.get_conn(agency)
    base = BASE[agency]
    where = "agency=%s AND status='pending'"
    params = [agency]
    if sections:
        where += " AND section = ANY(%s)"
        params.append(list(sections))
    params.append(limit)
    rows = conn.execute(
        f"SELECT url, program, doc_type FROM corpus_manifest WHERE {where} "
        f"ORDER BY program, url LIMIT %s", params).fetchall()
    stored = skipped = failed = 0
    for path, program, doc_type in rows:
        if SKIP_PAT.search(path):
            conn.execute("UPDATE corpus_manifest SET status='mapped' WHERE agency=%s AND url=%s",
                         (agency, path))
            skipped += 1
            continue
        url = path if path.startswith("http") else base + path
        try:
            text = fetch_clean(url)
        except Exception as e:
            # A dead link is a permanent fact about the URL, not a transient
            # failure -- mark 'mapped' (no content) so re-running crawl()
            # doesn't refetch it every time.
            conn.execute("UPDATE corpus_manifest SET status='mapped' WHERE agency=%s AND url=%s",
                         (agency, path))
            print(f"  FAILED {path}: {e}")
            failed += 1
            continue
        if len(text) < 80:            # empty/placeholder page -> no content to store
            conn.execute("UPDATE corpus_manifest SET status='mapped' WHERE agency=%s AND url=%s",
                         (agency, path))
            skipped += 1
            continue
        key = doc_key(path)
        chunks = chunk_text(text)
        for idx, piece in enumerate(chunks):
            conn.execute(
                "INSERT INTO doc_chunks (reg, chunk_idx, text, source_url, agency) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (agency, reg, chunk_idx) DO NOTHING",
                (key, idx, piece, url, agency))
        conn.execute("UPDATE corpus_manifest SET status='stored' WHERE agency=%s AND url=%s",
                     (agency, path))
        stored += 1
        print(f"  stored {program[:30]:30} {path[:60]:60} -> {len(chunks)} chunks")
        time.sleep(0.2)
    print(f"\ncrawl[{agency}]: {stored} stored, {skipped} skipped (aggregate/empty), {failed} failed "
          f"(of {len(rows)} processed)")
    conn.close()


def embed(limit=150, agency="cdtfa"):
    conn = agency_db.get_conn(agency)
    rows = conn.execute(
        "SELECT id, text FROM doc_chunks WHERE embedding IS NULL ORDER BY id LIMIT %s",
        (limit,)).fetchall()
    done = 0
    for cid, text in rows:
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
        conn.execute("UPDATE doc_chunks SET embedding=%s::vector WHERE id=%s", (emb, cid))
        done += 1
        time.sleep(0.4)
    print(f"embedded {done} this run")
    status(conn)
    conn.close()


def status(conn=None, agency="cdtfa"):
    own = conn is None
    conn = conn or agency_db.get_conn(agency)
    tot = conn.execute("SELECT count(*) FROM doc_chunks").fetchone()[0]
    emb = conn.execute("SELECT count(*) FROM doc_chunks WHERE embedding IS NOT NULL").fetchone()[0]
    pend = conn.execute("SELECT count(*) FROM corpus_manifest WHERE status='pending'").fetchone()[0]
    print(f"doc_chunks: {tot} total | embedded {emb} | remaining {tot-emb} || manifest pending: {pend}")
    if own:
        conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "crawl":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 300
        ag = sys.argv[3] if len(sys.argv) > 3 else "cdtfa"
        secs = sys.argv[4].split(",") if len(sys.argv) > 4 else None
        crawl(n, ag, secs)
    elif cmd == "embed":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 150
        ag = sys.argv[3] if len(sys.argv) > 3 else "cdtfa"
        embed(n, ag)
    elif cmd == "status":
        status(agency=sys.argv[2] if len(sys.argv) > 2 else "cdtfa")
    else:
        print(__doc__)
