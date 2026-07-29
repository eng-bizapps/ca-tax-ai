"""Phase B: auto-verify + triage for product_rules.

Deterministic checks over every rule (no API, fully reproducible). Flags the class
of bug that matters most for "never confidently wrong": a taxable/exempt boolean
that contradicts its own summary text (this is exactly the Reg 1591 medicines error
caught earlier), plus lookup hazards (duplicate keys), rate anomalies, and citation
sanity.

Usage:
  python verify.py            # print triaged findings, grouped by severity
"""
import re
from collections import defaultdict

import db

KNOWN_RATES = {0.0, 0.0725, 0.0225, 0.13}

# Negated / exemption constructs removed BEFORE scanning for taxable phrases, so
# "neither sales tax nor use tax applies" doesn't read as "tax applies".
NEGATED = [
    r"neither sales tax nor use tax applies", r"nor use tax applies",
    r"is not taxable", r"are not taxable", r"not taxable",
    r"tax does not apply", r"does not apply",
    r"not subject to (?:sales |use )?tax", r"not subject to",
    r"exempt from (?:sales |use )?tax", r"exempt from",
    r"no (?:sales|use) tax", r"not a taxable (?:sale|retail)",
    r"does not include", r"excluded from the (?:taxable |sales tax )?measure",
    r"excluded from the",
]
# Phrases that assert NON-taxability.
EXEMPT_PAT = [
    r"\bis not taxable\b", r"\bare not taxable\b", r"\bnot a taxable\b",
    r"\bnot subject to (?:sales |use )?tax\b", r"\bexempt from\b",
    r"\bis exempt\b", r"\bare exempt\b", r"\btax does not apply\b",
    r"\bneither sales tax nor use tax\b", r"\bnontaxable\b",
    r"\bno (?:sales|use) tax\b", r"\bnot (?:a )?taxable (?:sale|retail)\b",
    r"\bis not a sale\b", r"\bnot a sale of tangible\b", r"\bnot taxed\b",
    r"\bdoes not include\b", r"\bexcluded from the\b",
]
# Phrases that assert taxability.
TAXABLE_PAT = [
    r"\bis taxable\b", r"\bare taxable\b", r"\bfully taxable\b",
    r"\bsales tax applies\b", r"\buse tax applies\b", r"\btax applies\b",
    r"\bsubject to (?:sales |use )?tax\b", r"\bis subject to\b",
    r"\btaxable (?:sale|retail sale)\b", r"\btax is due\b",
    r"\bis taxed\b", r"\bare taxed\b", r"\bremains taxable\b",
    r"\bowes? (?:the )?tax\b", r"\bliable for\b",
    r"\b(?:included in|part of|is the) (?:the )?(?:taxable |sales tax )?measure\b",
    r"\btaxable measure\b",
]


def _score(text, patterns):
    return sum(1 for p in patterns if re.search(p, text))


def _taxable_score(text):
    """Count taxable phrases AFTER stripping negated/exempt constructs."""
    stripped = text
    for p in NEGATED:
        stripped = re.sub(p, " ", stripped)
    if "neither" in text:  # "neither ... nor ... is taxable" -> exempt, not taxable
        stripped = re.sub(r"\b(is|are) taxable\b", " ", stripped)
    return _score(stripped, TAXABLE_PAT)


