"""Load Claude Code-authored rule drafts into the rule_drafts table (no LLM)."""
import json

import db

drafts = json.load(open("rule_drafts_authored.json", encoding="utf-8"))

with db.get_conn() as conn:
    for d in drafts:
        reg = d["reg"]
        url = f"https://cdtfa.ca.gov/lawguides/vol1/sutr/{reg}.html"
        conn.execute(
            "INSERT INTO rule_drafts "
            "(reg, category, is_taxability_rule, taxable, rate, citation, summary, source_url, status, model_used) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ai_drafted','claude-code') "
            "ON CONFLICT (reg) DO UPDATE SET "
            "category=EXCLUDED.category, is_taxability_rule=EXCLUDED.is_taxability_rule, "
            "taxable=EXCLUDED.taxable, rate=EXCLUDED.rate, citation=EXCLUDED.citation, "
            "summary=EXCLUDED.summary, source_url=EXCLUDED.source_url, "
            "model_used='claude-code', status='ai_drafted'",
            (reg, d.get("category"), d.get("is_taxability_rule"), d.get("taxable"),
             d.get("rate") or 0, d.get("citation"), d.get("summary"), url),
        )

print(f"loaded {len(drafts)} drafts into rule_drafts")
