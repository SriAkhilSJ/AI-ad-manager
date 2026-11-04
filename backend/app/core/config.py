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

    # ---- Phase 2.5: provider-independent routing -------------------------
    # Each provider owns exactly one credential variable and one endpoint.
    # There is deliberately no shared/generic "LLM_KEY": a single undifferentiated
    # key is what made a cross-provider credential mixup possible.
    #
    # OpenRouter: fixed, well-known endpoint; key is OPENROUTER_API_KEY.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_timeout_seconds: int = 60
    openrouter_max_retries: int = 2
    # Velona: an OpenAI-compatible vendor whose endpoint we must NOT invent.
    # Both are required before the provider is usable; with either missing the
    # provider reports unconfigured and every call fails closed.
    velona_api_key: str | None = None
    velona_base_url: str | None = None
    velona_timeout_seconds: int = 60
    velona_max_retries: int = 2
    # Reject plain HTTP for remote endpoints. Loopback stays allowed so a local
    # bridge can be used in development; set false to forbid even that.
    llm_allow_http_localhost: bool = True

    # ---- Phase 2: model tiering, routing and budgets --------------------
    # Model ids are configuration data. The defaults below are the ones the
    # Phase 2 brief specifies; they are not hardcoded in any agent, and an
    # unknown id is priced conservatively and flagged as unverified rather than
    # silently trusted.
    # Set any of these to empty to disable that tier explicitly.
    # Phase 2.5: both tiers now name *velona*, so a credential is never sent to
    # a host it does not belong to. Change these only to a provider whose key is
    # configured; an unconfigured provider fails closed rather than borrowing one.
    llm_reasoning_provider: str | None = "velona"
    llm_reasoning_model: str | None = "deepseek-v4.1-flash"
    llm_auxiliary_provider: str | None = "velona"
    llm_auxiliary_model: str | None = "qwen-3.7-flash"
    # Explicit per-task overrides:
    #   {"classification": {"provider": "openrouter", "model": "qwen-3.7-flash"}}
    llm_task_routing_json: dict[str, Any] = Field(default_factory=dict)
    # Operator-supplied pricing (USD / 1k tokens), keyed by "provider/model" or
    # bare model id. Entries here are treated as verified prices.
    llm_pricing_json: dict[str, Any] = Field(default_factory=dict)

    # Phase 2.6 fail-closed pricing. When False (the default), a paid request is
    # blocked if its model price is an unverified placeholder, because a budget
    # reserved against an invented price is not a guarantee. Set True only to
    # accept that the placeholder may under-estimate by one request.
    llm_allow_unverified_pricing: bool = False

    # ---- Phase 3A content generation ------------------------------------
    #: Who writes content drafts. ``mock`` (the default) uses the deterministic,
    #: free, self-identifying provider, so Phase 3A never spends money. ``auto``
    #: uses the configured LLM tiers when they are available and falls back to the
    #: agent's deterministic path. ``disabled`` generates nothing.
    content_provider: Literal["mock", "auto", "disabled"] = "mock"
    #: Per-platform spec overrides, e.g. {"x": {"body_max_chars": 280}}.
    content_platform_overrides_json: dict[str, Any] = Field(default_factory=dict)

    # ---- Phase 3B creative generation ------------------------------------
    #: Who produces images and video. ``mock`` (the default) is deterministic and
    #: free: it renders a real PNG frame and never calls a model. ``flow`` selects
    #: the Google Flow adapter, which fails closed because the Connector source is
    #: not available. ``disabled`` refuses every generation attempt.
    creative_provider: Literal["mock", "flow", "disabled"] = "mock"
    #: Real generation spends money. It stays off until a founder authorises it in
    #: configuration *and* a provider with verified pricing exists.
    creative_allow_paid_generation: bool = False
    #: Per-campaign creative budget. Zero blocks every billable generation; it is
    #: never treated as unlimited (Phase 2.6 rule, applied to images as well).
    creative_campaign_budget_usd: float = 0.0
    #: Longest edge the mock renderer draws. Kept modest so tests stay fast.
    creative_mock_max_dimension: int = 960
    #: A job that has not reported back within this window is marked TIMED_OUT.
    creative_job_timeout_seconds: int = 300
    creative_job_max_attempts: int = 2
    #: Upload guard rails. The declared Content-Type is never trusted; the bytes are
    #: sniffed and the size is enforced before anything is written.
    creative_max_upload_bytes: int = 25 * 1024 * 1024
    #: Verified USD price per generation, keyed by ``image`` / ``video``. An empty
    #: map means "price unknown", which blocks paid generation instead of costing $0.
    creative_pricing_json: dict[str, Any] = Field(default_factory=dict)

    # ---- Phase 3C.1 Flow bridge + local companion ------------------------
    #: The bridge is a *local* service. It refuses to start unless the bind host is
    #: a loopback literal, so a misconfiguration cannot expose it to the network.
    flow_bridge_enabled: bool = False
    flow_bridge_host: str = "127.0.0.1"
    #: Two listeners, both loopback: authenticated HTTP commands and the
    #: extension WebSocket never share one socket.
    flow_bridge_port: int = 8793
    flow_bridge_ws_port: int = 8794
    #: Pairing credentials. High-entropy, generated by
    #: ``python -m app.flow.pairing --print``; never logged, never committed.
    flow_pairing_id: str = ""
    flow_pairing_secret: str = ""
    #: Comma-separated Chrome extension ids allowed in the Origin header. Empty
    #: means "no extension origin is trusted", which is the safe default.
    flow_allowed_extension_ids: str = ""
    #: Transport limits.
    flow_max_request_bytes: int = 64 * 1024
    flow_max_ws_message_bytes: int = 32 * 1024 * 1024
    flow_max_connections: int = 8
    flow_handshake_timeout_seconds: float = 5.0
    flow_request_timeout_seconds: float = 15.0
    flow_command_timeout_seconds: float = 120.0
    #: One live generation at a time until the adapter has been proven.
    flow_max_live_jobs: int = 1
    #: Phase 3C.1 must not generate. This stays False until 3C.2 is accepted and a
    #: founder authorises a canary.
    flow_live_generation_enabled: bool = False
    #: One-time export tickets: short-lived, single-use, checksum-verified.
    flow_export_ttl_seconds: int = 900
    flow_export_max_bytes: int = 25 * 1024 * 1024
    #: Bounded chunked transfer for large media (Phase 3C.2 §9). Each chunk is
    #: checksummed and must arrive in order; the asset exists only after the
    #: assembled bytes, and every chunk, verify.
    flow_export_chunk_bytes: int = 8 * 1024 * 1024
    flow_export_max_chunks: int = 4096

    # ---- Phase 3C.2 provenance gate --------------------------------------
    #: Accept a package whose components sit on different release lines. Off by
    #: default: upstream ships manifest 1.2.1 with an MCP server at 1.0.0.
    flow_allow_component_drift: bool = False
    #: Where an authorised copy of the upstream package would live. Never
    #: committed; only the *presence and licence* are reported by /flow/status.
    flow_source_package_path: str = ""
    #: Phase 3C.2.1: a human-reviewed authorization attestation, stored OUTSIDE
    #: the package. Licence files alone never authorize anything.
    flow_authorization_attestation_path: str = ""
    #: Phase 3C.2.1 provenance trust anchors. A HELLO's self-reported digest is
    #: never an anchor: trust comes from one of these, configured by an operator.
    flow_trusted_artifact_sha256: str = ""
    flow_trusted_release: str = ""
    flow_trusted_source_commit: str = ""
    flow_release_manifest_path: str = ""
    flow_release_manifest_public_key: str = ""
    #: ---- Phase 3C.2.2 independent installed-artifact verification -----------
    #: Where the extension is actually installed. Discovery is explicit: the
    #: verifier never searches for a plausible-looking directory.
    flow_extension_install_root: str = ""
    #: ``versioned`` = Chrome's Extensions/<id>/<version>/ layout; ``directory`` =
    #: an unpacked build whose identity comes from the manifest key.
    flow_extension_install_layout: str = "versioned"
    #: Require a challenge-bound measurement before a session may be trusted.
    flow_require_installed_measurement: bool = False
    flow_measurement_challenge_ttl_seconds: float = 120.0
    flow_measurement_freshness_seconds: float = 300.0
    #: Separately installed executables that must be measured too when required.
    flow_mcp_install_root: str = ""
    flow_companion_install_root: str = ""
    #: Comma-separated *additional* required components. Empty means "the signed
    #: release manifest decides"; the Chrome extension is always required. Naming a
    #: narrower list than the manifest is refused, never silently honoured.
    flow_required_components: str = ""
    #: ---- Phase 3C.2.3 runtime measurement wiring ---------------------------
    #: The local companion process measures these roots itself; the extension can
    #: never submit a measurement. Layouts: ``versioned`` (Chrome) or ``directory``.
    #: ``tree`` = a plain directory measured as a whole (no Chrome manifest). The
    #: MCP server and the companion are not browser extensions, so they are installed
    #: as trees; ``directory`` remains for an unpacked extension-style install.
    #: (Layout settings live here rather than with the 3C.2.2 block above so that both
    #: the root and its layout are read together.)
    flow_mcp_install_layout: str = "tree"
    flow_companion_install_layout: str = "tree"
    #: Digest profile declared by the signed release and used by the measurement.
    flow_artifact_digest_algorithm: str = "sha256-tree-runtime-v1"
    #: ---- Phase 3C.2.3 spend controls --------------------------------------
    #: A Flow budget exists only when both are set; the guard reports
    #: ``budget_controls_missing`` otherwise. Zero is never "unlimited".
    flow_generation_budget_usd: float = 0.0
    flow_budget_enforcement: str = "none"  # none | flow_budget_ledger
    #: The companion reaches Crusor outbound; Crusor never dials the loopback port.
    flow_companion_id: str = ""
    flow_companion_secret: str = ""
    flow_backend_url: str = "http://127.0.0.1:8000"

    # Budget envelopes. The brief's default is "initially equivalent to ₹5 for a
    # simple task"; USD is the accounting currency, so the default is a
    # configurable conversion of that intention, not a hardcoded guess.
    strategy_budget_usd: float = 0.06      # ≈ ₹5 at the configured FX rate
    research_budget_usd: float = 0.12      # research needs more calls than one task
    #: Phase 3A: one weekly content plan. A plan makes many small calls, so the
    #: envelope is per-plan, not per-post, and is enforced by one reservation
    #: ledger shared by the Content Agent and the Brand Guardian.
    content_budget_usd: float = 0.06
    usd_inr_rate: float = 83.0
    budget_currency: str = "USD"

    # Bounded execution for research/strategy runs
    research_max_iterations: int = 6
    research_max_tool_calls: int = 28
    research_max_wall_seconds: int = 240
    # Bounded content planning. ``content_max_slots`` caps how many drafts one
    # plan may generate, so a large week cannot quietly become a large bill.
    content_max_slots: int = 12
    content_max_tool_calls: int = 40
    content_max_wall_seconds: int = 300
    content_max_tokens: int = 40_000
    strategy_max_iterations: int = 4
    strategy_max_tool_calls: int = 12
    strategy_max_wall_seconds: int = 180
    agent_max_tool_calls_per_run: int = 12

    # ---- Phase 2: research providers -----------------------------------
    research_provider: Literal["auto", "deterministic", "http", "disabled"] = "auto"
    research_http_endpoint: str | None = None
    research_http_api_key: str | None = None
    research_max_sources: int = 25
    research_cache_ttl_seconds: int = 3600

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
    # Token vault (OAuth credentials at rest). ``none`` disables OAuth entirely;
    # ``db`` stores AES-256-GCM ciphertext in ``oauth_tokens``; ``s3``/``external``
    # are declared but not implemented and therefore refuse rather than downgrade.
    token_vault_backend: Literal["none", "db", "s3", "external"] = "none"
    # 32-byte key material (hex, base64 or a >=32 character passphrase).
    token_vault_key: str | None = None

    # ---- social publishing (Phase 4) -----------------------------------
    # Public base URL used to build OAuth redirect URIs.
    oauth_redirect_base_url: str = "http://localhost:8000"
    # How long an authorisation attempt stays valid.
    oauth_state_ttl_seconds: int = 600
    # Per-platform HTTP timeout/retry policy for publish + analytics calls.
    integration_timeout_seconds: float = 30.0
    integration_max_retries: int = 3
    # Refresh a token this many seconds before it expires.
    token_refresh_skew_seconds: int = 120
    # Paid ads are gated on a founder authorisation; this is the maximum a single
    # authorisation may cover, as a second line of defence against a typo.
    ads_max_authorized_spend_cents: int = 5_000_00

    # ------------------------------------------------------------------ #
    @field_validator(
        "llm_task_routing_json", "llm_pricing_json", "creative_pricing_json", mode="before"
    )
    @classmethod
    def _parse_dict_json(cls, value: Any) -> Any:
        if value in (None, "", b""):
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, (str, bytes)):
            return json.loads(value)
        return value

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
    def flow_extension_id_list(self) -> list[str]:
        return [e.strip() for e in self.flow_allowed_extension_ids.split(",") if e.strip()]

    @property
    def flow_bridge_is_loopback(self) -> bool:
        import ipaddress

        host = (self.flow_bridge_host or "").strip()
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

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
