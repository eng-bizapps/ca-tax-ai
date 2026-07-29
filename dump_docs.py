"""Export crawled regulation text so Claude Code can read and draft rules from it."""
import db

rows = db.get_conn().execute(
    "SELECT reg, source_url, left(text, 1800) FROM documents WHERE reg IS NOT NULL ORDER BY reg"
).fetchall()

with open("docs_dump.txt", "w", encoding="utf-8") as f:
    for reg, url, text in rows:
        f.write(f"=== REG {reg} | {url} ===\n{text}\n\n")

print(f"wrote docs_dump.txt ({len(rows)} regs)")
