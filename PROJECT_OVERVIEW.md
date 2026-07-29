# California Sales-Tax Assistant — High-Level Design & Status

**A neuro-symbolic assistant that answers California sales & use tax questions with
a cited, deterministic verdict — and honestly says "Needs review" when it isn't sure.**

Status: working demonstrator (rules AI-drafted, human verification deferred).
Last updated: 2026-07-01. Location: `ca-tax-real/`.

---

## 1. Purpose & guiding principle

The single design goal is **"never confidently wrong."** The system would rather
defer than guess. Every answer is either:

- a **verdict backed by a deterministic rule and a statute citation**, or
- **"Needs review — not covered by current rules."**

The language model is deliberately fenced off from being the source of truth. It
handles *language* (understanding the question, writing the answer); the *facts*
come from a curated rule base, and a *guard* enforces honesty.

This is a **demonstrator**, not a filing tool: the rules are AI-drafted and not yet
verified by a tax professional (that step is deliberately deferred — see §9).

---

## 2. Architecture — Crawl → Store → Respond

```
  CRAWL                 STORE (Postgres + pgvector)             RESPOND (engine.py)
  ─────                 ───────────────────────────            ────────────────────
  CDTFA regs   ──▶      documents / doc_chunks (law text)   ─┐
  (Title 18,           product_rules (fine verdicts)        │  question
   Reg 1500–1707)      rule_embeddings (routing index)      │     │
                       local_rates (city/county rates)      │     ▼
                                                            │  route → lookup → localize
                                                            │  → cite → compose → guard
                                                            └─────────▶ cited answer
                                                                        or "Needs review"
```

**The neuro-symbolic split:**

| Layer | Does | Implemented by |
|---|---|---|
| **Neuro** (language) | understand the question, compose prose | Gemini (swappable) |
| **Symbolic** (truth) | is it taxable? rate? citation? | `product_rules` (deterministic lookup) |
| **Guard** (honesty) | defer when no rule matches | distance threshold → "Needs review" |

The model layer is swappable via `config.py`/`.env` (Gemini today; Azure later for
compliance) with no logic changes.

---

## 3. Request lifecycle

For each question, `engine.answer()`:

1. **Route** — map the question to a rule *key*. Default is **local embedding
   similarity** over the rule catalog (`rule_embeddings`), with a curated
   disambiguation tier and a conservative lexical rerank. A Gemini-generation
   router is available as a fallback (`router="gemini"`).
2. **Guard** — if the nearest rule is beyond a calibrated distance threshold
   (0.35), return **"Needs review"** (covers off-topic / out-of-scope questions).
3. **Look up** — fetch the verdict from `product_rules` (fine) then `rules` (coarse).
4. **Localize the rate** — standard-rate taxable items get the **combined
   city/county rate** from `local_rates`; partial/special-rate items (fuel,
   partial exemptions) are left as-is and flagged; exempt stays 0.
5. **Cite** — retrieve the most relevant law passage via pgvector (`doc_chunks`).
6. **Compose** — Gemini writes a 2–3 sentence answer **from those facts only**,
   always including the citation.

---

## 4. Data stores (Neon Postgres + pgvector)

| Table | Rows | Role |
|---|---|---|
| `product_rules` | **492** across **84 regs** (245 taxable / 247 exempt) | The depth layer: one fine, condition-aware verdict per product/scenario, with subsection citation. |
| `rule_embeddings` | **510** (all embedded) | Routing index — question → rule by vector similarity. |
| `doc_chunks` | **317** across 97 regs (all embedded) | Chunked full reg text for citation retrieval. |
| `local_rates` | **540** jurisdictions (all 58 counties) | City/county **combined** sales-tax rates. |
| `documents` | 97 (20 embedded, legacy) | Original capped crawl; superseded by `doc_chunks`. |
| `rule_drafts` | 97 | Coarse AI drafts (one per reg), pre-depth. |
| `rules` | 20 | Coarse MVP rules (breadth fallback). |

---

## 5. Codebase (by role)

**Pipeline (build the stores)**
- `registry.py`, `crawl.py`, `crawl_all.py` — discover & crawl the 97 CDTFA regs.
- `fetch_full.py` — full cleaned reg text (for deep reading & chunking).
- `classify_regs.py` — bucket regs by size (large/medium/small).
- `load_product_rules.py` — load the fine rules from JSON (`reg####_rules.json`).
- `embed_docs.py` — chunk + embed reg text → `doc_chunks`.
- `embed_rules.py` — embed the rule catalog → `rule_embeddings`.
- `local_rates.py` — fetch/load/verify/resolve city-county rates.
- `db.py`, `config.py` — schema/connection and central config.

**Responder**
- `engine.py` — the full pipeline: routing (embed + disambiguation + rerank + guard),
  lookup, rate localization, citation, composition.
- `app.py` — Streamlit chat UI (`streamlit run app.py`).

**Evaluation & QA**
- `route_eval.py` — router calibration + coverage measurement (cached).
- `coverage.py` — end-to-end coverage harness by topic (cached).
- `verify.py` — Phase B internal-consistency audit of all rules.
- `smoke_test.py` — quick end-to-end routing check.

