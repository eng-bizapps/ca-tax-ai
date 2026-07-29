"""Phase 0 - corpus discovery. Enumerate the WHOLE official source (CDTFA today,
FTB as of Ring 2) into a `corpus_manifest` so completeness is provable: every
program/topic area tracked with a status (pending -> stored -> ruled). You cannot
claim 'nothing missed' without this denominator.

  python discover_corpus.py sections [agency]     # map the corpus (index/section level) -- cdtfa only
  python discover_corpus.py expand [N] [agency]   # follow N index pages -> leaf documents -- cdtfa only
  python discover_corpus.py bfs [agency] [N]      # breadth-first content crawl (ftb; works for any agency)
  python discover_corpus.py report [agency]       # inventory + coverage summary (omit agency for all)
"""
import re
import sys
import collections
from urllib.parse import urljoin
import requests

import agency_db

H = {"User-Agent": "Mozilla/5.0 (ca-tax-pipeline)"}

# CDTFA uses a two-phase index->leaf model (numbered Law Guide volumes with
# explicit *-index/*-toc pages). FTB has no equivalent index hierarchy -- its
# content is a flatter topic tree reached via nav, so it gets a breadth-first
# crawl (bfs_discover) instead of sections()/expand().
AGENCIES = {
    "cdtfa": {
        "base": "https://www.cdtfa.ca.gov",
        "seeds": [
            "/lawguides/",
            "/taxes-and-fees/sutprograms.htm",
            "/taxes-and-fees/stfprograms.htm",
            "/industry/",
            "/lawguides/vol1/sutr/sales-and-use-tax-regulations.html",
        ],
    },
    "ftb": {
        "base": "https://www.ftb.ca.gov",
        "seeds": [
            "/file/index.html",
            "/file/personal/index.html",
            "/file/personal/deductions/index.html",
            "/file/personal/filing-status/index.html",
            "/file/business/index.html",
            "/pay/index.html",
        ],
    },
}

LANG = ("-spanish", "-chinese", "-korean", "-punjabi", "-vietnamese", "-tagalog",
        "-japanese", "-hmong", "-thai", "-arabic", "-farsi", "-russian", "-cambodian")
FTB_SKIP_PREFIXES = ("/myftb/", "/about-ftb/", "/help/", "/certification.html",
                     "/contactus/", "/media/", "/forms/")
FTB_LANG_RE = re.compile(r"-es(\.html|\.asp)?$|/index-es")

SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_manifest (
    url           TEXT PRIMARY KEY,
    program       TEXT,
    section       TEXT,          -- lawguide | industry-guide | stf-guide | publication | personal-tax | business-tax | pay
    doc_type      TEXT,          -- regulations-index | annotations-index | law-index
                                 -- | regulation | annotation | guide-page | publication
    parent_url    TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    discovered_at TIMESTAMPTZ DEFAULT now()
);

