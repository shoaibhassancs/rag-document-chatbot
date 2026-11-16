"""Pacing under the free-tier limits, so a run never trips a 429 on purpose.

Each model (and Voyage) gets a sliding 60-second window of reserved tokens and requests, plus a
per-day budget. A call reserves its prompt estimate PLUS its output cap before it is made (the
same rule as the rest of the project: Groq reserves the cap at admission). `acquire` sleeps until
the window has room. If the daily budget would be exceeded it raises DailyBudgetExceeded: the
runner stops and the saved state lets the run continue tomorrow.

The clock and sleep are injected so tests run without real waiting.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


class DailyBudgetExceeded(Exception):
    pass


class CallTooLarge(Exception):
    """One call alone would exceed the per-minute limit; it could never be admitted."""


@dataclass
class Limits:
    tokens_per_minute: int
    requests_per_minute: int
    tokens_per_day: int | None = None
    requests_per_day: int | None = None
    margin: float = 0.9

    def tpm(self) -> int:
        return int(self.tokens_per_minute * self.margin)

    def rpm(self) -> int:
        return max(1, int(self.requests_per_minute * self.margin))


class Pacer:
    def __init__(
        self,
        limits: dict[str, Limits],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        daily_used: dict[str, tuple[int, int]] | None = None,
    ):
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._window: dict[str, deque[tuple[float, int]]] = {k: deque() for k in limits}
        # model -> (tokens, requests) already used today (restored from the state file on resume)
        self._day: dict[str, list[int]] = {
            k: list((daily_used or {}).get(k, (0, 0))) for k in limits
        }
        self.slept = 0.0

    def acquire(self, model: str, tokens: int) -> None:
        limits = self._limits[model]
        if tokens > limits.tpm():
            raise CallTooLarge(f"{model}: {tokens} tokens exceeds the per-minute budget")
        day_tokens, day_requests = self._day[model]
        if limits.tokens_per_day is not None and day_tokens + tokens > int(
            limits.tokens_per_day * limits.margin
        ):
            raise DailyBudgetExceeded(f"{model}: the daily token budget would be exceeded")
        if limits.requests_per_day is not None and day_requests + 1 > int(
            limits.requests_per_day * limits.margin
        ):
            raise DailyBudgetExceeded(f"{model}: the daily request budget would be exceeded")
        while True:
            now = self._clock()
            window = self._window[model]
            while window and now - window[0][0] >= 60:
                window.popleft()
            used = sum(t for _, t in window)
            if used + tokens <= limits.tpm() and len(window) + 1 <= limits.rpm():
                break
            wait = max(0.0, 60 - (now - window[0][0])) if window else 0.0
            wait = max(wait, 0.01)
            self.slept += wait
            self._sleep(wait)
        self._window[model].append((self._clock(), tokens))
        self._day[model][0] += tokens
        self._day[model][1] += 1

    def used_today(self, model: str) -> tuple[int, int]:
        tokens, requests = self._day[model]
        return tokens, requests


def estimate_tokens_for_text(text: str) -> int:
    """About four characters per token (an estimate; the real count is only known afterwards)."""
    return max(1, len(text) // 4)
