"""AppTest tests for the Streamlit page. UNVERIFIED: written while Streamlit was not installed, so
they have never run. They are skipped until Streamlit is installed (a morning item); when they run
for the first time, expect to adjust details to the installed Streamlit version.

The HTTP API is replaced by a mock transport; `ui.api_client.client_from_env` is monkeypatched, so
no network is used.
"""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from ui.api_client import ApiClient  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "ui" / "streamlit_app.py")
KEY = "fake-ui-secret-value"
DOC_ID = str(uuid4())
EVIL = (
    "![x](http://example.invalid/x.png) [link](http://example.invalid) "
    '<img src="http://example.invalid/x.png">'
)


class FakeApi:
    def __init__(self):
        self.chat_reply = httpx.Response(
            200,
            json={
                "answer": "Yes, we accept Delta Dental.",
                "status": "answered",
                "sources": [
                    {"file": "insurance.pdf", "page": 2, "section": None, "document_id": "d"}
                ],
                "latency_ms": 1500,
            },
        )
        self.documents = [
            {
                "id": DOC_ID,
                "name": "faq.md",
                "source_type": "markdown",
                "status": "ready",
                "chunks": 4,
                "chunks_done": 4,
                "error": None,
            }
        ]
        self.ingest_reply = httpx.Response(
            202, json={"results": [{"name": "u", "status": "queued", "chunks": 1}]}
        )
        self.delete_reply = httpx.Response(204)
        self.down = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused")
        path, method = request.url.path, request.method
        if (method, path) == ("POST", "/chat"):
            return self.chat_reply
        if (method, path) == ("GET", "/documents"):
            return httpx.Response(
                200, json={"documents": self.documents, "total": len(self.documents)}
            )
        if (method, path) == ("POST", "/ingest"):
            return self.ingest_reply
        if method == "DELETE":
            return self.delete_reply
        return httpx.Response(404, json={})

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(
        "ui.api_client.client_from_env",
        lambda: ApiClient("http://api.test", KEY, transport=httpx.MockTransport(fake)),
    )
    return fake


def ask(message: str = "Do you accept Delta Dental?") -> AppTest:
    at = AppTest.from_file(APP, default_timeout=15).run()
    at.chat_input[0].set_value(message).run()
    return at


def everything(at: AppTest) -> str:
    """All text on the page, for the 'nothing leaks' checks."""
    parts = (
        [e.value for e in at.markdown] + [e.value for e in at.text] + [e.value for e in at.caption]
    )
    parts += (
        [e.value for e in at.info] + [e.value for e in at.warning] + [e.value for e in at.error]
    )
    parts += [e.value for e in at.success]
    return "\n".join(str(p) for p in parts)


def test_the_page_always_shows_the_personal_information_note(api):
    at = AppTest.from_file(APP, default_timeout=15).run()
    assert not at.exception
    assert any("do not enter personal information" in i.value for i in at.info)


def test_an_answered_reply_shows_its_badge_text_and_sources(api):
    at = ask()
    page = everything(at)
    assert "Answered" in page and "Yes, we accept Delta Dental." in page
    assert "insurance.pdf, page 2" in page and "Sources" in page


def test_a_clarify_reply_shows_the_question_and_no_sources(api):
    api.chat_reply = httpx.Response(
        200,
        json={"answer": "Which treatment?", "status": "clarify", "sources": [], "latency_ms": 900},
    )
    page = everything(ask("How much is it?"))
    assert "One quick question" in page and "Which treatment?" in page and "Sources" not in page


def test_a_refused_reply_has_its_own_badge(api):
    api.chat_reply = httpx.Response(
        200,
        json={
            "answer": "I couldn't find that in our documents.",
            "status": "refused",
            "sources": [],
            "latency_ms": 700,
        },
    )
    page = everything(ask("lawyer?"))
    assert "Not in our documents" in page and "Answered" not in page


def test_a_503_error_reply_shows_the_busy_message(api):
    api.chat_reply = httpx.Response(
        503,
        json={
            "answer": "The assistant is busy right now.",
            "status": "error",
            "sources": [],
            "latency_ms": 20,
        },
    )
    page = everything(ask())
    assert "Service busy" in page and "The assistant is busy right now." in page


