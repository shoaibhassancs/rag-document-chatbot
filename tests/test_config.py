import pytest

from app.config import Settings


def make(**kwargs) -> Settings:
    # _env_file=None: tests must never read the developer's real .env.
    return Settings(_env_file=None, **kwargs)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GROQ_API_KEY", "VOYAGE_API_KEY", "GEMINI_API_KEY", "APP_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_use_groq_models_per_role():
    s = make()
    assert s.llm_role("answer").model == "openai/gpt-oss-20b"
    assert s.llm_role("rewrite").model == "openai/gpt-oss-20b"
    assert s.llm_role("judge").model == "qwen/qwen3.8-27b"
    assert s.answer_fallback_model == "openai/gpt-oss-120b"
    assert s.answer_prompt_version == "answer_v2"


def test_judge_is_a_different_model_from_the_answerer():
    s = make()
    assert s.llm_role("judge").model != s.llm_role("answer").model


def test_role_key_resolves_from_the_named_setting():
    s = make(groq_api_key="fake-groq-test-value")
    assert s.llm_role("answer").api_key.get_secret_value() == "fake-groq-test-value"


def test_missing_key_is_none_not_an_error():
    assert make().llm_role("answer").api_key is None


def test_roles_can_point_at_another_provider_by_config_only():
    s = make(
        gemini_api_key="g-test",
        judge_llm_provider="gemini",
        judge_llm_base_url="https://example.invalid/v1",
        judge_llm_model="some-model",
        judge_llm_api_key_var="GEMINI_API_KEY",
    )
    role = s.llm_role("judge")
    assert (role.provider, role.model) == ("gemini", "some-model")
    assert role.api_key.get_secret_value() == "g-test"


def test_secrets_never_appear_in_repr_or_str():
    s = make(groq_api_key="fake-groq-super-secret", voyage_api_key="fake-voyage-super-secret")
    text = repr(s) + str(s) + repr(s.llm_role("answer"))
    assert "super-secret" not in text


def test_unknown_role_or_key_name_is_rejected():
    s = make()
    with pytest.raises(ValueError):
        s.llm_role("nonsense")
    with pytest.raises(ValueError):
        s.api_key_for("DATABASE_URL")


def test_embedding_defaults_match_spec():
    s = make()
    assert (s.embedding_model, s.embedding_dimensions) == ("voyage-4", 1024)


def test_tests_cannot_reach_the_internet():
    """The network guard in conftest.py makes any real Groq or Voyage call fail the test."""
    import socket

    with pytest.raises(AssertionError, match="non-local"):
        socket.create_connection(("93.184.216.34", 443), timeout=1)


def test_a_blank_similarity_threshold_means_not_set():
    assert Settings(_env_file=None, similarity_threshold="").similarity_threshold is None
    assert Settings(_env_file=None, similarity_threshold=" ").similarity_threshold is None
    assert Settings(_env_file=None, similarity_threshold="0.55").similarity_threshold == 0.55
    assert Settings(_env_file=None).similarity_threshold is None


def test_reply_texts_and_llm_client_limits_have_defaults_and_can_be_overridden():
    s = Settings(_env_file=None)
    assert "couldn't find" in s.refusal_message and "busy" in s.busy_message
    assert (s.top_k, s.llm_max_retries, s.llm_max_wait_seconds) == (5, 3, 20.0)
    assert (s.prompts_dir / f"{s.answer_prompt_version}.txt").is_file()
    assert Settings(_env_file=None, refusal_message="No.").refusal_message == "No."
