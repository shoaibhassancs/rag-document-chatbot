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

    def estimate_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def embed(self, texts: list[str], input_type: str) -> EmbedResult:
        if self.fail_after_requests is not None and self.requests >= self.fail_after_requests:
            raise EmbeddingAccountError("fake: quota exhausted")
        self.requests += 1
        self.texts_embedded.extend(texts)
        self.input_types.append(input_type)
        return EmbedResult(
            [fake_vector(t, self.dimensions) for t in texts],
            sum(estimate_tokens(t) for t in texts),
        )
