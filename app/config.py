"""Settings loaded from environment variables and a local .env file.

Keys are SecretStr, so printing a Settings object never shows them.
Each LLM role (answer, judge, rewrite) has its own provider, base URL, model and key
setting, so switching a role to another OpenAI-compatible provider is a config change.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, SecretStr, field_validator
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
    answer_llm_model: str = "openai/gpt-oss-20b"
    answer_llm_api_key_var: str = "GROQ_API_KEY"
    answer_llm_reasoning_effort: str | None = "low"
    answer_llm_max_output_tokens: int = 600
    # Fallback when the answer model hits its daily cap or repeated 429s.
    answer_fallback_enabled: bool = True
    answer_fallback_model: str = "openai/gpt-oss-120b"

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
    rewrite_llm_max_output_tokens: int = 250

    # --- Embeddings ---
    embedding_provider: str = "voyage"
    embedding_model: str = "voyage-4"
    embedding_dimensions: int = 1024
    embedding_base_url: str = "https://api.voyageai.com/v1"
    # Voyage free tier without a payment method: 3 requests/min and 10,000 tokens/min together.
    voyage_requests_per_minute: int = 3
    voyage_tokens_per_minute: int = 10_000
    voyage_max_batch_tokens: int = 8_000

    # --- Chunking (token counts are estimates: characters / 4) ---
    chunk_size_tokens: int = 600
    chunk_overlap_tokens: int = 100

    # --- Ingestion limits ---
    max_upload_mb: int = 10
    max_files_per_request: int = 20
    url_timeout_seconds: int = 10
    url_max_bytes: int = 2_000_000
    # Off by default: URL ingestion refuses private, loopback and link-local addresses.
    allow_private_urls: bool = False

    # --- Retrieval and answering ---
    top_k: int = 5
    # PROVISIONAL (Milestone 3): chunks scoring below this cosine similarity are dropped, and if
    # none are left the question is refused without calling the LLM. Blank means "not set":
    # nothing is dropped and the service warns. Calibrated with scripts/calibrate_threshold.py.
    similarity_threshold: float | None = None
    prompts_dir: Path = Path(__file__).resolve().parent.parent / "prompts"
    answer_prompt_version: str = "answer_v2"
    # Fixed reply texts. Configurable so a client can change the wording without code changes.
    refusal_message: str = "I couldn't find that in our documents. Please contact the clinic."
    busy_message: str = "The assistant is busy right now. Please try again in a minute."

    # --- Clarify-before-retrieving and session memory (Milestone 4) ---
    # One small-model call per question rewrites a follow-up into a standalone search query or asks
    # one clarifying question. False = skip it and search with the user's message as written.
    clarify_enabled: bool = True
    clarify_prompt_version: str = "clarify_v1"
    history_turns: int = 4  # past exchanges given to the clarify call
    history_message_max_chars: int = 300  # each past message is cut to this length in the prompt
    session_ttl_hours: int = 24  # a session idle longer than this is forgotten

    # A chat question waits at most this long (limiter plus retries) for its Voyage embedding;
    # over the cap the user gets the busy reply. Ingestion keeps its own, longer schedule.
    query_embed_max_wait_seconds: float = 20.0

    # --- LLM client behaviour ---
    llm_max_retries: int = 3  # retries after a 429 before falling back or giving up
    llm_max_wait_seconds: float = 20.0  # a longer Retry-After is treated as a daily cap
    llm_timeout_seconds: float = 30.0

    log_level: str = "INFO"

    @field_validator("similarity_threshold", mode="before")
    @classmethod
    def _blank_threshold_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
