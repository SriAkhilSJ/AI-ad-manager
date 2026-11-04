"""Platform publishing adapters.

An adapter answers one question: *can this platform actually publish right now,
and if so through what?*

Three availability inputs must all be true before a transport exists:

1. **Application credentials** for that platform (its own, never another's).
2. **A usable token vault** — without `TOKEN_VAULT_KEY` there is nowhere safe to
   keep an OAuth token, so publishing is refused rather than attempted.
3. **A connected account** with a token reference for the brand.

When any is missing the adapter is an explicit refusal with a reason, surfaced
through `/api/v1/system/status` and `/api/v1/social/platforms`, and publishing
raises ``publishing_disabled`` without touching the network. Nothing here ever
fabricates a publication.

Real transports live in :mod:`app.integrations.platforms`; each one verifies the
created object before returning, so a "successful" HTTP response alone is never
enough to mark content published.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.integrations.crypto import TokenCipherError
from app.integrations.http import PlatformError, RetryPolicy
from app.integrations.oauth import OAuthError, get_provider, refresh_access_token
from app.integrations.vault import TokenNotFoundError, TokenRecord, VaultError, build_vault
from app.models.social import Platform


@dataclass(frozen=True)
class PublishPayload:
    content_item_id: uuid.UUID
    content_version_id: uuid.UUID
    approval_record_id: uuid.UUID
    platform: str
    body: str
    headline: str
    hashtags: list[str]
    call_to_action: str
    media_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PublishResult:
    external_post_id: str
    permalink: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PlatformAdapter(Protocol):
    platform: Platform
    credentials_configured: bool
    transport_available: bool

    async def publish(self, payload: PublishPayload, account: Any) -> PublishResult: ...


class UnavailableAdapter:
    """Explicit 'not configured' adapter. Never fabricates a publish."""

    transport_available = False

    def __init__(
        self,
        platform: Platform,
        credentials_configured: bool = False,
        *,
        reason: str = "",
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        self.platform = platform
        self.credentials_configured = credentials_configured
        self._reason = reason
        self.capabilities = capabilities or {}

    @property
    def reason(self) -> str:
        if self._reason:
            return self._reason
        if self.credentials_configured:
            return (
                f"{self.platform.value}: application credentials are present but no usable "
                "token vault is configured (set TOKEN_VAULT_BACKEND=db and TOKEN_VAULT_KEY)."
            )
        return (
            f"{self.platform.value}: application credentials are not configured. "
            "Set the platform app id/secret to enable."
        )

    async def publish(self, payload: PublishPayload, account: Any) -> PublishResult:
        from app.core.errors import PublishingDisabledError

        raise PublishingDisabledError(self.reason, details={"platform": self.platform.value})


class LiveAdapter:
    """A real transport, bound to one account and one OAuth credential."""

    transport_available = True

    def __init__(
        self,
        *,
        platform: Platform,
        settings: Settings,
        session: Any,
        account: Any,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.platform = platform
        self.credentials_configured = True
        self.settings = settings
        self.session = session
        self.account = account
        self._http = http
        self.capabilities: dict[str, Any] = {}

    @property
    def reason(self) -> str:
        return f"{self.platform.value}: live transport available"

    async def publish(self, payload: PublishPayload, account: Any) -> PublishResult:
        from app.core.errors import PublishingDisabledError
        from app.integrations import platforms as transports

        if not account.token_ref:
            raise PublishingDisabledError(
                f"{self.platform.value}: the connected account has no stored credential. "
                "Reconnect the account.",
                details={"platform": self.platform.value},
            )

        token = await self._access_token(account)
        ref = transports.AccountRef(
            platform=self.platform.value,
            external_account_id=account.external_account_id or account.handle,
            handle=account.handle,
            settings=dict(account.capabilities or {}),
        )
        policy = RetryPolicy(
            max_attempts=max(1, int(self.settings.integration_max_retries)),
            timeout_seconds=float(self.settings.integration_timeout_seconds),
        )
        own_client = self._http is None
        client = self._http or httpx.AsyncClient(follow_redirects=True)
        try:
            transport = transports.build_client(
                self.platform.value, self.settings, client, policy=policy
            )
            return await transport.publish(payload, account=ref, token=token)
        finally:
            if own_client:
                await client.aclose()

    async def _access_token(self, account: Any) -> TokenRecord:
        """Load the credential, refreshing it when it is close to expiry."""
        vault = build_vault(self.session, self.settings)
        if not vault.available:
            from app.core.errors import PublishingDisabledError

            raise PublishingDisabledError(vault.reason, details={"platform": self.platform.value})
        try:
            record = await vault.load(account.token_ref)
        except TokenNotFoundError as exc:
            raise _auth_problem(self.platform.value, str(exc)) from exc
        except (VaultError, TokenCipherError) as exc:
            raise _auth_problem(self.platform.value, f"credential unreadable: {exc}") from exc

        skew = int(self.settings.token_refresh_skew_seconds)
        if not record.is_expired(skew_seconds=skew) or not record.refresh_token:
            return record

        provider = get_provider(self.platform.value)
        client = self._http or httpx.AsyncClient(follow_redirects=True)
        own = self._http is None
        try:
            refreshed = await refresh_access_token(
                self.settings, provider, refresh_token=record.refresh_token, client=client
            )
        except OAuthError as exc:
            account.status = "expired"
            self.session.add(account)
            await self.session.flush()
            raise _auth_problem(self.platform.value, f"token refresh failed: {exc}") from exc
        finally:
            if own:
                await client.aclose()

        await vault.store(
            brand_profile_id=account.brand_profile_id,
            platform=self.platform.value,
            account_key=account.external_account_id or account.handle,
            record=refreshed,
        )
        return refreshed


def _auth_problem(platform: str, detail: str):  # type: ignore[no-untyped-def]
    from app.core.errors import IntegrationUnavailableError

    return IntegrationUnavailableError(
        f"{platform}: {detail}. Reconnect the account.", details={"platform": platform}
    )


def _credential_map(settings: Settings) -> dict[Platform, bool]:
    return {
        Platform.INSTAGRAM: bool(settings.meta_app_id and settings.meta_app_secret),
        Platform.FACEBOOK: bool(settings.meta_app_id and settings.meta_app_secret),
        Platform.LINKEDIN: bool(settings.linkedin_client_id and settings.linkedin_client_secret),
        Platform.YOUTUBE: bool(settings.google_client_id and settings.google_client_secret),
        Platform.X: bool(settings.x_client_id and settings.x_client_secret),
        Platform.REDDIT: bool(settings.reddit_client_id and settings.reddit_client_secret),
    }


class AdapterRegistry:
    """Reports honest availability and builds live transports on demand."""

    def __init__(
        self,
        settings: Settings,
        vault_available: bool | None = None,
        vault_reason: str = "",
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        from app.integrations.platforms import capabilities_for

        self.settings = settings
        creds = _credential_map(settings)
        if vault_available is None:
            vault_available = (
                (settings.token_vault_backend or "none") == "db"
                and bool(settings.token_vault_key)
            )
            if not vault_available and not vault_reason:
                vault_reason = (
                    "token vault is not configured (TOKEN_VAULT_BACKEND must be 'db' and "
                    "TOKEN_VAULT_KEY must be set)"
                )
        self.vault_available = bool(vault_available)
        self.vault_reason = vault_reason
        #: Injected for tests (``httpx.MockTransport``); None means a real client.
        self.http_client = http_client
        self._adapters: dict[Platform, UnavailableAdapter] = {}
        for platform in Platform:
            reason = ""
            if not creds[platform]:
                reason = (
                    f"{platform.value}: application credentials are not configured."
                )
            elif not self.vault_available:
                reason = f"{platform.value}: {self.vault_reason}."
            self._adapters[platform] = UnavailableAdapter(
                platform,
                credentials_configured=creds[platform],
                reason=reason,
                capabilities=capabilities_for(platform.value),
            )

    def get(self, platform: str) -> UnavailableAdapter:
        return self._adapters[Platform(platform)]

    def availability(self) -> list[dict[str, Any]]:
        return [
            {
                "platform": platform.value,
                "credentials_configured": adapter.credentials_configured,
                "vault_available": self.vault_available,
                "transport_available": adapter.credentials_configured
                and self.vault_available,
                "publish_enabled": adapter.credentials_configured and self.vault_available,
                "capabilities": adapter.capabilities,
                "reason": adapter.reason,
            }
            for platform, adapter in self._adapters.items()
        ]

    @property
    def any_publish_enabled(self) -> bool:
        return any(
            a.credentials_configured and self.vault_available for a in self._adapters.values()
        )

    async def resolve(
        self, platform: str, *, session: Any, account: Any
    ) -> PlatformAdapter:
        """Return a live adapter for this account, or raise ``publishing_disabled``."""
        from app.core.errors import PublishingDisabledError
        from app.integrations.platforms import capabilities_for

        adapter = self.get(platform)
        if not adapter.credentials_configured:
            raise PublishingDisabledError(
                adapter.reason, details={"platform": platform, "stage": "credentials"}
            )
        if not self.vault_available:
            raise PublishingDisabledError(
                adapter.reason, details={"platform": platform, "stage": "vault"}
            )
        if not getattr(account, "token_ref", None):
            raise PublishingDisabledError(
                f"{platform}: the connected account has no stored credential; "
                "complete the OAuth connection first.",
                details={"platform": platform, "stage": "account"},
            )
        live = LiveAdapter(
            platform=Platform(platform),
            settings=self.settings,
            session=session,
            account=account,
            http=self.http_client,
        )
        live.capabilities = capabilities_for(platform)
        return live


_registry: AdapterRegistry | None = None


def get_adapter_registry(settings: Settings | None = None) -> AdapterRegistry:
    global _registry
    if _registry is None:
        from app.core.config import get_settings

        _registry = AdapterRegistry(settings or get_settings())
    return _registry


def reset_adapter_registry() -> None:
    global _registry
    _registry = None
