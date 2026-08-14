"""Self-consistency probe generator: raise item_sweep's coverage ceiling past
its ~128 manually-imagined items by generating one independently-phrased
probe for every consumer rule item_sweep has never directly touched, then
checking whether the live router actually sends that probe back to the rule
it came from.

CRITICAL DESIGN CONSTRAINT: a generated probe is only useful if it does NOT
reuse the rule's own distinctive vocabulary -- otherwise it's a tautology (of
course a probe that repeats "modular furniture installation labor" routes to
modular_furniture_installation_labor). The generation prompt explicitly
forbids reusing the rule's own technical wording and asks for how an
ordinary person would actually phrase the question.

WHAT THIS DOES AND DOES NOT PROVE: a probe's expected verdict is inherited
from the rule's own `taxable` flag, so a passing probe confirms ROUTING
correctness (a naturally-phrased question about this thing reaches a rule
with the right verdict) -- it does NOT confirm the rule's own legal
determination is correct. That is the separate, still human-verification-
gated risk this tool cannot touch.

Domain-parameterized (2026-08-10) to also probe the income-tax catalog
(income_tax_topics, via income_db) -- it never went through this pass while
being built credit-by-credit. Only topics with a real `taxable` boolean are
probed (income_tax_topics also holds non-boolean eligibility/informational
rows like head_of_household_eligibility -- not this tool's shape). Separate
cache files per domain so sales' existing 349-probe history is untouched.

Usage:
  python probe_gen.py generate [N] [--domain=sales|income]
  python probe_gen.py run [N] [--domain=sales|income]
  python probe_gen.py report [--domain=sales|income]
  python probe_gen.py reset [--domain=sales|income]
  # default: N=25, domain=sales
"""
import json
import os
import sys
import time

import db
import engine
import income_db

RATE_LIMIT_BACKOFF = 20   # seconds to wait on a 429 before retrying -- the
                          # generator hits a per-MINUTE throttle (~15-17
                          # calls/burst), not the 500/day cap; matches the
                          # backoff embed_docs.py already uses for the
                          # analogous embedding rate limit
RATE_LIMIT_RETRIES = 4

_HERE = os.path.dirname(__file__)

GEN_PROMPT = (
    "You write realistic questions ordinary people ask about {domain_desc}. "
    "Given this internal rule (not shown to the user), write ONE natural, "
    "plain-English question a real person might ask that this exact rule "
    "should answer.\n"
    "STRICT RULES:\n"
    "- Do NOT reuse more than one or two of this rule's own technical/legal "
    "words verbatim -- describe the everyday SITUATION, not the regulation's "
    "own vocabulary. (e.g. for a rule about 'modular furniture installation "
    "labor', ask about paying someone to set up new office furniture, not "
    "'is modular furniture installation labor taxable'.)\n"
    "- Keep it a genuine yes/no taxability question, one sentence.\n"
    "- Do not mention the rule, citation, or regulation number.\n"
    "Reply with ONLY the question, nothing else.\n"
    "RULE LABEL: {{label}}\nCONDITION: {{condition}}\nSUMMARY: {{summary}}"
)

_DOMAINS = {
    "sales": {
        "conn_module": db,
        "cache": os.path.join(_HERE, "probe_gen_results.json"),
        "item_sweep_cache": os.path.join(_HERE, "item_sweep_results.json"),
        "targets_sql": "SELECT product_key, product_label, condition, summary, taxable "
                        "FROM product_rules WHERE tier='consumer'",
        "covered_keys": lambda cache: {
            k for v in cache.values()
            for k in ([v["key"]] if v.get("key") else []) + list(v.get("branch_keys") or [])
        },
        "gen_prompt": GEN_PROMPT.format(domain_desc="California sales tax"),
        "tax_type": None,
    },
    "income": {
        "conn_module": income_db,
        "cache": os.path.join(_HERE, "probe_gen_results_income.json"),
        "item_sweep_cache": os.path.join(_HERE, "income_item_sweep_results.json"),
        "targets_sql": "SELECT topic_key, topic_label, condition, summary, taxable "
                        "FROM income_tax_topics WHERE taxable IS NOT NULL",
        "covered_keys": lambda cache: {
            v["category"] for v in cache.values() if v.get("category")
        },
        "gen_prompt": GEN_PROMPT.format(
            domain_desc="California personal income tax (FTB) -- which income "
                        "types are taxable in California"),
        "tax_type": "income",
    },
}


def _cache_path(domain):
    return _DOMAINS[domain]["cache"]


def _load(domain):
    p = _cache_path(domain)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def _save(domain, c):
    json.dump(c, open(_cache_path(domain), "w", encoding="utf-8"), indent=2)


def _already_covered_keys(domain):
    """Keys the domain's item_sweep already directly probes (primary or, for
    sales, branch) -- skip these; the ceiling this tool exists to raise is
    the rest of the catalog item_sweep's manually-imagined items never
    touch."""
    spec = _DOMAINS[domain]
    if not os.path.exists(spec["item_sweep_cache"]):
        return set()
    cache = json.load(open(spec["item_sweep_cache"], encoding="utf-8"))
    return spec["covered_keys"](cache)


def _targets(conn, domain):
    covered = _already_covered_keys(domain)
    rows = conn.execute(_DOMAINS[domain]["targets_sql"]).fetchall()
    return [r for r in rows if r[0] not in covered]


