"""Informational EDD data for the employer domain -- Ring 5: which payments are
subject wages (EDD's Types of Payments sheet, DE 231TP) and the registration
rules for a new employer (DE 44).

Deliberately a curated subset of DE 231TP, not all of it. That sheet is Rev. 1
(6-16), a 2016 edition; rows whose law has since changed (moving expenses,
HSA contributions, the domestic-partner federal-tax row, adult-child health
coverage) are left out, and dollar limits that are indexed (dependent care,
educational assistance, achievement awards) are described by the Internal
Revenue Code section instead of a number. Rows kept agree with DE 44 Rev. 52
(4-26)'s own "What Are Wages?" section. Every answer built from this table
names the sheet's edition.

Usage:
  python employer_payment_types.py create   # create tables (idempotent)
  python employer_payment_types.py load     # upsert rows
  python employer_payment_types.py status
"""
import re
import sys

import employer_db as db

DE231TP = "EDD Types of Payments (DE 231TP, Rev. 1, 6-16)"
DE231TP_URL = "https://edd.ca.gov/pdf_pub_ctr/de231tp.pdf"
DE44 = "EDD DE 44 Employer's Guide, Rev. 52 (4-26)"
DE44_URL = "https://edd.ca.gov/pdf_pub_ctr/de44.pdf"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ca_payment_types (
    payment_key    TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    aliases        TEXT NOT NULL,
    excludes       TEXT,
    ui_ett         TEXT NOT NULL,
    sdi            TEXT NOT NULL,
    pit_withholding TEXT NOT NULL,
    pit_wages      TEXT NOT NULL,
    conditions     TEXT,
    code_refs      TEXT,
    citation       TEXT,
    source_url     TEXT
);

