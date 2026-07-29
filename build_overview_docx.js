// Generate PROJECT_OVERVIEW.docx from the project's high-level design.
// Run: NODE_PATH=<global node_modules> node build_overview_docx.js
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, LevelFormat, HeadingLevel, BorderStyle, WidthType, ShadingType,
  TableOfContents, PageBreak, PageNumber, Header, Footer,
} = require("docx");

const CW = 9360; // content width (US Letter, 1" margins)
const B = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const BORDERS = { top: B, bottom: B, left: B, right: B };

const runs = (text, opts = {}) => new TextRun({ text, font: "Arial", ...opts });

function p(text, opts = {}) {
  return new Paragraph({ spacing: { after: 120 }, children: [runs(text, opts)] });
}
function bullet(text, level = 0) {
  return new Paragraph({
    numbering: { reference: "bul", level },
    spacing: { after: 60 },
    children: Array.isArray(text) ? text : [runs(text)],
  });
}
function code(lines) {
  return lines.map((ln) => new Paragraph({
    shading: { type: ShadingType.CLEAR, fill: "F3F3F3" },
    spacing: { after: 0, line: 240 },
    children: [new TextRun({ text: ln.length ? ln : " ", font: "Consolas", size: 16 })],
  }));
}
function cell(text, w, o = {}) {
  return new TableCell({
    borders: BORDERS,
    width: { size: w, type: WidthType.DXA },
    shading: o.fill ? { fill: o.fill, type: ShadingType.CLEAR } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    verticalAlign: "center",
    children: [new Paragraph({ children: [runs(String(text), { bold: !!o.bold, size: 20 })] })],
  });
}
function table(headers, rows, widths) {
  const head = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => cell(h, widths[i], { bold: true, fill: "D6E4F0" })),
  });
  const body = rows.map((r) => new TableRow({ children: r.map((c, i) => cell(c, widths[i])) }));
  return new Table({ width: { size: CW, type: WidthType.DXA }, columnWidths: widths, rows: [head, ...body] });
}
const H1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [runs(t)] });
const H2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [runs(t)] });

const children = [];

// ---- Title ----
children.push(new Paragraph({
  spacing: { after: 60 },
  children: [runs("California Sales-Tax Assistant", { bold: true, size: 44 })],
}));
children.push(new Paragraph({
  spacing: { after: 60 },
  children: [runs("High-Level Design & Status", { size: 28, color: "555555" })],
}));
children.push(new Paragraph({
  spacing: { after: 240 },
  children: [runs(
    "A neuro-symbolic assistant that answers California sales & use tax questions with a cited, deterministic verdict — and honestly says “Needs review” when it isn’t sure.",
    { italics: true, color: "555555" })],
}));
children.push(table(
  ["Field", "Value"],
  [["Status", "Working demonstrator (rules AI-drafted; human verification deferred)"],
   ["Last updated", "2026-07-01"],
   ["Location", "ca-tax-real/"],
   ["Domain", "California sales & use tax (CDTFA Title 18, Reg 1500–1707)"]],
  [2200, 7160]));

children.push(new Paragraph({ spacing: { before: 240 }, children: [
  new TextRun({ text: "Contents", bold: true, font: "Arial", size: 24 })] }));
children.push(new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }));
children.push(new Paragraph({ children: [new PageBreak()] }));

// ---- 1 ----
children.push(H1("1. Purpose & guiding principle"));
children.push(p("The single design goal is “never confidently wrong.” The system would rather defer than guess. Every answer is either:"));
children.push(bullet("a verdict backed by a deterministic rule and a statute citation, or"));
children.push(bullet("“Needs review — not covered by current rules.”"));
children.push(p("The language model is deliberately fenced off from being the source of truth: it handles language (understanding the question, writing the answer); the facts come from a curated rule base; and a guard enforces honesty. This is a demonstrator, not a filing tool — the rules are AI-drafted and not yet verified by a tax professional (see §9)."));

