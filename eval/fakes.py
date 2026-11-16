"""Fakes used by `--dry-run` (and handy in tests): no network, no database, deterministic.

A dry run exercises the whole harness (banner, pacing, state file, resume, judge parsing, scoring,
report) with these stand-ins. Its output is NOT a result and is labelled that way everywhere.
"""

from app.services.chat import ChatResult


class FakeClock:
    """A clock that only moves when something sleeps, so pacing runs instantly."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakePipeline:
    """Stands in for ChatService. Answerable questions get the expected answer and sources, out of
    scope ones are refused, vague ones get a clarifying question. Setup messages are answered."""

    def __init__(self, questions: list[dict]):
        self._by_message = {q["question"]: q for q in questions}
        self._ids: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    def _chunk_id(self, name: str) -> int:
        return self._ids.setdefault(name, 1000 + len(self._ids))

    def ask(self, session_id: str | None, message: str) -> ChatResult:
        self.calls.append((session_id or "", message))
        q = self._by_message.get(message)
        usage = {"model": "fake-answer-model", "fallback_used": False, "attempts": 1}
        if q is None:  # a setup turn
            return ChatResult(
                "Fake setup answer.",
                "answered",
                latency_ms=100,
                llm_usage=usage,
                standalone_query=message,
                clarify_action="search",
            )
        if q["category"] == "answerable":
            files = q["expected_sources"]
            retrieved = [
                {"chunk_id": self._chunk_id(f), "file": f, "page": None, "similarity": 0.8}
                for f in files
            ]
            return ChatResult(
                q["expected_answer"],
                "answered",
                sources=[
                    {"file": f, "page": None, "section": None, "document_id": "d"} for f in files
                ],
                latency_ms=100 + len(message),
                retrieved=retrieved,
                llm_usage=usage,
                standalone_query=message,
                clarify_action="search",
                used_chunk_ids=[retrieved[0]["chunk_id"]],
            )
        if q["category"] == "vague":
            return ChatResult(
                "Which one do you mean?",
                "clarify",
                latency_ms=80,
                standalone_query=message,
                clarify_action="clarify",
            )
        return ChatResult(
            "I couldn't find that in our documents.",
            "refused",
            latency_ms=90,
            retrieved=[
                {
                    "chunk_id": self._chunk_id("none.md"),
                    "file": "none.md",
                    "page": None,
                    "similarity": 0.2,
                }
            ],
            standalone_query=message,
            clarify_action="search",
        )

    def chunk_texts(self, ids: list[int]) -> dict[int, str]:
        return {i: f"Fake chunk text {i}." for i in ids}


class FakeJudge:
    """Always says correct and faithful. Records what it was asked."""

    model = "fake-judge-model"
    versions = {"correctness": "fake", "faithfulness": "fake", "combined": "fake"}

    def __init__(self):
        self.calls: list[str] = []

    def correctness(self, question, expected, answer):
        from eval.judge import Verdict

        self.calls.append("correctness")
        return Verdict(1, "fake: correct", 1)

    def faithfulness(self, answer, chunks):
        from eval.judge import Verdict

        self.calls.append("faithfulness")
        return Verdict(1, "fake: faithful", 1)

    def both(self, question, expected, answer, chunks):
        from eval.judge import Verdict

        self.calls.append("both")
        return Verdict(1, "fake: correct", 1), Verdict(1, "fake: faithful", 1)
