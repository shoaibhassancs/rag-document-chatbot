"""The question-to-answer pipeline. No FastAPI or Streamlit imports: routes, scripts and tests all
call `ChatService.chat`, so projects 3 and 6 can reuse it.

Flow (Milestone 3):  prepare query -> embed -> search -> threshold -> answer -> citations.
The prepare step is the hook where Milestone 4 plugs in the clarify-before-retrieving call; today
`PassthroughPreparer` uses the user's message as the search query.

Statuses: answered | refused | clarify | error.
  refused  no chunk passed the threshold (no LLM call is made), or the model said the chunks do
           not contain the answer. Both give the same fixed message.
  clarify  produced by the Milestone 4 preparer, never by this milestone's code.
  error    embedding or LLM failure after retries (or a missing key). Friendly message, no trace.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Settings
from app.db import connect
from app.generation.answer import Answerer, AnswerFormatError, build_sources
from app.generation.llm import LLMError
from app.ingest.embedder import EmbeddingError
from app.retrieval.search import apply_threshold, search

log = logging.getLogger(__name__)


@dataclass
class PreparedQuery:
    """What the prepare step decided. `clarifying_question` set means: ask it, retrieve nothing."""

    search_query: str
    clarifying_question: str | None = None


class QueryPreparer(Protocol):
    def prepare(self, session_id: str, message: str) -> PreparedQuery: ...


class PassthroughPreparer:
    def prepare(self, session_id: str, message: str) -> PreparedQuery:
        return PreparedQuery(search_query=message)


class QueryEmbedder(Protocol):
    def embed_query(self, text: str, max_wait: float | None = None) -> list[float]: ...


@dataclass
class ChatResult:
    answer: str
    status: str
    sources: list[dict] = field(default_factory=list)
    latency_ms: int = 0
    # Everything below is for logs, scripts and tests; the API returns only the first four.
    timings_ms: dict[str, int] = field(default_factory=dict)
    retrieved: list[dict] = field(default_factory=list)  # chunk id, file, page, similarity
    llm_usage: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None  # short internal reason; never shown to the user
    raw_reply: str | None = None  # the model's raw text, for smoke tests and evaluation only


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


class ChatService:
    def __init__(
        self,
        settings: Settings,
        embedder: QueryEmbedder,
        answerer: Answerer,
        preparer: QueryPreparer | None = None,
    ):
        self._settings = settings
        self._embedder = embedder
        self._answerer = answerer
        self._preparer = preparer or PassthroughPreparer()
        if settings.similarity_threshold is None:
            log.warning("SIMILARITY_THRESHOLD is not set: weak matches are NOT being dropped.")

    def chat(self, session_id: str, message: str) -> ChatResult:
        started = time.perf_counter()
        timings: dict[str, int] = {}
        result = self._run(session_id, message, started, timings)
        result.timings_ms = {**timings, "total": _ms(started)}
        result.latency_ms = result.timings_ms["total"]
        log.info(
            "chat status=%s retrieved=%d sources=%d ms=%s warnings=%d error=%s",
            result.status,
            len(result.retrieved),
            len(result.sources),
            result.timings_ms,
            len(result.warnings),
            result.error,
        )
        return result

    def _run(self, session_id: str, message: str, started: float, timings: dict) -> ChatResult:
        s = self._settings
        try:
            prepared = self._preparer.prepare(session_id, message)
            if prepared.clarifying_question:
                return ChatResult(prepared.clarifying_question, "clarify")

            t = time.perf_counter()
            vector = self._embedder.embed_query(
                prepared.search_query, s.query_embed_max_wait_seconds
            )
            timings["embed"] = _ms(t)

            t = time.perf_counter()
            with connect(s.database_url) as conn:
                found = search(conn, vector, top_k=s.top_k, embedding_model=s.embedding_model)
            timings["search"] = _ms(t)
            retrieved = [
                {
                    "chunk_id": h.chunk_id,
                    "file": h.file,
                    "page": h.page,
                    "similarity": round(h.similarity, 4),
                }
                for h in found
            ]
            hits = apply_threshold(found, s.similarity_threshold)
            if not hits:  # empty database, or nothing relevant: refuse without an LLM call
                return ChatResult(s.refusal_message, "refused", retrieved=retrieved)

            t = time.perf_counter()
            answer = self._answerer.answer(message, hits)
            timings["llm"] = _ms(t)
        except (EmbeddingError, LLMError, AnswerFormatError) as exc:
            log.error("chat_failed %s: %s", type(exc).__name__, exc)
            return ChatResult(s.busy_message, "error", error=type(exc).__name__)

        usage = _usage(answer.llm)
        if not answer.answerable:
            return ChatResult(
                s.refusal_message,
                "refused",
                retrieved=retrieved,
                llm_usage=usage,
                raw_reply=answer.llm.text,
            )
        return ChatResult(
            answer.text,
            "answered",
            sources=build_sources(answer.used),
            retrieved=retrieved,
            llm_usage=usage,
            warnings=answer.warnings,
            raw_reply=answer.llm.text,
        )


def _usage(llm) -> dict:
    return {
        "model": llm.model,
        "prompt_tokens": llm.prompt_tokens,
        "completion_tokens": llm.completion_tokens,
        "reasoning_tokens": llm.reasoning_tokens,
        "total_tokens": llm.total_tokens,
        "cached_tokens": llm.cached_tokens,
        "attempts": llm.attempts,
        "fallback_used": llm.fallback_used,
        "rate_limit": llm.rate_limit,
    }


def error_result(settings: Settings, reason: str) -> ChatResult:
    """The friendly failure reply, for problems found before the service could run."""
    return ChatResult(settings.busy_message, "error", error=reason)
