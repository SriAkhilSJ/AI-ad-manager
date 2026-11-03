"""Application configuration.

All secrets are environment-driven. Nothing secret has a usable default in
production: :meth:`Settings.validate_for_environment` refuses to start a
production process with a placeholder key.
"""

from __future__ import annotations

import json
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER_SECRETS = {
    "change-me-dev-only-secret-key-not-for-production-use",
    "change-me",
    "",
}

Environment = Literal["development", "staging", "production", "test"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- application ----
    app_name: str = "Crusor AI Marketing Director"
    environment: Environment = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---- security ----
    secret_key: str = "change-me-dev-only-secret-key-not-for-production-use"
    access_token_expire_minutes: int = 60
    rate_limit_per_minute: int = 120
    bootstrap_founder_email: str = "founder@crusor.ai"
    bootstrap_founder_password: str = "change-me-dev-password"

    # ---- database ----
    database_url: str = "sqlite+aiosqlite:///./.data/crusor.db"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    alembic_database_url: str | None = None

    # ---- redis / tasks ----
    redis_url: str | None = None
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    task_dispatcher: Literal["auto", "inline", "celery", "disabled"] = "auto"

    # ---- storage ----
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_public_base_url: str | None = None
    storage_backend: Literal["auto", "local", "s3", "disabled"] = "auto"

    # ---- LLM gateway ----
    llm_default_provider: str | None = None
    llm_default_model: str | None = None
    llm_routing_json: dict[str, Any] = Field(default_factory=dict)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    llm_monthly_budget_usd: float = 100.0
    llm_request_timeout_seconds: int = 60

    # ---- agent limits ----
    agent_max_attempts: int = 2
    agent_max_tokens_per_run: int = 4000
    agent_timeout_seconds: int = 120
    director_max_iterations: int = 12

    # ---- social (Phase 2) ----
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    linkedin_client_id: str | None = None
    linkedin_client_secret: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    x_client_id: str | None = None
    x_client_secret: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    token_vault_backend: Literal["none", "s3", "external"] = "none"

    # ------------------------------------------------------------------ #
    @field_validator("llm_routing_json", mode="before")
    @classmethod
    def _parse_routing(cls, value: Any) -> Any:
        if value in (None, "", b""):
            return {}
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, (str, bytes)):
            return json.loads(value)
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def resolved_alembic_url(self) -> str:
        return self.alembic_database_url or self.database_url

    @property
    def storage_root(self) -> Path:
        return BACKEND_ROOT / ".data" / "storage"

    def validate_for_environment(self) -> None:
        """Fail fast on unsafe production configuration."""
        if not self.is_production:
            return
        problems: list[str] = []
        if self.secret_key in PLACEHOLDER_SECRETS or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be a random value of >=32 chars")
        if self.bootstrap_founder_password in PLACEHOLDER_SECRETS:
            problems.append("BOOTSTRAP_FOUNDER_PASSWORD must not be a placeholder")
        if self.debug:
            problems.append("DEBUG must be false in production")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must not be SQLite in production")
        if problems:
            raise RuntimeError(
                "Unsafe production configuration: " + "; ".join(problems)
            )

    @staticmethod
    def generate_secret() -> str:
        return secrets.token_hex(32)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that patch the environment."""
    get_settings.cache_clear()
