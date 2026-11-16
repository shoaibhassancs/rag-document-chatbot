"""Embedder interface and the Voyage implementation.

Voyage free tier (no payment method): 3 requests/min and 10,000 tokens/min together. Each
`embed` call is exactly one request: callers build batches with `plan_batches`, sized by
estimated tokens, never by text count.

Error policy (the HTTP status meanings other than 429 are not verified against Voyage's
error-code page, which was unreachable, so every non-429 4xx stops the run):
  429            wait for Retry-After if present, else 20/40/60 s; give up after 5 tries
  401/402/403    EmbeddingAccountError: stop at once and tell the owner (billing/quota/key)
  other 4xx      EmbeddingRequestError: our request was wrong; no retry
  5xx, timeouts  retried 3 times with backoff, then EmbeddingServiceError
"""

import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.ingest.ratelimit import RateLimiter
from app.ingest.textutil import estimate_tokens

log = logging.getLogger(__name__)

InputType = Literal["document", "query"]


class EmbeddingError(Exception):
    """Base class. Messages never include request headers or keys."""


class EmbeddingAccountError(EmbeddingError):
    """401/402/403 from the provider: key, billing or quota problem. Stop and tell the owner."""


class EmbeddingRateLimited(EmbeddingError):
    """429 kept coming back after all retries. Progress is saved; run again later."""


class EmbeddingServiceError(EmbeddingError):
    """5xx, timeouts or connection errors kept happening after all retries."""


class EmbeddingRequestError(EmbeddingError):
    """The provider rejected our request (bad input). Retrying will not help."""


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    tokens: int  # tokens the provider reported (or our estimate when it reported none)


class Embedder(Protocol):
    model: str
    dimensions: int
    max_batch_tokens: int

    def estimate_tokens(self, text: str) -> int: ...

    def embed(
        self, texts: list[str], input_type: InputType, max_wait: float | None = None
    ) -> EmbedResult: ...


def plan_batches(
    token_counts: list[int], max_tokens: int, max_items: int = 1000
) -> list[list[int]]:
    """Group item indexes into ordered batches of at most `max_tokens` estimated tokens."""
    batches: list[list[int]] = []
    current: list[int] = []
    used = 0
    for index, tokens in enumerate(token_counts):
        if tokens > max_tokens:
            raise EmbeddingRequestError(
                f"A single text is about {tokens} tokens, over the {max_tokens} batch limit."
            )
        if current and (used + tokens > max_tokens or len(current) >= max_items):
            batches.append(current)
            current, used = [], 0
        current.append(index)
        used += tokens
    if current:
        batches.append(current)
    return batches


