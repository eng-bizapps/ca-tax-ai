"""Step 7 - Measure. Runs the gold questions and prints a scorecard.

The number that matters: UNSAFE (confidently wrong) must be 0.

Run: python eval.py
"""
import json

from engine import answer


def run():
    with open("gold.json", encoding="utf-8") as f:
        gold = json.load(f)

    correct = safe_defer = over_defer = unsafe = 0
    for case in gold:
        res = answer(case["question"], compose=False)  # 1 model call per question
        got = res["status"]
        if case["expected_status"] == "answered":
            if got == "answered" and res["category"] == case.get("expected_category"):
                correct += 1
                mark = "OK"
            elif got == "needs_review":
                over_defer += 1
                mark = "OVER-DEFER (said review, should answer)"
            else:
                unsafe += 1
                mark = f"WRONG (category={res['category']})"
        else:  # expected needs_review
            if got == "needs_review":
                safe_defer += 1
                mark = "OK (deferred)"
            else:
                unsafe += 1
                mark = f"UNSAFE (answered, should defer; category={res['category']})"
        print(f"[{mark}]  {case['question']}")

    total = len(gold)
    print("\n=== scorecard ===")
    print(f"total questions          : {total}")
    print(f"correct answers          : {correct}")
    print(f"safe deferrals           : {safe_defer}")
    print(f"over-deferrals           : {over_defer}")
    print(f"UNSAFE (confident wrong) : {unsafe}   <- must be 0")


if __name__ == "__main__":
    run()
