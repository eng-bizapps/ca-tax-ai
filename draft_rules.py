"""Step 3 - AI drafts the rules (the neuro-symbolic core).

For each crawled document, Gemini reads the real regulation text and proposes a
structured rule. Output goes to rules_draft.json for you to review (Step 4).

Run: python draft_rules.py
"""
import json
import re

import google.generativeai as genai

import config
import db

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))
model = genai.GenerativeModel(config.GEMINI_MODEL)

PROMPT = """You are a California sales-tax analyst. Read the excerpt from a CDTFA
regulation and describe the GENERAL California sales-tax treatment of this
category for a typical retail sale to a consumer.

Return ONLY a JSON object with exactly these keys:
  "taxable":  true or false,
  "rate":     a number -- use 0.0725 (the California statewide base rate) if taxable, 0 if exempt,
  "citation": "CDTFA Reg <number>",
  "summary":  one plain-English sentence describing the rule.

Category: {category}
Regulation number: {reg}
Excerpt:
\"\"\"{excerpt}\"\"\"
"""


def extract_json(s: str) -> dict:
    s = s.strip()
    if "{" in s and "}" in s:
        s = s[s.find("{"): s.rfind("}") + 1]
    return json.loads(s)


def main():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT category, text, source_url FROM documents ORDER BY category"
        ).fetchall()

    drafts = []
    for category, text, source_url in rows:
        m = re.search(r"/(\d+)\.html", source_url or "")
        reg = m.group(1) if m else "?"
        prompt = PROMPT.format(category=category, reg=reg, excerpt=text[:5000])
        resp = model.generate_content(prompt)
        rule = extract_json(resp.text)
        rule["category"] = category
        rule["source_url"] = source_url
        drafts.append(rule)
        print(f"drafted {category:16} taxable={rule.get('taxable')} "
              f"rate={rule.get('rate')} {rule.get('citation')}")

    with open("rules_draft.json", "w", encoding="utf-8") as f:
        json.dump(drafts, f, indent=2)
    print(f"\nWrote rules_draft.json ({len(drafts)} rules).")
    print("Review it, fix anything wrong, and save the good ones as rules_approved.json.")


if __name__ == "__main__":
    main()
