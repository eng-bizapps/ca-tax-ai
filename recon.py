"""Recon v2: harvest content links from CDTFA index pages, stripping the shared
nav/footer boilerplate (any link that appears on most pages)."""
import re
import collections
import requests

H = {"User-Agent": "Mozilla/5.0 (ca-tax-pipeline)"}
PAGES = {
    "lawguides": "https://www.cdtfa.ca.gov/lawguides/",
    "sut_programs": "https://www.cdtfa.ca.gov/taxes-and-fees/sutprograms.htm",
    "stf_programs": "https://www.cdtfa.ca.gov/taxes-and-fees/stfprograms.htm",
    "industry": "https://www.cdtfa.ca.gov/industry/",
    "formspubs": "https://www.cdtfa.ca.gov/formspubs/",
    "sut_regs": "https://www.cdtfa.ca.gov/lawguides/vol1/sutr/sales-and-use-tax-regulations.html",
}


def get_paths(url):
    try:
        html = requests.get(url, headers=H, timeout=30).text
    except Exception:
        return []
    out = []
    for h in re.findall(r'href="([^"#]+)"', html):
        h = h.split("?")[0]
        if h.startswith("http") and "cdtfa.ca.gov" not in h:
            continue
        h = re.sub(r"^https?://[^/]*cdtfa\.ca\.gov", "", h)
        if h.startswith("/") and not re.search(r"\.(css|js|png|jpg|svg|ico|gif|woff)", h):
            out.append(h)
    return out


harvest = {k: get_paths(u) for k, u in PAGES.items()}

# boilerplate = paths appearing on >= 3 of the pages (shared nav/footer)
freq = collections.Counter()
for paths in harvest.values():
    for p in set(paths):
        freq[p] += 1
boiler = {p for p, c in freq.items() if c >= 3}
print(f"stripped {len(boiler)} boilerplate nav links\n")

for k, paths in harvest.items():
    content = sorted(set(p for p in paths if p not in boiler))
    print(f"===== {k}: {len(content)} content links =====")
    for p in content[:40]:
        print(f"   {p}")
    if len(content) > 40:
        print(f"   ... +{len(content)-40} more")
    print()
