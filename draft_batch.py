"""Pipeline stage 3 - batch draft a rule for every crawled regulation.

For each reg, Gemini reads the text and drafts a structured rule into the
rule_drafts table (status = 'ai_drafted'). Resumable (skips regs already drafted),
rate-limited, with retry/backoff.

Requires Gemini billing enabled (drafting ~97 regs exceeds the free 20/day).

Run: python draft_batch.py
"""
import json
import time

import google.generativeai as genai

import config
import db

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))
model = genai.GenerativeModel(config.GEMINI_MODEL)

PROMPT = """You are a California sales-tax analyst. Read this excerpt from a CDTFA
Sales & Use Tax regulation and describe the GENERAL treatment for a typical
retail sale to a consumer.

Return ONLY a JSON object with these keys:
{{
  "is_taxability_rule": true if this regulation states whether some product or service is taxable or exempt for consumers; false if it is purely administrative or procedural (records, permits, audits, definitions, collection),
  "category": a short snake_case label for the product/service (e.g. "prescription_medicines"), or "not_applicable",
  "taxable": true or false (only meaningful when is_taxability_rule is true),
  "rate": 0.0725 if taxable else 0,
  "citation": "CDTFA Reg {reg}",
  "summary": one plain-English sentence describing the rule.
}}

Regulation number: {reg}
Excerpt:
\"\"\"{excerpt}\"\"\"
"""


def extract_json(s: str) -> dict:
    s = s.strip()
    if "{" in s and "}" in s:
        s = s[s.find("{"): s.rfind("}") + 1]
    return json.loads(s)


def draft_one(reg: str, text: str) -> dict:
    prompt = PROMPT.format(reg=reg, excerpt=text[:5000])
    for i in range(5):
        try:
            return extract_json(model.generate_content(prompt).text)
        except Exception as e:
            if ("429" in str(e) or "exhaust" in str(e).lower()) and i < 4:
                wait = 10 * (i + 1)
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise


def main():
    with db.get_conn() as conn:
        done = {r[0] for r in conn.execute("SELECT reg FROM rule_drafts").fetchall()}
        docs = conn.execute(
            "SELECT reg, text, source_url FROM documents WHERE reg IS NOT NULL ORDER BY reg"
        ).fetchall()

        drafted = 0
        for reg, text, source_url in docs:
            if reg in done:
                continue
            rule = draft_one(reg, text)
            conn.execute(
                "INSERT INTO rule_drafts "
                "(reg, category, is_taxability_rule, taxable, rate, citation, summary, source_url, status, model_used) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ai_drafted',%s) "
                "ON CONFLICT (reg) DO UPDATE SET "
                "category=EXCLUDED.category, is_taxability_rule=EXCLUDED.is_taxability_rule, "
                "taxable=EXCLUDED.taxable, rate=EXCLUDED.rate, citation=EXCLUDED.citation, "
                "summary=EXCLUDED.summary, model_used=EXCLUDED.model_used, status='ai_drafted'",
                (reg, rule.get("category"), rule.get("is_taxability_rule"),
                 rule.get("taxable"), rule.get("rate") or 0,
                 rule.get("citation") or f"CDTFA Reg {reg}",
                 rule.get("summary"), source_url, config.GEMINI_MODEL),
            )
            drafted += 1
            print(f"drafted {reg}: taxability={rule.get('is_taxability_rule')} "
                  f"taxable={rule.get('taxable')} [{rule.get('category')}]")
            time.sleep(0.5)

    print(f"\nDone. drafted {drafted} rules into rule_drafts.")


if __name__ == "__main__":
    main()
