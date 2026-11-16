"""A fake embedder: deterministic vectors, no network, optional failure after N requests."""

import hashlib
import math

from app.ingest.embedder import EmbeddingAccountError, EmbedResult
from app.ingest.textutil import estimate_tokens


def fake_vector(text: str, dimensions: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [digest[i % len(digest)] - 127.5 for i in range(dimensions)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


class FakeEmbedder:
    def __init__(
        self,
        dimensions: int = 8,
        model: str = "fake-model",
        max_batch_tokens: int = 400,
        fail_after_requests: int | None = None,
    ):
        self.model = model
        self.dimensions = dimensions
        self.max_batch_tokens = max_batch_tokens
        self.fail_after_requests = fail_after_requests
        self.requests = 0
        self.texts_embedded: list[str] = []
        self.input_types: list[str] = []
        self.max_waits: list[float | None] = []

    def estimate_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def embed(
        self, texts: list[str], input_type: str, max_wait: float | None = None
    ) -> EmbedResult:
        if self.fail_after_requests is not None and self.requests >= self.fail_after_requests:
            raise EmbeddingAccountError("fake: quota exhausted")
        self.requests += 1
        self.texts_embedded.extend(texts)
        self.input_types.append(input_type)
        self.max_waits.append(max_wait)
        return EmbedResult(
            [fake_vector(t, self.dimensions) for t in texts],
            sum(estimate_tokens(t) for t in texts),
        )


def fake_llm_result(text: str = "{}", **overrides):
    from app.generation.llm import LLMResult

    values = dict(
        text=text,
        model="fake-llm",
        finish_reason="stop",
        prompt_tokens=100,
        completion_tokens=40,
        total_tokens=140,
        reasoning_tokens=10,
        cached_tokens=0,
        latency_ms=5,
        attempts=1,
        fallback_used=False,
    )
    values.update(overrides)
    return LLMResult(**values)


class FakeAnswerer:
    """Stands in for the answer model. Records every call. `reply` is an Answer-building
    function of (question, hits); `error` is raised instead when set."""

    prompt_version = "fake"

    def __init__(self, reply=None, error: Exception | None = None):
        from app.generation.answer import Answer

        self._reply = reply or (
            lambda q, hits: Answer(True, "Fake answer.", hits[:1], fake_llm_result())
        )
        self._error = error
        self.calls: list[tuple[str, list]] = []

    def answer(self, question, hits):
        self.calls.append((question, list(hits)))
        if self._error is not None:
            raise self._error
        return self._reply(question, hits)
