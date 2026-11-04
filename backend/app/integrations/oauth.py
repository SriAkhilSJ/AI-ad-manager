"""OAuth 2.0 authorisation-code flow for the supported social platforms.

What is real here and what is not:

* **Real:** endpoint URLs, grant types, PKCE, client-authentication styles,
  scope strings and token-response parsing for each platform. These follow the
  providers' published OAuth documentation (URLs are pinned in
  :data:`PROVIDERS` with a link to the docs).
* **Not verifiable from this repository:** that a given *application* is
  approved for a given scope, and that a provider's live behaviour matches its
  documentation. Both require the operator's own app credentials and a real
  account, so the presets are honest about being *presets*: a scope can still be
  refused by the provider at authorisation time, and that refusal is surfaced.

Security properties implemented here:

* **Signed, expiring state** — HMAC-SHA256 over the payload with ``SECRET_KEY``,
  compared in constant time, plus an absolute expiry. An attacker cannot forge a
  callback for another brand.
* **PKCE S256** where the platform supports it, with the verifier sealed at rest
  and bound to the state.
* **No credential sharing.** Each platform has its own client id/secret variable;
  there is no fallback that borrows another platform's credentials.

Nothing in this module decides whether publishing is allowed. It only converts an
authorisation code into a credential the vault can hold.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from app.integrations.vault import TokenRecord

STATE_VERSION = 1
_UNSAFE_DEBUG = False


class OAuthError(Exception):
    """A provider refused or the exchange could not be completed."""

    def __init__(self, message: str, *, code: str = "oauth_error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class OAuthStateError(OAuthError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="oauth_state_invalid", status=400)


class OAuthNotConfigured(OAuthError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="oauth_not_configured", status=503)


@dataclass(frozen=True)
class OAuthProvider:
    """Pinned description of one platform's OAuth implementation."""

    platform: str
    display_name: str
    authorize_url: str
    token_url: str
    api_base: str
    scopes: tuple[str, ...]
    uses_pkce: bool = False
    #: ``body`` sends client_id/secret as form fields; ``basic`` uses HTTP Basic.
    token_auth: Literal["body", "basic"] = "body"
    revoke_url: str | None = None
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    docs_url: str = ""
    #: Named credential settings, resolved against ``Settings``.
    client_id_setting: str = ""
    client_secret_setting: str = ""
    notes: str = ""


