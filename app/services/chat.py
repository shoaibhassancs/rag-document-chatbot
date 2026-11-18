"""The question-to-answer pipeline. No FastAPI or Streamlit imports: routes, scripts and tests all
call `ChatService.chat`, so projects 3 and 6 can reuse it.

Flow:  remember -> prepare query (clarify or standalone query) -> embed -> search -> threshold ->
answer -> citations -> save the turn and write the log.
The prepare step is the clarify-before-retrieving hook (app/services/clarify.py). The answer call
receives the user's own message when there is no history, and the standalone query when there is
(so a follow-up such as "and on Saturdays?" is answered as a full question).
Memory and logging are best-effort: a failure there never changes the reply.

Statuses: answered | refused | clarify | error.
  refused  no chunk passed the threshold (no LLM call is made), or the model said the chunks do
           not contain the answer. Both give the same fixed message.
  clarify  the preparer asked one short clarifying question; nothing is retrieved or answered.
  error    embedding or LLM failure after retries (or a missing key). Friendly message, no trace.

A missing or empty session id is allowed at this level (scripts, other services): the exchange is
answered without memory and logged with session_id NULL. The HTTP route still requires one.
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

PURGE_EVERY_SECONDS = 600


@dataclass
class PreparedQuery:
    """What the prepare step decided. `clarifying_question` set means: ask it, retrieve nothing."""

    search_query: str
    clarifying_question: str | None = None
    action: str = "passthrough"  # passthrough | search | clarify | search_failed_open
    usage: dict = field(default_factory=dict)  # the clarify call's model and token use
    # P1a: the question the ANSWER model receives. None = the same as search_query. With no history
    # it is the user's original message; with history it is the standalone rewrite.
    answer_question: str | None = None
    # The model's own rewrite, kept for the log when it was not the text that was searched.
    rewrite_query: str | None = None


class QueryPreparer(Protocol):
    def prepare(self, session_id: str | None, message: str) -> PreparedQuery: ...


class PassthroughPreparer:
    """Used when the clarify step is switched off or unavailable."""

    prompt_version = None

    def prepare(self, session_id: str | None, message: str) -> PreparedQuery:
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
    standalone_query: str | None = (
        None  # the clarify step's rewrite (what was searched, unless P1b)
    )
    searched_query: str | None = None  # the exact text that was embedded and searched
    clarify_action: str | None = None
    clarify_usage: dict = field(default_factory=dict)
    used_chunk_ids: list[int] = field(default_factory=list)


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


class ChatService:
    def __init__(
        self,
        settings: Settings,
        embedder: QueryEmbedder,
        answerer: Answerer,
        preparer: QueryPreparer | None = None,
        memory=None,
        chat_logger=None,
    ):
        self._settings = settings
        self._embedder = embedder
        self._answerer = answerer
        self._preparer = preparer or PassthroughPreparer()
        self._memory = memory  # MemoryStore or None (no memory)
        self._logger = chat_logger  # ChatLogger or None (no log rows)
        self._last_purge: float | None = None
        if settings.similarity_threshold is None:
            log.warning("SIMILARITY_THRESHOLD is not set: weak matches are NOT being dropped.")

    def chat(self, session_id: str | None, message: str) -> ChatResult:
        started = time.perf_counter()
        timings: dict[str, int] = {}
        trace: dict = {}
        result = self._run(session_id, message, timings, trace)
        prepared: PreparedQuery | None = trace.get("prepared")
        if prepared is not None:
            result.standalone_query = prepared.rewrite_query or prepared.search_query
            result.searched_query = prepared.search_query
            result.clarify_action = prepared.action
            result.clarify_usage = prepared.usage
        result.timings_ms = {**timings, "total": _ms(started)}
        result.latency_ms = result.timings_ms["total"]
        log.info(
            "chat status=%s clarify=%s retrieved=%d sources=%d ms=%s warnings=%d error=%s",
            result.status,
            result.clarify_action,
            len(result.retrieved),
            len(result.sources),
            result.timings_ms,
            len(result.warnings),
            result.error,
        )
        self._remember_and_log(session_id, message, result)
        return result

    def _remember_and_log(self, session_id: str | None, message: str, result: ChatResult) -> None:
        """Save the turn and write the log row. Each step may fail without changing the reply."""
        if self._memory is not None and session_id:
            try:
                self._memory.save_turn(session_id, message, result.answer, result.status)
            except Exception as exc:  # best-effort by design
                log.warning("memory_write_failed %s", type(exc).__name__)
            self._purge_if_due()
        if self._logger is not None:
            try:
                self._logger.write(
                    session_id=session_id or None,
                    question=message,
                    result=result,
                    prompt_version=getattr(self._answerer, "prompt_version", None),
                    clarify_prompt_version=getattr(self._preparer, "prompt_version", None),
                )
            except Exception as exc:
                log.warning("chat_log_write_failed %s", type(exc).__name__)

    def _purge_if_due(self) -> None:
        now = time.monotonic()
        if self._last_purge is not None and now - self._last_purge < PURGE_EVERY_SECONDS:
            return
        self._last_purge = now
        try:
            removed = self._memory.purge_expired()
            if removed:
                log.info("sessions_purged count=%d", removed)
        except Exception as exc:
            log.warning("session_purge_failed %s", type(exc).__name__)

    def _run(self, session_id: str | None, message: str, timings: dict, trace: dict) -> ChatResult:
        s = self._settings
        try:
            t = time.perf_counter()
            prepared = self._preparer.prepare(session_id, message)
            trace["prepared"] = prepared
            if prepared.action != "passthrough":
                timings["clarify"] = _ms(t)
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
            answer = self._answerer.answer(prepared.answer_question or prepared.search_query, hits)
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
            used_chunk_ids=[h.chunk_id for h in answer.used],
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
