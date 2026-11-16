"""The real Voyage client against httpx.MockTransport. Nothing here touches the network."""

import json

import httpx
import pytest

from app.config import Settings
from app.ingest.embedder import (
    CachingEmbedder,
    EmbeddingAccountError,
    EmbeddingNotConfigured,
    EmbeddingRateLimited,
    EmbeddingRequestError,
    EmbeddingServiceError,
    VoyageEmbedder,
    build_embedder,
    plan_batches,
)
from app.ingest.ratelimit import RateLimiter

SECRET = "fake-voyage-secret-key"
DIMS = 4


class Harness:
    """Builds an embedder with a scripted sequence of responses and a recording sleep."""

    def __init__(self, responses, dims=DIMS):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.dims = dims
        self.now = 0.0  # fake clock: the limiter's sleeps move it forward

        def handler(request):
            self.requests.append(request)
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        self.embedder = VoyageEmbedder(
            api_key=SECRET,
            model="voyage-test",
            dimensions=dims,
            base_url="https://voyage.test/v1",
            limiter=RateLimiter(3, 10_000, clock=lambda: self.now, sleep=self._advance),
            transport=httpx.MockTransport(handler),
            sleep=self.sleeps.append,
        )

    def _advance(self, seconds: float) -> None:
        self.now += seconds


def ok(n_texts: int, tokens: int | None = 10, dims: int = DIMS, shuffle=False) -> httpx.Response:
    data = [
        {"object": "embedding", "index": i, "embedding": [float(i)] * dims} for i in range(n_texts)
    ]
    if shuffle:
        data.reverse()
    body = {"data": data}
    if tokens is not None:
        body["usage"] = {"total_tokens": tokens}
    return httpx.Response(200, json=body)


def test_request_shape_headers_and_result():
    h = Harness([ok(2, tokens=17)])
    result = h.embedder.embed(["alpha", "beta"], "document")
    request = h.requests[0]
    assert str(request.url) == "https://voyage.test/v1/embeddings"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(request.content)
    assert body == {
        "input": ["alpha", "beta"],
        "model": "voyage-test",
        "input_type": "document",
        "output_dimension": DIMS,
        "truncation": False,
    }
    assert result.vectors == [[0.0] * DIMS, [1.0] * DIMS]
    assert result.tokens == 17 and h.embedder.requests == 1 and h.embedder.tokens_actual == 17


def test_query_input_type_is_sent():
    h = Harness([ok(1)])
    h.embedder.embed(["what time?"], "query")
    assert json.loads(h.requests[0].content)["input_type"] == "query"


def test_vectors_are_returned_in_input_order_even_if_the_api_shuffles_them():
    h = Harness([ok(3, shuffle=True)])
    assert [v[0] for v in h.embedder.embed(["a", "b", "c"], "document").vectors] == [0.0, 1.0, 2.0]


def test_429_with_retry_after_waits_that_long_then_succeeds():
    h = Harness([httpx.Response(429, headers={"retry-after": "7"}), ok(1)])
    h.embedder.embed(["x"], "document")
    assert h.sleeps == [7.0] and len(h.requests) == 2


def test_429_without_retry_after_backs_off_20_then_40_seconds():
    h = Harness([httpx.Response(429), httpx.Response(429), ok(1)])
    h.embedder.embed(["x"], "document")
    assert h.sleeps == [20, 40]


def test_429_retry_after_is_capped_and_garbage_falls_back():
    h = Harness(
        [
            httpx.Response(429, headers={"retry-after": "9999"}),
            httpx.Response(429, headers={"retry-after": "soon"}),
            ok(1),
        ]
    )
    h.embedder.embed(["x"], "document")
    assert h.sleeps == [120, 40]


def test_429_that_never_clears_stops_cleanly_after_five_tries():
    h = Harness([httpx.Response(429) for _ in range(5)])
    with pytest.raises(EmbeddingRateLimited) as err:
        h.embedder.embed(["x"], "document")
    assert len(h.requests) == 5 and h.sleeps == [20, 40, 60, 60]
    assert "saved" in str(err.value)