// ---- 2 ----
children.push(H1("2. Architecture — Crawl → Store → Respond"));
children.push(...code([
  "CRAWL              STORE (Postgres + pgvector)        RESPOND (engine.py)",
  "-----              ---------------------------        -------------------",
  "CDTFA regs   -->   documents / doc_chunks (law text)      question",
  "(Reg 1500-         product_rules (fine verdicts)             |",
  " 1707)             rule_embeddings (routing index)           v",
  "                   local_rates (city/county rates)   route->lookup->localize",
  "                                                     ->cite->compose->guard",
  "                                                             |",
  "                                                             v",
  "                                                     cited answer OR",
  "                                                     “Needs review”",
]));
children.push(new Paragraph({ spacing: { before: 120, after: 80 }, children: [runs("The neuro-symbolic split:", { bold: true })] }));
children.push(table(
  ["Layer", "Does", "Implemented by"],
  [["Neuro (language)", "understand the question, compose prose", "Gemini (swappable)"],
   ["Symbolic (truth)", "is it taxable? rate? citation?", "product_rules (deterministic lookup)"],
   ["Guard (honesty)", "defer when no rule matches", "distance threshold → Needs review"]],
  [2100, 4260, 3000]));
children.push(p("The model layer is swappable via config.py/.env (Gemini today; Azure later for compliance) with no logic changes.", { }));

// ---- 3 ----
children.push(H1("3. Request lifecycle"));
children.push(p("For each question, engine.answer():"));
[["Route", "map the question to a rule key via local embedding similarity over the rule catalog, with a curated disambiguation tier and a conservative lexical rerank (a Gemini-generation router is available as a fallback)."],
 ["Guard", "if the nearest rule is beyond a calibrated distance threshold (0.35), return “Needs review” (off-topic / out-of-scope)."],
 ["Look up", "fetch the verdict from product_rules (fine) then rules (coarse)."],
 ["Localize the rate", "standard-rate taxable items get the combined city/county rate from local_rates; partial/special-rate items (fuel, partial exemptions) are left as-is and flagged; exempt stays 0."],
 ["Cite", "retrieve the most relevant law passage via pgvector (doc_chunks)."],
 ["Compose", "Gemini writes a 2–3 sentence answer from those facts only, always including the citation."],
].forEach(([k, v], i) => children.push(new Paragraph({
  numbering: { reference: "num", level: 0 }, spacing: { after: 60 },
  children: [runs(k + " — ", { bold: true }), runs(v)],
})));

// ---- 4 ----
children.push(H1("4. Data stores (Neon Postgres + pgvector)"));
children.push(table(
  ["Table", "Rows", "Role"],
  [["product_rules", "492 / 84 regs (245 taxable, 247 exempt)", "The depth layer: one fine, condition-aware verdict per scenario, with subsection citation."],
   ["rule_embeddings", "510 (all embedded)", "Routing index — question → rule by vector similarity."],
   ["doc_chunks", "317 / 97 regs (all embedded)", "Chunked full reg text for citation retrieval."],
   ["local_rates", "540 (all 58 counties)", "City/county combined sales-tax rates."],
   ["documents", "97 (20 embedded, legacy)", "Original capped crawl; superseded by doc_chunks."],
   ["rule_drafts", "97", "Coarse AI drafts (one per reg), pre-depth."],
   ["rules", "20", "Coarse MVP rules (breadth fallback)."]],
  [2000, 2560, 4800]));

