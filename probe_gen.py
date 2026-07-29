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

Usage:
  python probe_gen.py generate [N]   # generate up to N new probes (1 LLM call each, cached)
  python probe_gen.py run [N]        # route + grade up to N generated probes (1 embed call each, cached)
  python probe_gen.py report
  python probe_gen.py reset
"""
import json
import os
import sys
import time

import db
import engine

RATE_LIMIT_BACKOFF = 20   # seconds to wait on a 429 before retrying -- the
                          # generator hits a per-MINUTE throttle (~15-17
                          # calls/burst), not the 500/day cap; matches the
                          # backoff embed_docs.py already uses for the
                          # analogous embedding rate limit
RATE_LIMIT_RETRIES = 4

CACHE = os.path.join(os.path.dirname(__file__), "probe_gen_results.json")
ITEM_SWEEP_CACHE = os.path.join(os.path.dirname(__file__), "item_sweep_results.json")

GEN_PROMPT = (
    "You write realistic questions ordinary people ask about California sales "
    "tax. Given this internal rule (not shown to the user), write ONE natural, "
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
    "RULE LABEL: {label}\nCONDITION: {condition}\nSUMMARY: {summary}"
)


def _load():
    return json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _save(c):
    json.dump(c, open(CACHE, "w", encoding="utf-8"), indent=2)


def _already_covered_keys():
    """product_keys item_sweep already directly probes (primary or branch) --
    skip these; the ceiling this tool exists to raise is the rest of the
    catalog item_sweep's manually-imagined items never touch."""
    if not os.path.exists(ITEM_SWEEP_CACHE):
        return set()
    cache = json.load(open(ITEM_SWEEP_CACHE, encoding="utf-8"))
    keys = set()
    for v in cache.values():
        if v.get("key"):
            keys.add(v["key"])
        keys.update(v.get("branch_keys") or [])
    return keys


def _targets(conn):
    covered = _already_covered_keys()
    rows = conn.execute(
        "SELECT product_key, product_label, condition, summary, taxable "
        "FROM product_rules WHERE tier='consumer'").fetchall()
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


def generate(limit=25):
    conn = db.get_conn()
    cache = _load()
    targets = _targets(conn)
    todo = [t for t in targets if t[0] not in cache]
    made = 0
    for pk, label, cond, summ, taxable in todo:
        if made >= limit:
            break
        prompt = GEN_PROMPT.format(label=label, condition=cond or "", summary=summ or "")
        try:
            q = _generate_one(prompt)
        except Exception as e:
            print(f"STOP after {made} (not rate-limit related): {str(e)[:120]}")
            break
        cache[pk] = {"question": q, "expected_taxable": bool(taxable), "graded": False}
        made += 1
        print(f"  {pk:45} -> {q}")
        _save(cache)
    conn.close()
    print(f"\ngenerated {made} new; {len(cache)}/{len(targets)} of the "
          "item_sweep-uncovered catalog now has a probe")


def _answer_one(q):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return engine.answer(q, compose=False, source="probe_gen")
        except Exception as e:
            if "429" not in str(e) or attempt == RATE_LIMIT_RETRIES:
                raise
            print(f"    (rate limited, waiting {RATE_LIMIT_BACKOFF}s...)")
            time.sleep(RATE_LIMIT_BACKOFF)


def run(limit=999):
    cache = _load()
    graded = 0
    for pk, entry in cache.items():
        if entry.get("graded") or graded >= limit:
            continue
        q = entry["question"]
        try:
            r = _answer_one(q)
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
        _save(cache)
    print(f"\ngraded {graded} new")
    report()


def report():
    cache = _load()
    graded = [(k, v) for k, v in cache.items() if v.get("graded")]
    if not graded:
        print("nothing graded yet; run: python probe_gen.py generate && python probe_gen.py run")
        return
    by_verdict = {}
    for pk, v in graded:
        by_verdict.setdefault(v["verdict"], []).append((pk, v))
    print(f"=== PROBE GENERATOR ({len(graded)} graded / {len(cache)} generated) ===")
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


def reset():
    if os.path.exists(CACHE):
        os.remove(CACHE)
    print("cache cleared")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if cmd == "generate":
        generate(n or 25)
    elif cmd == "run":
        run(n or 999)
    elif cmd == "report":
        report()
    elif cmd == "reset":
        reset()
    else:
        print(__doc__)
