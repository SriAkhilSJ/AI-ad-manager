"""Platform publishing adapters (Phase 1: interfaces + explicit availability).

No network client is implemented here. Each platform reports whether it is
*configured* (application credentials present) and whether a *live* transport
exists. In Phase 1 every adapter is ``transport_available = False``, which is
surfaced honestly through ``/api/v1/system/status`` and blocks publishing with a
clear ``publishing_disabled`` error instead of pretending to post.

This is deliberate: a fake "publish" that writes ``PUBLISHED`` without contacting
a network would be a security and integrity failure, not a feature.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import Settings
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

    def __init__(self, platform: Platform, credentials_configured: bool = False) -> None:
        self.platform = platform
        self.credentials_configured = credentials_configured

    @property
    def reason(self) -> str:
        if self.credentials_configured:
            return (
                f"{self.platform.value}: credentials present but no live transport is "
                "implemented in Phase 1."
            )
        return (
            f"{self.platform.value}: application credentials are not configured. "
            "Set the platform app id/secret to enable."
        )

    async def publish(self, payload: PublishPayload, account: Any) -> PublishResult:
        from app.core.errors import PublishingDisabledError

        raise PublishingDisabledError(self.reason, details={"platform": self.platform.value})


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
    def __init__(self, settings: Settings) -> None:
        creds = _credential_map(settings)
        self._adapters: dict[Platform, UnavailableAdapter] = {
            p: UnavailableAdapter(p, credentials_configured=creds[p]) for p in Platform
        }

    def get(self, platform: str) -> UnavailableAdapter:
        return self._adapters[Platform(platform)]

    def availability(self) -> list[dict[str, Any]]:
        return [
            {
                "platform": platform.value,
                "credentials_configured": adapter.credentials_configured,
                "transport_available": adapter.transport_available,
                "publish_enabled": adapter.credentials_configured
                and adapter.transport_available,
                "reason": adapter.reason,
            }
            for platform, adapter in self._adapters.items()
        ]

    @property
    def any_publish_enabled(self) -> bool:
        return any(
            a.credentials_configured and a.transport_available
            for a in self._adapters.values()
        )


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
