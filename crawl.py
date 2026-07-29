"""Step 2 - Crawl: fetch real CDTFA regulation pages, clean them, and store the
text (with source URL + date) in the documents table. No embeddings yet.

Run: python crawl.py
"""
import datetime
import re

import requests
from bs4 import BeautifulSoup

import db

def _u(reg):
    return f"https://cdtfa.ca.gov/lawguides/vol1/sutr/{reg}.html"

TOPICS = [
    {"category": "food_products",                   "reg": "1602", "url": _u("1602")},
    {"category": "prepared_food",                   "reg": "1603", "url": _u("1603")},
    {"category": "transportation",                  "reg": "1628", "url": _u("1628")},
    {"category": "medicines",                       "reg": "1591", "url": _u("1591")},
    {"category": "leases_rentals",                  "reg": "1660", "url": _u("1660")},
    {"category": "sales_for_resale",                "reg": "1668", "url": _u("1668")},
    {"category": "sales_to_us_government",          "reg": "1614", "url": _u("1614")},
    {"category": "occasional_sales",                "reg": "1595", "url": _u("1595")},
    {"category": "interstate_commerce",             "reg": "1620", "url": _u("1620")},
    {"category": "works_of_art",                    "reg": "1586", "url": _u("1586")},
    {"category": "printed_matter",                  "reg": "1541", "url": _u("1541")},
    {"category": "repair_labor",                    "reg": "1546", "url": _u("1546")},
    {"category": "electronically_delivered_software", "reg": "1502", "url": _u("1502")},
    {"category": "cellular_telephones",             "reg": "1585", "url": _u("1585")},
    {"category": "coins_and_bullion",               "reg": "1599", "url": _u("1599")},
    {"category": "pet_food",                        "reg": "1587", "url": _u("1587")},
    {"category": "newspapers_periodicals",          "reg": "1590", "url": _u("1590")},
    {"category": "nonprofit_sales",                 "reg": "1570", "url": _u("1570")},
    {"category": "vending_machine_sales",           "reg": "1574", "url": _u("1574")},
    {"category": "mobile_transportation_equipment", "reg": "1661", "url": _u("1661")},
]

HEADERS = {"User-Agent": "Mozilla/5.0 (ca-tax-demo crawler)"}
MAX_CHARS = 6000


def clean(html: str, reg: str) -> str:
    """Strip boilerplate and trim to the regulation body."""
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
    today = datetime.date.today()
    with db.get_conn() as conn:
        conn.execute("TRUNCATE documents RESTART IDENTITY;")
        for t in TOPICS:
            resp = requests.get(t["url"], headers=HEADERS, timeout=30)
            resp.raise_for_status()
            text = clean(resp.text, t["reg"])
            conn.execute(
                "INSERT INTO documents (category, text, source_url, date) VALUES (%s,%s,%s,%s)",
                (t["category"], text, t["url"], today),
            )
            print(f"crawled {t['category']:16} reg {t['reg']}  ({len(text)} chars)")
    print("Done. Real CDTFA text is now in the documents table.")


if __name__ == "__main__":
    main()
