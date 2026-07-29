"""Load fine-grained product rules for one regulation into product_rules (no LLM).

Usage: python load_product_rules.py reg1602_rules.json
"""
import json
import sys

import db

path = sys.argv[1] if len(sys.argv) > 1 else "reg1602_rules.json"
rules = json.load(open(path, encoding="utf-8"))

with db.get_conn() as conn:
    for r in rules:
        conn.execute(
            "INSERT INTO product_rules "
            "(reg, product_key, product_label, taxable, rate, citation, condition, summary, source_url, tier, status, model_used) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ai_drafted','claude-code') "
            "ON CONFLICT (reg, product_key) DO UPDATE SET "
            "product_label=EXCLUDED.product_label, taxable=EXCLUDED.taxable, rate=EXCLUDED.rate, "
            "citation=EXCLUDED.citation, condition=EXCLUDED.condition, summary=EXCLUDED.summary, "
            "source_url=EXCLUDED.source_url, tier=EXCLUDED.tier",
            (r["reg"], r["product_key"], r.get("product_label"), r["taxable"],
             r.get("rate") or 0, r.get("citation"), r.get("condition"), r.get("summary"),
             r.get("source_url")
             or f"https://cdtfa.ca.gov/lawguides/vol1/sutr/{r['reg']}.html",
             r.get("tier", "consumer")),
        )

print(f"loaded {len(rules)} product rules from {path}")
