"""check_keys is tested against httpx.MockTransport: no real network calls, no real keys."""

import httpx

from app.config import Settings
from scripts.check_keys import check_groq, check_voyage

GROQ_MODELS = {
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
}


def settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def groq_handler(models, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fake-groq-test"
        return httpx.Response(status, json={"data": [{"id": m} for m in models]})

    return handler


def test_groq_ok():
    with client_for(groq_handler(GROQ_MODELS)) as c:
        assert check_groq(c, settings(groq_api_key="fake-groq-test")) == "OK"


def test_groq_missing_key():
    with client_for(groq_handler(GROQ_MODELS)) as c:
        assert check_groq(c, settings()) == "FAIL (GROQ_API_KEY not set)"


def test_groq_bad_key_http_401():
    with client_for(groq_handler([], status=401)) as c:
        assert check_groq(c, settings(groq_api_key="fake-groq-test")) == "FAIL (HTTP 401)"


def test_groq_configured_model_not_listed():
    with client_for(groq_handler(GROQ_MODELS - {"qwen/qwen3.8-27b"})) as c:
        result = check_groq(c, settings(groq_api_key="fake-groq-test"))
    assert result.startswith("FAIL (models not listed: qwen/qwen3.8-27b")


def test_groq_network_error_reports_only_the_exception_type():
    def boom(request):
        raise httpx.ConnectError("secret-detail")

    with client_for(boom) as c:
        result = check_groq(c, settings(groq_api_key="fake-groq-test"))
    assert result == "FAIL (ConnectError)"


def voyage_handler(dim=1024, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert '"input_type":"query"' in body.replace(" ", "")
        if status != 200:
            return httpx.Response(status, json={"detail": "x"})
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * dim}]})

    return handler


def test_voyage_ok():
    with client_for(voyage_handler()) as c:
        assert check_voyage(c, settings(voyage_api_key="fake-voyage-test")) == "OK"


def test_voyage_missing_key():
    with client_for(voyage_handler()) as c:
        assert check_voyage(c, settings()) == "FAIL (VOYAGE_API_KEY not set)"


def test_voyage_rate_limited_is_reported_clearly():
    with client_for(voyage_handler(status=429)) as c:
        assert "rate limited" in check_voyage(c, settings(voyage_api_key="fake-voyage-test"))


def test_voyage_dimension_mismatch():
    with client_for(voyage_handler(dim=512)) as c:
        result = check_voyage(c, settings(voyage_api_key="fake-voyage-test"))
    assert "512 dimensions" in result
