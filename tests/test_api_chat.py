import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.generation.answer import Answer, AnswerFormatError, build_answerer
from app.generation.llm import LLMBusyError
from app.main import create_app
from app.services.ingestion import embed_pending
from app.startup import prepare_database
from tests.fakes import FakeAnswerer, fake_llm_result
from tests.helpers import md, register, rows, section

GUIDE = "# Guide\n\n" + section("Fees", "alpha") + section("Hours", "beta")


def client_for(settings, embedder, answerer=None, **overrides):
    app = create_app(
        embedder_factory=lambda s: embedder,
        answerer_factory=(lambda s: answerer) if answerer is not None else build_answerer,
    )
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update=overrides)
    return TestClient(app)


@pytest.fixture
def ready(settings, embedder):
    prepare_database(settings)
    register(settings, md("guide.md", GUIDE))
    embed_pending(settings.database_url, embedder)
    return rows(settings, "SELECT text FROM chunks WHERE section = 'Fees'")[0][0]


def post(client, message="What are the fees?", session="demo-1", **extra):
    return client.post("/chat", json={"session_id": session, "message": message, **extra})


def test_answered_reply_has_the_contract_shape(settings, embedder, ready):
    with client_for(settings, embedder, FakeAnswerer()) as client:
        r = post(client, ready)
    body = r.json()
    assert r.status_code == 200 and set(body) == {"answer", "status", "sources", "latency_ms"}
    assert body["status"] == "answered" and body["answer"] == "Fake answer."
    assert set(body["sources"][0]) == {"file", "page", "section", "document_id"}
    assert body["sources"][0]["file"] == "guide.md" and isinstance(body["latency_ms"], int)


def test_chat_needs_no_key(settings, embedder, ready):
    with client_for(settings, embedder, FakeAnswerer()) as client:
        assert post(client, ready).status_code == 200


def test_refused_reply_is_a_200_with_the_fixed_message(settings, embedder, ready):
    answerer = FakeAnswerer(reply=lambda q, h: Answer(False, "", [], fake_llm_result()))
    with client_for(settings, embedder, answerer) as client:
        r = post(client, "Can you recommend a good lawyer?")
    assert r.status_code == 200 and r.json()["status"] == "refused"
    assert r.json()["answer"] == settings.refusal_message and r.json()["sources"] == []


def test_weak_match_is_refused_and_the_model_is_never_called(settings, embedder, ready):
    answerer = FakeAnswerer()
    with client_for(settings, embedder, answerer, similarity_threshold=0.999) as client:
        r = post(client, "Can you recommend a good lawyer?")
    assert r.json()["status"] == "refused" and answerer.calls == []


@pytest.mark.parametrize("error", [LLMBusyError("quota"), AnswerFormatError("bad")])
def test_a_failure_is_503_with_status_error_and_a_friendly_message(
    settings, embedder, ready, error
):
    with client_for(settings, embedder, FakeAnswerer(error=error)) as client:
        r = post(client, ready)
    body = r.json()
    assert r.status_code == 503 and body["status"] == "error"
    assert body["answer"] == settings.busy_message and body["sources"] == []
    assert "Traceback" not in r.text and "quota" not in r.text


def test_a_missing_llm_key_is_a_friendly_503_not_a_stack_trace(settings, embedder, ready):
    with client_for(settings, embedder, groq_api_key=None) as client:
        r = post(client, ready)
    assert r.status_code == 503 and r.json()["status"] == "error"
    assert r.json()["answer"] == settings.busy_message


def test_a_missing_embedding_key_is_a_friendly_503(settings, ready):
    from app.ingest.embedder import build_embedder

    app = create_app(embedder_factory=build_embedder, answerer_factory=lambda s: FakeAnswerer())
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"voyage_api_key": None}
    )
    with TestClient(app) as client:
        r = post(client, "hello")
    assert r.status_code == 503 and r.json()["status"] == "error"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": "a"},
        {"message": "hi"},
        {"session_id": "", "message": "hi"},
        {"session_id": "a" * 65, "message": "hi"},
        {"session_id": "bad id!", "message": "hi"},
        {"session_id": "a", "message": ""},
        {"session_id": "a", "message": "   "},
        {"session_id": "a", "message": "x" * 1001},
        {"session_id": "a", "message": 42},
    ],
)
def test_bad_requests_are_422(settings, embedder, payload):
    with client_for(settings, embedder, FakeAnswerer()) as client:
        assert client.post("/chat", json=payload).status_code == 422


def test_a_message_of_exactly_the_maximum_length_is_accepted(settings, embedder, ready):
    with client_for(settings, embedder, FakeAnswerer()) as client:
        assert post(client, "x" * 1000).status_code == 200


def test_the_message_is_trimmed_before_it_is_used(settings, embedder, ready):
    answerer = FakeAnswerer()
    with client_for(settings, embedder, answerer) as client:
        post(client, "  how much?  ")
    assert answerer.calls[0][0] == "how much?"
