"""The token vault: where platform credentials live at rest.

Two rules, both enforced here rather than by convention:

1. **The API never sees a token.** ``social_accounts.token_ref`` is an opaque
   ``vault:<uuid>`` pointer. Callers ask the vault for a :class:`TokenRecord`
   *inside* the process that is about to call the platform.
2. **Failing closed beats guessing.** If ``TOKEN_VAULT_KEY`` is absent the vault
   reports ``available = False`` and every store/load raises
   :class:`VaultUnavailableError`. It never falls back to plaintext and never
   invents a development key, because a default key is indistinguishable from a
   real one to everyone except an attacker.

The vault is a thin, deliberately boring layer over
:class:`app.models.integration.OAuthToken` plus :class:`TokenCipher`. Keeping it
boring is the point: every credential path in the application funnels through
these four methods.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations.crypto import (
    AssociatedData,
    TokenCipher,
    TokenCipherError,
    TokenCipherUnavailable,
    TokenDecryptionError,
)
from app.models.integration import OAuthToken

TOKEN_REF_PREFIX = "vault:"


class VaultError(Exception):
    """Base class for vault failures."""


class VaultUnavailableError(VaultError):
    """No vault key/backend is configured, so credentials cannot be handled."""


class TokenNotFoundError(VaultError):
    """The reference does not resolve, or the credential was revoked."""


@dataclass(frozen=True)
class TokenRecord:
    """A decrypted credential. Exists in memory for one call."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None

    def is_expired(self, *, skew_seconds: int = 60) -> bool:
        if self.expires_at is None:
            return False
        now = datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return (expires - now).total_seconds() <= skew_seconds


class TokenVault(Protocol):
    available: bool
    reason: str

    async def store(
        self,
        *,
        brand_profile_id: uuid.UUID,
        platform: str,
        account_key: str,
        record: TokenRecord,
    ) -> str: ...

    async def load(self, token_ref: str) -> TokenRecord: ...

    async def revoke(self, token_ref: str) -> None: ...


def make_token_ref(token_id: uuid.UUID) -> str:
    return f"{TOKEN_REF_PREFIX}{token_id}"


def parse_token_ref(token_ref: str) -> uuid.UUID:
    if not token_ref.startswith(TOKEN_REF_PREFIX):
        raise TokenNotFoundError("token reference is not a vault reference")
    try:
        return uuid.UUID(token_ref[len(TOKEN_REF_PREFIX) :])
    except ValueError as exc:
        raise TokenNotFoundError("token reference is malformed") from exc