CREATE TABLE IF NOT EXISTS ca_employer_rules (
    rule_key    TEXT PRIMARY KEY,
    value       NUMERIC NOT NULL,
    unit        TEXT NOT NULL,
    description TEXT,
    citation    TEXT,
    source_url  TEXT
);
"""

S, N = "subject", "not subject"
R, NR = "reportable", "not reportable"

# (key, label, aliases, excludes, ui_ett, sdi, pit_withholding, pit_wages, conditions, code_refs)
PAYMENT_ROWS = [
    ("bonus", "Bonuses", "bonus,bonuses", "bonus depreciation", S, S, S, R, None,
     "CUIC 926, 13009, 13009.5"),
    ("commission", "Commissions", "commission,commissions", None, S, S, S, R, None,
     "CUIC 926, 13009, 13009.5"),
    ("supplemental", "Supplemental wages (overtime, sales awards, etc.)",
     "supplemental wage,supplemental wages,overtime,sales award", None, S, S, S, R, None,
     "CUIC 926, 13009, 13009.5"),
    ("severance", "Dismissal or severance pay", "severance,dismissal pay,separation pay", None,
     "conditional", "conditional", S, R,
     "Subject to UI/ETT and SDI unless the conditions of CUIC 1265 are met.",
     "CUIC 926, 1265, 13009(q), 13009.5"),
    ("vacation_paid", "Vacation, holiday or PTO pay paid while employed",
     "vacation pay,holiday pay,pto,paid time off,paid vacation", "accrued,unused,payout,terminat",
     S, S, S, R,
     "Ordinary vacation or holiday pay is wages; DE 44 lists vacation pay among supplemental wages.",
     "CUIC 926, 13009; DE 44 What Are Wages?"),
    ("vacation_termination", "Vacation, sick or holiday pay earned but unpaid at termination",
     "accrued vacation,unused vacation,vacation payout,pto payout,accrued pto,unused pto,accrued sick,unused sick,final paycheck vacation",
     None, N, N, S, R,
     "Applies only to pay earned but not paid before termination by voluntary quit, discharge, or layoff due to lack of "
     "work with no specific return date; it is still subject to PIT withholding.",
     "CUIC 1265.5, 1265.6, 1265.7, 13009, 13009.5"),
    ("sick_pay", "Sick pay while still employed", "sick pay,sick leave,paid sick leave", "accrued,unused,third-party,third party",
     S, S, S, R, None, "CUIC 931, 13009, 13009.5"),
    ("expense_reimbursement", "Reimbursement of required or necessary business expenses",
     "expense reimbursement,reimbursement,reimburse,reimbursed,business expense,business expenses", "long-term",
     N, N, N, NR,
     "Long-term travel expenses (allowed under IRC 162) are the exception: subject to all four.",
     "CUIC 929, 13009(q), 13009.5"),
    ("tips", "Tips", "tip,tips,gratuity,gratuities", None, "conditional", "conditional", "conditional", "conditional",
     "Cash tips of $20 or more in a month that the employee reports to you in writing are subject to all four. Banquet "
     "and other employer-controlled tips are regular wages and are subject whether or not the employee reports them. "
     "Noncash tips are not subject and not reportable.",
     "CUIC 926, 927, 13009, 13009(n), 13009.5; 22 CCR 927-1"),
    ("meals", "Meals provided to an employee", "meals,meal allowance,free lunch", None, S, S, "conditional", "conditional",
     "Subject to UI/ETT and SDI. Not subject to PIT withholding and not reportable if furnished for the employer's "
     "convenience and on the employer's premises. 2026 values when no contract amount applies (DE 44): $15.85 for "
     "three meals a day; breakfast $3.25, lunch $4.90, dinner $7.70, unidentified meal $5.70.",
     "CUIC 926; 22 CCR 4309-1(b)(9); CUIC 13009.5; DE 44"),
    ("lodging", "Lodging provided to an employee", "lodging,housing provided,employer-provided housing,free housing",
     None, S, S, "conditional", "conditional",
     "Subject to UI/ETT and SDI. Not subject to PIT withholding and not reportable if furnished on the employer's "
     "premises, for the employer's convenience, and as a condition of employment. 2026 value when no contract amount "
     "applies (DE 44): 66 2/3% of ordinary rental value, at least $66.10 a week and at most $2,038 a month.",
     "CUIC 926; 22 CCR 4309-1(b)(9); CUIC 13009.5; DE 44"),
    ("gifts_nominal", "Gifts of nominal value",
     "nominal gift,small gift,holiday gift,token gift", "gift card,gift certificate,cash,gift cards", N, N, N, NR,
     "Only a gift of nominal value given as an expression of goodwill, not based on pay or length or degree of service "
     "and not required by a union agreement or contract of hire. Cash and cash equivalents are not covered by this row.",
     "CUIC 926, 13009(q), 13009.5; 22 CCR 926-2"),
    ("stock_options", "Stock options", "stock option,stock options,iso,nso,espp,incentive stock option,employee stock purchase",
     None, "conditional", "conditional", "conditional", "conditional",
     "Depends on the type. Incentive stock options and employee stock purchase plan shares: not subject to UI/ETT, SDI "
     "or PIT withholding; reportable as PIT wages only on a disqualifying disposition. Nonstatutory options: subject "
     "to all four when granted if they have a readily ascertainable fair market value, otherwise when exercised. "
     "California qualified stock options (RTC 17502): not subject and not reportable.",
     "CUIC 928, 13003(b)(4), 13009; DE 231SK"),
    ("401k_employee", "Employee 401(k) salary deferrals", "401(k),401k,salary deferral,elective deferral,cash or deferred",
     None, S, S, N, NR,
     "UI/ETT and SDI are subject unless the benefit is under a Section 125 cafeteria plan (CUIC 938.3(d)). PIT is not "
     "withheld and not reportable if the deferral is not includable in gross income for California income tax.",
     "CUIC 926, 938.3(d), 13003(b)(4), 13009(i)(1), 13009(q), 13009.5; DE 44"),
    ("roth", "Employee Roth contributions", "roth,roth 401(k),roth contribution", None, S, S, S, R, None,
     "CUIC 926, 13009, 13009.5"),
    ("health_plan", "Employer-provided health coverage",
     "health insurance,health plan,medical coverage,accident or health,employer-paid health", "domestic partner",
     N, N, N, NR,
     "PIT is subject and reportable for 2% shareholder-employees of S corporations.",
     "CUIC 931, 13009(q), 13009.5"),
    ("educational_assistance", "Educational assistance", "educational assistance,tuition assistance,tuition reimbursement",
     None, N, N, N, NR,
     "Not subject or reportable only if excludable from gross income under IRC 127 (within that section's annual limit).",
     "CUIC 938.3(a), 13009(q), 13009.5"),
    ("dependent_care", "Dependent care assistance", "dependent care,child care assistance,daycare benefit", None,
     N, N, N, NR,
     "Not subject or reportable only if excludable from gross income under IRC 129 (within that section's limit).",
     "CUIC 938.3(b), 13009(q), 13009.5"),
    ("achievement_awards", "Employee achievement awards", "achievement award,service award,length of service award",
     None, N, N, N, NR,
     "Not subject or reportable only if excludable under IRC 74(c) (within the qualified and nonqualified plan limits).",
     "CUIC 938.3(c), 13009(q), 13009.5"),
    ("group_term_life", "Group-term life insurance premiums paid for employees",
     "group term life,group-term life,life insurance", None, N, N, N, "conditional",
     "Not subject to UI/ETT, SDI or PIT withholding; reportable as PIT wages only if the face value of group-term "
     "coverage exceeds $50,000 (IRC 79).",
     "CUIC 931(c), 13009(l), 13009.5(a)(2); IRC 79"),
    ("vehicle_personal_use", "Personal use of an employer vehicle",
     "company car,employer vehicle,personal use of vehicle,personal use of a company vehicle", None, S, S, S, R, None,
     "CUIC 926, 13009, 13009.5"),
    ("employer_paid_fica", "Employer-paid FICA, SDI or Medicare for the employee",
     "employer-paid fica,pay the employee's fica,employer pays employee's sdi,employer-paid sdi", None, S, S, S, R, None,
     "CUIC 926, 13009, 13009.5; DE 231Q"),
    ("irc132_fringe", "Fringe benefits excluded under IRC 132 (discounts, parking, bus passes, free coffee)",
     "employee discount,parking benefit,transit pass,bus pass,free coffee,working condition fringe", None, N, N, N, NR,
     "Only benefits that are actually excluded from gross income under IRC 132.",
     "CUIC 938.3(c), 13009(q), 13009.5"),
    ("family_payments", "Payments to a family member who works for you",
     "pay my spouse,pay my wife,pay my husband,employ my spouse,employ my wife,employ my husband,pay my child,"
     "employ my child,my son works,my daughter works,employ my parent,family employee,family employees,"
     "registered domestic partner",
     None, N, N, S, R,
     "Applies to: a child under 18 employed by their own parent (or a partnership of only the parents); a spouse "
     "employed by their spouse; a registered domestic partner employed by their partner; a parent employed by their own "
     "son or daughter. Not subject to UI/ETT or SDI, but subject to PIT and reportable as PIT wages.",
     "CUIC 631, 13009, 13009.5; DE 231TE, DE 231FAM"),
    ("director_fees", "Board of director fees", "director fees,board fees,director's fees", None, N, N, "conditional",
     "conditional",
     "Not subject to UI/ETT or SDI. Subject to PIT withholding and reportable if the director is a common-law employee.",
     "CUIC 622, 13009"),
    ("salary_advance", "Salary advances", "salary advance,advance on pay,paycheck advance,pay advance", None, S, S, S, R,
     "Wages when the advance is paid, not when it is later deducted from future pay.",
     "CUIC 926, 13009, 13009.5"),
    ("workers_comp", "Workers' compensation payments", "workers' compensation,workers compensation,workers comp", None,
     N, N, N, NR, None, "CUIC 931(a), 13009(q), 13009.5"),
    ("deceased_wages", "Wages paid to a deceased employee's estate or survivor",
     "deceased employee,paid after death,death of an employee,died", None, "conditional", "conditional", N, NR,
     "Subject to UI/ETT and SDI only in the calendar year in which the employee died; not subject to PIT withholding "
     "and not reportable.",
     "CUIC 938.7, 13009(q), 13009.5"),
    ("debts_paid", "Debts of the employee paid by the employer in lieu of wages", "debt paid by employer,pay off employee's debt,paid debt for employee",
     None, S, S, S, R, None, "CUIC 926, 13009, 13009.5"),
    ("warn_payments", "WARN Act payments (no advance notice of a facility closure)", "warn act,warn payment,plant closure pay",
     None, N, N, S, R, None, "CUIC 1265.1, 13009(q), 13009.5"),
    ("sub_plan", "Supplemental unemployment compensation plan benefits", "supplemental unemployment benefit,sub pay,sub plan",
     None, N, N, S, R, None, "CUIC 1265, 13028.5, 13009.5"),
    ("scholarships", "Scholarships and fellowships excludable under IRC 117", "scholarship,fellowship,stipend award",
     None, N, N, N, NR, "Only amounts excludable from gross income under IRC 117.", "CUIC 938.3(c), 13009(q), 13009.5"),
    ("cafeteria_plan", "Cafeteria (IRC 125) plan payments",
     "cafeteria plan,section 125,flexible spending,fsa", None, N, N, N, NR,
     "Generally not subject; reportable only if not excluded from taxable income.",
     "CUIC 938.3(d), 13009(i)(5), 13009.5"),
]

# (rule_key, value, unit, description, citation)
RULE_ROWS = [
    ("subject_employer_quarterly_wages", 100, "dollars",
     "You become a subject employer when you pay wages OVER this amount in any calendar quarter", DE44),
    ("register_within_days", 15, "days", "Register with EDD within this many days of paying the wages that make you a subject employer", DE44),
    ("de34_within_days", 20, "days", "Report a new or rehired employee within this many calendar days of the start-of-work date", DE44),
    ("de542_within_days", 20, "days", "Report an independent contractor within this many calendar days of paying or contracting for the threshold amount", DE44),
    ("de542_threshold", 600, "dollars", "Independent-contractor reporting threshold (payments or contract of this amount or more)", DE44),
    ("rehire_separation_days", 60, "days", "A rehire after a separation of at least this many consecutive days must be reported on a DE 34", DE44),
]


def create():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
    status()


def load():
    with db.get_conn() as conn:
        conn.execute(SCHEMA)
        keep = [r[0] for r in PAYMENT_ROWS]
        conn.execute("DELETE FROM ca_payment_types WHERE payment_key <> ALL(%s)", (keep,))
        for key, label, aliases, excludes, ui, sdi, pitw, pitr, cond, refs in PAYMENT_ROWS:
            conn.execute(
                "INSERT INTO ca_payment_types (payment_key, label, aliases, excludes, ui_ett, sdi, pit_withholding, "
                "pit_wages, conditions, code_refs, citation, source_url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (payment_key) DO UPDATE SET label=EXCLUDED.label, aliases=EXCLUDED.aliases, "
                "excludes=EXCLUDED.excludes, ui_ett=EXCLUDED.ui_ett, sdi=EXCLUDED.sdi, "
                "pit_withholding=EXCLUDED.pit_withholding, pit_wages=EXCLUDED.pit_wages, "
                "conditions=EXCLUDED.conditions, code_refs=EXCLUDED.code_refs, citation=EXCLUDED.citation, "
                "source_url=EXCLUDED.source_url",
                (key, label, aliases, excludes, ui, sdi, pitw, pitr, cond, refs, DE231TP, DE231TP_URL))
        for key, value, unit, desc, cite in RULE_ROWS:
            conn.execute(
                "INSERT INTO ca_employer_rules (rule_key, value, unit, description, citation, source_url) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (rule_key) DO UPDATE SET value=EXCLUDED.value, "
                "unit=EXCLUDED.unit, description=EXCLUDED.description, citation=EXCLUDED.citation",
                (key, value, unit, desc, cite, DE44_URL))
    status()


def status():
    conn = db.get_conn()
    for tbl in ("ca_payment_types", "ca_employer_rules"):
        try:
            n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        except Exception:
            n = "(not created)"
        print(f"  {tbl:22} {n} rows")
    conn.close()


def _alias_hit(text, alias):
    return re.search(r"(?<![a-z0-9])" + re.escape(alias.strip()) + r"(?![a-z0-9])", text) is not None


def find_payment_types(conn, text):
    """Rows whose alias appears in text (whole-word match) and whose exclude
    terms do not. Longest matching alias wins ordering so the more specific
    row comes first (e.g. 'accrued vacation' before 'vacation pay')."""
    text = text.lower()
    hits = []
    for row in conn.execute("SELECT payment_key, label, aliases, excludes, ui_ett, sdi, pit_withholding, pit_wages, "
                            "conditions, code_refs, citation, source_url FROM ca_payment_types").fetchall():
        excludes = [e for e in (row[3] or "").split(",") if e.strip()]
        if any(e.strip() in text for e in excludes):
            continue
        matched = [a for a in row[2].split(",") if a.strip() and _alias_hit(text, a)]
        if matched:
            hits.append((max(len(a) for a in matched), row))
    hits.sort(key=lambda h: -h[0])
    keys = ("key", "label", "aliases", "excludes", "ui_ett", "sdi", "pit_withholding", "pit_wages",
            "conditions", "code_refs", "citation", "source_url")
    return [dict(zip(keys, h[1])) for h in hits]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"create": create, "load": load}.get(cmd, status)()
