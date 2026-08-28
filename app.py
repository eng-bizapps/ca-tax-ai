"""Step 6 - Chat UI.  Run: streamlit run app.py"""
import streamlit as st

from engine import answer

EXAMPLES = [
    "How much tax on a $50 restaurant meal in California?",
    "How much California tax do I owe on $100,000 self-employed married filing jointly?",
    "What is my CalEITC if I make $9,975 with 2 qualifying children?",
    "Is cannabis taxable in California?",
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
st.caption("CDTFA sales/use tax + FTB income tax → Postgres/pgvector (two separate "
           "databases) → Gemini + guard")

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.subheader("Options")
    tax_type_choice = st.radio(
        "Tax type",
        ["Auto-detect", "Sales & Use Tax", "Income Tax"],
        help="A hint, not a hard filter -- if the domain you pick can't "
             "answer, the other one is still tried.",
    )
    tax_type = {"Auto-detect": None, "Sales & Use Tax": "sales", "Income Tax": "income"}[
        tax_type_choice]

    st.divider()
    st.subheader("Try an example")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True, key=f"ex_{ex}"):
            st.session_state.pending_question = ex

    st.divider()
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("result"):
            res = msg["result"]
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
            res = answer(question, tax_type=tax_type)

        display_text = md_safe(res["answer_text"])
        if res["status"] == "needs_review":
            st.warning(display_text)
        else:
            st.success(display_text)

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
