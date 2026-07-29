"""Classify every CA SUT regulation by cleaned-text size: small / medium / large.

Uses the stored text when it is under the 6k crawl cap; fetches the full page
for anything that was truncated (so medium vs large is accurate).
"""
import re
import time

import requests
from bs4 import BeautifulSoup

import db
import registry

DONE = {"1602", "1603", "1591"}
HEADERS = {"User-Agent": "Mozilla/5.0 (ca-tax-pipeline)"}

# load titles
titles = {}
for line in open("titles.txt", encoding="utf-8"):
    if "\t" in line:
        reg, title = line.rstrip("\n").split("\t", 1)
        titles[reg] = title


def clean_len(reg: str) -> int:
    html = requests.get(registry.reg_url(reg), headers=HEADERS, timeout=30).text
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "form"]):
        t.decompose()
    main = soup.find("main") or soup.find(id="content") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(separator=" ")).strip()
    m = re.search(rf"Regulation\s*{reg}\b", text)
    if m:
        text = text[m.start():]
    return len(text)


def bucket(n: int) -> str:
    if n < 6000:
        return "small"
    if n <= 25000:
        return "medium"
    return "large"


conn = db.get_conn()
rows = conn.execute("SELECT reg, length(text) FROM documents WHERE reg IS NOT NULL ORDER BY reg").fetchall()

sizes = {}
for reg, ln in rows:
    if ln < 6000:
        sizes[reg] = ln
    else:
        sizes[reg] = clean_len(reg)
        time.sleep(0.2)

groups = {"large": [], "medium": [], "small": []}
for reg, n in sizes.items():
    groups[bucket(n)].append((reg, n))

for g in ("large", "medium", "small"):
    lst = sorted(groups[g])
    print(f"\n===== {g.upper()} ({len(lst)}) =====")
    for reg, n in lst:
        mark = "  [done]" if reg in DONE else ""
        print(f"  {reg}  {n:>6}  {titles.get(reg,'?')[:55]}{mark}")

print(f"\nTotals: large={len(groups['large'])}, medium={len(groups['medium'])}, small={len(groups['small'])}")
print(f"Remaining (excluding {sorted(DONE)}): {sum(len(v) for v in groups.values()) - len(DONE)}")
