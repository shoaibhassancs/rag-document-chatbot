"""The /chat HTTP contract for all four statuses, plus the HTTP-level failure modes (Groq 429, Groq
timeout, Voyage 429, empty database). Mocked Groq and Voyage; real throwaway Postgres."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.generation.answer import Answer, GroundedAnswerer
from app.generation.llm import LLMBusyError, LLMClient
from app.ingest.embedder import EmbeddingRateLimited
from app.main import create_app
from app.services.ingestion import embed_pending
from app.startup import prepare_database
from tests.fakes import FakeAnswerer, FakeEmbedder, fake_llm_result
from tests.helpers import md, register, rows, section
from tests.test_llm_client import Server, resp, role

GUIDE = "# Guide\n\n" + section("Fees", "alpha") + section("Hours", "beta")
FIELDS = {"answer", "status", "sources", "latency_ms"}


@pytest.fixture
def ready(settings, embedder):
    prepare_database(settings)
    register(settings, md("guide.md", GUIDE))
    embed_pending(settings.database_url, embedder)
    return rows(settings, "SELECT text FROM chunks WHERE section = 'Fees'")[0][0]


def call(settings, embedder, message, answerer=None, preparer=None, **overrides):
    app = create_app(
        embedder_factory=lambda s: embedder,
        answerer_factory=lambda s: answerer or FakeAnswerer(),
        preparer_factory=(lambda s, m: preparer),
    )
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update=overrides)
    with TestClient(app) as client:
        return client.post("/chat", json={"session_id": "contract", "message": message})


class ScriptedPreparer:
    """Returns a fixed PreparedQuery."""

    prompt_version = "t"

    def __init__(self, query=None, clarify=None):
        self.query, self.clarify = query, clarify

    def prepare(self, session_id, message):
        from app.services.chat import PreparedQuery

        action = "clarify" if self.clarify else "search"
        return PreparedQuery(self.query or message, self.clarify, action)


def check(r, status, code):
    body = r.json()
    assert r.status_code == code and set(body) == FIELDS and body["status"] == status
    assert isinstance(body["answer"], str) and isinstance(body["latency_ms"], int)
    assert "Traceback" not in r.text
    return body


def test_answered_is_200_with_sources_from_metadata(settings, embedder, ready):
    body = check(call(settings, embedder, ready), "answered", 200)
    assert body["sources"][0]["file"] == "guide.md" and body["sources"][0]["section"] == "Fees"


def test_refused_by_the_model_is_200_with_the_fixed_message_and_no_sources(
    settings, embedder, ready
):
    refuse = FakeAnswerer(reply=lambda q, h: Answer(False, "", [], fake_llm_result()))
    body = check(call(settings, embedder, ready, refuse), "refused", 200)
    assert body["answer"] == settings.refusal_message and body["sources"] == []


def test_refused_at_the_gate_never_calls_the_model(settings, embedder, ready):
    answerer = FakeAnswerer()
    body = check(
        call(settings, embedder, "unrelated words", answerer, similarity_threshold=0.999),
        "refused",
        200,
    )
    assert body["sources"] == [] and answerer.calls == []


def test_clarify_is_200_with_the_question_and_no_sources_and_no_model_call(
    settings, embedder, ready
):
    answerer = FakeAnswerer()
    before = embedder.requests
    r = call(
        settings,
        embedder,
        "How much is it?",
        answerer,
        ScriptedPreparer(clarify="Which treatment?"),
    )
    body = check(r, "clarify", 200)
    assert body["answer"] == "Which treatment?" and body["sources"] == []
    assert answerer.calls == [] and embedder.requests == before


def test_error_from_the_answer_model_is_503_with_the_busy_message(settings, embedder, ready):
    r = call(settings, embedder, ready, FakeAnswerer(error=LLMBusyError("quota")))
    body = check(r, "error", 503)
    assert body["answer"] == settings.busy_message and body["sources"] == []
    assert "quota" not in r.text


def test_groq_429_with_a_long_retry_after_and_no_fallback_is_a_503(settings, embedder, ready):
    server = Server(resp(429, {}, {"retry-after": "3600"}))
    llm = LLMClient(role(), transport=httpx.MockTransport(server), sleep=lambda s: None)
    answerer = GroundedAnswerer(llm, "SYS", "answer_t")
    check(call(settings, embedder, ready, answerer), "error", 503)
    assert len(server.requests) == 1  # it did not wait an hour


def test_groq_429_on_the_main_model_falls_back_and_still_answers(settings, embedder, ready):
    reply = json.dumps({"answerable": True, "answer": "From the fallback.", "used_chunks": [1]})
    from tests.test_llm_client import ok_body

    server = Server(
        resp(429, {}, {"retry-after": "3600"}), resp(body=ok_body(reply, model="small-model"))
    )
    llm = LLMClient(
        role(),
        fallback_model="small-model",
        transport=httpx.MockTransport(server),
        sleep=lambda s: None,
    )
    body = check(
        call(settings, embedder, ready, GroundedAnswerer(llm, "SYS", "answer_t")), "answered", 200
    )
    assert body["answer"] == "From the fallback."


def test_groq_timeouts_end_in_a_503_with_no_trace(settings, embedder, ready):
    server = Server(*[httpx.ReadTimeout("slow") for _ in range(3)])
    llm = LLMClient(role(), transport=httpx.MockTransport(server), sleep=lambda s: None)
    r = call(settings, embedder, ready, GroundedAnswerer(llm, "SYS", "answer_t"))
    body = check(r, "error", 503)
    assert body["answer"] == settings.busy_message and "slow" not in r.text


def test_groq_malformed_json_from_the_model_is_a_503(settings, embedder, ready):
    from tests.test_llm_client import ok_body

    server = Server(resp(body=ok_body("not json at all")))
    llm = LLMClient(role(), transport=httpx.MockTransport(server), sleep=lambda s: None)
    check(call(settings, embedder, ready, GroundedAnswerer(llm, "SYS", "a")), "error", 503)


class RateLimitedVoyage(FakeEmbedder):
    def embed(self, texts, input_type, max_wait=None):
        if input_type == "query":
            raise EmbeddingRateLimited("Voyage would need a 40s wait")
        return super().embed(texts, input_type, max_wait)


def test_voyage_429_over_the_wait_cap_is_a_503_not_a_hang(settings, ready):
    voyage = RateLimitedVoyage(dimensions=settings.embedding_dimensions, max_batch_tokens=400)
    r = call(settings, voyage, "anything")
    body = check(r, "error", 503)
    assert body["answer"] == settings.busy_message and "Voyage" not in r.text


def test_an_empty_database_is_a_refusal_over_http(settings, embedder):
    prepare_database(settings)
    answerer = FakeAnswerer()
    body = check(call(settings, embedder, "Do you take Delta Dental?", answerer), "refused", 200)
    assert body["sources"] == [] and answerer.calls == []
