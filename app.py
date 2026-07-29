"""Step 6 - Chat UI.  Run: streamlit run app.py"""
import streamlit as st

from engine import answer


def md_safe(text):
    """Streamlit's markdown renderer treats a bare $ as a LaTeX math
    delimiter (st.success/st.warning/st.markdown all render markdown) --
    any answer with two or more dollar amounts (e.g. a bracket computation
    stating both the standard deduction and the resulting tax) gets the
    text between them silently reinterpreted as a math expression instead
    of displayed as currency. Escape literal $ so it always renders as
    plain text, which is Streamlit's own documented fix for this."""
    return text.replace("$", "\\$") if text else text

st.set_page_config(page_title="CA tax assistant")
st.title("California tax assistant")
st.caption("CDTFA sales/use tax + FTB income tax -> Postgres/pgvector (two separate "
           "databases) -> Gemini + guard")

tax_type_choice = st.radio(
    "Tax type", ["Auto-detect", "Sales & Use Tax", "Income Tax"], horizontal=True,
    help="A hint, not a hard filter -- if the domain you pick can't answer, "
         "the other one is still tried.",
)
tax_type = {"Auto-detect": None, "Sales & Use Tax": "sales", "Income Tax": "income"}[
    tax_type_choice]

question = st.text_input(
    "Ask a California tax question",
    "How much tax on a $50 restaurant meal in California?",
)

if st.button("Ask") and question.strip():
    with st.spinner("Thinking..."):
        res = answer(question, tax_type=tax_type)
    if res["status"] == "needs_review":
        st.warning(md_safe(res["answer_text"]))
    else:
        st.success(md_safe(res["answer_text"]))

        fees = res.get("fees") or []
        if fees:
            st.markdown("**Plus CDTFA fees (in addition to sales tax):**")
            for f in fees:
                st.markdown(f"- **{f['name']}** — {md_safe(f['detail'])}  \n"
                            f"  <sub>{f['citation']} · as of {f['as_of']}</sub>",
                            unsafe_allow_html=True)

        if res.get("branches"):
            st.info("This answer depends on the situation — see the cases above.")

        with st.expander("Details from the rules engine"):
            st.json({k: res.get(k) for k in
                     ["status", "domain", "category", "taxable", "rate", "amount", "tax",
                      "citation", "source_url", "location", "branches", "fees"]})
