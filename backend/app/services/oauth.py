"""Connecting a social account through OAuth.

The sequence implemented here:

1. ``start()`` — allocate a single-use flow row, sign the state, seal the PKCE
   verifier, and return the provider's consent URL. Nothing is stored that could
   be used as a credential yet.
2. ``callback()`` — verify the signed state (HMAC + expiry), confirm the flow row
   is unconsumed and belongs to the same brand, exchange the code, encrypt the
   tokens into the vault, point ``social_accounts.token_ref`` at them, and mark
   the flow consumed.
3. ``refresh()`` — renew an expiring credential and re-store it.

Failure behaviour is deliberate: a failed exchange marks the flow with a reason
and the account stays ``pending``/``error``. It never falls back to an
unauthenticated "connected" state, and it never stores a token without encryption
(the vault refuses, and the exception propagates).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    IntegrationUnavailableError,
    NotFoundError,
    ValidationProblem,
)
from app.integrations import oauth as oauth_mod
from app.integrations.crypto import AssociatedData, TokenCipherError
from app.integrations.http import PlatformAuthError, request_json
from app.integrations.vault import (
    TokenRecord,
    VaultUnavailableError,
    build_cipher,
    build_vault,
)
from app.models.integration import OAuthFlow
from app.models.social import Platform, SocialAccount, SocialAccountStatus

HttpClientFactory = Callable[[], httpx.AsyncClient]


def _default_client_factory(settings: Settings) -> HttpClientFactory:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True, timeout=float(settings.integration_timeout_seconds)
        )

    return factory


class OAuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self._client_factory = client_factory or _default_client_factory(settings)

    # ----------------------------------------------------------------- start
    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.oauth_redirect_base_url.rstrip('/')}/api/v1/social/oauth/callback"

    async def start(
        self,
        *,
        platform: str,
        brand_profile_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        provider = oauth_mod.get_provider(platform)
        if (self.settings.token_vault_backend or "none") != "db" or not self.settings.token_vault_key:
            raise IntegrationUnavailableError(
                "OAuth connections are disabled: set TOKEN_VAULT_BACKEND=db and "
                "TOKEN_VAULT_KEY before connecting an account.",
                details={"platform": platform},
            )
        # Fails closed if this platform's own app credentials are missing.
        try:
            oauth_mod.client_credentials(self.settings, provider)
        except oauth_mod.OAuthError as exc:
            raise IntegrationUnavailableError(str(exc), details={"platform": platform}) from exc

        state, nonce = oauth_mod.build_state(
            self.settings.secret_key,
            platform=platform,
            brand_profile_id=brand_profile_id,
            redirect_uri=self.redirect_uri,
            ttl_seconds=int(self.settings.oauth_state_ttl_seconds),
        )
        verifier = oauth_mod.new_code_verifier() if provider.uses_pkce else None
        sealed_verifier = None
        if verifier:
            cipher = build_cipher(self.settings)
            if cipher is None:
                raise IntegrationUnavailableError(
                    "TOKEN_VAULT_KEY is required for PKCE; it is not usable.",
                    details={"platform": platform},
                )
            sealed_verifier = cipher.encrypt(
                verifier, AssociatedData(str(brand_profile_id), platform, nonce)
            )

        flow = OAuthFlow(
            brand_profile_id=brand_profile_id,
            platform=platform,
            state=state,
            sealed_code_verifier=sealed_verifier,
            redirect_uri=self.redirect_uri,
            requested_scopes=list(provider.scopes),
            created_by=actor_id,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=int(self.settings.oauth_state_ttl_seconds)),
            pkce_used=provider.uses_pkce,
        )
        self.session.add(flow)
        await self.session.flush()

        authorize_url = oauth_mod.build_authorize_url(
            self.settings,
            provider,
            state=state,
            redirect_uri=self.redirect_uri,
            code_challenge_value=oauth_mod.code_challenge(verifier) if verifier else None,
        )
        return {
            "platform": platform,
            "authorize_url": authorize_url,
            "state": state,
            "redirect_uri": self.redirect_uri,
            "scopes": list(provider.scopes),
            "pkce": provider.uses_pkce,
            "expires_in": int(self.settings.oauth_state_ttl_seconds),
        }

    # -------------------------------------------------------------- callback
    async def callback(
        self,
        *,
        state: str,
        code: str,
        brand_profile_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        try:
            payload = oauth_mod.verify_state(self.settings.secret_key, state)
        except oauth_mod.OAuthStateError as exc:
            raise ValidationProblem(
                f"The authorisation callback is not valid: {exc}",
                details={"reason": str(exc)},
            ) from exc
        if str(payload.get("b")) != str(brand_profile_id):
            raise ValidationProblem("The authorisation state belongs to a different brand.")
        platform = str(payload.get("p") or "")

        flow = (
            await self.session.execute(select(OAuthFlow).where(OAuthFlow.state == state))
        ).scalar_one_or_none()
        if flow is None:
            raise ValidationProblem("Unknown authorisation state.")
        if flow.consumed_at is not None:
            raise ConflictError("This authorisation has already been used.")
        expires_at = flow.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            flow.failure_reason = "expired"
            self.session.add(flow)
            await self.session.flush()
            raise ValidationProblem("This authorisation has expired; start again.")

        provider = oauth_mod.get_provider(platform)
        verifier = None
        if flow.pkce_used and flow.sealed_code_verifier:
            cipher = build_cipher(self.settings)
            if cipher is None:
                raise IntegrationUnavailableError("TOKEN_VAULT_KEY is unusable.")
            nonce = str(payload.get("n") or "")
            try:
                verifier = cipher.decrypt(
                    flow.sealed_code_verifier,
                    AssociatedData(str(brand_profile_id), platform, nonce),
                )
            except TokenCipherError as exc:
                raise ValidationProblem("The PKCE verifier could not be recovered.") from exc

        vault = build_vault(self.session, self.settings)
        if not vault.available:
            raise IntegrationUnavailableError(vault.reason, details={"platform": platform})

        client = self._client_factory()
        try:
            try:
                record = await oauth_mod.exchange_code(
                    self.settings,
                    provider,
                    code=code,
                    redirect_uri=flow.redirect_uri or self.redirect_uri,
                    code_verifier=verifier,
                    client=client,
                )
            except oauth_mod.OAuthError as exc:
                flow.failure_reason = f"{exc.code}: {exc}"[:500]
                self.session.add(flow)
                await self.session.flush()
                raise ValidationProblem(f"Authorisation failed: {exc}") from exc

            identity = await self._probe_identity(platform, record, client)  # best effort
        finally:
            await client.aclose()

        account_key = identity.get("external_account_id") or identity.get("handle") or str(uuid.uuid4())
        token_ref = await vault.store(
            brand_profile_id=brand_profile_id,
            platform=platform,
            account_key=account_key,
            record=record,
        )

        account = await self._upsert_account(
            brand_profile_id=brand_profile_id,
            platform=platform,
            identity=identity,
            token_ref=token_ref,
            record=record,
        )
        flow.consumed_at = datetime.now(timezone.utc)
        flow.failure_reason = ""
        self.session.add(flow)
        await self.session.flush()

        return {
            "platform": platform,
            "status": account.status,
            "handle": account.handle,
            "external_account_id": account.external_account_id,
            "scopes": list(record.scopes or provider.scopes),
            "expires_at": record.expires_at,
            "refreshed": False,
            "verified": bool(identity.get("verified")),
            "detail": identity.get("detail")
            or "credential stored encrypted; connect verification not performed",
        }

    # --------------------------------------------------------------- refresh
    async def refresh(self, account_id: uuid.UUID) -> dict[str, Any]:
        account = await self.session.get(SocialAccount, account_id)
        if account is None:
            raise NotFoundError(f"Social account {account_id} not found.")
        if not account.token_ref:
            raise ValidationProblem("This account has no stored credential to refresh.")
        vault = build_vault(self.session, self.settings)
        if not vault.available:
            raise IntegrationUnavailableError(vault.reason)
        record = await vault.load(account.token_ref)
        if not record.refresh_token:
            raise ValidationProblem(
                "The provider issued no refresh token for this account, so it cannot be "
                "renewed silently. Reconnect it."
            )
        provider = oauth_mod.get_provider(account.platform)
        client = self._client_factory()
        try:
            refreshed = await oauth_mod.refresh_access_token(
                self.settings, provider, refresh_token=record.refresh_token, client=client
            )
        except oauth_mod.OAuthError as exc:
            account.status = SocialAccountStatus.EXPIRED.value
            self.session.add(account)
            await self.session.flush()
            raise IntegrationUnavailableError(
                f"Token refresh failed: {exc}", details={"platform": account.platform}
            ) from exc
        finally:
            await client.aclose()

        await vault.store(
            brand_profile_id=account.brand_profile_id,
            platform=account.platform,
            account_key=account.external_account_id or account.handle,
            record=refreshed,
        )
        account.token_expires_at = refreshed.expires_at
        account.status = SocialAccountStatus.CONNECTED.value
        account.scopes = list(refreshed.scopes or account.scopes or [])
        self.session.add(account)
        await self.session.flush()
        return {
            "platform": account.platform,
            "status": account.status,
            "handle": account.handle,
            "external_account_id": account.external_account_id,
            "scopes": account.scopes,
            "expires_at": refreshed.expires_at,
            "refreshed": True,
            "verified": False,
            "detail": "access token renewed",
        }

    # --------------------------------------------------------------- helpers
    async def _upsert_account(
        self,
        *,
        brand_profile_id: uuid.UUID,
        platform: str,
        identity: dict[str, Any],
        token_ref: str,
        record: TokenRecord,
    ) -> SocialAccount:
        handle = str(identity.get("handle") or "")[:200]
        external = str(identity.get("external_account_id") or "")[:200] or None
        account = (
            await self.session.execute(
                select(SocialAccount).where(
                    SocialAccount.brand_profile_id == brand_profile_id,
                    SocialAccount.platform == platform,
                    SocialAccount.handle == handle,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            account = SocialAccount(
                brand_profile_id=brand_profile_id, platform=platform, handle=handle
            )
        account.display_name = str(identity.get("display_name") or account.display_name)[:200]
        account.external_account_id = external or account.external_account_id
        account.status = SocialAccountStatus.CONNECTED.value
        account.token_ref = token_ref
        account.token_expires_at = record.expires_at
        account.scopes = list(record.scopes or [])
        account.capabilities = dict(identity.get("settings") or {})
        account.notes = str(identity.get("detail") or "")[:2000]
        account.last_synced_at = datetime.now(timezone.utc)
        self.session.add(account)
        await self.session.flush()
        return account

    async def _probe_identity(
        self, platform: str, record: TokenRecord, client: httpx.AsyncClient
    ) -> dict[str, Any]:
        """Ask the platform who this credential belongs to.

        A best-effort read that gives the account a real handle and an
        opportunity to prove the credential works. Failure is reported as
        ``verified: False`` with the reason — it never invents a handle.
        """
        provider = oauth_mod.get_provider(platform)
        base = provider.api_base
        headers = {"Authorization": f"{record.token_type or 'Bearer'} {record.access_token}"}
        try:
            if platform in {"facebook", "instagram"}:
                data = await request_json(client, "GET", f"{base}/me", params={"fields": "id,name"})
                return {
                    "handle": data.get("name", "") or str(data.get("id", "")),
                    "external_account_id": str(data.get("id", "")),
                    "display_name": data.get("name", ""),
                    "verified": True,
                    "detail": "identity verified against /me",
                }
            if platform == "linkedin":
                data = await request_json(client, "GET", f"{base}/v2/me", headers=headers)
                return {
                    "handle": data.get("localizedFirstName", "") or str(data.get("id", "")),
                    "external_account_id": f"urn:li:person:{data.get('id', '')}",
                    "display_name": data.get("localizedFirstName", ""),
                    "verified": True,
                    "detail": "identity verified against /v2/me",
                }
            if platform == "x":
                data = await request_json(client, "GET", f"{base}/users/me", headers=headers)
                payload = data.get("data") or {}
                return {
                    "handle": str(payload.get("username", "")),
                    "external_account_id": str(payload.get("id", "")),
                    "display_name": str(payload.get("name", "")),
                    "verified": True,
                    "detail": "identity verified against /2/users/me",
                }
            if platform == "youtube":
                data = await request_json(
                    client,
                    "GET",
                    f"{base}/youtube/v3/channels",
                    params={"part": "snippet", "mine": "true"},
                    headers=headers,
                )
                items = data.get("items") or []
                if not items:
                    return {"verified": False, "detail": "no YouTube channel on this credential"}
                channel = items[0]
                return {
                    "handle": str(channel.get("snippet", {}).get("title", "")),
                    "external_account_id": str(channel.get("id", "")),
                    "display_name": str(channel.get("snippet", {}).get("title", "")),
                    "verified": True,
                    "detail": "identity verified against channels.list",
                }
            if platform == "reddit":
                data = await request_json(
                    client,
                    "GET",
                    f"{base}/api/v1/me",
                    headers={**headers, "User-Agent": "crusor-marketing-director/1.0"},
                )
                return {
                    "handle": str(data.get("name", "")),
                    "external_account_id": str(data.get("id", "")),
                    "verified": True,
                    "detail": "identity verified against /api/v1/me",
                }
        except PlatformAuthError as exc:
            return {"verified": False, "detail": f"credential rejected when probed: {exc}"}
        except Exception as exc:  # noqa: BLE001 - probe is best effort
            return {"verified": False, "detail": f"identity probe failed: {type(exc).__name__}"}
        return {"verified": False, "detail": f"no identity probe for {platform}"}


def platform_from_str(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError as exc:
        raise ValidationProblem(f"Unsupported platform {value!r}.") from exc


__all__ = ["OAuthService", "platform_from_str", "VaultUnavailableError"]