@pytest.mark.parametrize("status", [401, 402, 403])
def test_account_billing_and_key_errors_stop_immediately_without_retry(status):
    h = Harness([httpx.Response(status, json={"detail": "payment required"})])
    with pytest.raises(EmbeddingAccountError) as err:
        h.embedder.embed(["x"], "document")
    assert len(h.requests) == 1 and h.sleeps == []
    assert str(status) in str(err.value) and "tell the owner" in str(err.value)


def test_other_client_errors_are_not_retried():
    h = Harness([httpx.Response(400, json={"detail": "input too long"})])
    with pytest.raises(EmbeddingRequestError) as err:
        h.embedder.embed(["x"], "document")
    assert len(h.requests) == 1 and "input too long" in str(err.value)


def test_server_errors_are_retried_three_times_then_reported():
    h = Harness([httpx.Response(503) for _ in range(4)])
    with pytest.raises(EmbeddingServiceError):
        h.embedder.embed(["x"], "document")
    assert len(h.requests) == 4 and h.sleeps == [2, 5, 10]


def test_server_error_then_success_and_network_error_then_success():
    h = Harness([httpx.Response(500), httpx.ConnectError("down"), ok(1)])
    h.embedder.embed(["x"], "document")
    assert len(h.requests) == 3 and h.sleeps == [2, 5]


def test_timeouts_exhaust_into_a_service_error_without_leaking_the_key():
    h = Harness([httpx.ReadTimeout("slow") for _ in range(4)])
    with pytest.raises(EmbeddingServiceError) as err:
        h.embedder.embed(["x"], "document")
    assert SECRET not in str(err.value)


def test_no_error_message_contains_the_api_key():
    for response, exc in [
        (httpx.Response(401, text="bad"), EmbeddingAccountError),
        (httpx.Response(400, text="bad"), EmbeddingRequestError),
    ]:
        h = Harness([response])
        with pytest.raises(exc) as err:
            h.embedder.embed(["x"], "document")
        assert SECRET not in str(err.value)


def test_wrong_vector_size_or_count_is_an_error():
    with pytest.raises(EmbeddingRequestError):
        Harness([ok(1, dims=DIMS + 1)]).embedder.embed(["x"], "document")
    with pytest.raises(EmbeddingRequestError):
        Harness([ok(1)]).embedder.embed(["x", "y"], "document")
    with pytest.raises(EmbeddingRequestError):
        Harness([httpx.Response(200, json={"unexpected": True})]).embedder.embed(["x"], "document")


def test_missing_usage_falls_back_to_our_estimate():
    h = Harness([ok(1, tokens=None)])
    assert h.embedder.embed(["x" * 40], "document").tokens == 10


def test_estimates_start_high_then_calibrate_from_reported_usage():
    h = Harness([ok(1, tokens=50)])
    text = "x" * 400  # chars / 4 = 100 raw tokens
    assert h.embedder.estimate_tokens(text) == 115  # first batch: x1.15 safety factor
    h.embedder.embed([text], "document")  # Voyage says only 50 tokens were used
    assert h.embedder.estimate_tokens(text) == 53  # 100 raw * (50/100) * 1.05 margin


def test_empty_input_makes_no_request():
    h = Harness([])
    assert h.embedder.embed([], "document").vectors == [] and h.requests == []


# ----------------------------------------------------------------------------- batching


def test_plan_batches_groups_by_tokens_not_by_count():
    assert plan_batches([300, 300, 300, 100], 700) == [[0, 1], [2, 3]]
    assert plan_batches([10] * 5, 1000, max_items=2) == [[0, 1], [2, 3], [4]]
    assert plan_batches([], 100) == []


def test_a_single_text_over_the_batch_limit_is_an_error():
    with pytest.raises(EmbeddingRequestError):
        plan_batches([900], 800)


# ----------------------------------------------------------------------------- query cache


