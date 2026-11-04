"""Pulling real post metrics from platform APIs into ``analytics_snapshots``.

Honesty contract:

* A snapshot row is written **only** when the platform returned at least one
  measured value for that post. A platform that refuses, times out, or has no
  credential produces an entry in ``unavailable`` — never a zero-filled row.
* ``metrics.measured`` records the exact key/value pairs the platform returned.
  ``metrics.unavailable`` records what it did not return, so the UI can say "not
  reported" instead of drawing a zero.
* ``metrics.derived`` records anything this service computed (engagement totals),
  so a derived number is never mistaken for a reported one.

Every platform call runs through the same vault + transport path as publishing,
which means an unconfigured vault is a refusal here too, not a silently empty
chart.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations.http import PlatformError, PlatformRejected, RetryPolicy
from app.integrations.platforms import AccountRef, build_client
from app.integrations.vault import TokenNotFoundError, VaultError, build_vault
from app.models.social import PublishedPost, SocialAccount, SocialAccountStatus
from app.services.analytics import AnalyticsService

HttpClientFactory = Callable[[], httpx.AsyncClient]

#: Metric keys that map onto dedicated columns.
COLUMN_METRICS = ("impressions", "reach", "clicks", "likes", "comments", "shares", "saves")
#: Engagement is derived, so it is disclosed as derived rather than measured.
ENGAGEMENT_KEYS = (
    "likes",
    "comments",
    "shares",
    "saves",
    "reposts",
    "quotes",
    "bookmarks",
    "reactions",
)


class AnalyticsIngestService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.analytics = AnalyticsService(session)
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                follow_redirects=True, timeout=float(settings.integration_timeout_seconds)
            )
        )

    async def ingest(
        self,
        *,
        brand_profile_id: uuid.UUID,
        platforms: list[str] | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        vault = build_vault(self.session, self.settings)
        if not vault.available:
            return {
                "requested": 0,
                "ingested": 0,
                "skipped": 0,
                "unavailable": [{"platform": "all", "reason": vault.reason}],
                "detail": "Analytics ingestion is disabled: no credential vault.",
            }

        since = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(PublishedPost, SocialAccount)
            .join(SocialAccount, PublishedPost.social_account_id == SocialAccount.id)
            .where(
                SocialAccount.brand_profile_id == brand_profile_id,
                PublishedPost.published_at.is_not(None),
                PublishedPost.published_at >= since,
            )
        )
        if platforms:
            stmt = stmt.where(PublishedPost.platform.in_(platforms))
        rows = list((await self.session.execute(stmt)).all())
        if not rows:
            return {
                "requested": 0,
                "ingested": 0,
                "skipped": 0,
                "unavailable": [],
                "detail": "No published posts in the window; there was nothing to measure.",
            }

        unavailable: list[dict[str, str]] = []
        ingested = 0
        skipped = 0
        policy = RetryPolicy(
            max_attempts=max(1, int(self.settings.integration_max_retries)),
            timeout_seconds=float(self.settings.integration_timeout_seconds),
        )

        client = self._client_factory()
        tokens: dict[str, Any] = {}
        try:
            for post, account in rows:
                if account.status != SocialAccountStatus.CONNECTED.value or not account.token_ref:
                    skipped += 1
                    unavailable.append(
                        {
                            "platform": post.platform,
                            "reason": f"account is {account.status}; no live credential",
                        }
                    )
                    continue
                try:
                    if account.token_ref not in tokens:
                        tokens[account.token_ref] = await vault.load(account.token_ref)
                    token = tokens[account.token_ref]
                except (TokenNotFoundError, VaultError) as exc:
                    skipped += 1
                    unavailable.append(
                        {"platform": post.platform, "reason": f"credential unavailable: {exc}"}
                    )
                    continue

                if not post.external_post_id:
                    skipped += 1
                    unavailable.append(
                        {
                            "platform": post.platform,
                            "reason": "published post has no external id to measure",
                        }
                    )
                    continue

                transport = build_client(post.platform, self.settings, client, policy=policy)
                ref = AccountRef(
                    platform=post.platform,
                    external_account_id=account.external_account_id or account.handle,
                    handle=account.handle,
                    settings=dict(account.capabilities or {}),
                )
                try:
                    metrics = await transport.fetch_metrics(
                        post.external_post_id, account=ref, token=token
                    )
                except PlatformRejected as exc:
                    skipped += 1
                    unavailable.append({"platform": post.platform, "reason": str(exc)})
                    continue
                except PlatformError as exc:
                    skipped += 1
                    unavailable.append(
                        {"platform": post.platform, "reason": f"{exc.code}: {exc}"}
                    )
                    continue

                measured = dict(metrics.measured)
                if not measured:
                    skipped += 1
                    unavailable.append(
                        {
                            "platform": post.platform,
                            "reason": "the platform returned no measurable fields",
                        }
                    )
                    continue

                columns = {
                    name: int(measured.get(name, 0)) for name in COLUMN_METRICS
                }
                derived = {
                    "engagements": sum(
                        int(measured.get(key, 0)) for key in ENGAGEMENT_KEYS
                    )
                }
                await self.analytics.record_snapshot(
                    brand_profile_id=brand_profile_id,
                    platform=post.platform,
                    published_post_id=post.id,
                    impressions=columns["impressions"],
                    reach=columns["reach"],
                    engagements=derived["engagements"],
                    clicks=columns["clicks"],
                    likes=columns["likes"],
                    comments=columns["comments"],
                    shares=columns["shares"],
                    saves=columns["saves"],
                    metrics={
                        "measured": measured,
                        "unavailable": list(metrics.unavailable),
                        "derived": derived,
                        "external_post_id": post.external_post_id,
                    },
                    source=f"platform_api:{metrics.source}",
                )
                ingested += 1
        finally:
            await client.aclose()

        return {
            "requested": len(rows),
            "ingested": ingested,
            "skipped": skipped,
            "unavailable": unavailable,
            "detail": (
                f"{ingested} snapshot(s) written from platform APIs, {skipped} skipped. "
                "Only metrics the platform actually reported were stored."
            ),
        }