def _generate_one(prompt):
    """generate_content with backoff on the per-minute 429 throttle. Raises
    on anything else (a real failure, not just rate-limited)."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return engine.model.generate_content(prompt).text.strip().strip('"')
        except Exception as e:
            if "429" not in str(e) or attempt == RATE_LIMIT_RETRIES:
                raise
            print(f"    (rate limited, waiting {RATE_LIMIT_BACKOFF}s...)")
            time.sleep(RATE_LIMIT_BACKOFF)


def generate(limit=25, domain="sales"):
    spec = _DOMAINS[domain]
    conn = spec["conn_module"].get_conn()
    cache = _load(domain)
    targets = _targets(conn, domain)
    todo = [t for t in targets if t[0] not in cache]
    made = 0
    for pk, label, cond, summ, taxable in todo:
        if made >= limit:
            break
        prompt = spec["gen_prompt"].format(label=label, condition=cond or "", summary=summ or "")
        try:
            q = _generate_one(prompt)
        except Exception as e:
            print(f"STOP after {made} (not rate-limit related): {str(e)[:120]}")
            break
        cache[pk] = {"question": q, "expected_taxable": bool(taxable), "graded": False}
        made += 1
        print(f"  {pk:45} -> {q}")
        _save(domain, cache)
    conn.close()
    print(f"\ngenerated {made} new; {len(cache)}/{len(targets)} of the "
          "item_sweep-uncovered catalog now has a probe")


def _answer_one(q, domain):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return engine.answer(q, compose=False, source="probe_gen",
                                  tax_type=_DOMAINS[domain]["tax_type"])
        except Exception as e:
            if "429" not in str(e) or attempt == RATE_LIMIT_RETRIES:
                raise
            print(f"    (rate limited, waiting {RATE_LIMIT_BACKOFF}s...)")
            time.sleep(RATE_LIMIT_BACKOFF)


def run(limit=999, domain="sales"):
    cache = _load(domain)
    graded = 0
    for pk, entry in cache.items():
        if entry.get("graded") or graded >= limit:
            continue
        q = entry["question"]
        try:
            r = _answer_one(q, domain)
        except Exception as e:
            print(f"STOP after {graded} (not rate-limit related): {str(e)[:120]}")
            break
        routed_key = r.get("category")
        routed_taxable = r.get("taxable")
        expected = entry["expected_taxable"]
        if r["status"] == "needs_review":
            verdict = "needs_review"
        elif routed_taxable == expected:
            verdict = "match" if routed_key == pk else "ok_diff_key"
        else:
            verdict = "WRONG"
        entry.update({"graded": True, "routed_key": routed_key,
                      "routed_taxable": routed_taxable, "verdict": verdict})
        graded += 1
        print(f"  [{verdict:12}] {pk:40} -> {routed_key}  ({q[:55]})")
        _save(domain, cache)
    print(f"\ngraded {graded} new")
    report(domain)


def report(domain="sales"):
    cache = _load(domain)
    graded = [(k, v) for k, v in cache.items() if v.get("graded")]
    if not graded:
        print(f"nothing graded yet ({domain}); run: python probe_gen.py generate "
              f"&& python probe_gen.py run --domain={domain}")
        return
    by_verdict = {}
    for pk, v in graded:
        by_verdict.setdefault(v["verdict"], []).append((pk, v))
    print(f"=== PROBE GENERATOR -- {domain} ({len(graded)} graded / {len(cache)} generated) ===")
    for verdict in ("match", "ok_diff_key", "WRONG", "needs_review"):
        print(f"  {verdict:12} {len(by_verdict.get(verdict, []))}")
    wrong = by_verdict.get("WRONG", [])
    if wrong:
        print(f"\n--- WRONG ({len(wrong)}) -- danger: routed to an opposite-verdict rule ---")
        for pk, v in wrong:
            print(f"  {pk} -> {v['routed_key']}   Q: {v['question']}")
    nr = by_verdict.get("needs_review", [])
    if nr:
        print(f"\n--- needs_review ({len(nr)}) -- safe gap, not dangerous ---")
        for pk, v in nr[:15]:
            print(f"  {pk}   Q: {v['question']}")
    diff = by_verdict.get("ok_diff_key", [])
    if diff:
        print(f"\n--- ok_diff_key ({len(diff)}) -- same verdict, different rule; "
              "worth a glance, not urgent ---")
        for pk, v in diff[:15]:
            print(f"  {pk} -> {v['routed_key']}   Q: {v['question']}")


def reset(domain="sales"):
    p = _cache_path(domain)
    if os.path.exists(p):
        os.remove(p)
    print(f"{domain} cache cleared")


if __name__ == "__main__":
    positional = [a for a in sys.argv[1:] if not a.startswith("--domain")]
    domain_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--domain=")), "sales")
    cmd = positional[0] if positional else "report"
    n = int(positional[1]) if len(positional) > 1 else None
    if cmd == "generate":
        generate(n or 25, domain=domain_arg)
    elif cmd == "run":
        run(n or 999, domain=domain_arg)
    elif cmd == "report":
        report(domain=domain_arg)
    elif cmd == "reset":
        reset(domain=domain_arg)
    else:
        print(__doc__)
