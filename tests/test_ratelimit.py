import pytest

from app.ingest.ratelimit import RateLimiter, simulate_duration


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def limiter(rpm=3, tpm=10_000):
    clock = Clock()
    return RateLimiter(rpm, tpm, clock=clock.time, sleep=clock.sleep), clock


def test_three_small_requests_go_immediately_then_the_fourth_waits_for_the_window():
    lim, clock = limiter()
    for _ in range(3):
        lim.acquire(100)
    assert clock.now == 0
    lim.acquire(100)
    assert 60 <= clock.now < 61  # waited for the first request to leave the 60 s window


def test_token_limit_alone_forces_a_wait():
    lim, clock = limiter(rpm=100, tpm=10_000)
    lim.acquire(8000)
    lim.acquire(8000)  # 16,000 in one minute is over the limit
    assert 60 <= clock.now < 61


def test_both_limits_apply_together():
    lim, clock = limiter()
    lim.acquire(4000)
    lim.acquire(4000)
    clock.now += 10
    assert lim.seconds_until_allowed(4000) == pytest.approx(50)  # tokens: 12,000 > 10,000
    assert lim.seconds_until_allowed(2000) == 0  # tokens fit, and only 2 requests so far


def test_window_slides_and_old_requests_are_forgotten():
    lim, clock = limiter()
    lim.acquire(9000)
    clock.now += 61
    assert lim.seconds_until_allowed(9000) == 0


def test_waits_only_as_long_as_needed_for_enough_tokens():
    lim, clock = limiter(rpm=100, tpm=10_000)
    lim.acquire(5000)
    clock.now += 20
    lim.acquire(5000)
    clock.now += 10  # now at t=30; tokens used 10,000
    # a 5,000 request fits once the first event (t=0) leaves the window at t=60
    assert lim.seconds_until_allowed(5000) == pytest.approx(30)


def test_actual_usage_corrects_the_estimate():
    lim, clock = limiter()
    event = lim.acquire(9000)
    lim.correct(event, 1000)
    assert lim.seconds_until_allowed(9000) == 0


def test_request_bigger_than_the_per_minute_budget_is_an_error():
    lim, _ = limiter(tpm=1000)
    with pytest.raises(ValueError):
        lim.acquire(1001)


def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        RateLimiter(0, 100)


def test_simulate_duration_matches_free_tier_expectations():
    # Four 6,000-token batches: the token limit allows one per minute, so 3 waits of ~60 s.
    seconds = simulate_duration([6000] * 4, 3, 10_000)
    assert 180 <= seconds < 190
    # Eight tiny batches are limited by 3 requests per minute instead.
    assert 120 <= simulate_duration([100] * 8, 3, 10_000) < 130
    assert simulate_duration([], 3, 10_000) == 0