-- `url` alone was the PK, but it's stored DOMAIN-STRIPPED (see content_links()) --
-- a second agency's site (e.g. ftb.ca.gov) can easily share a relative path
-- (/index.html, /forms/, language-variant pages) with an existing CDTFA row,
-- and every insert here is ON CONFLICT DO NOTHING, so that would SILENTLY drop
-- the second agency's row. Widen the key to (agency, url) before any second
-- agency is crawled. Idempotent: safe to re-run (drops+recreates the same
-- default-named constraint each time).
ALTER TABLE corpus_manifest ADD COLUMN IF NOT EXISTS agency TEXT NOT NULL DEFAULT 'cdtfa';
ALTER TABLE corpus_manifest DROP CONSTRAINT IF EXISTS corpus_manifest_pkey;
ALTER TABLE corpus_manifest ADD PRIMARY KEY (agency, url);
"""


def fetch(path, base):
    url = path if path.startswith("http") else base + path
    try:
        return requests.get(url, headers=H, timeout=30).text
    except Exception:
        return ""


def content_links(html, page_path, base):
    """Extract same-domain content link PATHS, resolving relative hrefs against the page."""
    domain = re.sub(r"^https?://", "", base)
    page_url = page_path if page_path.startswith("http") else base + page_path
    out = []
    for h in re.findall(r'href="([^"#]+)"', html):
        h = h.split("?")[0]
        full = urljoin(page_url, h)
        if domain not in full:
            continue
        path = re.sub(rf"^https?://[^/]*{re.escape(domain)}", "", full)
        if path.startswith("/") and not re.search(
                r"\.(css|js|png|jpg|jpeg|svg|ico|gif|woff|xml|pdf|zip|doc|docx|xls|xlsx|csv|mp4|mp3)$",
                path, re.I):
            out.append(path)
    return out


def classify_cdtfa(path):
    """Return (section, program, doc_type) or None to skip."""
    p = path.lower()
    if "/lawguides/" in p:
        m = re.search(r"/lawguides/vol\d+/([^/]+)/([^/]+)\.html", p)
        if not m:
            return None
        stem = m.group(2)
        if "annotation" in stem:
            dt, prog = "annotations-index", re.sub(r"-annotations$", "", stem)
        elif "regulation" in stem:
            dt, prog = "regulations-index", re.sub(r"-regulations$", "", stem)
        elif stem.endswith("-law") or "-code" in stem:
            dt, prog = "law-index", re.sub(r"-law$", "", stem)
        elif stem.endswith("-toc") or stem.endswith("-all"):
            dt, prog = "index", re.sub(r"-(toc|all)$", "", stem)
        else:
            dt, prog = "lawguide-page", stem
        return ("lawguide", prog, dt)
    if p.startswith("/industry/"):
        m = re.search(r"/industry/([^/]+)", p)
        prog = m.group(1) if m else "misc"
        if any(l in prog for l in LANG):
            return None                     # skip non-English translations
        return ("industry-guide", prog, "guide-page")
    if p.startswith("/taxes-and-fees/") and p.count("/") >= 3:
        m = re.search(r"/taxes-and-fees/([^/]+)", p)
        prog = m.group(1) if m else "misc"
        if prog in ("sutprograms.htm", "stfprograms.htm", "rates.aspx",
                    "tax-rates-stfd.htm"):
            return None
        return ("stf-guide", prog, "guide-page")
    return None


def classify_ftb(path):
    """Return (section, program, doc_type) or None to skip.

    FTB has no numbered-regulation index hierarchy like CDTFA's Law Guide, so
    this classifies by top-level content area instead (personal/business/pay).
    /forms/, /myftb/ (account portal), /about-ftb/, /help/ are administrative
    or non-content (same "0 consumer value" pattern already proven for CDTFA's
    equivalent sections) -- skipped rather than crawled.
    """
    p = path.lower()
    if p.startswith(FTB_SKIP_PREFIXES):
        return None
    if FTB_LANG_RE.search(p):
        return None
    if "eitc-calculator" in p:
        # Interactive session-driven calculator tool (Home/Logout/Help pages),
        # not static guidance text -- one of its Help pages also returned a
        # response containing a NUL byte Postgres rejects on INSERT.
        return None
    if p.startswith("/file/personal/"):
        m = re.search(r"/file/personal/([^/]+)/", p)
        prog = m.group(1) if m else "personal-misc"
        return ("personal-tax", prog, "guide-page")
    if p.startswith("/file/business/"):
        m = re.search(r"/file/business/([^/]+)/", p)
        prog = m.group(1) if m else "business-misc"
        return ("business-tax", prog, "guide-page")
    if p.startswith("/file/"):
        return ("filing-general", "filing", "guide-page")
    if p.startswith("/pay/"):
        m = re.search(r"/pay/([^/]+)/", p)
        prog = m.group(1) if m else "pay-misc"
        return ("pay", prog, "guide-page")
    return None


def classify(path, agency="cdtfa"):
    return classify_ftb(path) if agency == "ftb" else classify_cdtfa(path)


def sections(agency="cdtfa"):
    """CDTFA-style: harvest links off a handful of hub/seed pages."""
    conn = agency_db.get_conn(agency)
    conn.execute(SCHEMA)
    base = AGENCIES[agency]["base"]
    seeds = AGENCIES[agency]["seeds"]
    harvest = {s: content_links(fetch(s, base), s, base) for s in seeds}
    freq = collections.Counter()
    for links in harvest.values():
        for p in set(links):
            freq[p] += 1
    boiler = {p for p, c in freq.items() if c >= 3}
    seen, added = set(), 0
    for s, links in harvest.items():
        for p in links:
            if p in boiler or p in seen:
                continue
            seen.add(p)
            c = classify(p, agency)
            if not c:
                continue
            section, prog, dt = c
            conn.execute(
                "INSERT INTO corpus_manifest (url, program, section, doc_type, parent_url, agency) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (agency, url) DO NOTHING",
                (p, prog, section, dt, s, agency))
            added += 1
    print(f"sections[{agency}]: stripped {len(boiler)} boilerplate links; added {added} manifest rows")
    report(conn, agency)
    conn.close()


def expand(limit=20, agency="cdtfa"):
    """CDTFA-style: follow index pages -> enumerate leaf documents (regs, annotations, sub-pages)."""
    conn = agency_db.get_conn(agency)
    base = AGENCIES[agency]["base"]
    idx = conn.execute(
        "SELECT url, program, section, doc_type FROM corpus_manifest "
        "WHERE agency=%s AND doc_type LIKE '%%index%%' AND status='pending' LIMIT %s",
        (agency, limit)).fetchall()
    total = 0
    for url, prog, section, dt in idx:
        leaf_type = {"regulations-index": "regulation",
                     "annotations-index": "annotation",
                     "law-index": "statute"}.get(dt, "lawguide-page")
        html = fetch(url, base)
        base_dir = re.sub(r"/[^/]+$", "/", url)
        added = 0
        for h in content_links(html, url, base):
            # leaf docs live in the same program folder as the index
            if not h.startswith(base_dir) or h == url:
                continue
            conn.execute(
                "INSERT INTO corpus_manifest (url, program, section, doc_type, parent_url, agency) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (agency, url) DO NOTHING",
                (h, prog, section, leaf_type, url, agency))
            added += 1
        conn.execute("UPDATE corpus_manifest SET status='mapped' WHERE agency=%s AND url=%s", (agency, url))
        total += added
        print(f"  {prog[:34]:34} {dt:20} -> {added} leaf docs")
    print(f"\nexpanded {len(idx)} index pages -> {total} leaf documents")
    report(conn, agency)
    conn.close()


def bfs_discover(agency, max_pages=150):
    """Breadth-first content crawl from an agency's seed pages.

    Unlike sections()/expand(), this doesn't assume an index/leaf hierarchy --
    it fetches every accepted (classify() != None) content page too, looking
    for further links, up to max_pages fetches total. HTTP is free (only
    embedding is quota-limited), so this is safe to run for reconnaissance.
    Pages classify() rejects (portal/admin/forms/language-variant) are neither
    stored nor traversed, same as CDTFA's approach of not delving into
    non-content areas.
    """
    conn = agency_db.get_conn(agency)
    conn.execute(SCHEMA)
    base = AGENCIES[agency]["base"]
    seeds = AGENCIES[agency]["seeds"]
    seen = set(seeds)
    queue = list(seeds)
    added = fetched = 0
    # Seed/hub pages are traversal roots, but they can ALSO carry real content
    # (e.g. /file/personal/deductions/index.html states the actual standard
    # deduction table, not just a links list) -- store them too if classify()
    # accepts them, not just the pages they link to.
    for s in seeds:
        c = classify(s, agency)
        if not c:
            continue
        section, prog, dt = c
        conn.execute(
            "INSERT INTO corpus_manifest (url, program, section, doc_type, parent_url, agency) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (agency, url) DO NOTHING",
            (s, prog, section, dt, None, agency))
        added += 1
    while queue and fetched < max_pages:
        path = queue.pop(0)
        html = fetch(path, base)
        fetched += 1
        for h in content_links(html, path, base):
            if h in seen:
                continue
            seen.add(h)
            c = classify(h, agency)
            if not c:
                continue
            section, prog, dt = c
            conn.execute(
                "INSERT INTO corpus_manifest (url, program, section, doc_type, parent_url, agency) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (agency, url) DO NOTHING",
                (h, prog, section, dt, path, agency))
            added += 1
            queue.append(h)
    print(f"bfs_discover[{agency}]: fetched {fetched} pages, added {added} manifest rows "
          f"({len(queue)} still queued, {max_pages} page cap)")
    report(conn, agency)
    conn.close()


def report(conn, agency):
    """Prints ONE domain's inventory from an already-open connection to that
    domain's own database. Post-split, each physical DB holds only one
    agency's corpus_manifest rows, so no WHERE-agency filter is needed here
    -- see report_all() for the "show me everything" combined view, which
    can no longer be one query once the databases are physically separate."""
    n = conn.execute("SELECT count(*) FROM corpus_manifest").fetchone()[0]
    print(f"\n===== CORPUS MANIFEST ({agency}): {n} documents =====")
    print("by section:")
    for sec, c in conn.execute(
            "SELECT section, count(*) FROM corpus_manifest GROUP BY section ORDER BY 2 DESC").fetchall():
        print(f"  {sec:16} {c}")
    print("by doc_type:")
    for dt, c in conn.execute(
            "SELECT doc_type, count(*) FROM corpus_manifest GROUP BY doc_type ORDER BY 2 DESC").fetchall():
        print(f"  {dt:20} {c}")
    print("by status:")
    for st, c in conn.execute(
            "SELECT status, count(*) FROM corpus_manifest GROUP BY status ORDER BY 2 DESC").fetchall():
        print(f"  {st:12} {c}")
    if agency == "cdtfa":
        nprog = conn.execute("SELECT count(distinct program) FROM corpus_manifest "
                             "WHERE section='lawguide'").fetchone()[0]
        print(f"\nlaw-guide programs ({nprog}):")
        for (prog,) in conn.execute(
                "SELECT DISTINCT program FROM corpus_manifest WHERE section='lawguide' "
                "ORDER BY program").fetchall():
            print(f"  {prog}")
    return n


def report_all():
    """Opens each domain's own connection in turn and prints both sections,
    plus a combined agency summary computed in Python -- no single query can
    span both physical databases post-split."""
    totals = {}
    for agency in ("cdtfa", "ftb"):
        conn = agency_db.get_conn(agency)
        totals[agency] = report(conn, agency)
        conn.close()
    print("\nby agency (combined):")
    for agency, n in totals.items():
        print(f"  {agency:12} {n}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "sections":
        sections(sys.argv[2] if len(sys.argv) > 2 else "cdtfa")
    elif cmd == "expand":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        expand(n, sys.argv[3] if len(sys.argv) > 3 else "cdtfa")
    elif cmd == "bfs":
        ag = sys.argv[2] if len(sys.argv) > 2 else "ftb"
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 150
        bfs_discover(ag, n)
    elif cmd == "report":
        if len(sys.argv) > 2:
            ag = sys.argv[2]
            conn = agency_db.get_conn(ag)
            report(conn, ag)
            conn.close()
        else:
            report_all()
    else:
        print(__doc__)