// ---- 5 ----
children.push(H1("5. Codebase (by role)"));
children.push(new Paragraph({ spacing: { after: 60 }, children: [runs("Pipeline (build the stores)", { bold: true })] }));
["registry.py / crawl.py / crawl_all.py — discover & crawl the 97 CDTFA regs",
 "fetch_full.py — full cleaned reg text for deep reading & chunking",
 "classify_regs.py — bucket regs by size (large/medium/small)",
 "load_product_rules.py — load fine rules from JSON (reg####_rules.json)",
 "embed_docs.py — chunk + embed reg text → doc_chunks",
 "embed_rules.py — embed the rule catalog → rule_embeddings",
 "local_rates.py — fetch/load/verify/resolve city-county rates",
 "db.py / config.py — schema/connection and central config",
].forEach((t) => children.push(bullet(t)));
children.push(new Paragraph({ spacing: { before: 80, after: 60 }, children: [runs("Responder", { bold: true })] }));
["engine.py — routing (embed + disambiguation + rerank + guard), lookup, rate localization, citation, composition",
 "app.py — Streamlit chat UI (streamlit run app.py)",
].forEach((t) => children.push(bullet(t)));
children.push(new Paragraph({ spacing: { before: 80, after: 60 }, children: [runs("Evaluation & QA", { bold: true })] }));
["route_eval.py — router calibration + coverage measurement (cached)",
 "coverage.py — end-to-end coverage harness by topic (cached)",
 "verify.py — Phase B internal-consistency audit of all rules",
 "smoke_test.py — quick end-to-end routing check",
].forEach((t) => children.push(bullet(t)));

// ---- 6 ----
children.push(H1("6. What’s implemented"));
["Deep-read rule base — every taxability-bearing CDTFA sales/use-tax reg read line-by-line: 492 fine rules across 84 regs (the other 13 regs are administrative, no verdicts).",
 "Neuro-symbolic responder — routing, deterministic lookup, citation, compose, guard.",
 "Embedding-based routing — removed the 20/day generation bottleneck; scales past hundreds of rules; calibrated guard; disambiguation + conservative rerank.",
 "Citation retrieval — full reg text chunked and embedded (317/317).",
 "Location-aware rates — 540 jurisdictions from the authoritative CDTFA source, verified.",
 "QA tooling — internal-consistency verifier, coverage harness, calibration eval.",
 "Demo UI — Streamlit chat front end.",
].forEach((t) => children.push(bullet(t)));

// ---- 7 ----
children.push(H1("7. Current metrics / evidence"));
["Rule base: 492 rules / 84 regs; deep-read verified complete (13 no-rule regs confirmed administrative).",
 "Internal audit (Phase B): 0 verdict/rate inconsistencies, 0 duplicate keys, 0 malformed rows; 1 real bug found & fixed.",
 "Coverage sweep (53 realistic probes): 100% in-scope coverage, 100% verdict accuracy (51/51 after the disambiguation fix), out-of-scope questions correctly deferred.",
 "Local rates: verified — 540 rows, all rates in the valid 7.25%–11.25% band, cross-checked against published values.",
].forEach((t) => children.push(bullet(t)));
children.push(p("“100%” is on the 53-probe evaluation set — a strong signal, not a guarantee across all possible questions.", { italics: true, color: "555555" }));

// ---- 8 ----
children.push(H1("8. Operational notes (Gemini free tier)"));
children.push(table(
  ["Model", "Limit"],
  [["Generation (gemini-2.5-flash-lite)", "20 / day"],
   ["Embedding (gemini-embedding-001)", "1,000 / day and ~30 / minute"]],
  [5600, 3760]));
children.push(p("Routing was moved off generation onto embeddings to escape the 20/day wall. Batch embedding jobs use backoff and are resumable. Billing (or Azure) removes these limits for scale."));

// ---- 9 ----
children.push(H1("9. Scope & honest limitations"));
["Domain: California sales & use tax only (CDTFA Title 18). Not property tax, income tax, or other states — those correctly return “Needs review.”",
 "Verification: rules are AI-drafted and unverified. Internal consistency is checked; the underlying legal determinations are not professionally verified — demonstrator only, not for real filing.",
 "Rate granularity: city/county level. Sub-city district pockets that only a full street address resolves are not captured.",
 "Snapshots: rules and rates are point-in-time; statutes and district taxes change and require periodic refresh.",
 "Two known routing edges (generic vs. specific “ice”/“water” rules) are handled by a curated disambiguation tier; the root fix (better rule-embedding text) is queued.",
].forEach((t) => children.push(bullet(t)));

