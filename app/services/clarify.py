"""The merged clarify-before-retrieving and history-rewrite call (Milestone 4).

One small-model call sees the last few exchanges and the new message and returns either a
standalone search query or one clarifying question (prompts/clarify_v1.txt). The model only
PROPOSES; the rules that matter are enforced here in code:

  * when in doubt, answer: a clarifying question is accepted only if it is a short single question;
    anything malformed becomes a search;
  * never two clarifying questions in a row: if the previous exchange was a clarify, a new clarify
    is overridden to a search;
  * fail open: if the call fails or its reply is unusable, search with the message as written.

Everything from the user, the history and the model is data: wrapped in tags, tag-like text is
defanged, and the model's `query` is only ever used as a search string.
"""

import logging
import re
from dataclasses import dataclass, field

import psycopg

from app.config import Settings
from app.generation.answer import AnswerFormatError, parse_model_json
from app.generation.llm import LLMClient, LLMConfigError, LLMError
from app.memory import MemoryStore, SessionState, Turn
from app.services.chat import PreparedQuery

log = logging.getLogger(__name__)

# User, history and model text must not be able to close or open our own delimiter tags.
_TAG = re.compile(r"<(/?\s*(?:history|turn|user|assistant|message)\b)", re.IGNORECASE)
MAX_QUESTION_CHARS = 200


def _clean(text: str, limit: int | None = None) -> str:
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return _TAG.sub(r"&lt;\1", text)


def build_clarify_message(turns: list[Turn], message: str, max_chars: int = 300) -> str:
    parts = ["<history>"]
    for number, turn in enumerate(turns, start=1):
        parts.append(
            f'<turn n="{number}"><user>{_clean(turn.user, max_chars)}</user>'
            f"<assistant>{_clean(turn.assistant, max_chars)}</assistant></turn>"
        )
    parts.append("</history>")
    parts.append(f"<message>\n{_clean(message)}\n</message>")
    return "\n".join(parts)


@dataclass
class Decision:
    action: str  # "search" or "clarify"
    query: str
    clarifying_question: str | None = None
    notes: list[str] = field(default_factory=list)


def decide(data: dict, previous_status: str | None, state: SessionState, message: str) -> Decision:
    """Turn the model's proposal into a decision, applying the code-side rules."""
    notes: list[str] = []
    query = data.get("query")
    query = query.strip() if isinstance(query, str) else ""
    action = data.get("action")
    question = data.get("clarifying_question")
    question = question.strip() if isinstance(question, str) else ""

    if action == "clarify":
        if not question or "?" not in question or len(question) > MAX_QUESTION_CHARS:
            notes.append("clarify rejected: not one short question; searching instead")
        elif previous_status == "clarify":
            notes.append("clarify overridden: the previous reply was already a clarify")
        else:
            return Decision("clarify", query or message, question)
    elif action != "search":
        notes.append("unknown action; searching instead")

    if not query:
        # Never leave the search empty. After a clarify, join the vague question and the answer.
        if previous_status == "clarify" and state.turns:
            query = f"{state.turns[-1].user} {message}".strip()
        else:
            query = message
    return Decision("search", query, None, notes)


class ClarifyingPreparer:
    """Implements the QueryPreparer hook of app/services/chat.py."""

    def __init__(
        self,
        llm: LLMClient,
        system_prompt: str,
        prompt_version: str,
        memory: MemoryStore,
        max_chars: int = 300,
    ):
        self._llm = llm
        self._system = system_prompt
        self.prompt_version = prompt_version
        self._memory = memory
        self._max_chars = max_chars

    def prepare(self, session_id: str | None, message: str) -> PreparedQuery:
        state = SessionState()
        if session_id:
            try:
                state = self._memory.state(session_id)
            except psycopg.Error as exc:
                log.warning("clarify_memory_unavailable %s", type(exc).__name__)
        try:
            result = self._llm.complete(
                self._system, build_clarify_message(state.turns, message, self._max_chars)
            )
            if result.finish_reason == "length":
                raise AnswerFormatError("The clarify reply was cut off at the output cap.")
            data = parse_model_json(result.text)
        except (LLMError, AnswerFormatError) as exc:
            log.warning("clarify_failed_open %s: %s", type(exc).__name__, exc)
            return PreparedQuery(message, action="search_failed_open")
        decision = decide(data, state.last_status, state, message)
        for note in decision.notes:
            log.info("clarify_note %s", note)
        usage = {
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "attempts": result.attempts,
            "fallback_used": result.fallback_used,
        }
        return PreparedQuery(
            search_query=decision.query,
            clarifying_question=decision.clarifying_question,
            action=decision.action,
            usage=usage,
        )


def build_preparer(settings: Settings, memory: MemoryStore | None):
    """The preparer for the app, or None (use the passthrough) when clarify is switched off, memory
    is missing, or the rewrite model's key is not set. Never raises."""
    if not settings.clarify_enabled or memory is None:
        return None
    path = settings.prompts_dir / f"{settings.clarify_prompt_version}.txt"
    try:
        llm = LLMClient(
            settings.llm_role("rewrite"),
            fallback_model=None,
            max_retries=settings.llm_max_retries,
            max_wait_seconds=settings.llm_max_wait_seconds,
            timeout=settings.llm_timeout_seconds,
        )
        prompt = path.read_text(encoding="utf-8").strip()
    except (LLMConfigError, OSError) as exc:
        log.warning("clarify_disabled %s: %s", type(exc).__name__, exc)
        return None
    return ClarifyingPreparer(
        llm, prompt, settings.clarify_prompt_version, memory, settings.history_message_max_chars
    )