class DatabaseTokenVault:
    """AES-GCM ciphertext in ``oauth_tokens``, keyed by ``TOKEN_VAULT_KEY``."""

    def __init__(self, session: AsyncSession, cipher: TokenCipher | None, reason: str = "") -> None:
        self.session = session
        self._cipher = cipher
        self._reason = reason
        self.available = cipher is not None

    @property
    def reason(self) -> str:
        if self.available:
            return "vault key configured; credentials are stored encrypted (AES-256-GCM)"
        return self._reason or "TOKEN_VAULT_KEY is not configured"

    # ------------------------------------------------------------------ write
    async def store(
        self,
        *,
        brand_profile_id: uuid.UUID,
        platform: str,
        account_key: str,
        record: TokenRecord,
    ) -> str:
        cipher = self._require_cipher()
        aad = AssociatedData(str(brand_profile_id), platform, account_key)
        sealed_access = cipher.encrypt(record.access_token, aad)
        sealed_refresh = (
            cipher.encrypt(record.refresh_token, aad) if record.refresh_token else None
        )

        existing = (
            await self.session.execute(
                select(OAuthToken).where(
                    OAuthToken.brand_profile_id == brand_profile_id,
                    OAuthToken.platform == platform,
                    OAuthToken.account_key == account_key,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = OAuthToken(
                brand_profile_id=brand_profile_id,
                platform=platform,
                account_key=account_key,
                sealed_access=sealed_access,
                sealed_refresh=sealed_refresh,
                token_type=record.token_type,
                scopes=list(record.scopes),
                expires_at=record.expires_at,
            )
            self.session.add(existing)
        else:
            existing.sealed_access = sealed_access
            existing.sealed_refresh = sealed_refresh
            existing.token_type = record.token_type
            existing.scopes = list(record.scopes)
            existing.expires_at = record.expires_at
            existing.revoked_at = None
            if record.refresh_token:
                existing.refresh_count = int(existing.refresh_count or 0) + 1
                existing.last_refreshed_at = datetime.now(timezone.utc)
            self.session.add(existing)

        await self.session.flush()
        return make_token_ref(existing.id)

    async def revoke(self, token_ref: str) -> None:
        self._require_cipher()
        row = await self._row(token_ref)
        row.revoked_at = datetime.now(timezone.utc)
        row.sealed_access = ""
        row.sealed_refresh = None
        self.session.add(row)
        await self.session.flush()

    # ------------------------------------------------------------------- read
    async def load(self, token_ref: str) -> TokenRecord:
        cipher = self._require_cipher()
        row = await self._row(token_ref)
        if row.revoked_at is not None or not row.sealed_access:
            raise TokenNotFoundError("credential was revoked")
        aad = AssociatedData(str(row.brand_profile_id), row.platform, row.account_key)
        try:
            access = cipher.decrypt(row.sealed_access, aad)
            refresh = cipher.decrypt(row.sealed_refresh, aad) if row.sealed_refresh else None
        except TokenDecryptionError as exc:
            raise VaultError(f"stored credential cannot be decrypted: {exc}") from exc
        return TokenRecord(
            access_token=access,
            refresh_token=refresh,
            token_type=row.token_type,
            scopes=list(row.scopes or []),
            expires_at=row.expires_at,
        )

    # ---------------------------------------------------------------- helpers
    async def _row(self, token_ref: str) -> OAuthToken:
        token_id = parse_token_ref(token_ref)
        row = await self.session.get(OAuthToken, token_id)
        if row is None:
            raise TokenNotFoundError("credential reference does not resolve")
        return row

    def _require_cipher(self) -> TokenCipher:
        if self._cipher is None:
            raise VaultUnavailableError(self.reason)
        return self._cipher


class UnavailableVault:
    """Explicit 'no vault' implementation. Every operation refuses."""

    available = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def store(self, **_kwargs: object) -> str:
        raise VaultUnavailableError(self.reason)

    async def load(self, _token_ref: str) -> TokenRecord:
        raise VaultUnavailableError(self.reason)

    async def revoke(self, _token_ref: str) -> None:
        raise VaultUnavailableError(self.reason)


def build_vault(session: AsyncSession, settings: Settings) -> TokenVault:
    """Return a usable vault or an explicit refusal.

    ``token_vault_backend``:
      * ``none``     — disabled (default); OAuth is unavailable.
      * ``db``       — AES-256-GCM ciphertext in ``oauth_tokens``.
      * ``s3`` / ``external`` — declared but not implemented here; refused rather
        than silently downgraded to the local backend, because an operator who
        asked for an external KMS should not get a database row instead.
    """
    backend = (settings.token_vault_backend or "none").lower()

    if backend == "none":
        return UnavailableVault(
            "token vault is disabled (TOKEN_VAULT_BACKEND=none); OAuth connections are unavailable"
        )
    if backend in {"s3", "external"}:
        return UnavailableVault(
            f"token vault backend '{backend}' is declared but not implemented in this build"
        )
    try:
        cipher = TokenCipher.from_settings(settings.token_vault_key)
    except TokenCipherUnavailable as exc:
        return UnavailableVault(str(exc))
    except TokenCipherError as exc:  # pragma: no cover - defensive
        return UnavailableVault(f"token vault key is unusable: {exc}")
    return DatabaseTokenVault(session, cipher)


def build_cipher(settings: Settings) -> TokenCipher | None:
    """Key-only cipher for sealing short-lived flow material (PKCE verifiers)."""
    try:
        return TokenCipher.from_settings(settings.token_vault_key)
    except TokenCipherError:
        return None