# Scope choices are the minimum needed for *publishing* plus *reading* the
# resulting post's metrics. Requesting more than the product uses would make the
# consent screen misleading.
PROVIDERS: dict[str, OAuthProvider] = {
    "facebook": OAuthProvider(
        platform="facebook",
        display_name="Facebook Page",
        authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
        token_url="https://graph.facebook.com/v21.0/oauth/access_token",
        api_base="https://graph.facebook.com/v21.0",
        scopes=("pages_manage_posts", "pages_read_engagement", "read_insights"),
        token_auth="body",
        docs_url="https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow",
        client_id_setting="meta_app_id",
        client_secret_setting="meta_app_secret",
        notes="Graph API v21.0. Page posts and insights.",
    ),
    "instagram": OAuthProvider(
        platform="instagram",
        display_name="Instagram Business",
        authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
        token_url="https://graph.facebook.com/v21.0/oauth/access_token",
        api_base="https://graph.facebook.com/v21.0",
        scopes=(
            "instagram_basic",
            "instagram_content_publish",
            "instagram_manage_insights",
            "pages_show_list",
        ),
        token_auth="body",
        docs_url="https://developers.facebook.com/docs/instagram-api/guides/content-publishing",
        client_id_setting="meta_app_id",
        client_secret_setting="meta_app_secret",
        notes="Shares the Meta app with Facebook; content publishing is image/video only.",
    ),
    "linkedin": OAuthProvider(
        platform="linkedin",
        display_name="LinkedIn",
        authorize_url="https://www.linkedin.com/oauth/v2/authorization",
        token_url="https://www.linkedin.com/oauth/v2/accessToken",
        api_base="https://api.linkedin.com",
        scopes=("w_member_social", "r_liteprofile", "r_organization_social", "rw_organization_admin"),
        token_auth="body",
        docs_url="https://learn.microsoft.com/en-us/linkedin/shared/authentication/authentication",
        client_id_setting="linkedin_client_id",
        client_secret_setting="linkedin_client_secret",
        notes="UGC Posts API. Organization posting requires an approved Marketing Developer app.",
    ),
    "x": OAuthProvider(
        platform="x",
        display_name="X",
        authorize_url="https://twitter.com/i/oauth2/authorize",
        token_url="https://api.twitter.com/2/oauth2/token",
        api_base="https://api.twitter.com/2",
        scopes=("tweet.read", "tweet.write", "users.read", "offline.access"),
        uses_pkce=True,
        token_auth="basic",
        revoke_url="https://api.twitter.com/2/oauth2/revoke",
        docs_url="https://developer.x.com/en/docs/authentication/oauth-2-0/authorization-code",
        client_id_setting="x_client_id",
        client_secret_setting="x_client_secret",
        notes="OAuth 2.0 with PKCE; offline.access is required to refresh.",
    ),
    "youtube": OAuthProvider(
        platform="youtube",
        display_name="YouTube",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        api_base="https://www.googleapis.com",
        scopes=(
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/yt-analytics.readonly",
        ),
        token_auth="body",
        revoke_url="https://oauth2.googleapis.com/revoke",
        extra_authorize_params={
            "access_type": "offline",
            # Without this Google only issues a refresh token the first time,
            # which silently breaks renewal after a re-consent.
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
        docs_url="https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps",
        client_id_setting="google_client_id",
        client_secret_setting="google_client_secret",
        notes="Video upload is resumable and requires an actual video asset.",
    ),
    "reddit": OAuthProvider(
        platform="reddit",
        display_name="Reddit",
        authorize_url="https://www.reddit.com/api/v1/authorize",
        token_url="https://www.reddit.com/api/v1/access_token",
        api_base="https://oauth.reddit.com",
        scopes=("identity", "submit", "read", "flair"),
        token_auth="basic",
        revoke_url="https://www.reddit.com/api/v1/revoke_token",
        extra_authorize_params={"duration": "permanent"},
        docs_url="https://github.com/reddit-archive/reddit/wiki/OAuth2",
        client_id_setting="reddit_client_id",
        client_secret_setting="reddit_client_secret",
        notes="Reddit requires an explicit User-Agent; a missing one is rate-limited hard.",
    ),
}


def get_provider(platform: str) -> OAuthProvider:
    try:
        return PROVIDERS[platform]
    except KeyError as exc:
        raise OAuthError(f"unsupported platform {platform!r}", code="unsupported_platform", status=400) from exc


def client_credentials(settings: Any, provider: OAuthProvider) -> tuple[str, str]:
    """Resolve this platform's *own* credentials.

    There is deliberately no fallback: a missing pair is a refusal, never a
    borrowed credential from another provider.
    """
    client_id = getattr(settings, provider.client_id_setting, None)
    client_secret = getattr(settings, provider.client_secret_setting, None)
    if not client_id or not client_secret:
        raise OAuthNotConfigured(
            f"{provider.display_name}: {provider.client_id_setting}/"
            f"{provider.client_secret_setting} are not configured."
        )
    return str(client_id), str(client_secret)


# --------------------------------------------------------------------- PKCE
def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# -------------------------------------------------------------------- state
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def build_state(
    secret: str,
    *,
    platform: str,
    brand_profile_id: uuid.UUID | str,
    redirect_uri: str,
    ttl_seconds: int = 600,
    jti: str | None = None,
) -> tuple[str, str]:
    """Return ``(state, nonce)``. The nonce is the server-side handle."""
    nonce = jti or secrets.token_urlsafe(18)
    payload = {
        "v": STATE_VERSION,
        "n": nonce,
        "p": platform,
        "b": str(brand_profile_id),
        "r": redirect_uri,
        "iat": int(time.time()),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{encoded}.{_sign(secret, encoded)}", nonce


def verify_state(secret: str, state: str, *, now: float | None = None) -> dict[str, Any]:
    try:
        encoded, signature = state.split(".", 1)
    except ValueError as exc:
        raise OAuthStateError("state is malformed") from exc
    expected = _sign(secret, encoded)
    if not hmac.compare_digest(expected, signature):
        raise OAuthStateError("state signature is invalid")
    try:
        payload = json.loads(_unb64(encoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise OAuthStateError("state payload is unreadable") from exc
    if payload.get("v") != STATE_VERSION:
        raise OAuthStateError("state version is unsupported")
    current = int(now if now is not None else time.time())
    if int(payload.get("exp", 0)) < current:
        raise OAuthStateError("state has expired")
    return payload


# ------------------------------------------------------------------- flow
def build_authorize_url(
    settings: Any,
    provider: OAuthProvider,
    *,
    state: str,
    redirect_uri: str,
    code_challenge_value: str | None = None,
    scopes: tuple[str, ...] | None = None,
) -> str:
    client_id, _ = client_credentials(settings, provider)
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(scopes or provider.scopes),
    }
    params.update(provider.extra_authorize_params)
    if provider.uses_pkce:
        if not code_challenge_value:
            raise OAuthError("PKCE challenge required for this platform", code="pkce_required")
        params["code_challenge"] = code_challenge_value
        params["code_challenge_method"] = "S256"
    return f"{provider.authorize_url}?{urlencode(params)}"


def _parse_token_response(provider: OAuthProvider, data: dict[str, Any]) -> TokenRecord:
    access = data.get("access_token")
    if not access:
        raise OAuthError(
            f"{provider.display_name} returned no access_token",
            code="oauth_token_missing",
            status=502,
        )
    expires_in = data.get("expires_in")
    expires_at = None
    if expires_in not in (None, "", 0):
        try:
            from datetime import datetime, timedelta, timezone

            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            expires_at = None
    raw_scope = data.get("scope") or ""
    scopes = raw_scope.split() if isinstance(raw_scope, str) else list(raw_scope or [])
    return TokenRecord(
        access_token=str(access),
        refresh_token=data.get("refresh_token") or None,
        token_type=str(data.get("token_type") or "Bearer"),
        scopes=scopes,
        expires_at=expires_at,
    )


async def exchange_code(
    settings: Any,
    provider: OAuthProvider,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str | None,
    client: httpx.AsyncClient,
) -> TokenRecord:
    client_id, client_secret = client_credentials(settings, provider)
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Accept": "application/json"}
    if provider.token_auth == "body":
        data["client_id"] = client_id
        data["client_secret"] = client_secret
    else:
        token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
        # X additionally requires client_id in the body for this grant.
        data["client_id"] = client_id
    if provider.uses_pkce:
        if not code_verifier:
            raise OAuthError("code_verifier is required", code="pkce_required")
        data["code_verifier"] = code_verifier

    response = await client.post(provider.token_url, data=data, headers=headers)
    return _handle_token_response(provider, response)


async def refresh_access_token(
    settings: Any,
    provider: OAuthProvider,
    *,
    refresh_token: str,
    client: httpx.AsyncClient,
) -> TokenRecord:
    client_id, client_secret = client_credentials(settings, provider)
    data: dict[str, str] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    headers = {"Accept": "application/json"}
    if provider.token_auth == "body":
        data["client_id"] = client_id
        data["client_secret"] = client_secret
    else:
        token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
        data["client_id"] = client_id

    response = await client.post(provider.token_url, data=data, headers=headers)
    record = _handle_token_response(provider, response)
    # Providers commonly omit the refresh token on renewal; keep the old one.
    if not record.refresh_token:
        record = TokenRecord(
            access_token=record.access_token,
            refresh_token=refresh_token,
            token_type=record.token_type,
            scopes=record.scopes or list(provider.scopes),
            expires_at=record.expires_at,
        )
    return record


async def revoke_token(
    settings: Any,
    provider: OAuthProvider,
    *,
    token: str,
    client: httpx.AsyncClient,
) -> bool:
    """Best-effort revocation at the provider. Returns whether it was accepted."""
    if not provider.revoke_url:
        return False
    try:
        client_id, client_secret = client_credentials(settings, provider)
    except OAuthNotConfigured:
        return False
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    if provider.platform == "youtube":
        response = await client.post(provider.revoke_url, data={"token": token})
    else:
        response = await client.post(
            provider.revoke_url,
            data={"token": token, "token_type_hint": "access_token"},
            headers={"Authorization": f"Basic {auth}"},
        )
    return response.status_code < 300


def _handle_token_response(provider: OAuthProvider, response: httpx.Response) -> TokenRecord:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "error" in data:
        # Never echo the request body: it contains the client secret.
        detail = data.get("error_description") or data.get("error") or data.get("message")
        raise OAuthError(
            f"{provider.display_name} token exchange failed: {detail or response.status_code}",
            code="oauth_exchange_failed",
            status=400 if response.status_code < 500 else 502,
        )
    return _parse_token_response(provider, data)


def safe_redirect_target(url: str, allowed_prefixes: tuple[str, ...]) -> str:
    """Reject open-redirect targets after the callback."""
    for prefix in allowed_prefixes:
        if url.startswith(prefix):
            return url
    raise OAuthError("redirect target is not allowed", code="redirect_not_allowed", status=400)
