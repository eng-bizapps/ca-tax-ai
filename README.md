# CA sales tax assistant - real-product demo

A small but real slice of the architecture:
**crawl real CDTFA law -> Gemini drafts rules -> you verify -> Postgres/pgvector -> Gemini answers with a guard.**

The LLM only does language (understand the question, write the answer). The tax
decision, rate, math, and citation come from the rules + database. A guard makes
it say "Needs review" when no rule covers the question.

## One-time setup
1. Gemini key: https://aistudio.google.com -> Get API key
2. Neon Postgres: https://neon.tech -> create project -> copy connection string
   -> in the SQL editor run `CREATE EXTENSION IF NOT EXISTS vector;`
3. `copy .env.example .env` and fill in `GEMINI_API_KEY` and `DATABASE_URL`
4. Install deps:
   ```
   pip install -r requirements.txt
   ```
   (If install fails on Python 3.14, use Python 3.12 for this project.)

## Run order
```
python db.py            # create tables, verify connection
python crawl.py         # Step 2: fetch real CDTFA regulation pages
python draft_rules.py   # Step 3: Gemini drafts rules -> rules_draft.json
#   --> review rules_draft.json, save the good ones as rules_approved.json (Step 4)
python load.py          # Step 5: load rules + embeddings into Postgres
streamlit run app.py    # Step 6: ask questions in a chat UI
python eval.py          # Step 7: scorecard (UNSAFE must be 0)
```

## Files
| File | Role |
|------|------|
| config.py | env + settings (the one place to repoint to Azure) |
| db.py | Postgres + pgvector: schema, connection, vector search |
| llm via google-generativeai | Gemini, configured in each step |
| crawl.py | Step 2 - fetch + clean real CDTFA pages |
| draft_rules.py | Step 3 - Gemini drafts rules from the law |
| load.py | Step 5 - load approved rules + embed documents |
| engine.py | Step 6 - responder: extract -> rules -> retrieve -> compose -> guard |
| app.py | Step 6 - Streamlit chat UI |
| gold.json / eval.py | Step 7 - measured scorecard |

## Moving to Azure later
Change `config.py` / `.env` only:
- model -> Azure OpenAI deployment
- database -> Azure Database for PostgreSQL (or Azure AI Search for vectors)

No logic changes - the architecture is the same.
