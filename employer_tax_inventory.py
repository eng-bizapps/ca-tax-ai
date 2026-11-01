"""Completeness ledger for the employer payroll-tax (EDD) domain -- Ring 5.
Same generic shape as property_tax_inventory.py. status: built | partial |
deferred | out_of_scope.

  python employer_tax_inventory.py load     # upsert rows
  python employer_tax_inventory.py status   # counts by status
"""
import sys

import employer_db as db

TAX_YEAR = 2026
CITE = "EDD DE 44 Employer's Guide, Rev. 52 (4-26)"

# (part, line_ref, item_label, status, topic_key, notes)
ITEMS = [
    ("core", "ui-ett", "UI and ETT on first $7,000 per employee", "built", "employer_payroll_cost",
     "New-employer 3.4% default or a stated experience rate (1.5%-6.2%); ETT 0.1%."),
    ("core", "sdi", "SDI 1.3% withheld on all wages, no cap", "built", "employer_payroll_cost",
     "Shown as the employee's cost the employer remits."),
    ("core", "ui-rate-assignment", "Experience-rated UI rate from reserve ratio (Schedules AA-F+)", "built",
     "employer_ui_rate", "DE 231Z table (38 bands x 8 schedules, F+ = F x 1.15 verified); F+ for 2026 and 2027. "
     "Reserve transfers/cancellations and EDD's ratio rounding not modeled."),
    ("core", "ui-schedule-selection", "Which schedule EDD puts in effect (UI Fund balance ratio)", "built",
     "employer_schedule",
     "DE 231Z thresholds (AA >= 1.8 down to F+ < 0.6 percent); schedule per year stored (F+ 2026-2027). The "
     "Sept 30 fund balance itself is not fetched: the user supplies the ratio, or EDD's published schedule governs."),
    ("core", "futa", "Federal FUTA 6.0% less 5.4% credit on first $7,000", "built", "employer_futa",
     "2025 and 2026; assumes state UI paid on time (late payments reduce the credit)."),
    ("core", "futa-credit-reduction", "California FUTA credit reduction", "partial", "employer_futa",
     "2022-2025 final (DOL). 2026 is preliminary (1.5%, up to 5.3% with BCR add-on) until DOL sets it Nov 10, 2026: "
     "REFRESH ca_futa_credit_reductions after that date."),
    ("core", "rates-2027", "2027 UI/ETT/SDI rates (SDI 1.4%, F+, $7,000)", "partial", "employer_payroll_cost",
     "Loaded from EDD's rates page 2026-11-01. 2027 FUTA reduction, supplemental rates, penalty, deposit "
     "rules and due dates are not published yet: add with the new DE 44 edition (expected January)."),
    ("deposits", "deposit-schedule", "California PIT/SDI deposit schedule from federal schedule + accumulated PIT",
     "built", "employer_deposit_schedule", "Includes next-day, semi-weekly, monthly, quarterly due dates."),
    ("deposits", "holidays", "Legal-holiday due-date extensions", "partial", "employer_deposit_schedule",
     "2026: California state holidays (Secretary of State list) shift due dates; federal-only bank holidays are "
     "flagged, not skipped (EDD publishes no list of its own). 2027 state list not published: weekends only."),
    ("withholding", "supplemental", "Supplemental-wage PIT flat rates 10.23% / 6.6%", "built",
     "employer_supplemental_withholding", "Separate-payment flat-rate method only."),
    ("penalties", "late-penalty", "15% late-payment penalty plus daily-compounded interest", "built",
     "employer_late_payment", "Interest on tax+penalty is an approximation, disclosed."),
    ("penalties", "filing-penalties", "DE 34 / DE 542 ($24, $490 intentional), DE 9C ($20 per wage item), paper filing, W-2",
     "built", "employer_filing_penalty",
     "From EDD's DE 34 form, DE 542B guide and DE 44. Maximums EDD may assess; waiver for good cause noted."),
    ("info", "wages-subject", "Which payments are subject wages (DE 231TP)", "partial", "employer_wages_subject",
     "33 curated payment types from the Rev. 1 (6-16) sheet, cross-checked with DE 44 (4-26). Rows whose law changed "
     "(moving expenses, HSA, domestic-partner taxes, adult-child coverage) are omitted; gift cards answered as a "
     "hedged needs_review."),
    ("core", "vui", "Voluntary UI contributions to lower the UI rate", "built", "employer_vui",
     "DE 231Z / CUIC 976.5: allowed only under schedules AA-D, so NOT available in 2026 or 2027 (F+). Eligibility "
     "rules and March deadline stated; the DE 2088VU savings worksheet is not computed."),
    ("core", "reimbursable", "Reimbursable method for nonprofits and public entities", "built", "employer_reimbursable",
     "Informational from DE 44 and DE 231NP (Rev. 5, 9-19): eligibility, election, 2/5-year minimums, quarterly "
     "billing, 3-year tail. Reimbursement amounts are not computed."),
    ("core", "household", "Household employers (DE 3HW / DE 3BHW)", "partial", "employer_household",
     "EDD household page: $750/quarter cash wages to register and withhold SDI, $1,000 for UI and ETT, annual "
     "filing option at $20,000 or less, PIT optional. Household UI/ETT/SDI rates and due dates not stated on the "
     "page, so no cost calculation."),
    ("benefits", "edd-benefits", "Worker-side UI / disability / paid-family-leave benefit amounts", "partial",
     "employer_benefit",
     "UI: official DE 1101BT5 table (411 bands, $40-$450). DI: EDD's 2026 tiers ($50 min, 90%, $1,127 flat, 70%, "
     "$1,765 max; weekly wages = highest quarter / 13, verified by arithmetic). PFL assumed to use the DI tiers "
     "(same maximum), hedged in the answer. Eligibility rules are not evaluated."),
    ("info", "registration", "Registering as an employer (DE 1), >$100 wages in a quarter", "built",
     "employer_registration",
     "Subject-employer test (over $100), 15-day deadline from the payment date, forms by employer type, first-"
     "steps list (DE 34, DE 542, DE 4, DE 88, DE 9/9C). Successor-employer and household specifics not detailed."),
    ("info", "worker-class", "ABC test / worker classification", "out_of_scope", None, "Fact-specific legal question."),
]