def test_an_unreachable_api_shows_a_friendly_message_and_no_traceback(api):
    api.down = True
    at = ask()
    page = everything(at)
    assert "Can't reach the assistant" in page and not at.exception
    for word in ("Traceback", "httpx", "ConnectError", "refused", "Exception"):
        assert word not in page


def test_no_key_and_no_raw_error_text_ever_appear_on_the_page(api):
    api.chat_reply = httpx.Response(500, text=f"boom {KEY} <html>")
    at = ask()
    page = everything(at)
    assert KEY not in page and "boom" not in page and "<html>" not in page


def test_hostile_markdown_and_html_in_an_answer_stay_plain_text(api):
    api.chat_reply = httpx.Response(
        200, json={"answer": EVIL, "status": "answered", "sources": [], "latency_ms": 1}
    )
    at = ask()
    assert any("example.invalid" in e.value for e in at.text)  # shown as characters
    assert not any("example.invalid" in e.value for e in at.markdown)  # never parsed as markdown


def test_the_session_id_is_stable_within_a_session_and_changes_after_new_conversation(api):
    at = ask("first question")
    first = at.session_state["session_id"]
    at.chat_input[0].set_value("second question").run()
    assert at.session_state["session_id"] == first
    sent = [json.loads(r.content)["session_id"] for r in api.sent("POST", "/chat")]
    assert sent == [first, first]
    at.sidebar.button[0].click().run()  # "New conversation"
    assert at.session_state["session_id"] != first and at.session_state["history"] == []


def test_two_sessions_get_different_ids(api):
    a = AppTest.from_file(APP, default_timeout=15).run()
    b = AppTest.from_file(APP, default_timeout=15).run()
    assert a.session_state["session_id"] != b.session_state["session_id"]


def test_a_message_over_1000_characters_is_not_sent(api):
    at = ask("x" * 1001)
    assert api.sent("POST", "/chat") == []
    assert any("too long" in w.value for w in at.warning)


def test_a_blank_message_is_not_sent(api):
    ask("   ")
    assert api.sent("POST", "/chat") == []


def test_delete_needs_the_confirm_box_then_calls_delete_with_the_key_header(api):
    at = AppTest.from_file(APP, default_timeout=15).run()
    delete = next(b for b in at.sidebar.button if b.key == f"delete-{DOC_ID}")
    delete.click().run()
    assert api.sent("DELETE", f"/documents/{DOC_ID}") == []  # not confirmed yet
    next(c for c in at.sidebar.checkbox if c.key == f"confirm-{DOC_ID}").check().run()
    next(b for b in at.sidebar.button if b.key == f"delete-{DOC_ID}").click().run()
    calls = api.sent("DELETE", f"/documents/{DOC_ID}")
    assert len(calls) == 1 and calls[0].headers["x-api-key"] == KEY


def test_adding_a_web_page_calls_ingest_with_the_key_header(api):
    # AppTest cannot drive file_uploader in every version, so this uses the URL box.
    at = AppTest.from_file(APP, default_timeout=15).run()
    at.sidebar.text_input[0].set_value("https://example.test/page").run()
    next(b for b in at.sidebar.button if b.label == "Add to knowledge base").click().run()
    calls = api.sent("POST", "/ingest")
    assert len(calls) == 1 and calls[0].headers["x-api-key"] == KEY
    assert "Accepted" in everything(at) or any("Accepted" in s.value for s in at.sidebar.success)


def test_without_a_key_upload_and_delete_are_disabled(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(
        "ui.api_client.client_from_env",
        lambda: ApiClient("http://api.test", None, transport=httpx.MockTransport(fake)),
    )
    at = AppTest.from_file(APP, default_timeout=15).run()
    buttons = {b.label: b for b in at.sidebar.button}
    assert buttons["Add to knowledge base"].disabled
    assert all(b.disabled for b in at.sidebar.button if b.label == "Delete")
    assert "APP_API_KEY" in everything(at) or any(
        "APP_API_KEY" in c.value for c in at.sidebar.caption
    )


def test_an_unreachable_api_still_renders_the_page_with_a_friendly_document_message(api):
    api.down = True
    at = AppTest.from_file(APP, default_timeout=15).run()
    assert not at.exception
    assert any("document list" in w.value for w in at.sidebar.warning)
