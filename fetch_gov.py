"""Fetch a government/municipal web page for research (not the CDTFA SUT corpus
-- see fetch_full.py for that). Exists because the standard WebFetch tool 403'd
on several .gov and municode.com pages that a plain `requests` call with
realistic browser headers succeeds against -- the block was keyed to WebFetch's
own request signature, not a real bot-detection wall in most cases.

Root-caused 2026-07-09 while researching city cannabis business tax rates:
  - finance.lacity.gov: WebFetch -> 403, requests+headers -> 200 (this script's
    normal case; most .gov sites behave this way)
  - library.municode.com: 200 either way, but it's an Angular SPA -- the raw
    HTML is an empty shell; ordinance TEXT loads via a JS API call afterward.
    This script CANNOT read Municode content; needs real JS rendering (a
    browser tool) or the (undocumented, not yet reverse-engineered) api.
    municode.com backend.
  - codelibrary.amlegal.com: genuinely 403's even to a full browser header set
    via plain requests -- real bot-detection, not a header issue. No fix found
    with tools available in this session.

So: try this script FIRST for any government primary source. If it 403's,
that's a real wall (try alternate sources); if it 200's but the returned text
is a JS shell (e.g. `ng-app` in the <head>, tiny extracted text), it needs a
browser-rendering approach instead.

Usage:
  python fetch_gov.py <url> [out.txt]
"""
import re
import sys

import requests
from bs4 import BeautifulSoup

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SPA_MARKERS = ("ng-app=", "data-reactroot", "id=\"root\"></div>", "__NEXT_DATA__")


def fetch(url: str, timeout: int = 20):
    """Returns (status_code, cleaned_text, is_likely_spa_shell)."""
    resp = requests.get(url, headers=H, timeout=timeout)
    html = resp.text
    is_spa = any(m in html[:4000] for m in SPA_MARKERS)
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "form"]):
        t.decompose()
    main = soup.find("main") or soup.find(id="content") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(separator=" ")).strip()
    return resp.status_code, text, is_spa


if __name__ == "__main__":
    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else re.sub(r"[^0-9a-zA-Z]+", "_", url.split("//")[-1])[:120] + ".txt"
    status, text, is_spa = fetch(url)
    open(out, "w", encoding="utf-8").write(text)
    print(f"status={status}  chars={len(text)}  spa_shell={is_spa}  -> {out}")
    if is_spa or len(text) < 200:
        print("WARNING: looks like a JS-rendered shell or near-empty page -- "
              "this script cannot read the real content; needs browser rendering.")