def main():
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT reg, product_key, product_label, taxable, rate, citation, "
                "condition, summary, COALESCE(tier,'consumer') FROM product_rules")
    rows = cur.fetchall()
    conn.close()

    errors, warnings, notes = [], [], []
    keys = defaultdict(list)          # product_key -> [reg,...] (lookup hazard)

    for reg, pk, label, taxable, rate, citation, cond, summary, tier in rows:
        rate = float(rate)
        keys[pk].append(reg)
        text = f"{summary} {cond}".lower()

        # Administrative rules (distributor/licensing mechanics) never route to a
        # consumer answer: they are intentionally taxable=false/rate=0 while their
        # summaries describe distributor-level excise taxes. The verdict<->summary
        # and verdict<->rate semantic checks only apply to consumer tier.
        reg_dot = reg.replace("-", ".")  # URL stem '2558-1' -> reg number '2558.1'
        if tier != "consumer":
            # structural checks only
            if citation and f"Reg {reg}" not in citation and reg not in citation \
                    and reg_dot not in citation:
                warnings.append((reg, pk, f"citation '{citation}' does not reference reg {reg}"))
            if not summary or len(summary) < 40:
                warnings.append((reg, pk, "summary too short/thin"))
            continue

        # 1. verdict <-> summary contradiction
        ex = _score(text, EXEMPT_PAT)
        tx = _taxable_score(text)
        # Suppress known-legit patterns that mix exemption language with a taxable verdict:
        #  - partial-exemption / additional-tax rules (non-standard rate) legitimately say "exempt"
        #  - transactions taxed via USE tax whose summaries note the sales-tax exemption
        partial = rate in (0.0225, 0.13)
        use_tax_owed = bool(re.search(r"use tax (?:to the|measured|applies|on)|owes use tax|pays use tax", text))
        consumer_use = ("consumer" in text and "use tax" in text)  # exempt retail sale, seller owes use tax
        if taxable and (partial or use_tax_owed):
            pass  # expected to contain exemption language; not a contradiction
        elif (not taxable) and consumer_use:
            pass  # "sale not taxed; seller/buyer is consumer owing use tax" is a valid exempt-sale rule
        elif taxable and ex and not tx:
            errors.append((reg, pk, f"marked TAXABLE but summary reads exempt (ex={ex},tx={tx})"))
        elif (not taxable) and tx and not ex:
            errors.append((reg, pk, f"marked EXEMPT but summary reads taxable (ex={ex},tx={tx})"))
        elif taxable and ex > tx:
            warnings.append((reg, pk, f"TAXABLE but exempt-language dominates (ex={ex},tx={tx})"))
        elif (not taxable) and tx > ex:
            warnings.append((reg, pk, f"EXEMPT but taxable-language dominates (ex={ex},tx={tx})"))

        # 2. verdict <-> rate consistency
        if taxable and rate == 0:
            errors.append((reg, pk, "TAXABLE but rate is 0"))
        if (not taxable) and rate > 0:
            errors.append((reg, pk, f"EXEMPT but rate is {rate}"))

        # 3. rate anomaly
        if rate not in KNOWN_RATES:
            warnings.append((reg, pk, f"unusual rate {rate} (not in {sorted(KNOWN_RATES)})"))

        # 4. citation should reference this reg
        if citation and f"Reg {reg}" not in citation and reg not in citation \
                and reg_dot not in citation:
            warnings.append((reg, pk, f"citation '{citation}' does not reference reg {reg}"))

        # 5. thin content
        if not summary or len(summary) < 40:
            warnings.append((reg, pk, "summary too short/thin"))
        if not cond or len(cond) < 15:
            notes.append((reg, pk, "condition thin/absent"))

    # 6. duplicate product_key across regs -> _lookup(product_key) is ambiguous
    dupes = {k: v for k, v in keys.items() if len(v) > 1}

    def dump(title, items):
        print(f"\n===== {title} ({len(items)}) =====")
        for reg, pk, msg in items:
            print(f"  reg {reg} / {pk}: {msg}")

    print(f"Verified {len(rows)} rules.")
    dump("ERRORS (fix)", errors)
    dump("WARNINGS (review)", warnings)
    print(f"\n===== DUPLICATE product_key ACROSS REGS ({len(dupes)}) =====")
    for k, regs in sorted(dupes.items()):
        print(f"  '{k}' in regs {regs}")
    print(f"\nSummary: {len(errors)} errors, {len(warnings)} warnings, "
          f"{len(dupes)} duplicate keys, {len(notes)} thin-condition notes.")


if __name__ == "__main__":
    main()
