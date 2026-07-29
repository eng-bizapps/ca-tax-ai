"""Step 5 - Store + index.

Loads your approved rules into the rules table, and computes Gemini embeddings
for every crawled document so retrieval (vector search) works.

Run AFTER you have created rules_approved.json:  python load.py
"""
import json
import time

import google.generativeai as genai

import config
import db

genai.configure(api_key=config.require("GEMINI_API_KEY", config.GEMINI_API_KEY))


def embed(text: str, tries: int = 5):
    """Embed with simple backoff so a per-minute rate limit doesn't abort the load."""
    for i in range(tries):
        try:
            return genai.embed_content(
                model=config.EMBED_MODEL, content=text,
                output_dimensionality=config.EMBED_DIM,
            )["embedding"]
        except Exception as e:
            if ("429" in str(e) or "exhaust" in str(e).lower()) and i < tries - 1:
                wait = 5 * (i + 1)
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("embedding failed after retries")


def main():
    with open("rules_approved.json", encoding="utf-8") as f:
        rules = json.load(f)

    with db.get_conn() as conn:
        conn.execute("TRUNCATE rules RESTART IDENTITY;")
        for r in rules:
            conn.execute(
                "INSERT INTO rules (category, taxable, rate, citation, summary, source_url, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,'approved') "
                "ON CONFLICT (category) DO UPDATE SET "
                "taxable=EXCLUDED.taxable, rate=EXCLUDED.rate, citation=EXCLUDED.citation, "
                "summary=EXCLUDED.summary, source_url=EXCLUDED.source_url",
                (r["category"], r["taxable"], r["rate"], r["citation"],
                 r.get("summary"), r.get("source_url")),
            )
        print(f"loaded {len(rules)} rules into the rules table.")

        docs = conn.execute("SELECT id, text FROM documents WHERE embedding IS NULL").fetchall()
        for doc_id, text in docs:
            conn.execute("UPDATE documents SET embedding=%s WHERE id=%s", (embed(text), doc_id))
        print(f"embedded {len(docs)} documents for retrieval.")
    print("Done. The stores are ready.")


if __name__ == "__main__":
    main()
