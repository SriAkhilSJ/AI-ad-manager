"""Paid advertising transports.

This build implements the **governance** for paid campaigns (proposal →
founder authorisation → single-use consumption → append-only ledger) and reports
the external transports honestly as unavailable.

Why not implement them half-way: an ads API call spends money. A transport that
guesses an endpoint or a field name would fail *somewhere*, and "somewhere" in an
ads API is a campaign that is live with the wrong budget. The Marketing APIs also
require an approved app (Meta) or a developer token with a spending account
(Google Ads) — neither of which can be verified from this repository.

So each transport declares what it is and why it cannot run. The service layer
checks availability *before* consuming an authorisation, which means wiring one of
these up later is an additive change that immediately works through the existing
gate rather than a rewrite of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.models.ads import AdPlatform


@dataclass(frozen=True)
class AdsTransportDescriptor:
    platform: str
    implemented: bool
    credentials_configured: bool
    reason: str
    docs_url: str


class UnavailableAdsTransport:
    """A declared-but-inert transport. Every execution is refused."""

    available = False

    def __init__(self, descriptor: AdsTransportDescriptor) -> None:
        self.descriptor = descriptor
        self.reason = descriptor.reason

    def describe(self) -> dict[str, Any]:
        return {
            "platform": self.descriptor.platform,
            "implemented": self.descriptor.implemented,
            "credentials_configured": self.descriptor.credentials_configured,
            "available": False,
            "reason": self.descriptor.reason,
            "docs_url": self.descriptor.docs_url,
        }

    async def execute(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"verified": False, "reason": self.reason}


#: Documented endpoints, kept so the next engineer does not have to rediscover
#: them, and so the reason string can say precisely what is missing.
_DESCRIPTORS: dict[str, AdsTransportDescriptor] = {
    AdPlatform.META_ADS.value: AdsTransportDescriptor(
        platform=AdPlatform.META_ADS.value,
        implemented=False,
        credentials_configured=False,
        reason=(
            "Meta Marketing API transport is not implemented in this build. It also "
            "requires an app with ads_management approved and an ad account with a "
            "payment method; neither can be verified from this repository."
        ),
        docs_url="https://developers.facebook.com/docs/marketing-apis",
    ),
    AdPlatform.GOOGLE_ADS.value: AdsTransportDescriptor(
        platform=AdPlatform.GOOGLE_ADS.value,
        implemented=False,
        credentials_configured=False,
        reason=(
            "Google Ads API transport is not implemented in this build. It requires an "
            "approved developer token, an OAuth client with the adwords scope, and a "
            "customer id with billing enabled."
        ),
        docs_url="https://developers.google.com/google-ads/api/docs/start",
    ),
    AdPlatform.LINKEDIN_ADS.value: AdsTransportDescriptor(
        platform=AdPlatform.LINKEDIN_ADS.value,
        implemented=False,
        credentials_configured=False,
        reason=(
            "LinkedIn Marketing API transport is not implemented in this build. It "
            "requires the LinkedIn Advertising API access form to be approved for the "
            "app."
        ),
        docs_url="https://learn.microsoft.com/en-us/linkedin/marketing/",
    ),
}


def _credentials_configured(platform: str, settings: Settings) -> bool:
    if platform == AdPlatform.META_ADS.value:
        return bool(settings.meta_app_id and settings.meta_app_secret)
    if platform == AdPlatform.GOOGLE_ADS.value:
        return bool(settings.google_client_id and settings.google_client_secret)
    if platform == AdPlatform.LINKEDIN_ADS.value:
        return bool(settings.linkedin_client_id and settings.linkedin_client_secret)
    return False


def get_ads_transport(platform: str, settings: Settings) -> UnavailableAdsTransport:
    descriptor = _DESCRIPTORS.get(platform)
    if descriptor is None:
        raise ValueError(f"unsupported ads platform {platform!r}")
    configured = _credentials_configured(platform, settings)
    reason = descriptor.reason
    if not configured:
        reason = "application credentials for this ads platform are not configured. " + reason
    return UnavailableAdsTransport(
        AdsTransportDescriptor(
            platform=descriptor.platform,
            implemented=descriptor.implemented,
            credentials_configured=configured,
            reason=reason,
            docs_url=descriptor.docs_url,
        )
    )


def ads_transport_report(settings: Settings) -> list[dict[str, Any]]:
    return [get_ads_transport(p.value, settings).describe() for p in AdPlatform]
