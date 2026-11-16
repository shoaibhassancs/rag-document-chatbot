"""The LLM judge (qwen/qwen3.8-27b by default, a different family from the answerer).

Two verdicts per answered question: correctness (compared with the staff-written expected answer)
and faithfulness (supported by the chunks the answer was based on). `--judge-calls 1` asks for both
in one call. The judge returns JSON; this module parses it strictly:
  * the score must be the integer 0 or 1 (or a JSON boolean); "yes", 2, null, "1" are rejected;
  * a bad reply is retried once; a second bad reply (or an LLM error) raises JudgeError;
  * the caller records JudgeError as `judge_error` and the scorer excludes that verdict and counts
    it as unjudged. A failure is NEVER turned into a 0 or a 1.
Everything given to the judge (question, answers, chunks) is data: wrapped in tags with tag-like
text escaped, and the judge prompt says so.
"""

import re
from dataclasses import dataclass

from app.config import Settings
from app.generation.answer import AnswerFormatError, parse_model_json
from app.generation.llm import LLMClient, LLMConfigError, LLMError

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
        last = "no reply"
        for _ in range(2):  # one retry on an unusable reply
            calls += 1
            try:
                result = self._llm.complete(self._prompts[which], user)
            except LLMError as exc:
                raise JudgeError(f"{type(exc).__name__}") from None
            if result.finish_reason == "length":
                last = "reply cut off at the output cap"
                continue
            try:
                scores, reason = parse_verdict(result.text, keys)
            except AnswerFormatError as exc:
                last = str(exc)
                continue
            return scores, reason, calls
        raise JudgeError(f"unusable judge reply twice ({last})")

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
        max_retries=settings.llm_max_retries,
        max_wait_seconds=settings.llm_max_wait_seconds,
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
