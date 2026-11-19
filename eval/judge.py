"""The LLM judge (qwen/qwen3.8-27b by default, a different family from the answerer).

Two verdicts per answered question: correctness (compared with the staff-written expected answer)
and faithfulness (supported by the chunks the answer was based on). `--judge-calls 1` asks for both
in one call. The judge returns JSON; this module parses it strictly:
  * the score must be the integer 0 or 1 (or a JSON boolean); "yes", 2, null, "1" are rejected;
  * a bad reply is retried once; a second bad reply (or an LLM error) raises JudgeError whose
    message is a precise reason (`json_generation_failed`, `rate_limited`, `invalid_score`, ...);
  * when the API rejects a call with `json_generation_failed`, a verdict is read from the raw text
    in the error body if it passes the strict check, else ONE more attempt is made without JSON
    mode (same prompt text, same strict parsing); only then is it an error;
  * the caller records JudgeError as `judge_error` and the scorer excludes that verdict and counts
    it as unjudged. A failure is NEVER turned into a 0 or a 1.
Everything given to the judge (question, answers, chunks) is data: wrapped in tags with tag-like
text escaped, and the judge prompt says so.
"""

import json
import re
from dataclasses import dataclass

from app.config import Settings
from app.generation.answer import AnswerFormatError, parse_model_json
from app.generation.llm import LLMClient, LLMConfigError, LLMError

JSON_FAILED = "json_generation_failed"
JUDGE_429_RETRIES = 6  # judge phase only; the chat path keeps its short caps
JUDGE_MAX_WAIT_SECONDS = 90.0

_TAG = re.compile(r"<(/?\s*(?:question|expected_answer|answer|chunks?)\b)", re.IGNORECASE)


class JudgeError(Exception):
    """The judge could not produce a usable verdict. Message never contains the prompt or a key."""


@dataclass
class Verdict:
    score: int
    reason: str
    llm_calls: int


def _esc(text: str) -> str:
    return _TAG.sub(r"&lt;\1", text)


def build_correctness_message(question: str, expected: str, answer: str) -> str:
    return (
        f"<question>\n{_esc(question)}\n</question>\n"
        f"<expected_answer>\n{_esc(expected)}\n</expected_answer>\n"
        f"<answer>\n{_esc(answer)}\n</answer>"
    )


def build_chunks_block(chunks: list[str]) -> str:
    parts = ["<chunks>"]
    for number, text in enumerate(chunks, start=1):
        parts.append(f'<chunk n="{number}">\n{_esc(text)}\n</chunk>')
    parts.append("</chunks>")
    return "\n".join(parts)


def build_faithfulness_message(answer: str, chunks: list[str]) -> str:
    return f"<answer>\n{_esc(answer)}\n</answer>\n{build_chunks_block(chunks)}"


def build_combined_message(question: str, expected: str, answer: str, chunks: list[str]) -> str:
    return build_correctness_message(question, expected, answer) + "\n" + build_chunks_block(chunks)


def parse_score(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise AnswerFormatError(f"'{key}' must be 0 or 1")


def parse_verdict(text: str, keys: tuple[str, ...]) -> tuple[dict[str, int], str]:
    """Parse the judge's reply into ({key: 0|1}, reason). Raises AnswerFormatError."""
    data = parse_model_json(text)
    scores = {key: parse_score(data, key) for key in keys}
    reason = data.get("reason")
    return scores, reason.strip()[:300] if isinstance(reason, str) else ""


def _parse_failure_reason(exc: AnswerFormatError) -> str:
    return "invalid_score" if "must be 0 or 1" in str(exc) else "invalid_json"


def salvage_verdict(raw: str | None, keys: tuple[str, ...]) -> tuple[dict[str, int], str] | None:
    """Read a verdict from the raw text Groq returns with a JSON-generation failure. Accepts only
    the first JSON object whose scores are exactly 0 or 1 AND with a non-empty reason; anything
    else returns None (the call stays an error). Never guesses."""
    if not raw:
        return None
    start = raw.find("{")
    if start == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        scores = {key: parse_score(data, key) for key in keys}
    except AnswerFormatError:
        return None
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return scores, reason.strip()[:300]


class Judge:
    def __init__(
        self,
        llm: LLMClient,
        correctness_prompt: str,
        faithfulness_prompt: str,
        combined_prompt: str,
        versions: dict[str, str],
    ):
        self._llm = llm
        self._prompts = {
            "correctness": correctness_prompt,
            "faithfulness": faithfulness_prompt,
            "combined": combined_prompt,
        }
        self.versions = versions
        self.model = llm.model

    def _ask(self, which: str, user: str, keys: tuple[str, ...]) -> tuple[dict, str, int]:
        calls = 0
        last = "invalid_json"
        json_mode = True
        tries = 0
        while tries < 2:  # one retry on an unusable reply
            tries += 1
            calls += 1
            try:
                result = self._llm.complete(self._prompts[which], user, json_mode=json_mode)
            except LLMError as exc:
                if json_mode and getattr(exc, "reason", None) == JSON_FAILED:
                    salvaged = salvage_verdict(getattr(exc, "failed_generation", None), keys)
                    if salvaged:
                        return salvaged[0], salvaged[1], calls
                    # one more attempt without JSON mode, same prompt, same strict parsing
                    json_mode = False
                    last = JSON_FAILED
                    tries = 1
                    continue
                raise JudgeError(getattr(exc, "reason", None) or type(exc).__name__) from None
            if result.finish_reason == "length":
                last = "reply_cut_off"
                continue
            try:
                scores, reason = parse_verdict(result.text, keys)
            except AnswerFormatError as exc:
                last = _parse_failure_reason(exc)
                continue
            return scores, reason, calls
        raise JudgeError(last)

    def correctness(self, question: str, expected: str, answer: str) -> Verdict:
        scores, reason, calls = self._ask(
            "correctness", build_correctness_message(question, expected, answer), ("correct",)
        )
        return Verdict(scores["correct"], reason, calls)

    def faithfulness(self, answer: str, chunks: list[str]) -> Verdict:
        scores, reason, calls = self._ask(
            "faithfulness", build_faithfulness_message(answer, chunks), ("faithful",)
        )
        return Verdict(scores["faithful"], reason, calls)

    def both(
        self, question: str, expected: str, answer: str, chunks: list[str]
    ) -> tuple[Verdict, Verdict]:
        scores, reason, calls = self._ask(
            "combined",
            build_combined_message(question, expected, answer, chunks),
            ("correct", "faithful"),
        )
        return Verdict(scores["correct"], reason, calls), Verdict(scores["faithful"], reason, calls)


def build_judge(settings: Settings) -> Judge:
    """Raises LLMConfigError (message names the setting only) if the judge key is missing."""
    llm = LLMClient(
        settings.llm_role("judge"),
        fallback_model=None,  # a judge never silently switches model
        max_retries=JUDGE_429_RETRIES,
        max_wait_seconds=JUDGE_MAX_WAIT_SECONDS,
        timeout=settings.llm_timeout_seconds,
    )
    versions = {
        "correctness": settings.judge_correctness_prompt_version,
        "faithfulness": settings.judge_faithfulness_prompt_version,
        "combined": settings.judge_combined_prompt_version,
    }
    texts = {
        name: (settings.prompts_dir / f"{version}.txt").read_text(encoding="utf-8").strip()
        for name, version in versions.items()
    }
    return Judge(llm, texts["correctness"], texts["faithfulness"], texts["combined"], versions)


__all__ = ["Judge", "JudgeError", "Verdict", "build_judge", "LLMConfigError"]