---

## 6. What's implemented

- ✅ **Deep-read rule base** — every taxability-bearing CDTFA sales/use-tax reg read
  line-by-line: **492 fine rules across 84 regs**. The remaining 13 regs are
  administrative (permits, records, returns) with no product verdicts.
- ✅ **Neuro-symbolic responder** — routing, deterministic lookup, citation, compose, guard.
- ✅ **Embedding-based routing** — removed the 20/day generation bottleneck; scales past
  hundreds of rules; calibrated guard threshold; disambiguation + conservative rerank.
- ✅ **Citation retrieval** — full reg text chunked and embedded (317/317).
- ✅ **Location-aware rates** — 540 jurisdictions, authoritative CDTFA source, verified.
- ✅ **QA tooling** — internal-consistency verifier, coverage harness, calibration eval.
- ✅ **Demo UI** — Streamlit chat front end.

---

## 7. Current metrics / evidence

- **Rule base:** 492 rules / 84 regs; deep-read verified complete (documents vs
  product_rules cross-checked; 13 no-rule regs confirmed administrative).
- **Internal audit (Phase B):** 0 verdict/rate inconsistencies, 0 duplicate keys,
  0 malformed rows; 1 real bug found & fixed.
- **Coverage sweep (53 realistic probes):**
  - **100%** in-scope coverage (every question routed to a rule),
  - **100%** verdict accuracy (51/51, after the disambiguation fix),
  - out-of-scope questions (property tax, income tax) **correctly deferred**.
- **Local rates:** verified — 540 rows, all rates in the valid 7.25%–11.25% band,
  cross-checked against published values.

> "100%" is on the 53-probe evaluation set — a strong signal, not a guarantee across
> all possible questions.

---

## 8. Operational notes (Gemini free tier)

| Model | Limit |
|---|---|
| Generation (`gemini-2.5-flash-lite`) | **20 / day** |
| Embedding (`gemini-embedding-001`) | **1,000 / day** and **~30 / minute** |

Routing was moved off generation onto embeddings specifically to escape the 20/day
wall. Batch embedding jobs use backoff and are resumable. Billing (or Azure) removes
these limits for scale.

---

## 9. Scope & honest limitations

- **Domain:** California **sales & use tax** only (CDTFA Title 18). *Not* property
  tax, income tax, or other states — those correctly return "Needs review."
- **Verification:** rules are **AI-drafted and unverified**. Internal consistency is
  checked; the underlying legal determinations are **not** professionally verified.
  → demonstrator only, not for real filing.
- **Rate granularity:** **city/county** level. Sub-city district pockets that only a
  full street address resolves are not captured.
- **Snapshots:** rules and rates are point-in-time; statutes and district taxes
  change and require periodic refresh.
- **Two known routing edges** handled by a curated disambiguation tier (generic vs.
  specific "ice"/"water" rules); the root fix (better rule-embedding text) is queued.

---

## 10. Future steps

**Near-term**
- Re-embed the two generic rules with cleaner text (root fix for the disambiguation edge).
- Expand the coverage probe set (more topics, adversarial/edge phrasings); track over time.
- Promote `product_rules` to a stable served/prod snapshot; add schema migrations.

**Medium-term**
- **Human / tax-pro verification** — the real value-add (and real cost); promote
  verified rules to a served tier, keep drafts separate.
- **Address-level rates** via the CDTFA address API (rooftop precision).
- Reg-change & rate-change monitoring to keep snapshots fresh.
- Harden the demo (auth, logging, cost caching) for beta users.

**Long-term**
- Expand to other CA tax types (property, income) and/or other states.
- Move the model layer to Azure for compliance; enable billing for scale.
- Confidence scoring and per-topic calibrated thresholds.

---

## 11. What we can change / improve (design evolution)

- **Routing quality:** sentence-aware chunking; hybrid lexical+vector scoring;
  richer rule-embedding text; return **multiple** matching rules when a question
  spans several (today it picks one).
- **Rule interactions:** model stacking/interactions (e.g., excise on top of sales
  tax, use-tax scenarios, partial exemptions + district tax) explicitly rather than
  per-rule.
- **Temporal awareness:** effective-date handling for rules and rates (answer "as of"
  a date).
- **Guard calibration:** per-topic abstention thresholds; surface a confidence score.
- **Provenance & audit:** version rules, record which draft/source produced each
  answer, full audit trail.
- **Cost/perf:** cache embeddings and compositions; batch; consider a local embedding
  model to remove API limits entirely.

---

## 12. Quickstart

```bash
# one-time
python db.py                     # create schema
python local_rates.py fetch && python local_rates.py load   # city/county rates
python embed_docs.py build  && python embed_docs.py embed    # citation corpus
python embed_rules.py build && python embed_rules.py embed   # routing index

# use it
python engine.py "Is a $10 case of soda taxable in San Francisco?"
streamlit run app.py             # chat UI

# evaluate
python verify.py                 # internal-consistency audit
python route_eval.py run         # routing calibration + coverage
```
