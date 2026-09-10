"""Step 6 - Chat UI.  Run: streamlit run app.py"""
import altair as alt
import pandas as pd
import streamlit as st

import income_db
import payroll_withholding as pw
from engine import answer, _children_label

EXAMPLES = [
    "How much tax on a $50 restaurant meal in California?",
    "How much California tax do I owe on $100,000 self-employed married filing jointly?",
    "What is my CalEITC if I make $9,975 with 2 qualifying children?",
    "Is cannabis taxable in California?",
    "How much California property tax will I owe on a house I bought for $600,000 in 2020?",
    "What is the California disabled veterans' property tax exemption for 2025?",
]


def md_safe(text):
    """Streamlit's markdown renderer treats a bare $ as a LaTeX math
    delimiter (st.success/st.warning/st.markdown all render markdown) --
    any answer with two or more dollar amounts (e.g. a bracket computation
    stating both the standard deduction and the resulting tax) gets the
    text between them silently reinterpreted as a math expression instead
    of displayed as currency. Escape literal $ so it always renders as
    plain text, which is Streamlit's own documented fix for this."""
    return text.replace("$", "\\$") if text else text


st.set_page_config(page_title="CA tax assistant", page_icon="🧾", layout="centered")

st.markdown(
    """
    <style>
    [data-testid="stChatMessage"] { border-radius: 12px; }
    .stChatInput textarea { font-size: 0.95rem; }
    div[data-testid="stMetric"] {
        background: var(--secondary-background-color);
        border-radius: 10px;
        padding: 0.6rem 1rem;
        border: 1px solid rgba(31, 92, 78, 0.15);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🧾 California Tax Assistant")
st.caption("CDTFA sales/use tax + FTB income tax + county-assessor/BOE property tax → "
           "Postgres/pgvector (three separate databases) → Gemini + guard")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "remembered_filing_status" not in st.session_state:
    st.session_state.remembered_filing_status = None
if "remembered_filing_status_label" not in st.session_state:
    st.session_state.remembered_filing_status_label = None
if "remembered_prior_year_agi" not in st.session_state:
    st.session_state.remembered_prior_year_agi = None
if "remembered_qualifying_children_count" not in st.session_state:
    st.session_state.remembered_qualifying_children_count = None
if "remembered_qualifying_children_count_label" not in st.session_state:
    st.session_state.remembered_qualifying_children_count_label = None
if "remembered_exemption_credit_dependent_count" not in st.session_state:
    st.session_state.remembered_exemption_credit_dependent_count = None

# Deliberately OUTSIDE any `with tab_...:` block -- the sidebar is a
# page-level element in Streamlit's layout model, not tab-scoped, so it
# should render regardless of which tab is active rather than being
# nested inside one.
with st.sidebar:
    st.subheader("Options")
    tax_type_choice = st.radio(
        "Tax type",
        ["Auto-detect", "Sales & Use Tax", "Income Tax", "Property Tax"],
        help="A hint, not a hard filter -- if the domain you pick can't "
             "answer, another domain is still tried.",
    )
    tax_type = {"Auto-detect": None, "Sales & Use Tax": "sales", "Income Tax": "income",
                "Property Tax": "property"}[tax_type_choice]

    st.divider()
    st.subheader("Try an example")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True, key=f"ex_{ex}"):
            st.session_state.pending_question = ex

    st.divider()
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    st.divider()
    st.subheader("Filing status")
    if st.session_state.remembered_filing_status:
        st.caption(f"Remembered: **{st.session_state.remembered_filing_status_label}**")
        if st.button("Forget filing status", use_container_width=True):
            st.session_state.remembered_filing_status = None
            st.session_state.remembered_filing_status_label = None
            st.rerun()
    else:
        st.caption("Not stated yet this session.")

    st.divider()
    st.subheader("Prior-year AGI")
    if st.session_state.remembered_prior_year_agi is not None:
        st.caption(f"Remembered: **${st.session_state.remembered_prior_year_agi:,.2f}**")
        if st.button("Forget prior-year AGI", use_container_width=True):
            st.session_state.remembered_prior_year_agi = None
            st.rerun()
    else:
        st.caption("Not stated yet this session.")

    st.divider()
    st.subheader("Qualifying children (CalEITC)")
    if st.session_state.remembered_qualifying_children_count is not None:
        st.caption(f"Remembered: **{st.session_state.remembered_qualifying_children_count_label}**")
        if st.button("Forget qualifying-children count", use_container_width=True):
            st.session_state.remembered_qualifying_children_count = None
            st.session_state.remembered_qualifying_children_count_label = None
            st.rerun()
    else:
        st.caption("Not stated yet this session.")

    st.divider()
    st.subheader("Dependents (exemption credit)")
    if st.session_state.remembered_exemption_credit_dependent_count is not None:
        st.caption(
            f"Remembered: **{st.session_state.remembered_exemption_credit_dependent_count} "
            "dependent(s)**")
        if st.button("Forget dependent count", use_container_width=True):
            st.session_state.remembered_exemption_credit_dependent_count = None
            st.rerun()
    else:
        st.caption("Not stated yet this session.")

tab_chat, tab_paycheck = st.tabs(["Chat", "Paycheck Calculator"])

with tab_chat:
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("result"):
                res = msg["result"]
                if res.get("used_remembered_filing_status"):
                    st.caption(
                        f"Used your remembered filing status "
                        f"({res['remembered_filing_status_label']}) to answer this."
                    )
                if res.get("used_remembered_prior_year_agi"):
                    st.caption(
                        f"Used your remembered prior-year AGI "
                        f"(${res['remembered_prior_year_agi']:,.2f}) to answer this."
                    )
                if res.get("used_remembered_qualifying_children_count"):
                    st.caption(
                        f"Used your remembered qualifying-children count "
                        f"({res['remembered_qualifying_children_count_label']}) to answer this."
                    )
                if res.get("used_remembered_exemption_credit_dependent_count"):
                    st.caption(
                        f"Used your remembered dependent count "
                        f"({res['remembered_exemption_credit_dependent_count_label']}) to answer this."
                    )

                amount_field = "tax" if res.get("tax") is not None else "amount"
                if res.get(amount_field) is not None:
                    st.metric("Estimated amount", f"${res[amount_field]:,.2f}")

                fees = res.get("fees") or []
                if fees:
                    st.markdown("**Plus CDTFA fees (in addition to sales tax):**")
                    for f in fees:
                        st.markdown(
                            f"- **{f['name']}** — {md_safe(f['detail'])}  \n"
                            f"  <sub>{f['citation']} · as of {f['as_of']}</sub>",
                            unsafe_allow_html=True,
                        )

                if res.get("branches"):
                    st.info("This answer depends on the situation — see the cases above.")

                with st.expander("Details from the rules engine"):
                    st.json({k: res.get(k) for k in
                             ["status", "domain", "category", "taxable", "rate", "amount", "tax",
                              "citation", "source_url", "location", "branches", "fees"]})

    pending = st.session_state.pop("pending_question", None)
    question = st.chat_input("Ask a California tax question") or pending

    if question and question.strip():
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                res = answer(
                    question, tax_type=tax_type,
                    remembered_filing_status=st.session_state.remembered_filing_status,
                    remembered_prior_year_agi=st.session_state.remembered_prior_year_agi,
                    remembered_qualifying_children_count=st.session_state.remembered_qualifying_children_count,
                    remembered_exemption_credit_dependent_count=st.session_state.remembered_exemption_credit_dependent_count,
                )

            if res.get("detected_filing_status"):
                st.session_state.remembered_filing_status = res["detected_filing_status"]
                st.session_state.remembered_filing_status_label = res["detected_filing_status_label"]
            if res.get("detected_prior_year_agi") is not None:
                st.session_state.remembered_prior_year_agi = res["detected_prior_year_agi"]
            if res.get("detected_qualifying_children_count") is not None:
                st.session_state.remembered_qualifying_children_count = res["detected_qualifying_children_count"]
                st.session_state.remembered_qualifying_children_count_label = _children_label(
                    res["detected_qualifying_children_count"])
            if res.get("detected_exemption_credit_dependent_count") is not None:
                st.session_state.remembered_exemption_credit_dependent_count = res[
                    "detected_exemption_credit_dependent_count"]

            display_text = md_safe(res["answer_text"])
            if res["status"] == "needs_review":
                st.warning(display_text)
            else:
                st.success(display_text)

            if res.get("used_remembered_filing_status"):
                st.caption(
                    f"Used your remembered filing status "
                    f"({res['remembered_filing_status_label']}) to answer this."
                )
            if res.get("used_remembered_prior_year_agi"):
                st.caption(
                    f"Used your remembered prior-year AGI "
                    f"(${res['remembered_prior_year_agi']:,.2f}) to answer this."
                )
            if res.get("used_remembered_qualifying_children_count"):
                st.caption(
                    f"Used your remembered qualifying-children count "
                    f"({res['remembered_qualifying_children_count_label']}) to answer this."
                )
            if res.get("used_remembered_exemption_credit_dependent_count"):
                st.caption(
                    f"Used your remembered dependent count "
                    f"({res['remembered_exemption_credit_dependent_count_label']}) to answer this."
                )

            amount_field = "tax" if res.get("tax") is not None else "amount"
            if res.get(amount_field) is not None:
                st.metric("Estimated amount", f"${res[amount_field]:,.2f}")

            fees = res.get("fees") or []
            if fees:
                st.markdown("**Plus CDTFA fees (in addition to sales tax):**")
                for f in fees:
                    st.markdown(
                        f"- **{f['name']}** — {md_safe(f['detail'])}  \n"
                        f"  <sub>{f['citation']} · as of {f['as_of']}</sub>",
                        unsafe_allow_html=True,
                    )

            if res.get("branches"):
                st.info("This answer depends on the situation — see the cases above.")

            with st.expander("Details from the rules engine"):
                st.json({k: res.get(k) for k in
                         ["status", "domain", "category", "taxable", "rate", "amount", "tax",
                          "citation", "source_url", "location", "branches", "fees"]})

        st.session_state.messages.append(
            {"role": "assistant", "content": display_text, "result": res})

with tab_paycheck:
    st.subheader("Per-Paycheck Withholding Calculator")
    st.caption(
        f"Estimates federal + California withholding for ONE paycheck "
        f"({pw.PAYROLL_TAX_YEAR} IRS Pub 15-T / EDD Method B tables) -- "
        "not your annual tax liability. See the assumptions listed with "
        "every result before relying on it.")

    # OUTSIDE the form, deliberately: st.form batches ALL its own widgets
    # and does not rerun the script until submit, so a radio INSIDE the
    # form can't dynamically change what options another widget in the
    # SAME form offers (confirmed live -- selecting "Before 2020" while
    # this radio lived inside the form left Head of Household visible in
    # the filing-status dropdown until the next submit). Living outside
    # the form, this radio reruns the script immediately on change, so the
    # filing-status options below are correctly filtered before the user
    # ever opens that dropdown.
    w4_version = st.radio(
        "W-4 on file", ["2020 or later", "Before 2020"], horizontal=True,
        help="A pre-2020 W-4 has no Head-of-Household option on the actual form.")
    w4_is_2020_or_later = (w4_version == "2020 or later")

    with st.form("paycheck_form"):
        st.markdown("#### Earnings")
        c1, c2 = st.columns(2)
        with c1:
            gross_pay = st.number_input(
                "Gross pay this period ($)", min_value=0.0, step=100.0, format="%.2f")
        with c2:
            freq_label = st.selectbox("Pay frequency", list(pw.PAY_FREQUENCY_LABELS.values()))

        st.markdown("#### Federal (Form W-4)")
        status_options = (
            list(pw.FILING_STATUS_LABELS.values()) if w4_is_2020_or_later
            else [v for k, v in pw.FILING_STATUS_LABELS.items() if k != "hoh"])
        filing_label = st.selectbox("Filing status", status_options)

        if w4_is_2020_or_later:
            step2 = st.checkbox("Step 2: multiple jobs / spouse works box is checked")
            step3 = st.number_input(
                "Step 3: dependent/other credits (annual $)", min_value=0.0, step=100.0)
            step4a = st.number_input(
                "Step 4(a): other income (annual $)", min_value=0.0, step=100.0)
            step4b = st.number_input(
                "Step 4(b): deductions (annual $)", min_value=0.0, step=100.0)
            step4c = st.number_input(
                "Step 4(c): extra withholding (per period $)", min_value=0.0, step=10.0)
            pre2020_allowances = 0
        else:
            pre2020_allowances = st.number_input(
                "Withholding allowances claimed", min_value=0, step=1)
            step2, step3, step4a, step4b, step4c = False, 0.0, 0.0, 0.0, 0.0

        st.markdown("#### California (Form DE-4)")
        ca_reg = st.number_input("Regular allowances (DE-4 Line 1)", min_value=0, step=1)
        ca_est = st.number_input(
            "Estimated deduction allowances (DE-4 Line 2 / Worksheet B)", min_value=0, step=1)

        submitted = st.form_submit_button("Calculate paycheck")

    if submitted:
        label_to_freq = {v: k for k, v in pw.PAY_FREQUENCY_LABELS.items()}
        label_to_status = {v: k for k, v in pw.FILING_STATUS_LABELS.items()}
        with income_db.get_conn() as conn:
            st.session_state.paycheck_result = pw.compute_paycheck(
                conn, gross_pay=gross_pay, pay_frequency=label_to_freq[freq_label],
                filing_status=label_to_status[filing_label],
                w4_is_2020_or_later=w4_is_2020_or_later,
                step2_checkbox=step2, step3_dependent_credits=step3,
                step4a_other_income=step4a, step4b_deductions=step4b,
                step4c_extra_withholding=step4c, pre2020_allowances=pre2020_allowances,
                ca_regular_allowances=ca_reg, ca_estimated_deduction_allowances=ca_est,
            )

    result = st.session_state.get("paycheck_result")
    if result is None:
        st.info("Fill in the form above and click Calculate.")
    else:
        st.markdown("#### Results")
        m1, m2, m3 = st.columns(3)
        m1.metric("Gross pay", f"${result['gross_pay']:,.2f}")
        m2.metric("Total taxes", f"${result['total_taxes']:,.2f}")
        m3.metric("Take-home pay", f"${result['take_home_pay']:,.2f}")

        rows = [
            ("Take-home pay", result["take_home_pay"]),
            ("Federal withholding", result["federal_withholding"]),
            ("Social Security", result["social_security"]),
            ("Medicare", result["medicare"]),
            ("Additional Medicare", result["additional_medicare"]),
            ("CA withholding", result["ca_withholding"]),
            ("CA SDI", result["ca_sdi"]),
        ]
        chart_df = pd.DataFrame([r for r in rows if r[1] > 0], columns=["category", "amount"])
        if chart_df.empty:
            # Every component rounded to $0 (e.g. a very small gross pay) --
            # Vega-Lite can't compute a meaningful angle domain from zero
            # slices, so skip the chart rather than pass it degenerate data.
            st.info("No nonzero components to chart for this paycheck.")
        else:
            donut = alt.Chart(chart_df).mark_arc(innerRadius=70).encode(
                theta="amount",
                color=alt.Color("category:N", legend=alt.Legend(title="")),
                tooltip=["category", alt.Tooltip("amount:Q", format="$,.2f")],
            ).properties(height=320)
            st.altair_chart(donut, use_container_width=True)

        st.markdown("**Assumptions in this estimate:**")
        for a in result.get("assumptions", []):
            st.caption(f"- {md_safe(a)}")

        with st.expander("Details from the withholding engine"):
            st.json(result)
