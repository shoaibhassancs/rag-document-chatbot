"""Settings loaded from environment variables and a local .env file.

Keys are SecretStr, so printing a Settings object never shows them.
Each LLM role (answer, judge, rewrite) has its own provider, base URL, model and key
setting, so switching a role to another OpenAI-compatible provider is a config change.
"""

from functools import lru_cache

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Key settings a role may point at (lower-case Settings field names).
_KEY_FIELDS = {"groq_api_key", "voyage_api_key", "gemini_api_key"}
_ROLES = {"answer", "judge", "rewrite"}


class LLMRole(BaseModel):
    """Everything needed to call one OpenAI-compatible chat model."""

    name: str
    provider: str
    base_url: str
    model: str
    api_key: SecretStr | None
    reasoning_effort: str | None
    max_output_tokens: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Provider keys (values only ever come from .env or the environment) ---
    groq_api_key: SecretStr | None = None
    voyage_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None  # optional overflow slot, not built
    app_api_key: SecretStr | None = None

    # --- Database: one setting ---
    database_url: str = "postgresql://raguser:change-me@localhost:5440/ragdb"

    # --- Answer role ---
    answer_llm_provider: str = "groq"
    answer_llm_base_url: str = "https://api.groq.com/openai/v1"
    answer_llm_model: str = "openai/gpt-oss-120b"
    answer_llm_api_key_var: str = "GROQ_API_KEY"
    answer_llm_reasoning_effort: str | None = "low"
    answer_llm_max_output_tokens: int = 600
    # Fallback when the answer model hits its daily cap or repeated 429s.
    answer_fallback_enabled: bool = True
    answer_fallback_model: str = "openai/gpt-oss-20b"

    # --- Judge role (a different model family from the answerer) ---
    judge_llm_provider: str = "groq"
    judge_llm_base_url: str = "https://api.groq.com/openai/v1"
    judge_llm_model: str = "qwen/qwen3.8-27b"
    judge_llm_api_key_var: str = "GROQ_API_KEY"
    judge_llm_reasoning_effort: str | None = "low"
    judge_llm_max_output_tokens: int = 400

    # --- Rewrite / clarify role (small model) ---
    rewrite_llm_provider: str = "groq"
    rewrite_llm_base_url: str = "https://api.groq.com/openai/v1"
    rewrite_llm_model: str = "openai/gpt-oss-20b"
    rewrite_llm_api_key_var: str = "GROQ_API_KEY"
    rewrite_llm_reasoning_effort: str | None = "low"
    rewrite_llm_max_output_tokens: int = 150

    # --- Embeddings ---
    embedding_provider: str = "voyage"
    embedding_model: str = "voyage-4"
    embedding_dimensions: int = 1024
    embedding_base_url: str = "https://api.voyageai.com/v1"

    def api_key_for(self, var_name: str) -> SecretStr | None:
        field = var_name.lower()
        if field not in _KEY_FIELDS:
            raise ValueError(f"Unknown key setting: {var_name}")
        return getattr(self, field)

    def llm_role(self, role: str) -> LLMRole:
        if role not in _ROLES:
            raise ValueError(f"Unknown LLM role: {role}")

        def get(suffix: str):
            return getattr(self, f"{role}_llm_{suffix}")

        return LLMRole(
            name=role,
            provider=get("provider"),
            base_url=get("base_url"),
            model=get("model"),
            api_key=self.api_key_for(get("api_key_var")),
            reasoning_effort=get("reasoning_effort"),
            max_output_tokens=get("max_output_tokens"),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
