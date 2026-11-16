import json

import httpx
import pytest

from app.config import LLMRole
from app.generation.llm import (
    LLMBusyError,
    LLMClient,
    LLMConfigError,
    LLMRequestError,
)

KEY = "fake-test-secret-value"


def role(**overrides) -> LLMRole:
    values = dict(
        name="answer",
        provider="groq",
        base_url="https://llm.example/openai/v1",
        model="big-model",
        api_key=KEY,
        reasoning_effort="low",
        max_output_tokens=600,
    )
    values.update(overrides)
    return LLMRole(**values)


def ok_body(text='{"answerable": true}', model="big-model", **usage):
    return {
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 60,
            "total_tokens": 180,
            "completion_tokens_details": {"reasoning_tokens": 25},
            "prompt_tokens_details": {"cached_tokens": 7},
            **usage,
        },
    }


class Server:
    """A scripted fake of the provider: each item is an httpx.Response or an exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def models(self) -> list[str]:
        return [json.loads(r.content)["model"] for r in self.requests]


def make(server, sleeps=None, fallback="small-model", **kwargs):
    sleeps = sleeps if sleeps is not None else []
    return LLMClient(
        role(),
        fallback_model=fallback,
        transport=httpx.MockTransport(server),
        sleep=sleeps.append,
        **kwargs,
    )


def resp(status=200, body=None, headers=None):
    return httpx.Response(status, json=body if body is not None else ok_body(), headers=headers)


def test_success_returns_text_usage_and_rate_limit_headers():
    server = Server(
        resp(headers={"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-limit-tokens": "8000"})
    )
    result = make(server).complete("system text", "user text")
    assert result.text == '{"answerable": true}'
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (120, 60, 180)
    assert (result.reasoning_tokens, result.cached_tokens) == (25, 7)
    assert result.rate_limit["x-ratelimit-remaining-tokens"] == "7000"
    assert (result.attempts, result.fallback_used, result.finish_reason) == (1, False, "stop")


def test_request_body_and_headers():
    server = Server(resp())
    make(server).complete("SYS", "USR")
    request = server.requests[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://llm.example/openai/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "big-model" and body["temperature"] == 0
    assert body["max_completion_tokens"] == 600 and body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]


def test_json_mode_and_reasoning_effort_can_be_left_out():
    server = Server(resp())
    client = LLMClient(
        role(reasoning_effort=None), transport=httpx.MockTransport(server), sleep=lambda s: None
    )
    client.complete("s", "u", json_mode=False)
    body = json.loads(server.requests[0].content)
    assert "response_format" not in body and "reasoning_effort" not in body


def test_429_waits_for_retry_after_then_succeeds():
    sleeps: list[float] = []
    server = Server(resp(429, {"error": {"message": "slow"}}, {"retry-after": "3"}), resp())
    result = make(server, sleeps).complete("s", "u")
    assert sleeps == [3.0] and result.attempts == 2 and not result.fallback_used


def test_429_without_a_header_uses_default_delays():
    sleeps: list[float] = []
    server = Server(resp(429, {}), resp(429, {}), resp())
    make(server, sleeps).complete("s", "u")
    assert sleeps == [2.0, 5.0]


def test_a_long_retry_after_is_a_daily_cap_and_goes_straight_to_the_fallback():
    sleeps: list[float] = []
    server = Server(resp(429, {}, {"retry-after": "3600"}), resp(body=ok_body(model="small-model")))
    result = make(server, sleeps).complete("s", "u")
    assert sleeps == []  # did not wait an hour
    assert server.models() == ["big-model", "small-model"]
    assert result.fallback_used and result.model == "small-model"


def test_repeated_short_429s_end_in_the_fallback():
    sleeps: list[float] = []
    always = [resp(429, {}, {"retry-after": "1"}) for _ in range(4)]
    server = Server(*always, resp(body=ok_body(model="small-model")))
    result = make(server, sleeps, max_retries=3).complete("s", "u")
    assert sleeps == [1.0, 1.0, 1.0]
    assert server.models() == ["big-model"] * 4 + ["small-model"]
    assert result.fallback_used


def test_no_fallback_means_busy_error_when_the_cap_is_hit():
    server = Server(resp(429, {}, {"retry-after": "3600"}))
    with pytest.raises(LLMBusyError):
        make(server, fallback=None).complete("s", "u")


def test_the_fallback_failing_too_is_a_busy_error():
    server = Server(resp(429, {}, {"retry-after": "3600"}), resp(429, {}, {"retry-after": "3600"}))
    with pytest.raises(LLMBusyError):
        make(server).complete("s", "u")
    assert len(server.requests) == 2


def test_server_errors_are_retried_twice_then_busy_without_fallback():
    sleeps: list[float] = []
    server = Server(resp(503, {}), resp(503, {}), resp(503, {}))
    with pytest.raises(LLMBusyError):
        make(server, sleeps).complete("s", "u")
    assert sleeps == [1.0, 3.0] and server.models() == ["big-model"] * 3


def test_a_timeout_then_success():
    sleeps: list[float] = []
    server = Server(httpx.ReadTimeout("slow"), resp())
    assert make(server, sleeps).complete("s", "u").attempts == 2
    assert sleeps == [1.0]


def test_endless_timeouts_become_busy_and_the_message_has_no_key():
    server = Server(*[httpx.ConnectTimeout("t") for _ in range(3)])
    with pytest.raises(LLMBusyError) as info:
        make(server).complete("s", "u")
    assert KEY not in str(info.value)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_a_config_error_with_no_retry(status):
    server = Server(resp(status, {"error": {"message": "nope"}}))
    with pytest.raises(LLMConfigError) as info:
        make(server).complete("s", "u")
    assert len(server.requests) == 1 and KEY not in str(info.value)


def test_a_bad_request_is_not_retried_and_explains_itself():
    server = Server(resp(400, {"error": {"message": "response_format unsupported"}}))
    with pytest.raises(LLMRequestError, match="response_format unsupported"):
        make(server).complete("s", "u")
    assert len(server.requests) == 1


def test_a_malformed_success_body_is_a_request_error():
    server = Server(resp(200, {"unexpected": True}))
    with pytest.raises(LLMRequestError):
        make(server).complete("s", "u")


def test_missing_key_is_a_config_error_at_construction():
    with pytest.raises(LLMConfigError):
        LLMClient(role(api_key=None))


def test_usage_fields_missing_from_the_reply_are_none_not_errors():
    body = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    result = make(Server(resp(200, body))).complete("s", "u")
    assert result.prompt_tokens is None and result.reasoning_tokens is None


def test_each_call_is_logged_without_the_prompt_or_the_key(caplog):
    caplog.set_level("INFO")
    make(Server(resp())).complete("SECRET-PROMPT", "SECRET-QUESTION")
    text = caplog.text
    assert "llm_call" in text and "reasoning=25" in text
    assert KEY not in text and "SECRET-PROMPT" not in text and "SECRET-QUESTION" not in text


# ----------------------------------------------------------------------------- JSON failure 400

JSON_400 = {
    "error": {
        "message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation' "
        "for more details.",
        "type": "invalid_request_error",
        "code": "json_validate_failed",
    }
}


def test_a_json_generation_400_is_retried_once_on_the_same_model():
    server = Server(resp(400, JSON_400), resp())
    result = make(server).complete("s", "u")
    assert server.models() == ["big-model", "big-model"]
    assert not result.fallback_used and result.attempts == 2


def test_two_json_generation_400s_then_the_fallback_model_is_tried_once():
    server = Server(
        resp(400, JSON_400), resp(400, JSON_400), resp(body=ok_body(model="small-model"))
    )
    result = make(server).complete("s", "u")
    assert server.models() == ["big-model", "big-model", "small-model"]
    assert result.fallback_used


def test_json_generation_400_everywhere_ends_in_a_request_error_without_more_calls():
    server = Server(*[resp(400, JSON_400) for _ in range(5)])
    with pytest.raises(LLMRequestError):
        make(server).complete("s", "u")
    assert server.models() == ["big-model", "big-model", "small-model"]  # no retry on the fallback


def test_json_generation_400_without_a_fallback_is_a_request_error_after_one_retry():
    server = Server(resp(400, JSON_400), resp(400, JSON_400))
    with pytest.raises(LLMRequestError):
        make(server, fallback=None).complete("s", "u")
    assert len(server.requests) == 2


def test_the_json_failure_is_recognised_by_its_message_when_the_code_is_missing():
    body = {"error": {"message": "Failed to generate JSON. Please adjust your prompt."}}
    server = Server(resp(400, body), resp())
    assert make(server).complete("s", "u").attempts == 2


def test_any_other_400_is_not_retried_and_does_not_fall_back():
    server = Server(resp(400, {"error": {"message": "bad request", "code": "other"}}), resp())
    with pytest.raises(LLMRequestError):
        make(server).complete("s", "u")
    assert len(server.requests) == 1


def test_a_json_400_when_json_mode_is_off_is_not_treated_as_a_json_failure():
    server = Server(resp(400, JSON_400), resp())
    with pytest.raises(LLMRequestError):
        make(server).complete("s", "u", json_mode=False)
    assert len(server.requests) == 1
