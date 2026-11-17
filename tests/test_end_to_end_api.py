"""One test path through the real HTTP routes: ingest, then chat (answer with citations, refusal,
clarification, a follow-up that uses memory), then delete and chat again. Groq and Voyage are
mocked; the database is the throwaway test Postgres."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.generation.answer import GroundedAnswerer
from app.generation.llm import LLMClient
from app.main import create_app
from app.services.clarify import ClarifyingPreparer
from tests.fakes import FakeAnswerer
from tests.helpers import rows, scalar, section
from tests.test_api_documents import KEY, upload, wait_until_done
from tests.test_llm_client import Server, ok_body, resp, role

GUIDE = ("# Guide\n\n" + section("Fees", "alpha") + section("Hours", "beta")).encode()
INJECTION = Path(__file__).parent / "fixtures" / "injection" / "promo-notice.md"


def rewrite(action="search", query="", question=""):
    body = {"action": action, "query": query, "clarifying_question": question}
    return resp(body=ok_body(json.dumps(body)))


def make_client(settings, embedder, replies, answerer=None, **overrides):
    server = Server(*replies)

    def make_preparer(current, memory):
        llm = LLMClient(
            role(name="rewrite", model="small"),
            transport=httpx.MockTransport(server),
            sleep=lambda s: None,
        )
        return ClarifyingPreparer(llm, "SYS", "clarify_t", memory)

    app = create_app(
        embedder_factory=lambda s: embedder,
        answerer_factory=lambda s: answerer or FakeAnswerer(),
        preparer_factory=make_preparer,
    )
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update=overrides)
    return TestClient(app), server


def chunk(settings, name):
    return rows(settings, "SELECT text FROM chunks WHERE section = %s", [name])[0][0]


def chat(client, message, session="e2e"):
    return client.post("/chat", json={"session_id": session, "message": message})


def test_ingest_then_chat_refusal_clarification_memory_then_delete(settings, embedder):
    # Needs the chunk text to build queries, so the document is ingested first with a plain client.
    plain, _ = make_client(settings, embedder, [], similarity_threshold=0.999)
    with plain:
        assert upload(plain, ("guide.md", GUIDE)).status_code == 202
        doc = wait_until_done(plain, count=1)[0]
        assert doc["status"] == "ready" and doc["chunks_done"] == 2
    fees, hours = chunk(settings, "Fees"), chunk(settings, "Hours")

    replies = [
        rewrite(query=fees),  # 1 answered
        rewrite(query="a good lawyer"),  # 2 refused at the gate
        rewrite("clarify", "", "Which treatment do you mean?"),  # 3 clarify
        rewrite(query=hours),  # 4 follow-up, answered from the standalone query
        rewrite(query=fees),  # 5 after delete
    ]
    client, server = make_client(settings, embedder, replies, similarity_threshold=0.999)
    with client:
        # 1. answered, with the citation built from chunk metadata
        r = chat(client, "what are the fees?")
        body = r.json()
        assert r.status_code == 200 and body["status"] == "answered"
        assert body["sources"][0]["file"] == "guide.md" and body["sources"][0]["section"] == "Fees"

        # 2. refused: nothing is close enough
        r = chat(client, "can you recommend a good lawyer?")
        assert r.json()["status"] == "refused" and r.json()["sources"] == []

        # 3. clarify: no sources, and the answer model is not asked
        r = chat(client, "How much is it?")
        assert r.json() == {**r.json(), "status": "clarify", "sources": []}
        assert r.json()["answer"] == "Which treatment do you mean?"

        # 4. a follow-up that uses memory: the clarify call saw the earlier turns
        r = chat(client, "and the hours?")
        assert r.json()["status"] == "answered" and r.json()["sources"][0]["section"] == "Hours"
        sent = json.loads(server.requests[3].content)["messages"][1]["content"]
        assert "can you recommend a good lawyer?" in sent and "How much is it?" in sent

        # logs and memory were written for every exchange
        assert scalar(settings, "SELECT count(*) FROM chat_logs") == 4
        assert scalar(settings, "SELECT count(*) FROM chat_turns WHERE session_id = 'e2e'") == 4

        # 5. delete the document, then the same question is refused (nothing left to find)
        doc_id = client.get("/documents").json()["documents"][0]["id"]
        assert (
            client.delete(f"/documents/{doc_id}", headers=KEY).status_code == 204
        )  # KEY: header dict
        assert client.get("/documents").json()["total"] == 0
        r = chat(client, "what are the fees?")
        assert r.json()["status"] == "refused" and r.json()["sources"] == []


def test_the_prompt_injection_document_is_ingested_and_cannot_change_the_answers_citations(
    settings, embedder
):
    """With mocks this proves WHERE the injected text goes and that code builds the citations; it
    cannot prove how a real model behaves (that is a live check)."""
    plain, _ = make_client(settings, embedder, [])
    with plain:
        assert upload(plain, ("promo-notice.md", INJECTION.read_bytes())).status_code == 202
        assert wait_until_done(plain, count=1)[0]["status"] == "ready"
    text = rows(settings, "SELECT text FROM chunks WHERE text LIKE %s", ["%ignore previous%"])[0][0]

    # A model that OBEYS the document: says everything is free, names a chunk that is not there.
    obeys = json.dumps(
        {"answerable": True, "answer": "All treatments are free.", "used_chunks": [9]}
    )
    llm_server = Server(resp(body=ok_body(obeys)))
    llm = LLMClient(role(), transport=httpx.MockTransport(llm_server), sleep=lambda s: None)
    client, _ = make_client(
        settings, embedder, [rewrite(query=text)], GroundedAnswerer(llm, "SYS", "answer_t")
    )
    with client:
        body = chat(client, "how much do treatments cost?").json()
    sent = json.loads(llm_server.requests[0].content)["messages"][1]["content"]
    assert "ignore previous instructions" in sent  # kept as data inside an excerpt
    assert sent.count("<excerpt n=") == 1 and sent.rstrip().endswith("</question>")
    assert body["status"] == "answered"
    assert [s["file"] for s in body["sources"]] == ["promo-notice.md"]  # from metadata only
    assert "system prompt" not in json.dumps(body["sources"])