def test_repeated_queries_are_served_from_the_cache():
    h = Harness([ok(1), ok(1)])
    cached = CachingEmbedder(h.embedder)
    first = cached.embed_query("What are your opening hours?")
    again = cached.embed_query("  what are your OPENING hours?  ")  # same after normalising
    assert first == again and len(h.requests) == 1 and (cached.hits, cached.misses) == (1, 1)
    cached.embed_query("a different question")
    assert len(h.requests) == 2
    assert json.loads(h.requests[0].content)["input_type"] == "query"


def test_cache_evicts_the_oldest_entry():
    h = Harness([ok(1) for _ in range(4)])
    cached = CachingEmbedder(h.embedder, max_entries=2)
    for q in ("one", "two", "three"):
        cached.embed_query(q)
    cached.embed_query("one")  # "one" was evicted when "three" arrived, so this is a miss again
    assert len(h.requests) == 4 and cached.misses == 4 and cached.hits == 0


# ----------------------------------------------------------------------------- factory


def test_build_embedder_requires_a_key_and_the_voyage_provider():
    with pytest.raises(EmbeddingNotConfigured):
        build_embedder(Settings(_env_file=None, voyage_api_key=None))
    with pytest.raises(EmbeddingNotConfigured):
        build_embedder(Settings(_env_file=None, voyage_api_key="k", embedding_provider="other"))
    built = build_embedder(Settings(_env_file=None, voyage_api_key="k"))
    assert built.model == "voyage-4" and built.dimensions == 1024


# ----------------------------------------------------------------------------- wait cap (chat)


def test_with_a_cap_a_429_wait_over_the_cap_gives_up_without_sleeping():
    h = Harness([httpx.Response(429), ok(1)])  # no Retry-After: the default first wait is 20 s
    with pytest.raises(EmbeddingRateLimited, match="cap"):
        h.embedder.embed(["q"], "query", max_wait=10)
    assert h.sleeps == [] and len(h.requests) == 1  # it did not wait, and did not retry


def test_with_a_cap_short_waits_still_succeed_and_the_total_is_bounded():
    h = Harness(
        [
            httpx.Response(429, headers={"retry-after": "8"}),
            httpx.Response(429, headers={"retry-after": "8"}),
            ok(1),
        ]
    )
    assert len(h.embedder.embed(["q"], "query", max_wait=20).vectors) == 1
    assert sum(h.sleeps) == 16
    h = Harness([httpx.Response(429, headers={"retry-after": "8"})] * 3)
    with pytest.raises(EmbeddingRateLimited):
        h.embedder.embed(["q"], "query", max_wait=20)  # 8 + 8 fits, the third 8 would not
    assert sum(h.sleeps) == 16


def test_with_a_cap_the_limiters_own_wait_counts_too():
    h = Harness([ok(1) for _ in range(4)])
    for _ in range(3):
        h.embedder.embed(["q"], "query", max_wait=20)  # three requests fill the window
    with pytest.raises(EmbeddingRateLimited, match="cap"):
        h.embedder.embed(["q"], "query", max_wait=5)  # the 4th would wait about 60 s
    assert len(h.requests) == 3


def test_with_a_cap_server_error_backoff_is_bounded_too():
    h = Harness([httpx.Response(503)] * 4)
    with pytest.raises(EmbeddingRateLimited):
        h.embedder.embed(["q"], "query", max_wait=4)  # waits 2 s, then 5 s would pass the cap
    assert sum(h.sleeps) == 2


def test_without_a_cap_ingestion_keeps_its_long_schedule():
    h = Harness([httpx.Response(429), httpx.Response(429), ok(1)])
    h.embedder.embed(["q"], "document")
    assert h.sleeps == [20, 40]


def test_embed_query_passes_the_cap_down():
    h = Harness([httpx.Response(429), ok(1)])
    with pytest.raises(EmbeddingRateLimited):
        CachingEmbedder(h.embedder).embed_query("q", 5)
    assert h.sleeps == []
