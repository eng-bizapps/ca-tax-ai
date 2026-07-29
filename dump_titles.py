"""Export reg number + official regulation title for compact drafting."""
import re

import db

rows = db.get_conn().execute(
    "SELECT reg, text FROM documents WHERE reg IS NOT NULL ORDER BY reg"
).fetchall()

with open("titles.txt", "w", encoding="utf-8") as f:
    for reg, text in rows:
        m = re.search(rf"Regulation {reg}\.\s*([^.]+)\.", text or "")
        title = m.group(1).strip() if m else "?"
        f.write(f"{reg}\t{title}\n")

print(f"wrote titles.txt ({len(rows)} regs)")
