"""Quick end-to-end smoke test: confirm the responder routes to the new rules.
Uses compose=False (1 Gemini call/question) to conserve quota.
Each probe lists the expected verdict so we can eyeball routing quality.
"""
import sys
import engine

PROBES = [
    # question, expected_taxable, note (which reg it should hit)
    ("Is a wedding video taxable in California?", True, "1529 private noncommercial film"),
    ("Do I owe sales tax on a US flag bought from a veterans organization?", False, "1597 veterans flag"),
    ("Are sales to the United States government taxable?", False, "1614 US govt"),
    ("Is a racehorse purchased for breeding subject to sales tax?", True, "1535 breeding stock (partial)"),
    ("If I paid tax on something and resold it before using it, do I owe tax again?", False, "1701 tax-paid resold"),
    ("Is diesel fuel taxable in California?", True, "1598 diesel"),
]

def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(PROBES)
    ok = 0
    for q, exp, note in PROBES[:limit]:
        try:
            r = engine.answer(q, compose=False, source="smoke_test")
        except Exception as e:
            print(f"ERROR  {q!r}: {e}")
            break
        status = r["status"]
        got = r["taxable"]
        match = "OK " if (status == "answered" and got == exp) else "XX "
        if match == "OK ":
            ok += 1
        print(f"{match} [{status:12}] taxable={got!s:5} exp={exp!s:5}  key={r['category']}")
        print(f"     Q: {q}")
        print(f"     (expected reg: {note})")
    print(f"\n{ok}/{limit} routed correctly")

if __name__ == "__main__":
    main()
