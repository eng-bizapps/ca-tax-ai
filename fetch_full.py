"""Fetch the FULL cleaned text of a CDTFA document for deep reading.

  python fetch_full.py 1602            # a Sales & Use Tax regulation (shortcut)
  python fetch_full.py <full-cdtfa-url>  # any CDTFA law-guide / guide page
  python fetch_full.py <url> out.txt     # explicit output filename
"""
import re
import sys

import requests
from bs4 import BeautifulSoup

arg = sys.argv[1]
if arg.startswith("http"):
    url = arg
    out = sys.argv[2] if len(sys.argv) > 2 else re.sub(r"[^0-9a-zA-Z]+", "_", url.split("//")[-1]) + ".txt"
else:
    url = f"https://cdtfa.ca.gov/lawguides/vol1/sutr/{arg}.html"
    out = f"reg{arg}_full.txt"

html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).text
soup = BeautifulSoup(html, "html.parser")
for t in soup(["script", "style", "nav", "header", "footer", "form"]):
    t.decompose()
main = soup.find("main") or soup.find(id="content") or soup.body or soup
text = re.sub(r"\s+", " ", main.get_text(separator=" ")).strip()
if not arg.startswith("http"):
    m = re.search(rf"Regulation\s*{arg}\b", text)
    if m:
        text = text[m.start():]
open(out, "w", encoding="utf-8").write(text)
print(f"wrote {out} ({len(text)} chars)")
