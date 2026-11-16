import pytest

from eval.fakes import FakeClock
from eval.pacing import CallTooLarge, DailyBudgetExceeded, Limits, Pacer, estimate_tokens_for_text


def pacer(limits=None, clock=None, daily_used=None):
    clock = clock or FakeClock()
    limits = limits or {"m": Limits(8000, 30, 200_000, 1000, margin=0.9)}
    return Pacer(limits, clock, clock.sleep, daily_used), clock


def test_calls_under_the_limits_do_not_wait():
    p, clock = pacer()
    start = clock.now
    p.acquire("m", 2000)
    p.acquire("m", 2000)
    assert clock.now == start and p.slept == 0


def test_the_token_window_makes_the_third_call_wait_until_the_first_has_expired():
    p, clock = pacer()  # 8,000 x 0.9 = 7,200 tokens per minute
    start = clock.now
    p.acquire("m", 3000)
    clock.sleep(5)
    p.acquire("m", 3000)
    p.acquire("m", 3000)  # 9,000 > 7,200: must wait for the first call to leave the window
    assert clock.now - start >= 60 and clock.now - start < 70


def test_the_window_slides_after_sixty_seconds():
    p, clock = pacer()
    p.acquire("m", 7000)
    clock.sleep(61)
    start = clock.now
    p.acquire("m", 7000)
    assert clock.now == start


def test_the_request_limit_applies_even_for_tiny_calls():
    p, clock = pacer({"m": Limits(8000, 3, margin=0.9)})  # int(3 x 0.9) = 2 requests a minute
    start = clock.now
    p.acquire("m", 10)
    p.acquire("m", 10)
    p.acquire("m", 10)
    assert clock.now - start >= 59


def test_models_have_independent_windows():
    limits = {"a": Limits(8000, 30, margin=0.9), "b": Limits(8000, 30, margin=0.9)}
    p, clock = pacer(limits)
    start = clock.now
    p.acquire("a", 7000)
    p.acquire("b", 7000)
    assert clock.now == start
    p.acquire("a", 7000)
    assert clock.now - start >= 59


def test_a_call_larger_than_the_minute_budget_can_never_run():
    p, _ = pacer()
    with pytest.raises(CallTooLarge):
        p.acquire("m", 7300)


def test_the_daily_token_budget_stops_the_run_instead_of_waiting_a_day():
    p, clock = pacer({"m": Limits(8000, 30, 10_000, 1000, margin=0.9)})  # 9,000 a day
    p.acquire("m", 4000)
    clock.sleep(61)
    p.acquire("m", 4000)
    clock.sleep(61)
    with pytest.raises(DailyBudgetExceeded):
        p.acquire("m", 2000)
    assert p.used_today("m") == (8000, 2)


def test_the_daily_request_budget_stops_the_run():
    p, clock = pacer({"m": Limits(8000, 30, 200_000, 2, margin=1.0)})
    p.acquire("m", 10)
    p.acquire("m", 10)
    with pytest.raises(DailyBudgetExceeded):
        p.acquire("m", 10)


def test_usage_restored_from_the_state_file_counts_against_the_day():
    p, _ = pacer({"m": Limits(8000, 30, 10_000, 1000, margin=0.9)}, daily_used={"m": (8000, 5)})
    with pytest.raises(DailyBudgetExceeded):
        p.acquire("m", 2000)


def test_the_default_margin_leaves_headroom():
    limits = Limits(8000, 30, 200_000, 1000)
    assert limits.tpm() == 7200 and limits.rpm() == 27
    assert Limits(10_000, 3, margin=0.9).rpm() == 2 and Limits(10_000, 1, margin=0.1).rpm() == 1


def test_waiting_never_really_sleeps_in_tests():
    p, clock = pacer()
    for _ in range(10):
        p.acquire("m", 3000)
    assert p.slept > 0 and clock.now > 1000  # simulated time only


def test_token_estimates_are_about_four_characters_per_token():
    assert estimate_tokens_for_text("a" * 400) == 100 and estimate_tokens_for_text("") == 1
