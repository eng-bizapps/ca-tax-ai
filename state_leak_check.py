"""Cross-state leak check: a state selection must never be answered with another
state's rules.

  * California-worded questions asked under Illinois must come back tagged
    state == "IL" (an Illinois answer or refusal), never a California answer.
  * Illinois-worded questions asked under California must be refused with the
    'switch the state selector' answer, unless they are legitimate multi-state
    questions (credit for tax paid to another state).

  python state_leak_check.py
"""
import engine
import il_sweep

CA_QUESTIONS = [
    "Is soda taxable in California?",
    "How much California income tax do I owe on $60,000 filing MFJ?",
    "What is the California property tax on a $600,000 house bought in 2020?",
    "What is my CalEITC if I make $9,975 with 2 qualifying children?",
    "I'm a California employer with 3 employees paid $50,000 each. What UI and ETT do I owe?",
    "Is a wedding video taxable?",
    "what is the renters credit if I make $40,000 filing married filing jointly",
]

IL_QUESTIONS_NAMING_ILLINOIS = [q for q, _c, _n in il_sweep.CASES if "Illinois" in q]
MULTI_STATE_OK = [
    "I paid tax to Illinois while living there, what is my California credit for tax paid to another state?",
]


def main():
    bad = 0
    for q in CA_QUESTIONS:
        r = engine.answer(q, state="IL")
        good = r.get("state") == "IL"
        bad += not good
        print(("ok   " if good else "FAIL ") + f"[CA question under IL] {q}  -> {r.get('category')}")
    for q in IL_QUESTIONS_NAMING_ILLINOIS:
        r = engine.answer(q, state="CA")
        good = r.get("category") == "state_mismatch"
        bad += not good
        print(("ok   " if good else "FAIL ") + f"[IL question under CA] {q[:70]}  -> {r.get('category')}")
    for q in MULTI_STATE_OK:
        r = engine.answer(q, state="CA")
        good = r.get("category") != "state_mismatch"
        bad += not good
        print(("ok   " if good else "FAIL ") + f"[multi-state under CA] {q[:70]}  -> {r.get('category')}")
    print("leaks:", bad)


if __name__ == "__main__":
    main()
