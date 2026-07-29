"""Pipeline stage 1 - source registry.

Discovers every California Sales & Use Tax regulation from the CDTFA Law Guide
index, so the pipeline processes all of them instead of a hand-picked few.

Run: python registry.py
"""
import re

import requests

INDEX_URL = "https://cdtfa.ca.gov/lawguides/vol1/sutr/sales-and-use-tax-regulations.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (ca-tax-pipeline)"}


def reg_url(reg: str) -> str:
    return f"https://cdtfa.ca.gov/lawguides/vol1/sutr/{reg}.html"


def discover() -> list:
    """Return a sorted list of SUT regulation numbers (strings) from the index."""
    resp = requests.get(INDEX_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    regs = set()
    for href in re.findall(r'href="[^"]*"', resp.text):
        m = re.search(r"(1[0-9]{3})", href)
        if m:
            n = int(m.group(1))
            if 1500 <= n <= 1799:  # the SUT regulation range
                regs.add(m.group(1))
    return sorted(regs)


if __name__ == "__main__":
    regs = discover()
    print(f"discovered {len(regs)} CA sales/use tax regulations "
          f"(range {regs[0]}..{regs[-1]})")
    print(", ".join(regs))
