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
    ("core", "ui-schedule-selection", "Which schedule EDD puts in effect (UI Fund balance ratio)", "deferred", None,
     "Schedule per year is stored (F+ 2026-2027); the Sept 30 fund-balance test is not computed."),
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
    ("deposits", "holidays", "Legal-holiday due-date extensions", "deferred", None,
     "Weekend roll is modeled; legal holidays are disclosed as not modeled."),
    ("withholding", "supplemental", "Supplemental-wage PIT flat rates 10.23% / 6.6%", "built",
     "employer_supplemental_withholding", "Separate-payment flat-rate method only."),
    ("penalties", "late-penalty", "15% late-payment penalty plus daily-compounded interest", "built",
     "employer_late_payment", "Interest on tax+penalty is an approximation, disclosed."),
    ("penalties", "other-penalties", "DE 9 / DE 34 / DE 542 late-filing penalties", "deferred", None, "Not researched."),
    ("info", "registration", "Registering as an employer (DE 1), >$100 wages in a quarter", "deferred", None,
     "Informational redirect only."),
    ("info", "worker-class", "ABC test / worker classification", "out_of_scope", None, "Fact-specific legal question."),
]


def load():
    with db.get_conn() as conn:
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
