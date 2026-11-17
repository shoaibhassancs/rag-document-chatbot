"""The demo chat page. UNVERIFIED: Streamlit was not installed when this was written, so this file
has not been run (the client and view layers it uses are tested; the AppTest tests in
tests/test_ui.py are skipped until Streamlit is installed).

Run locally:  uv run streamlit run ui/streamlit_app.py --server.port 8510 --server.address 127.0.0.1
It needs the API running (API_BASE_URL, default http://127.0.0.1:8010). APP_API_KEY (optional) is
read from the environment for upload and delete; it is never shown anywhere.

Safety rule: text that came from a model or a document is drawn ONLY with st.text (plain text),
never with st.markdown or unsafe_allow_html. Only the badges are HTML, built from constants.
"""

import streamlit as st

from ui import view
from ui.api_client import client_from_env

st.set_page_config(page_title="BrightSmile Dental assistant", page_icon="\U0001f9b7", layout="wide")


def draw(blocks: list[view.Block]) -> None:
    for block in blocks:
        if block.kind == "badge":
            st.markdown(view.badge_html(block.badge), unsafe_allow_html=True)  # constants only
        elif block.kind == "text":
            st.text(block.text)
        elif block.kind == "heading":
            st.caption(block.text)
        elif block.kind == "source":
            st.text("• " + block.text)
        elif block.kind == "caption":
            st.caption(block.text)


def reset_conversation() -> None:
    st.session_state.session_id = view.new_session_id()
    st.session_state.history = []


if "session_id" not in st.session_state:
    reset_conversation()

client = client_from_env()
try:
    # ---- header
    st.title("BrightSmile Dental assistant")
    st.info(view.PRIVACY_NOTE)
    st.caption(view.SCOPE_NOTE)

    # ---- sidebar: conversation and documents
    with st.sidebar:
        if st.button("New conversation"):
            reset_conversation()
            st.rerun()

        st.header("Documents")
        st.button("Refresh")  # any interaction reruns the script and reloads the list
        listing = client.list_documents()
        if listing.kind != "ok":
            st.warning(view.docs_error(listing.kind))
        elif not listing.documents:
            st.caption("No documents yet.")
        for doc in listing.documents if listing.kind == "ok" else []:
            st.text(view.format_document(doc))
            doc_id = str(doc.get("id", ""))
            confirmed = st.checkbox("Confirm delete", key=f"confirm-{doc_id}")
            if st.button("Delete", key=f"delete-{doc_id}", disabled=not client.has_key):
                if not confirmed:
                    st.warning("Tick the box first to confirm.")
                else:
                    outcome = client.delete_document(doc_id)
                    if outcome.kind == "ok":
                        st.success("Deleted.")
                        st.rerun()
                    else:
                        st.warning(view.delete_error(outcome.kind))

        st.subheader("Add documents")
        if not client.has_key:
            st.caption(view.FRIENDLY["no_key"])
        files = st.file_uploader(
            "Files (PDF, Markdown, text or HTML)",
            type=["pdf", "md", "txt", "html", "htm"],
            accept_multiple_files=True,
        )
        url = st.text_input("or one web page address")
        if st.button("Add to knowledge base", disabled=not client.has_key):
            payload = [(f.name, f.getvalue()) for f in files or []]
            result = client.ingest(payload, url.strip() or None)
            if result.kind == "ok":
                if any(r.get("status") == "queued" for r in result.results):
                    st.success(view.INGEST_ACCEPTED)
                for line in view.ingest_lines(result.results):
                    st.text(line)
            else:
                st.warning(view.ingest_error(result.kind))

    # ---- chat history
    for message in st.session_state.history:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.text(message["text"])
            else:
                draw(message["blocks"])

    # ---- new question
    prompt = st.chat_input("Ask about prices, opening hours, insurance, aftercare...")
    if prompt is not None:
        problem = view.validate_message(prompt)
        if problem:
            st.warning(problem)
        else:
            text = prompt.strip()
            st.session_state.history.append({"role": "user", "text": text})
            with st.chat_message("user"):
                st.text(text)
            with st.chat_message("assistant"):
                with st.spinner("Looking through our documents..."):
                    outcome = client.chat(st.session_state.session_id, text)
                blocks = view.render_blocks(outcome)
                draw(blocks)
            st.session_state.history.append({"role": "assistant", "blocks": blocks})
finally:
    client.close()