def load():
    with db.get_conn() as conn:
        # drop rows for items that were renamed or removed, so the ledger never keeps a stale status
        keep = {(ref, label) for _, ref, label, *_ in ITEMS}
        for rid, ref, label in conn.execute(
                "SELECT id, line_ref, item_label FROM employer_tax_inventory WHERE tax_year=%s", (TAX_YEAR,)).fetchall():
            if (ref, label) not in keep:
                conn.execute("DELETE FROM employer_tax_inventory WHERE id=%s", (rid,))
        for part, ref, label, status, topic, notes in ITEMS:
            conn.execute(
                "INSERT INTO employer_tax_inventory (tax_year, part, line_ref, item_label, citation, status, "
                "topic_key, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tax_year, line_ref, item_label) "
                "DO UPDATE SET part=EXCLUDED.part, status=EXCLUDED.status, topic_key=EXCLUDED.topic_key, "
                "notes=EXCLUDED.notes, citation=EXCLUDED.citation",
                (TAX_YEAR, part, ref, label, CITE, status, topic, notes))
    status_report()


def status_report():
    conn = db.get_conn()
    for st, n in conn.execute("SELECT status, count(*) FROM employer_tax_inventory GROUP BY 1 ORDER BY 1"):
        print(f"  {st:14} {n}")
    conn.close()


if __name__ == "__main__":
    {"load": load}.get(sys.argv[1] if len(sys.argv) > 1 else "", status_report)()
