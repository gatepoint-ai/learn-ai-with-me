"""Environment-driven settings shared by every service.

Each service instantiates only the mixins it needs, so a service that never
touches ClickHouse cannot accidentally be configured with ClickHouse
credentials. That is the same reasoning behind routing all queries through the
prism service: credentials live where they are used and nowhere else.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


class LogSettings(BaseSettings):
    model_config = _BASE
    log_level: str = "INFO"
    log_json: bool = True
    service_name: str = "argus"


class ClickHouseSettings(BaseSettings):
    model_config = _BASE
    clickhouse_host: str = "clickhouse"
    clickhouse_port: int = 8123
    clickhouse_user: str = "app"
    clickhouse_password: str = "dev_password"
    clickhouse_database: str = "analytics"
    clickhouse_secure: bool = False


class PostgresSettings(BaseSettings):
    model_config = _BASE
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "argus"
    postgres_password: str = "dev_password"
    postgres_database: str = "pulse"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )


class CodexDbSettings(BaseSettings):
    """The knowledge store (pgvector) is a separate instance with its own prefix.

    Only Codex instantiates this. The Analyst reaches knowledge through Codex's
    HTTP API and never holds these credentials.
    """

    model_config = _BASE
    codex_host: str = "codex-db"
    codex_port: int = 5432
    codex_user: str = "argus"
    codex_password: str = "dev_password"
    codex_database: str = "codex"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.codex_user}:{self.codex_password}"
            f"@{self.codex_host}:{self.codex_port}/{self.codex_database}"
        )


class ValkeySettings(BaseSettings):
    model_config = _BASE
    valkey_host: str = "valkey"
    valkey_port: int = 6379
    valkey_db: int = 0
    cache_enabled: bool = True


class ServiceUrls(BaseSettings):
    """Where the other services live.

    The agent holds only these -- never a database credential. Every tool it
    exposes is an HTTP call to one of these URLs.
    """

    model_config = _BASE
    prism_url: str = "http://prism:8000"
    pulse_url: str = "http://pulse:8000"
    sentry_url: str = "http://sentry:8000"
    codex_url: str = "http://codex:8000"


class ConfigPaths(BaseSettings):
    model_config = _BASE
    config_dir: str = "/config"

    @property
    def lenses(self) -> str:
        return f"{self.config_dir}/lenses"

    @property
    def kpis(self) -> str:
        return f"{self.config_dir}/kpis"

    @property
    def rules(self) -> str:
        return f"{self.config_dir}/rules"

    @property
    def collections(self) -> str:
        return f"{self.config_dir}/codex/collections"

    @property
    def knowledge_docs(self) -> str:
        return f"{self.config_dir}/codex/docs"


class LLMSettings(BaseSettings):
    """Chat-provider selection.

    `openai_compat` is the default because it reaches every free backend that
    speaks the OpenAI chat-completions shape -- Groq, OpenRouter, Cerebras,
    Gemini's compat endpoint, Ollama, vLLM -- by changing `openai_base_url`
    alone. See docs/LLM-PROVIDERS.md.
    """

    model_config = _BASE

    llm_provider: str = "openai_compat"

    openai_base_url: str = "https://api.groq.com/openai/v1"
    openai_model: str = "openai/gpt-oss-120b"
    openai_api_key: str = ""
    #: native | weak | none -- drives full agent loop vs constrained mode.
    openai_tool_calling: str = "native"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    max_tool_iterations: int = Field(default=8, ge=1, le=32)
    request_timeout: float = Field(default=120.0, gt=0)


class EmbeddingSettings(BaseSettings):
    """Retrieval embeddings, deliberately decoupled from the chat provider.

    Groq -- the default chat backend -- has no embeddings endpoint at all, so
    retrieval cannot piggyback on it. Local ONNX inference via fastembed is the
    default: no key, no network, and no multi-gigabyte torch layer.
    """

    model_config = _BASE

    embedding_provider: str = "local_fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    rerank_enabled: bool = True
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"

    embeddings_base_url: str = ""
    embeddings_api_key: str = ""


@lru_cache
def log_settings() -> LogSettings:
    return LogSettings()