// ---- 10 ----
children.push(H1("10. Future steps"));
children.push(new Paragraph({ spacing: { after: 60 }, children: [runs("Near-term", { bold: true })] }));
["Re-embed the two generic rules with cleaner text (root fix for the disambiguation edge).",
 "Expand the coverage probe set (more topics, adversarial phrasings); track over time.",
 "Promote product_rules to a stable served/prod snapshot; add schema migrations.",
].forEach((t) => children.push(bullet(t)));
children.push(new Paragraph({ spacing: { before: 80, after: 60 }, children: [runs("Medium-term", { bold: true })] }));
["Human / tax-pro verification — the real value-add (and real cost); keep drafts separate from a verified served tier.",
 "Address-level rates via the CDTFA address API (rooftop precision).",
 "Reg-change & rate-change monitoring to keep snapshots fresh.",
 "Harden the demo (auth, logging, cost caching) for beta users.",
].forEach((t) => children.push(bullet(t)));
children.push(new Paragraph({ spacing: { before: 80, after: 60 }, children: [runs("Long-term", { bold: true })] }));
["Expand to other CA tax types (property, income) and/or other states.",
 "Move the model layer to Azure for compliance; enable billing for scale.",
 "Confidence scoring and per-topic calibrated thresholds.",
].forEach((t) => children.push(bullet(t)));

// ---- 11 ----
children.push(H1("11. What we can change / improve"));
["Routing quality: sentence-aware chunking; hybrid lexical+vector scoring; richer rule-embedding text; return multiple matching rules when a question spans several.",
 "Rule interactions: model stacking/interactions (excise on top of sales tax, use-tax scenarios, partial exemptions + district tax) explicitly rather than per-rule.",
 "Temporal awareness: effective-date handling for rules and rates (answer “as of” a date).",
 "Guard calibration: per-topic abstention thresholds; surface a confidence score.",
 "Provenance & audit: version rules, record which draft/source produced each answer.",
 "Cost/perf: cache embeddings and compositions; consider a local embedding model to remove API limits entirely.",
].forEach((t) => children.push(bullet(t)));

// ---- 12 ----
children.push(H1("12. Quickstart"));
children.push(...code([
  "# one-time",
  "python db.py                       # create schema",
  "python local_rates.py fetch && python local_rates.py load",
  "python embed_docs.py build  && python embed_docs.py embed",
  "python embed_rules.py build && python embed_rules.py embed",
  "",
  "# use it",
  "python engine.py \"Is a $10 case of soda taxable in San Francisco?\"",
  "streamlit run app.py               # chat UI",
  "",
  "# evaluate",
  "python verify.py                   # internal-consistency audit",
  "python route_eval.py run           # routing calibration + coverage",
]));

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: "Arial", color: "1F3864" },
        paragraph: { spacing: { before: 280, after: 140 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: "2E5496" },
        paragraph: { spacing: { before: 180, after: 100 }, outlineLevel: 1 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bul", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 540, hanging: 270 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 1080, hanging: 270 } } } }] },
      { reference: "num", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 540, hanging: 270 } } } }] },
    ],
  },
  sections: [{
    properties: { page: {
      size: { width: 12240, height: 15840 },
      margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
    } },
    footers: { default: new Footer({ children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [new TextRun({ text: "CA Sales-Tax Assistant — HLD  ·  Page ", font: "Arial", size: 16, color: "888888" }),
                 new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 16, color: "888888" })],
    })] }) },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync("PROJECT_OVERVIEW.docx", buf);
  console.log("wrote PROJECT_OVERVIEW.docx (" + buf.length + " bytes)");
});