class VoyageEmbedder:
    FIRST_BATCH_SAFETY = 1.15  # our chars/4 estimate is rough, so start a little high
    RETRY_429_DELAYS = (20, 40, 60, 60)  # used when there is no Retry-After header
    RETRY_5XX_DELAYS = (2, 5, 10)
    MAX_RETRY_AFTER = 120

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str,
        limiter: RateLimiter,
        max_batch_tokens: int = 8000,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ):
        self.model = model
        self.dimensions = dimensions
        self.max_batch_tokens = max_batch_tokens
        self._api_key = api_key  # never logged and never placed in an exception message
        self._url = base_url.rstrip("/") + "/embeddings"
        self._limiter = limiter
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._ratio = self.FIRST_BATCH_SAFETY
        self._raw_total = 0  # our raw estimates, for calibration
        self._actual_total = 0
        self.requests = 0
        self.tokens_actual = 0
        self.tokens_estimated = 0

    def estimate_tokens(self, text: str) -> int:
        return math.ceil(estimate_tokens(text) * self._ratio)

    def _calibrate(self, raw_estimate: int, actual: int) -> None:
        self._raw_total += raw_estimate
        self._actual_total += actual
        if self._raw_total > 0 and self._actual_total > 0:
            observed = self._actual_total / self._raw_total
            self._ratio = min(max(observed * 1.05, 0.5), 3.0)

    def embed(
        self, texts: list[str], input_type: InputType, max_wait: float | None = None
    ) -> EmbedResult:
        """`max_wait` caps the TOTAL seconds this call may spend waiting (limiter and backoff).
        Chat queries pass a small cap so a user is never left hanging; ingestion passes none and
        keeps the long retry schedule. Over the cap: EmbeddingRateLimited, nothing is sent late."""
        if not texts:
            return EmbedResult([], 0)
        waited = 0.0

        def spend(seconds: float) -> None:
            nonlocal waited
            if max_wait is not None and waited + seconds > max_wait:
                raise EmbeddingRateLimited(
                    f"Voyage would need a {seconds:.0f}s wait; the cap for this call is "
                    f"{max_wait:.0f}s."
                )
            waited += seconds

        raw = sum(estimate_tokens(t) for t in texts)
        budget = min(sum(self.estimate_tokens(t) for t in texts), self._limiter.tpm)
        body = {
            "input": texts,
            "model": self.model,
            "input_type": input_type,
            "output_dimension": self.dimensions,
            "truncation": False,
        }
        rate_attempt = 0
        server_attempt = 0
        while True:
            spend(self._limiter.seconds_until_allowed(budget))
            event = self._limiter.acquire(budget)
            self.requests += 1
            self.tokens_estimated += budget
            try:
                response = self._client.post(
                    self._url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.HTTPError as exc:
                if server_attempt >= len(self.RETRY_5XX_DELAYS):
                    raise EmbeddingServiceError(
                        f"Voyage unreachable after retries ({type(exc).__name__})."
                    ) from None
                spend(self.RETRY_5XX_DELAYS[server_attempt])
                self._backoff("network error", self.RETRY_5XX_DELAYS[server_attempt])
                server_attempt += 1
                continue

            status = response.status_code
            if status == 200:
                return self._parse(response, texts, raw, event)
            if status == 429:
                if rate_attempt >= len(self.RETRY_429_DELAYS):
                    raise EmbeddingRateLimited(
                        "Voyage kept returning 429 (rate limit). Progress is saved; run again later."  # noqa: E501
                    )
                delay = self._retry_after(response, self.RETRY_429_DELAYS[rate_attempt])
                spend(delay)
                self._backoff("429 rate limited", delay)
                rate_attempt += 1
                continue
            if status in (401, 402, 403):
                raise EmbeddingAccountError(
                    f"Voyage returned HTTP {status}: a key, billing or quota problem. "
                    f"Stop and tell the owner. Detail: {_detail(response)}"
                )
            if 500 <= status < 600:
                if server_attempt >= len(self.RETRY_5XX_DELAYS):
                    raise EmbeddingServiceError(f"Voyage returned HTTP {status} after retries.")
                spend(self.RETRY_5XX_DELAYS[server_attempt])
                self._backoff(f"HTTP {status}", self.RETRY_5XX_DELAYS[server_attempt])
                server_attempt += 1
                continue
            raise EmbeddingRequestError(
                f"Voyage rejected the request with HTTP {status}: {_detail(response)}"
            )

    def _backoff(self, reason: str, seconds: float) -> None:
        log.warning("Voyage %s; waiting %.0f s before retrying", reason, seconds)
        self._sleep(seconds)

    def _retry_after(self, response: httpx.Response, default: float) -> float:
        header = response.headers.get("retry-after")
        try:
            return min(float(header), self.MAX_RETRY_AFTER) if header else default
        except ValueError:
            return default

    def _parse(self, response: httpx.Response, texts: list[str], raw: int, event) -> EmbedResult:
        try:
            payload = response.json()
            rows = sorted(payload["data"], key=lambda row: row["index"])
            vectors = [row["embedding"] for row in rows]
            reported = payload.get("usage", {}).get("total_tokens")
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingRequestError(
                "Voyage returned a response in an unexpected shape."
            ) from exc
        if len(vectors) != len(texts):
            raise EmbeddingRequestError(
                f"Voyage returned {len(vectors)} vectors for {len(texts)} texts."
            )
        if any(len(v) != self.dimensions for v in vectors):
            raise EmbeddingRequestError(
                f"Voyage returned vectors whose size is not EMBEDDING_DIMENSIONS={self.dimensions}."
            )
        actual = int(reported) if reported is not None else raw
        self._limiter.correct(event, actual)
        self.tokens_actual += actual
        self._calibrate(raw, actual)
        return EmbedResult(vectors, actual)


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail") or response.json().get("error") or response.text
    except ValueError:
        detail = response.text
    return str(detail)[:200]


class CachingEmbedder:
    """Wraps an embedder and remembers query embeddings, so a repeated question costs no request."""

    def __init__(self, inner: Embedder, max_entries: int = 256):
        self._inner = inner
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max = max_entries
        self.hits = 0
        self.misses = 0
        self.model = inner.model
        self.dimensions = inner.dimensions
        self.max_batch_tokens = inner.max_batch_tokens

    def estimate_tokens(self, text: str) -> int:
        return self._inner.estimate_tokens(text)

    def embed(
        self, texts: list[str], input_type: InputType, max_wait: float | None = None
    ) -> EmbedResult:
        return self._inner.embed(texts, input_type, max_wait)

    def embed_query(self, text: str, max_wait: float | None = None) -> list[float]:
        key = " ".join(text.split()).lower()
        if key in self._cache:
            self._cache.move_to_end(key)
            self.hits += 1
            return self._cache[key]
        self.misses += 1
        vector = self._inner.embed([text], "query", max_wait).vectors[0]
        self._cache[key] = vector
        if len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return vector


class EmbeddingNotConfigured(EmbeddingError):
    """No usable embedding provider or key is configured."""


def build_embedder(
    settings,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> VoyageEmbedder:
    """Create the configured embedder. Only Voyage is built; other providers are a config swap
    that would add another class behind the same interface."""
    if settings.embedding_provider != "voyage":
        raise EmbeddingNotConfigured(
            f"Embedding provider '{settings.embedding_provider}' is not built."
        )
    if settings.voyage_api_key is None or not settings.voyage_api_key.get_secret_value():
        raise EmbeddingNotConfigured("VOYAGE_API_KEY is not set.")
    limiter = RateLimiter(
        settings.voyage_requests_per_minute, settings.voyage_tokens_per_minute, sleep=sleep
    )
    return VoyageEmbedder(
        api_key=settings.voyage_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.embedding_base_url,
        limiter=limiter,
        max_batch_tokens=settings.voyage_max_batch_tokens,
        transport=transport,
        sleep=sleep,
    )
