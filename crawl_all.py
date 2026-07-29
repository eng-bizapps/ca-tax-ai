"""Pipeline stage 2 - batch crawl every regulation in the registry.

Resumable: skips regs already in the documents table, so re-running is cheap.
Uses no LLM calls (HTTP only) - safe to run anytime.

Run: python crawl_all.py
"""
import datetime
import re
import time

import requests
from bs4 import BeautifulSoup

import db
import registry

HEADERS = {"User-Agent": "Mozilla/5.0 (ca-tax-pipeline)"}
MAX_CHARS = 6000


def clean(html: str, reg: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "form"]):
        tag.decompose()
    main = soup.find("main") or soup.find(id="content") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(separator=" ")).strip()
    m = re.search(rf"Regulation\s*{reg}\b", text)
    if m:
        text = text[m.start():]
    return text[:MAX_CHARS]


def main():
    regs = registry.discover()
    today = datetime.date.today()
    with db.get_conn() as conn:
        # backfill reg on any rows that predate this column (the 20-rule MVP docs)
        conn.execute(r"UPDATE documents SET reg = substring(source_url from '/([0-9]+)\.html') "
                     r"WHERE reg IS NULL AND source_url ~ '/[0-9]+\.html'")
        existing = {r[0] for r in conn.execute(
            "SELECT reg FROM documents WHERE reg IS NOT NULL").fetchall()}

        crawled = skipped = failed = 0
        for reg in regs:
            if reg in existing:
                skipped += 1
                continue
            url = registry.reg_url(reg)
            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                resp.raise_for_status()
            except Exception as e:
                print(f"  failed {reg}: {e}")
                failed += 1
                continue
            text = clean(resp.text, reg)
            conn.execute(
                "INSERT INTO documents (reg, text, source_url, date) VALUES (%s,%s,%s,%s)",
                (reg, text, url, today),
            )
            crawled += 1
            print(f"crawled {reg} ({len(text)} chars)")
            time.sleep(0.3)

    print(f"\nDone. crawled {crawled} new, skipped {skipped} existing, {failed} failed.")


if __name__ == "__main__":
    main()
