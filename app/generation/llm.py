"""One OpenAI-compatible chat client for every LLM role (Groq today; any compatible provider by
config). Responsibilities: send the request, retry sensibly, fall back from the answer model to
a smaller one when its quota is gone, and report token usage so cost and limits are visible.

Retry policy (Groq documents `retry-after` in seconds on 429 responses only):
  429            wait for Retry-After if it is at most `max_wait_seconds`, retry up to
                 `max_retries` times. A longer Retry-After means a daily cap: do not wait, move to
                 the fallback model at once (if there is one), else give up.
  5xx, timeouts  retried twice (1 s, 3 s), then LLMBusyError. No fallback: the spec reserves the
                 fallback for the daily cap and repeated 429s.
  401/403        LLMConfigError: the key is wrong or lacks access. Retrying cannot help.
  400 "Failed to generate JSON" (Groq json_object mode could not produce valid JSON; seen on
                 the 20b): retry ONCE on the same model, then try the fallback model once (no
                 extra retry there), then LLMRequestError. PROVISIONAL (DECISIONS.md D-037).
  other 4xx      LLMRequestError: our request was wrong. No retry, no fallback.
Messages never include the key, the prompt or the question.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from app.config import LLMRole

log = logging.getLogger(__name__)

SERVER_RETRY_DELAYS = (1.0, 3.0)
DEFAULT_429_DELAYS = (2.0, 5.0, 10.0)  # only used if a 429 carries no Retry-After


class LLMError(Exception):
    """Base class for LLM failures. `reason` is a short machine-readable cause (for example
    `rate_limited`, `json_generation_failed`); `failed_generation` is the raw text Groq puts in a
    JSON-failure error body, if any. Neither ever contains the key or the prompt."""

    def __init__(
        self, message: str = "", *, reason: str | None = None, failed_generation: str | None = None
    ):
        super().__init__(message)
        self.reason = reason
        self.failed_generation = failed_generation


class LLMBusyError(LLMError):
    """Rate limited, quota exhausted or the service is down, after retries. Show 'busy'."""


class LLMConfigError(LLMError):
    """No key, or the provider refused the key (401/403). Needs the owner."""


class LLMRequestError(LLMError):
    """The provider rejected our request (HTTP 400 and similar). Retrying will not help."""


class _QuotaExhausted(Exception):
    """Internal: this model cannot serve the request now; try the fallback if there is one."""


class _JSONGenerationFailed(Exception):
    """Internal: the model could not produce valid JSON (HTTP 400). Retry once, then fall back."""

    def __init__(self, message: str, failed_generation: str | None = None):
        super().__init__(message)
        self.failed_generation = failed_generation


@dataclass
class LLMResult:
    text: str
    model: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    latency_ms: int
    attempts: int
    fallback_used: bool
    rate_limit: dict[str, str] = field(default_factory=dict)  # x-ratelimit-* and retry-after


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class LLMClient:
    def __init__(
        self,
        role: LLMRole,
        *,
        fallback_model: str | None = None,
        max_retries: int = 3,
        max_wait_seconds: float = 20.0,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if role.api_key is None or not role.api_key.get_secret_value():
            raise LLMConfigError(f"The API key for the '{role.name}' model is not set.")
        self.role = role
        self._key = role.api_key.get_secret_value()  # never logged, never in a message
        self._url = role.base_url.rstrip("/") + "/chat/completions"
        self._models = [role.model]
        if fallback_model and fallback_model != role.model:
            self._models.append(fallback_model)
        self._max_retries = max_retries
        self._max_wait = max_wait_seconds
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout, transport=transport)

    @property
    def model(self) -> str:
        return self.role.model

    def complete(self, system: str, user: str, *, json_mode: bool = True) -> LLMResult:
        started = time.perf_counter()
        for index, model in enumerate(self._models):
            try:
                result = self._call(model, system, user, json_mode, index > 0)
            except (_QuotaExhausted, _JSONGenerationFailed) as exc:
                if index + 1 < len(self._models):
                    log.warning(
                        "llm_fallback role=%s from=%s to=%s reason=%s",
                        self.role.name,
                        model,
                        self._models[index + 1],
                        exc,
                    )
                    continue
                if isinstance(exc, _JSONGenerationFailed):
                    raise LLMRequestError(
                        str(exc),
                        reason="json_generation_failed",
                        failed_generation=exc.failed_generation,
                    ) from None
                raise LLMBusyError(str(exc), reason="rate_limited") from None
            result.latency_ms = round((time.perf_counter() - started) * 1000)
            return result
        raise LLMBusyError("No model available.")  # unreachable: the loop returns or raises

    def _call(self, model: str, system: str, user: str, json_mode: bool, fell_back: bool):
        body: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_completion_tokens": self.role.max_output_tokens,
        }
        if self.role.reasoning_effort:
            body["reasoning_effort"] = self.role.reasoning_effort
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._key}"}

        attempts = 0
        rate_tries = 0
        server_tries = 0
        json_retried = False
        while True:
            attempts += 1
            began = time.perf_counter()
            try:
                response = self._client.post(self._url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                if server_tries >= len(SERVER_RETRY_DELAYS):
                    raise LLMBusyError(
                        f"LLM unreachable ({type(exc).__name__}).", reason="unreachable"
                    ) from None
                self._wait("network error", SERVER_RETRY_DELAYS[server_tries])
                server_tries += 1
                continue

            status = response.status_code
            if status == 200:
                return self._parse(response, model, attempts, fell_back, began)
            if status == 429:
                wait = _retry_after(response)
                if wait is None:
                    wait = DEFAULT_429_DELAYS[min(rate_tries, len(DEFAULT_429_DELAYS) - 1)]
                if wait > self._max_wait:
                    raise _QuotaExhausted(f"429 with Retry-After {wait:.0f}s (daily cap?)")
                if rate_tries >= self._max_retries:
                    raise _QuotaExhausted("429 kept coming back after retries")
                self._wait("429 rate limited", wait)
                rate_tries += 1
                continue
            if status in (401, 403):
                raise LLMConfigError(
                    f"The LLM provider refused the key (HTTP {status}). Check the key and model access."  # noqa: E501
                )
            if 500 <= status < 600:
                if server_tries >= len(SERVER_RETRY_DELAYS):
                    raise LLMBusyError(
                        f"LLM service returned HTTP {status} after retries.",
                        reason="service_unavailable",
                    )
                self._wait(f"HTTP {status}", SERVER_RETRY_DELAYS[server_tries])
                server_tries += 1
                continue
            if status == 400 and json_mode and _is_json_failure(response):
                if fell_back or json_retried:
                    raise _JSONGenerationFailed(
                        f"The model could not produce valid JSON: {_detail(response)}",
                        _failed_generation(response),
                    )
                log.warning("llm_retry role=%s reason=json_generation_failed", self.role.name)
                json_retried = True
                continue
            raise LLMRequestError(
                f"LLM rejected the request: HTTP {status}: {_detail(response)}",
                reason="bad_request",
            )

    def _wait(self, reason: str, seconds: float) -> None:
        log.warning("llm_retry role=%s reason=%s wait=%.1fs", self.role.name, reason, seconds)
        self._sleep(seconds)

    def _parse(self, response, model, attempts, fell_back, began) -> LLMResult:
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMRequestError("The LLM reply was not in the expected format.") from None
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        rate_limit = {
            k: v
            for k, v in response.headers.items()
            if k.lower().startswith("x-ratelimit-") or k.lower() == "retry-after"
        }
        result = LLMResult(
            text=text,
            model=data.get("model") or model,
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=_int(usage.get("prompt_tokens")),
            completion_tokens=_int(usage.get("completion_tokens")),
            total_tokens=_int(usage.get("total_tokens")),
            reasoning_tokens=_int(details.get("reasoning_tokens")),
            cached_tokens=_int(prompt_details.get("cached_tokens")),
            latency_ms=round((time.perf_counter() - began) * 1000),
            attempts=attempts,
            fallback_used=fell_back,
            rate_limit=rate_limit,
        )
        log.info(
            "llm_call role=%s model=%s prompt=%s completion=%s reasoning=%s cached=%s "
            "finish=%s ms=%d attempts=%d fallback=%s remaining_tokens=%s remaining_requests=%s",
            self.role.name,
            result.model,
            result.prompt_tokens,
            result.completion_tokens,
            result.reasoning_tokens,
            result.cached_tokens,
            result.finish_reason,
            result.latency_ms,
            attempts,
            fell_back,
            rate_limit.get("x-ratelimit-remaining-tokens"),
            rate_limit.get("x-ratelimit-remaining-requests"),
        )
        return result


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    try:
        return max(float(header), 0.0) if header else None
    except ValueError:
        return None


def _is_json_failure(response: httpx.Response) -> bool:
    """True only for Groq's "could not generate JSON" 400, not for every 400. Matches the error
    code (json_validate_failed, from Groq's docs: not verified live) or the message text seen
    live on the 20b ("Failed to generate JSON")."""
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        return False
    if not isinstance(error, dict):
        return False
    return (
        error.get("code") == "json_validate_failed"
        or "failed to generate json" in str(error.get("message", "")).lower()
    )


def _failed_generation(response: httpx.Response) -> str | None:
    """The model's raw text from a Groq JSON-failure error body (`error.failed_generation`)."""
    try:
        value = response.json().get("error", {}).get("failed_generation")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        detail = ""
    return str(detail or response.text)[:200]
