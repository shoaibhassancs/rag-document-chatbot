"""Sliding-window limiter for two limits that apply together (Voyage free tier: 3 requests/min
and 10,000 tokens/min). The clock and sleep are injectable so tests run instantly."""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Event:
    at: float
    tokens: int


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        window_seconds: float = 60.0,
    ):
        if requests_per_minute < 1 or tokens_per_minute < 1:
            raise ValueError("limits must be positive")
        self.rpm = requests_per_minute
        self.tpm = tokens_per_minute
        self.window = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._events: deque[_Event] = deque()

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0].at >= self.window:
            self._events.popleft()

    def seconds_until_allowed(self, tokens: int) -> float:
        """0 if a request of `tokens` can go now, otherwise how long to wait."""
        if tokens > self.tpm:
            raise ValueError(f"A single request of {tokens} tokens can never fit {self.tpm}/min")
        now = self._clock()
        self._prune(now)
        wait = 0.0
        if len(self._events) >= self.rpm:
            wait = max(wait, self._events[0].at + self.window - now)
        used = sum(e.tokens for e in self._events)
        if used + tokens > self.tpm:
            running = used
            for event in self._events:
                running -= event.tokens
                if running + tokens <= self.tpm:
                    wait = max(wait, event.at + self.window - now)
                    break
        return max(wait, 0.0)

    def acquire(self, tokens: int) -> _Event:
        """Block until a request of `tokens` fits both limits, then record it."""
        while True:
            wait = self.seconds_until_allowed(tokens)
            if wait <= 0:
                break
            self._sleep(wait + 0.05)
        event = _Event(self._clock(), tokens)
        self._events.append(event)
        return event

    def correct(self, event: _Event, actual_tokens: int) -> None:
        """Replace the estimate with the real usage the provider reported."""
        event.tokens = actual_tokens

    def penalise(self, seconds: float) -> None:
        """Sleep for a provider-requested pause (after a 429)."""
        self._sleep(seconds)


def simulate_duration(
    batch_tokens: list[int], requests_per_minute: int, tokens_per_minute: int
) -> float:
    """Estimated seconds to send these batches in order under both limits (fake clock)."""
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    limiter = RateLimiter(
        requests_per_minute, tokens_per_minute, clock=lambda: clock["now"], sleep=sleep
    )
    for tokens in batch_tokens:
        limiter.acquire(tokens)
    return clock["now"]
