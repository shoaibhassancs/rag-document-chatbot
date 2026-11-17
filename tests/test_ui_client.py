"""The UI's HTTP client against a mocked API. No Streamlit needed, no network."""

import json
from uuid import uuid4

import httpx
import pytest

from ui.api_client import DEFAULT_BASE_URL, ApiClient, client_from_env

KEY = "fake-ui-secret-value"
DOC_ID = str(uuid4())


class Api:
    """A scripted API: each call pops the next reply (a Response or an exception)."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(api: Api, key: str | None = KEY) -> ApiClient:
    return ApiClient("http://api.test", key, transport=httpx.MockTransport(api))


def reply(status=200, body=None):
    return httpx.Response(status, json=body if body is not None else {})


def chat_body(status="answered", answer="Yes.", sources=None, latency=1234):
    return {"answer": answer, "status": status, "sources": sources or [], "latency_ms": latency}


# ----------------------------------------------------------------------------- chat


def test_chat_posts_the_session_and_message_without_the_key():
    api = Api(reply(200, chat_body(sources=[{"file": "a.pdf", "page": 2}])))
    out = client(api).chat("ui-abc", "hello?")
    request = api.requests[0]
    assert (request.method, request.url.path) == ("POST", "/chat")
    assert json.loads(request.content) == {"session_id": "ui-abc", "message": "hello?"}
    assert "x-api-key" not in request.headers
    assert (out.kind, out.status, out.answer, out.latency_ms) == ("ok", "answered", "Yes.", 1234)
    assert out.sources == [{"file": "a.pdf", "page": 2}]


@pytest.mark.parametrize("status", ["answered", "refused", "clarify"])
def test_each_status_comes_through_with_http_200(status):
    assert client(Api(reply(200, chat_body(status)))).chat("s", "q").status == status


def test_a_503_with_status_error_is_a_normal_reply_with_the_busy_message():
    out = client(Api(reply(503, chat_body("error", "The assistant is busy."))))
    reply_ = out.chat("s", "q")
    assert (reply_.kind, reply_.status, reply_.answer) == ("ok", "error", "The assistant is busy.")


def test_a_503_without_a_readable_body_is_unexpected_not_a_crash():
    api = Api(httpx.Response(503, text="<html>bad gateway</html>"))
    assert client(api).chat("s", "q").kind == "unexpected"


def test_a_422_is_bad_input():
    assert client(Api(reply(422, {"detail": "x"}))).chat("s", "q").kind == "bad_input"


@pytest.mark.parametrize("code", [400, 401, 404, 500, 502])
def test_other_http_codes_are_unexpected(code):
    assert client(Api(reply(code, {}))).chat("s", "q").kind == "unexpected"


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("closed"),
    ],
)
def test_connection_problems_and_timeouts_are_unreachable(error):
    assert client(Api(error)).chat("s", "q").kind == "unreachable"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"answer": "x"},
        {"status": "answered"},
        {"answer": 5, "status": "answered"},
        {"answer": "x", "status": "weird"},
        {"answer": "x", "status": "answered", "sources": "nope"},
        [1, 2],
    ],
)
def test_a_reply_without_the_expected_fields_is_unexpected(body):
    assert client(Api(reply(200, body))).chat("s", "q").kind == "unexpected"


def test_non_dict_sources_and_a_bad_latency_are_dropped_quietly():
    body = chat_body(sources=[{"file": "a.md"}, "junk", 3], latency="fast")
    out = client(Api(reply(200, body))).chat("s", "q")
    assert out.sources == [{"file": "a.md"}] and out.latency_ms is None


# ----------------------------------------------------------------------------- documents


def test_list_documents_reads_the_document_list_without_the_key():
    api = Api(reply(200, {"documents": [{"id": "1", "name": "a.md"}, "junk"], "total": 1}))
    out = client(api).list_documents()
    assert out.kind == "ok" and out.documents == [{"id": "1", "name": "a.md"}]
    assert (api.requests[0].method, api.requests[0].url.path) == ("GET", "/documents")
    assert "x-api-key" not in api.requests[0].headers


def test_list_documents_failures():
    assert client(Api(httpx.ConnectError("x"))).list_documents().kind == "unreachable"
    assert client(Api(reply(500, {}))).list_documents().kind == "unexpected"
    assert client(Api(reply(200, {"documents": "no"}))).list_documents().kind == "unexpected"
    assert client(Api(httpx.Response(200, text="not json"))).list_documents().kind == "unexpected"


# ----------------------------------------------------------------------------- ingest


def test_ingest_sends_the_files_and_the_key_header():
    api = Api(reply(202, {"results": [{"name": "a.md", "status": "queued", "chunks": 2}]}))
    out = client(api).ingest([("a.md", b"# hi"), ("b.txt", b"text")])
    request = api.requests[0]
    assert (request.method, request.url.path) == ("POST", "/ingest")
    assert request.headers["x-api-key"] == KEY
    body = request.content
    assert b'name="files"; filename="a.md"' in body and b'filename="b.txt"' in body
    assert out.kind == "ok" and out.results[0]["status"] == "queued"


def test_ingest_sends_a_url_as_the_url_form_field():
    api = Api(reply(202, {"results": [{"name": "https://x.test/p", "status": "queued"}]}))
    out = client(api).ingest([], "https://x.test/p")
    assert b'name="url"' in api.requests[0].content or b"url=https%3A%2F%2Fx.test%2Fp" in (
        api.requests[0].content
    )
    assert api.requests[0].headers["x-api-key"] == KEY and out.kind == "ok"


def test_ingest_with_every_item_rejected_still_returns_the_reasons():
    api = Api(
        reply(
            400,
            {
                "results": [
                    {"name": "x.csv", "status": "rejected", "reason_code": "unsupported_type"}
                ]
            },
        )
    )
    out = client(api).ingest([("x.csv", b"a,b")])
    assert out.kind == "ok" and out.results[0]["reason_code"] == "unsupported_type"


@pytest.mark.parametrize(
    "code, kind",
    [
        (401, "unauthorized"),
        (503, "unavailable"),
        (413, "too_many"),
        (422, "bad_input"),
        (500, "unexpected"),
    ],
)
def test_ingest_error_codes_become_kinds(code, kind):
    assert client(Api(reply(code, {"detail": "x"}))).ingest([("a.md", b"x")]).kind == kind


def test_ingest_connection_error_and_garbled_success():
    assert client(Api(httpx.ConnectError("x"))).ingest([("a.md", b"x")]).kind == "unreachable"
    assert client(Api(reply(202, {"results": "no"}))).ingest([("a.md", b"x")]).kind == "unexpected"
    assert client(Api(reply(202, {}))).ingest([("a.md", b"x")]).kind == "unexpected"


def test_without_a_key_ingest_and_delete_make_no_request():
    api = Api()
    c = client(api, key=None)
    assert c.has_key is False
    assert c.ingest([("a.md", b"x")]).kind == "no_key"
    assert c.delete_document(DOC_ID).kind == "no_key"
    assert api.requests == []


# ----------------------------------------------------------------------------- delete


def test_delete_calls_the_right_endpoint_with_the_key_header():
    api = Api(httpx.Response(204))
    assert client(api).delete_document(DOC_ID).kind == "ok"
    request = api.requests[0]
    assert (request.method, request.url.path) == ("DELETE", f"/documents/{DOC_ID}")
    assert request.headers["x-api-key"] == KEY


@pytest.mark.parametrize(
    "code, kind",
    [
        (401, "unauthorized"),
        (503, "unavailable"),
        (404, "not_found"),
        (409, "conflict"),
        (422, "bad_input"),
        (500, "unexpected"),
    ],
)
def test_delete_error_codes_become_kinds(code, kind):
    assert client(Api(reply(code, {"detail": "x"}))).delete_document(DOC_ID).kind == kind


def test_delete_rejects_a_malformed_id_before_calling_the_api():
    api = Api()
    assert client(api).delete_document("../../etc/passwd").kind == "bad_input"
    assert api.requests == []


def test_delete_connection_error():
    assert client(Api(httpx.ConnectError("x"))).delete_document(DOC_ID).kind == "unreachable"


# ----------------------------------------------------------------------------- the key


def test_the_key_never_appears_in_the_repr_or_in_any_outcome():
    c = client(Api(reply(401, {"detail": f"bad key {KEY}"}), reply(401, {"detail": KEY})))
    assert KEY not in repr(c) and "key_set=True" in repr(c)
    outcomes = [c.ingest([("a.md", b"x")]), c.delete_document(DOC_ID)]
    assert KEY not in repr(outcomes) and all(o.kind == "unauthorized" for o in outcomes)


def test_client_from_env_reads_the_environment(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://example.test:9/")
    monkeypatch.setenv("APP_API_KEY", KEY)
    monkeypatch.setenv("API_TIMEOUT_SECONDS", "7")
    c = client_from_env()
    assert (
        c.has_key and str(c._http.base_url) == "http://example.test:9" and c._http.timeout.read == 7
    )
    assert KEY not in repr(c)


def test_client_from_env_defaults_and_bad_values(monkeypatch):
    for name in ("API_BASE_URL", "APP_API_KEY", "API_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    c = client_from_env()
    assert not c.has_key and str(c._http.base_url).startswith(DEFAULT_BASE_URL)
    monkeypatch.setenv("API_TIMEOUT_SECONDS", "not a number")
    assert client_from_env()._http.timeout.read == 45.0
