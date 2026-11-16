"""Grounded answering: build the prompt from retrieved chunks, call the answer model, parse its
JSON, and build citations IN CODE from chunk metadata (never from text the model writes).

Model output contract (prompts/answer_v1.txt):
  {"answerable": bool, "answer": str, "used_chunks": [excerpt numbers]}
Excerpt numbers are 1-based positions in the list we sent, not database ids, so the model cannot
invent or confuse ids; we map them back to chunks here.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.generation.llm import LLMClient, LLMResult
from app.retrieval.search import Hit

log = logging.getLogger(__name__)

# Chunk text is data. Neutralise anything that looks like our own delimiter tags.
_TAG = re.compile(r"<(/?\s*(?:excerpts?|question)\b)", re.IGNORECASE)


class AnswerFormatError(Exception):
    """The model's reply was not the JSON we asked for (or was cut off)."""


@dataclass
class Answer:
    answerable: bool
    text: str
    used: list[Hit]
    llm: LLMResult
    warnings: list[str] = field(default_factory=list)


class Answerer(Protocol):
    prompt_version: str

    def answer(self, question: str, hits: list[Hit]) -> Answer: ...


def load_prompt(settings: Settings) -> str:
    path: Path = settings.prompts_dir / f"{settings.answer_prompt_version}.txt"
    return path.read_text(encoding="utf-8").strip()


def build_user_message(question: str, hits: list[Hit]) -> str:
    parts = ["<excerpts>"]
    for number, hit in enumerate(hits, start=1):
        parts.append(f'<excerpt n="{number}">\n{_TAG.sub(r"&lt;\1", hit.text)}\n</excerpt>')
    parts.append("</excerpts>")
    parts.append(f"<question>\n{_TAG.sub(r'&lt;\1', question)}\n</question>")
    return "\n".join(parts)


def parse_model_json(text: str) -> dict:
    """Accept a bare JSON object, tolerate a code fence or stray words around it."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise AnswerFormatError("The model reply contained no JSON object.") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except ValueError:
            raise AnswerFormatError("The model reply was not valid JSON.") from None
    if not isinstance(data, dict):
        raise AnswerFormatError("The model reply was not a JSON object.")
    return data


class GroundedAnswerer:
    def __init__(self, llm: LLMClient, system_prompt: str, prompt_version: str):
        self._llm = llm
        self._system = system_prompt
        self.prompt_version = prompt_version

    def answer(self, question: str, hits: list[Hit]) -> Answer:
        result = self._llm.complete(self._system, build_user_message(question, hits))
        if result.finish_reason == "length":
            raise AnswerFormatError(
                f"The model reply was cut off at the output cap (completion tokens: "
                f"{result.completion_tokens}, reasoning tokens: {result.reasoning_tokens})."
            )
        data = parse_model_json(result.text)
        answerable, text = data.get("answerable"), data.get("answer")
        if not isinstance(answerable, bool) or not isinstance(text, str):
            raise AnswerFormatError("The model reply lacked 'answerable' or 'answer'.")
        if not answerable:
            return Answer(False, "", [], result)
        if not text.strip():
            raise AnswerFormatError("The model said answerable but gave an empty answer.")
        used, warnings = select_used_chunks(data.get("used_chunks"), hits)
        return Answer(True, text.strip(), used, result, warnings)


def select_used_chunks(raw, hits: list[Hit]) -> tuple[list[Hit], list[str]]:
    """Map the model's excerpt numbers to chunks. If none are valid, cite only the top-scoring
    chunk (hits are sorted best first) and warn; never cite everything retrieved."""
    chosen: list[Hit] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= len(hits):
                hit = hits[item - 1]
                if hit not in chosen:
                    chosen.append(hit)
    if chosen:
        return chosen, []
    warning = "answerable but no valid used_chunks; cited the top-scoring chunk only"
    log.warning("answer_warning %s", warning)
    return hits[:1], [warning]


def build_sources(used: list[Hit]) -> list[dict]:
    """Citations come only from stored metadata. Identical (file, page, section) are merged."""
    sources: list[dict] = []
    seen: set[tuple] = set()
    for hit in used:
        key = (hit.document_id, hit.page, hit.section)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "file": hit.file,
                "page": hit.page,
                "section": hit.section,
                "document_id": hit.document_id,
            }
        )
    return sources


def build_answerer(settings: Settings) -> GroundedAnswerer:
    """Raises LLMConfigError if the answer model's key is missing."""
    role = settings.llm_role("answer")
    fallback = settings.answer_fallback_model if settings.answer_fallback_enabled else None
    if fallback and fallback == role.model:
        log.warning(
            "answer_fallback_is_main_model model=%s: no fallback is available; set "
            "ANSWER_FALLBACK_MODEL to the other model",
            fallback,
        )
    llm = LLMClient(
        role,
        fallback_model=fallback,
        max_retries=settings.llm_max_retries,
        max_wait_seconds=settings.llm_max_wait_seconds,
        timeout=settings.llm_timeout_seconds,
    )
    return GroundedAnswerer(llm, load_prompt(settings), settings.answer_prompt_version)
