"""Paid-advertising transports: Meta Marketing API, Google Ads API, LinkedIn Ads.

WHAT IS AND IS NOT CLAIMED HERE
-------------------------------
* The request shapes, endpoints, headers and verification reads follow each
  provider's published REST documentation, pinned to an explicit API version.
* **Nothing in this module has been executed against a live ad account.** Doing so
  requires an approved app, a funded ad account and (for Google) a developer
  token. Every transport therefore reports ``verification = "unverified"`` and the
  capability endpoints say so. Mocked tests prove the wiring, not the platform.
* Every mutation is preceded by a founder authorisation, and every transport
  refuses to run without credentials rather than half-working.

SAFETY RULES ENCODED HERE
-------------------------
1. **Create calls are never retried.** A retried create can duplicate a campaign
   and spend twice.
2. **Everything is created paused.** Meta campaigns and adsets are created with
   ``status=PAUSED``; Google campaigns with ``PAUSED``; LinkedIn campaigns with
   ``status=DRAFT``/``PAUSED``. Enabling delivery is a separate, authorised
   operation. This means a bug cannot start spending.
3. **Verification is mandatory.** After a create we read the object back and
   return its remote status; a create whose read-back fails returns
   ``verified: False`` and the service refuses to mark the campaign live.
4. **Dry-run is first-class.** ``preview()`` produces the exact request bodies
   (with secrets stripped) without sending anything, so a founder can see what
   would be created before authorising it.
5. **Money is in minor units.** Budgets are converted once, explicitly, at the
   transport boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.models.ads import AdPlatform

META_API_VERSION = "v21.0"
GOOGLE_ADS_API_VERSION = "v17"
LINKEDIN_API_VERSION = "202405"


class AdsTransportError(Exception):
    """Base class for ads transport failures."""

    def __init__(self, message: str, *, code: str = "ads_transport_error", retryable: bool = False,
                 payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.payload = payload or {}


class AdsNotConfigured(AdsTransportError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="ads_not_configured")


class AdsAccountInvalid(AdsTransportError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="ads_account_invalid", payload=payload)


class AdsRequestRejected(AdsTransportError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="ads_request_rejected", payload=payload)


@dataclass
class ExecutionResult:
    verified: bool
    external_campaign_id: str | None = None
    status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "external_campaign_id": self.external_campaign_id,
            "status": self.status,
            "details": self.details,
            "reason": self.reason,
        }


@dataclass
class AdsCredentials:
    """Everything a transport needs, resolved from settings.

    Each platform reads *only* its own keys — there is no shared ads credential.
    """

    meta_access_token: str | None = None
    meta_ad_account_id: str | None = None
    google_access_token: str | None = None
    google_developer_token: str | None = None
    google_login_customer_id: str | None = None
    linkedin_access_token: str | None = None
    linkedin_account_urn: str | None = None


def resolve_credentials(settings: Settings) -> AdsCredentials:
    return AdsCredentials(
        meta_access_token=settings.meta_ads_access_token,
        meta_ad_account_id=settings.meta_ads_account_id,
        google_access_token=settings.google_ads_access_token,
        google_developer_token=settings.google_ads_developer_token,
        google_login_customer_id=settings.google_ads_login_customer_id,
        linkedin_access_token=settings.linkedin_ads_access_token,
        linkedin_account_urn=settings.linkedin_ads_account_urn,
    )


class AdsTransport(Protocol):
    platform: str
    available: bool
    reason: str

    def describe(self) -> dict[str, Any]: ...
    def preview(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def validate_account(self) -> dict[str, Any]: ...
    async def execute(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> ExecutionResult: ...
    async def report(self, *, external_campaign_id: str) -> dict[str, Any]: ...


class BaseAdsTransport:
    platform: str = ""
    docs_url: str = ""
    implemented = True
    verification = "unverified"

    def __init__(
        self,
        settings: Settings,
        credentials: AdsCredentials,
        client: httpx.AsyncClient,
        *,
        timeout: float | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.client = client
        self.timeout = timeout or float(settings.integration_timeout_seconds)

    # ------------------------------------------------------------- reporting
    def describe(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "implemented": self.implemented,
            "credentials_configured": self.available,
            "available": self.available,
            "verification": self.verification,
            "reason": self.reason,
            "docs_url": self.docs_url,
            "api_version": self.api_version,
        }

    @property
    def api_version(self) -> str:  # pragma: no cover - overridden
        return ""

    @property
    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    @property
    def reason(self) -> str:  # pragma: no cover - overridden
        return "not configured"

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _minor_units_to_currency(amount: int, currency: str) -> str:
        """Budgets are stored in minor units; providers want major units."""
        if amount <= 0:
            raise AdsRequestRejected("A paid campaign needs a positive budget before it can run.")
        return f"{amount / 100:.2f}"

    @staticmethod
    def _redact(body: Any) -> Any:
        """Strip anything token-shaped before a preview is shown or logged."""
        if isinstance(body, dict):
            return {
                key: (
                    "***"
                    if key.lower() in {"access_token", "developer-token", "authorization", "token"}
                    else BaseAdsTransport._redact(value)
                )
                for key, value in body.items()
            }
        if isinstance(body, list):
            return [BaseAdsTransport._redact(item) for item in body]
        return body

    async def _request(
        self,
        method: str,
        url: str,
        *,
        idempotent: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = await self.client.request(method, url, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            detail = ""
            try:
                data = response.json()
                detail = json.dumps(data)[:600]
            except ValueError:
                detail = response.text[:200]
            raise AdsRequestRejected(
                f"{self.platform} {method} {url} returned {response.status_code}: {detail}",
                payload={"status": response.status_code},
            )
        # LinkedIn returns created ids in an ``x-restli-id`` header rather than
        # the body, so the headers travel with the decoded payload.
        header_map = {k.lower(): v for k, v in response.headers.items()}
        if not response.content:
            return {"_headers": header_map}
        try:
            data = response.json()
        except ValueError:
            return {"_headers": header_map}
        payload = data if isinstance(data, dict) else {"data": data}
        payload.setdefault("_headers", header_map)
        return payload


# ------------------------------------------------------------------------- Meta
class MetaAdsTransport(BaseAdsTransport):
    """Meta Marketing API (Graph v21.0)."""

    platform = AdPlatform.META_ADS.value
    docs_url = "https://developers.facebook.com/docs/marketing-apis"
    api_version = META_API_VERSION

    @property
    def base(self) -> str:
        return f"https://graph.facebook.com/{META_API_VERSION}"

    @property
    def available(self) -> bool:
        return bool(self.credentials.meta_access_token and self.credentials.meta_ad_account_id)

    @property
    def reason(self) -> str:
        if self.available:
            return "Meta Marketing API credentials configured (unverified against a live ad account)"
        return (
            "Meta Marketing API needs META_ADS_ACCESS_TOKEN and META_ADS_ACCOUNT_ID; "
            "POST /ads/transports reports this as unavailable until both exist."
        )

    @property
    def _headers(self) -> dict[str, str]:
        # Bearer auth keeps the token out of URLs, so a transport error can never
        # echo it into a log line.
        return {"Authorization": f"Bearer {self.credentials.meta_access_token}"}

    def _act(self) -> str:
        account = self.credentials.meta_ad_account_id or ""
        return account if account.startswith("act_") else f"act_{account}"

    def preview(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        campaign_body = {
            "name": campaign.name,
            "objective": (campaign.objective or "OUTCOME_TRAFFIC").upper(),
            # Created paused: a bug here cannot start spending.
            "status": "PAUSED",
            "special_ad_categories": params.get("special_ad_categories", []),
        }
        if campaign.daily_budget_cents:
            campaign_body["daily_budget"] = int(campaign.daily_budget_cents)
        adset_body = {
            "name": f"{campaign.name} — ad set",
            "campaign_id": "<created campaign id>",
            "daily_budget": int(campaign.daily_budget_cents or campaign.total_budget_cents),
            "billing_event": params.get("billing_event", "IMPRESSIONS"),
            "optimization_goal": params.get("optimization_goal", "LINK_CLICKS"),
            "bid_strategy": params.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP"),
            "targeting": params.get("targeting") or (campaign.targeting or {}),
            "status": "PAUSED",
        }
        creative_body = {
            "name": f"{campaign.name} — creative",
            "object_story_spec": {
                "page_id": params.get("page_id", "<page id>"),
                "link_data": {
                    "message": params.get("primary_text", ""),
                    "link": params.get("link_url", ""),
                    "name": params.get("headline", ""),
                },
            },
        }
        ad_body = {
            "name": f"{campaign.name} — ad",
            "adset_id": "<created ad set id>",
            "creative": {"creative_id": "<created creative id>"},
            "status": "PAUSED",
        }
        return {
            "platform": self.platform,
            "kind": kind,
            "endpoint": f"POST {self.base}/{self._act()}/campaigns",
            "requests": self._redact(
                [
                    {"step": "campaign", "body": campaign_body},
                    {"step": "adset", "body": adset_body},
                    {"step": "creative", "body": creative_body},
                    {"step": "ad", "body": ad_body},
                ]
            ),
            "estimated_daily_spend": self._minor_units_to_currency(
                int(campaign.daily_budget_cents or campaign.total_budget_cents), campaign.currency
            ),
            "currency": campaign.currency,
            "created_paused": True,
            "note": "Campaign, ad set and ad are all created with status=PAUSED.",
        }

    async def validate_account(self) -> dict[str, Any]:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        data = await self._request(
            "GET",
            f"{self.base}/{self._act()}",
            params={
                "fields": "id,name,account_status,currency,timezone_name,disable_reason",
            },
            headers=self._headers,
        )
        if not data.get("id"):
            raise AdsAccountInvalid("Meta returned no account for this token", payload=data)
        return {
            "platform": self.platform,
            "external_account_id": str(data.get("id")),
            "name": data.get("name", ""),
            "currency": data.get("currency", ""),
            "account_status": data.get("account_status"),
            "timezone": data.get("timezone_name", ""),
            "usable": int(data.get("account_status") or 0) == 1,
        }

    async def execute(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> ExecutionResult:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        preview = self.preview(campaign=campaign, kind=kind, params=params)
        account = self._act()

        if kind in {"pause", "resume"}:
            if not campaign.external_campaign_id:
                raise AdsRequestRejected("No external campaign id to pause or resume.")
            status = "PAUSED" if kind == "pause" else "ACTIVE"
            await self._request(
                "POST",
                f"{self.base}/{campaign.external_campaign_id}",
                data={"status": status},
                headers=self._headers,
                idempotent=True,
            )
            verified = await self._verify(campaign.external_campaign_id, expected=status)
            return ExecutionResult(
                verified=verified.get("matched", False),
                external_campaign_id=campaign.external_campaign_id,
                status=verified.get("status"),
                details={"preview": preview, "readback": verified},
                reason="" if verified.get("matched") else "status did not match after the update",
            )

        if kind == "budget_change":
            if not campaign.external_campaign_id:
                raise AdsRequestRejected("No external campaign id to change the budget on.")
            budget = int(params.get("daily_budget_cents") or campaign.daily_budget_cents)
            await self._request(
                "POST",
                f"{self.base}/{campaign.external_campaign_id}",
                data={"daily_budget": budget},
                headers=self._headers,
                idempotent=True,
            )
            verified = await self._verify(campaign.external_campaign_id)
            return ExecutionResult(
                verified=bool(verified.get("status")),
                external_campaign_id=campaign.external_campaign_id,
                status=verified.get("status"),
                details={"preview": preview, "readback": verified},
                reason="" if verified.get("status") else "could not read the campaign back",
            )

        # create / launch: build paused, verify, then (launch only) enable.
        created = await self._request(
            "POST",
            f"{self.base}/{account}/campaigns",
            data={**preview["requests"][0]["body"]},
            headers=self._headers,
        )
        campaign_id = created.get("id")
        if not campaign_id:
            raise AdsRequestRejected("Meta created no campaign id", payload=created)

        adset = await self._request(
            "POST",
            f"{self.base}/{account}/adsets",
            data={
                **preview["requests"][1]["body"],
                "campaign_id": campaign_id,
            },
            headers=self._headers,
        )
        creative_id = None
        if params.get("page_id") and params.get("link_url"):
            creative = await self._request(
                "POST",
                f"{self.base}/{account}/adcreatives",
                data={**preview["requests"][2]["body"]},
                headers=self._headers,
            )
            creative_id = creative.get("id")
            if creative_id and adset.get("id"):
                await self._request(
                    "POST",
                    f"{self.base}/{account}/ads",
                    data={
                        **preview["requests"][3]["body"],
                        "adset_id": adset.get("id"),
                        "creative": json.dumps({"creative_id": creative_id}),
                    },
                    headers=self._headers,
                )

        verified = await self._verify(str(campaign_id), expected="PAUSED")
        details = {
            "preview": preview,
            "readback": verified,
            "adset_id": adset.get("id"),
            "creative_id": creative_id,
            "created_paused": True,
        }
        if kind == "launch" and verified.get("matched"):
            # Activation is a distinct, separately authorised step.
            await self._request(
                "POST",
                f"{self.base}/{campaign_id}",
                data={"status": "ACTIVE"},
                headers=self._headers,
                idempotent=True,
            )
            verified = await self._verify(str(campaign_id), expected="ACTIVE")
            details["readback"] = verified

        return ExecutionResult(
            verified=bool(verified.get("matched")),
            external_campaign_id=str(campaign_id),
            status=verified.get("status"),
            details=details,
            reason="" if verified.get("matched") else "campaign could not be read back",
        )

    async def _verify(self, campaign_id: str, *, expected: str | None = None) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"{self.base}/{campaign_id}",
            params={
                "fields": "id,name,status,effective_status,daily_budget",
            },
            headers=self._headers,
            idempotent=True,
        )
        status = str(data.get("effective_status") or data.get("status") or "")
        return {
            "id": data.get("id"),
            "status": status,
            "matched": (expected is None and bool(status)) or status.upper() == (expected or "").upper(),
            "raw": self._redact(data),
        }

    async def report(self, *, external_campaign_id: str) -> dict[str, Any]:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        data = await self._request(
            "GET",
            f"{self.base}/{external_campaign_id}/insights",
            params={
                "fields": "campaign_name,impressions,clicks,spend,ctr,cpc,actions",
            },
            headers=self._headers,
            idempotent=True,
        )
        rows = data.get("data") or []
        return {
            "platform": self.platform,
            "source": "meta_marketing_api_insights",
            "measured": rows[0] if rows else {},
            "available": bool(rows),
            "note": "Values are whatever the Marketing API returned; nothing is estimated.",
        }


# ----------------------------------------------------------------------- Google
class GoogleAdsTransport(BaseAdsTransport):
    """Google Ads API (REST v17)."""

    platform = AdPlatform.GOOGLE_ADS.value
    docs_url = "https://developers.google.com/google-ads/api/docs/start"
    api_version = GOOGLE_ADS_API_VERSION

    @property
    def base(self) -> str:
        return f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"

    @property
    def available(self) -> bool:
        return bool(
            self.credentials.google_access_token
            and self.credentials.google_developer_token
            and self.credentials.google_login_customer_id
        )

    @property
    def reason(self) -> str:
        if self.available:
            return "Google Ads credentials configured (unverified against a live customer id)"
        return (
            "Google Ads needs GOOGLE_ADS_ACCESS_TOKEN, GOOGLE_ADS_DEVELOPER_TOKEN and "
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID (the customer id with billing). Without all three "
            "the transport reports unavailable."
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.google_access_token}",
            "developer-token": self.credentials.google_developer_token or "",
            "login-customer-id": (self.credentials.google_login_customer_id or "").replace("-", ""),
            "Content-Type": "application/json",
        }

    def _customer(self) -> str:
        return f"customers/{(self.credentials.google_login_customer_id or '').replace('-', '')}"

    def preview(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        budget_micros = int(campaign.daily_budget_cents or campaign.total_budget_cents) * 10_000
        budget_body = {
            "name": f"{campaign.name} budget",
            "amountMicros": str(budget_micros),
            "deliveryMethod": "STANDARD",
        }
        campaign_body = {
            "name": campaign.name,
            "status": "PAUSED",
            "advertisingChannelType": params.get("channel", "SEARCH"),
            "campaignBudget": f"{self._customer()}/campaignBudgets/-1",
            "manualCpc": {},
            "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False},
        }
        return {
            "platform": self.platform,
            "kind": kind,
            "endpoint": f"POST {self.base}/{self._customer()}/campaignBudgets:mutate",
            "requests": self._redact(
                [
                    {"step": "campaign_budget", "body": {"operations": [{"create": budget_body}]}},
                    {"step": "campaign", "body": {"operations": [{"create": campaign_body}]}},
                ]
            ),
            "estimated_daily_spend": self._minor_units_to_currency(
                int(campaign.daily_budget_cents or campaign.total_budget_cents), campaign.currency
            ),
            "currency": campaign.currency,
            "created_paused": True,
            "idempotency": "each mutate carries a request-id header, so a retry is de-duplicated by Google",
            "note": "Campaign is created with status=PAUSED; enabling it is a separate operation.",
        }

    async def validate_account(self) -> dict[str, Any]:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        data = await self._request(
            "POST",
            f"{self.base}/{self._customer()}/googleAds:search",
            headers=self._headers,
            json={
                "query": "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
                "customer.status, customer.time_zone FROM customer LIMIT 1"
            },
            idempotent=True,
        )
        results = data.get("results") or []
        if not results:
            raise AdsAccountInvalid("Google Ads returned no customer for this token", payload=data)
        customer = results[0].get("customer", {})
        return {
            "platform": self.platform,
            "external_account_id": str(customer.get("id", "")),
            "name": customer.get("descriptiveName", ""),
            "currency": customer.get("currencyCode", ""),
            "account_status": customer.get("status"),
            "timezone": customer.get("timeZone", ""),
            "usable": str(customer.get("status", "")).upper() in {"ENABLED", "2"},
        }

    async def execute(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> ExecutionResult:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        preview = self.preview(campaign=campaign, kind=kind, params=params)
        request_id = str(uuid.uuid4())

        if kind in {"pause", "resume"}:
            if not campaign.external_campaign_id:
                raise AdsRequestRejected("No external campaign resource name to update.")
            status = "PAUSED" if kind == "pause" else "ENABLED"
            await self._mutate_campaign_status(campaign.external_campaign_id, status, request_id)
            verified = await self._verify(campaign.external_campaign_id, expected=status)
            return ExecutionResult(
                verified=bool(verified.get("matched")),
                external_campaign_id=campaign.external_campaign_id,
                status=verified.get("status"),
                details={"preview": preview, "readback": verified, "request_id": request_id},
                reason="" if verified.get("matched") else "status did not match after the update",
            )

        if kind == "budget_change":
            if not campaign.external_campaign_id:
                raise AdsRequestRejected("No external campaign resource name to re-budget.")
            await self._request(
                "POST",
                f"{self.base}/{self._customer()}/campaignBudgets:mutate",
                headers={**self._headers, "request-id": request_id},
                json={
                    "operations": [
                        {
                            "update": {
                                "resourceName": params.get("budget_resource_name", ""),
                                "amountMicros": str(int(params.get("daily_budget_cents", campaign.daily_budget_cents)) * 10_000),
                            },
                            "updateMask": "amount_micros",
                        }
                    ]
                },
                idempotent=True,
            )
            verified = await self._verify(campaign.external_campaign_id)
            return ExecutionResult(
                verified=bool(verified.get("status")),
                external_campaign_id=campaign.external_campaign_id,
                status=verified.get("status"),
                details={"preview": preview, "readback": verified, "request_id": request_id},
                reason="" if verified.get("status") else "campaign could not be read back",
            )

        budget = await self._request(
            "POST",
            f"{self.base}/{self._customer()}/campaignBudgets:mutate",
            headers={**self._headers, "request-id": f"{request_id}-budget"},
            json={"operations": [{"create": preview["requests"][0]["body"]["operations"][0]["create"]}]},
        )
        budget_name = (budget.get("results") or [{}])[0].get("resourceName", "")
        if not budget_name:
            raise AdsRequestRejected("Google Ads created no budget resource", payload=budget)

        created = await self._request(
            "POST",
            f"{self.base}/{self._customer()}/campaigns:mutate",
            headers={**self._headers, "request-id": f"{request_id}-campaign"},
            json={
                "operations": [
                    {
                        "create": {
                            **preview["requests"][1]["body"]["operations"][0]["create"],
                            "campaignBudget": budget_name,
                        }
                    }
                ]
            },
        )
        resource_name = (created.get("results") or [{}])[0].get("resourceName", "")
        if not resource_name:
            raise AdsRequestRejected("Google Ads created no campaign resource", payload=created)

        verified = await self._verify(resource_name, expected="PAUSED")
        details = {
            "preview": preview,
            "readback": verified,
            "budget_resource_name": budget_name,
            "request_id": request_id,
        }
        if kind == "launch" and verified.get("matched"):
            await self._mutate_campaign_status(resource_name, "ENABLED", f"{request_id}-enable")
            verified = await self._verify(resource_name, expected="ENABLED")
            details["readback"] = verified

        return ExecutionResult(
            verified=bool(verified.get("matched")),
            external_campaign_id=resource_name,
            status=verified.get("status"),
            details=details,
            reason="" if verified.get("matched") else "campaign could not be read back",
        )

    async def _mutate_campaign_status(self, resource_name: str, status: str, request_id: str) -> None:
        await self._request(
            "POST",
            f"{self.base}/{self._customer()}/campaigns:mutate",
            headers={**self._headers, "request-id": request_id},
            json={
                "operations": [
                    {"update": {"resourceName": resource_name, "status": status},
                     "updateMask": "status"}
                ]
            },
            idempotent=True,
        )

    async def _verify(self, resource_name: str, *, expected: str | None = None) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"{self.base}/{self._customer()}/googleAds:search",
            headers=self._headers,
            json={"query": f"SELECT campaign.id, campaign.name, campaign.status FROM campaign "
                           f"WHERE campaign.resource_name = '{resource_name}' LIMIT 1"},
            idempotent=True,
        )
        results = data.get("results") or []
        result = (results[0].get("campaign") if results else {}) or {}
        status = str(result.get("status", ""))
        return {
            "id": result.get("id"),
            "status": status,
            "matched": (expected is None and bool(status)) or status.upper() == (expected or "").upper(),
        }

    async def report(self, *, external_campaign_id: str) -> dict[str, Any]:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        data = await self._request(
            "POST",
            f"{self.base}/{self._customer()}/googleAds:search",
            headers=self._headers,
            json={
                "query": "SELECT campaign.id, metrics.impressions, metrics.clicks, "
                "metrics.cost_micros, metrics.ctr FROM campaign "
                f"WHERE campaign.resource_name = '{external_campaign_id}'"
            },
            idempotent=True,
        )
        rows = data.get("results") or []
        metrics = (rows[0].get("metrics") if rows else {}) or {}
        return {
            "platform": self.platform,
            "source": "google_ads_search_stream",
            "measured": metrics,
            "available": bool(rows),
            "note": "cost_micros is returned raw; divide by 1,000,000 for the account currency.",
        }


# --------------------------------------------------------------------- LinkedIn
class LinkedInAdsTransport(BaseAdsTransport):
    """LinkedIn Marketing API (versioned REST)."""

    platform = AdPlatform.LINKEDIN_ADS.value
    docs_url = "https://learn.microsoft.com/en-us/linkedin/marketing/"
    api_version = LINKEDIN_API_VERSION

    @property
    def base(self) -> str:
        return "https://api.linkedin.com/rest"

    @property
    def available(self) -> bool:
        return bool(self.credentials.linkedin_access_token and self.credentials.linkedin_account_urn)

    @property
    def reason(self) -> str:
        if self.available:
            return "LinkedIn Ads credentials configured (unverified against a live ad account)"
        return (
            "LinkedIn Ads needs LINKEDIN_ADS_ACCESS_TOKEN and LINKEDIN_ADS_ACCOUNT_URN, plus "
            "an app approved for the Advertising API. Without them the transport is unavailable."
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.linkedin_access_token}",
            "LinkedIn-Version": LINKEDIN_API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    def preview(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        group_body = {
            "account": self.credentials.linkedin_account_urn,
            "name": f"{campaign.name} group",
            "status": "DRAFT",
            "runSchedule": {"start": int(params.get("start_epoch", 0))},
            "objectiveType": (campaign.objective or "BRAND_AWARENESS").upper(),
        }
        campaign_body = {
            "account": self.credentials.linkedin_account_urn,
            "name": campaign.name,
            "status": "DRAFT",
            "type": "SPONSORED_UPDATES",
            "costType": params.get("cost_type", "CPM"),
            "dailyBudget": {
                "amount": self._minor_units_to_currency(
                    int(campaign.daily_budget_cents or campaign.total_budget_cents), campaign.currency
                ),
                "currencyCode": campaign.currency,
            },
            "unitCost": {"amount": params.get("unit_cost", "10.00"), "currencyCode": campaign.currency},
            "targetingCriteria": (params.get("targeting") or campaign.targeting or {}),
        }
        return {
            "platform": self.platform,
            "kind": kind,
            "endpoint": f"POST {self.base}/adCampaignGroups",
            "requests": self._redact(
                [
                    {"step": "campaign_group", "body": group_body},
                    {"step": "campaign", "body": campaign_body},
                ]
            ),
            "estimated_daily_spend": self._minor_units_to_currency(
                int(campaign.daily_budget_cents or campaign.total_budget_cents), campaign.currency
            ),
            "currency": campaign.currency,
            "created_paused": True,
            "note": "Campaign groups and campaigns are created as DRAFT.",
        }

    async def validate_account(self) -> dict[str, Any]:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        urn = (self.credentials.linkedin_account_urn or "").split(":")[-1]
        data = await self._request(
            "GET",
            f"{self.base}/adAccounts/{urn}",
            headers=self._headers,
            idempotent=True,
        )
        if not data.get("id"):
            raise AdsAccountInvalid("LinkedIn returned no ad account", payload=data)
        return {
            "platform": self.platform,
            "external_account_id": str(data.get("id")),
            "name": data.get("name", ""),
            "currency": data.get("currency", ""),
            "account_status": data.get("status"),
            "usable": str(data.get("status", "")).upper() in {"ACTIVE", "ACTIVE_DRAFT"},
        }

    async def execute(self, *, campaign: Any, kind: str, params: dict[str, Any]) -> ExecutionResult:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        preview = self.preview(campaign=campaign, kind=kind, params=params)

        if kind in {"pause", "resume"}:
            if not campaign.external_campaign_id:
                raise AdsRequestRejected("No external campaign id to pause or resume.")
            status = "PAUSED" if kind == "pause" else "ACTIVE"
            await self._request(
                "POST",
                f"{self.base}/adCampaigns/{campaign.external_campaign_id}",
                headers={**self._headers, "X-RestLi-Method": "PARTIAL_UPDATE"},
                json={"patch": {"$set": {"status": status}}},
                idempotent=True,
            )
            verified = await self._verify(campaign.external_campaign_id, expected=status)
            return ExecutionResult(
                verified=bool(verified.get("matched")),
                external_campaign_id=campaign.external_campaign_id,
                status=verified.get("status"),
                details={"preview": preview, "readback": verified},
                reason="" if verified.get("matched") else "status did not match after the update",
            )

        if kind == "budget_change":
            if not campaign.external_campaign_id:
                raise AdsRequestRejected("No external campaign id to re-budget.")
            await self._request(
                "POST",
                f"{self.base}/adCampaigns/{campaign.external_campaign_id}",
                headers={**self._headers, "X-RestLi-Method": "PARTIAL_UPDATE"},
                json={
                    "patch": {
                        "$set": {
                            "dailyBudget": {
                                "amount": self._minor_units_to_currency(
                                    int(params.get("daily_budget_cents", campaign.daily_budget_cents)),
                                    campaign.currency,
                                ),
                                "currencyCode": campaign.currency,
                            }
                        }
                    }
                },
                idempotent=True,
            )
            verified = await self._verify(campaign.external_campaign_id)
            return ExecutionResult(
                verified=bool(verified.get("status")),
                external_campaign_id=campaign.external_campaign_id,
                status=verified.get("status"),
                details={"preview": preview, "readback": verified},
                reason="" if verified.get("status") else "campaign could not be read back",
            )

        group = await self._request(
            "POST",
            f"{self.base}/adCampaignGroups",
            headers=self._headers,
            json=preview["requests"][0]["body"],
        )
        group_id = group.get("id") or _header_id(group)
        if not group_id:
            raise AdsRequestRejected("LinkedIn created no campaign group", payload=group)

        created = await self._request(
            "POST",
            f"{self.base}/adCampaigns",
            headers=self._headers,
            json={**preview["requests"][1]["body"], "campaignGroup": f"urn:li:sponsoredCampaignGroup:{group_id}"},
        )
        campaign_id = created.get("id") or _header_id(created)
        if not campaign_id:
            raise AdsRequestRejected("LinkedIn created no campaign", payload=created)

        resource = f"urn:li:sponsoredCampaign:{campaign_id}"
        verified = await self._verify(str(campaign_id), expected="DRAFT")
        details = {
            "preview": preview,
            "readback": verified,
            "campaign_group_id": str(group_id),
            "created_paused": True,
        }
        if kind == "launch" and verified.get("matched"):
            await self._request(
                "POST",
                f"{self.base}/adCampaigns/{campaign_id}",
                headers={**self._headers, "X-RestLi-Method": "PARTIAL_UPDATE"},
                json={"patch": {"$set": {"status": "ACTIVE"}}},
                idempotent=True,
            )
            verified = await self._verify(str(campaign_id), expected="ACTIVE")
            details["readback"] = verified

        return ExecutionResult(
            verified=bool(verified.get("matched")),
            external_campaign_id=resource,
            status=verified.get("status"),
            details=details,
            reason="" if verified.get("matched") else "campaign could not be read back",
        )

    async def _verify(self, campaign_id: str, *, expected: str | None = None) -> dict[str, Any]:
        numeric = str(campaign_id).split(":")[-1]
        data = await self._request(
            "GET",
            f"{self.base}/adCampaigns/{numeric}",
            headers=self._headers,
            idempotent=True,
        )
        status = str(data.get("status", ""))
        return {
            "id": data.get("id"),
            "status": status,
            "matched": (expected is None and bool(status)) or status.upper() == (expected or "").upper(),
        }

    async def report(self, *, external_campaign_id: str) -> dict[str, Any]:
        if not self.available:
            raise AdsNotConfigured(self.reason)
        numeric = str(external_campaign_id).split(":")[-1]
        data = await self._request(
            "GET",
            f"{self.base}/adAnalytics",
            params={
                "q": "analytics",
                "campaigns": f"List(urn%3Ali%3AsponsoredCampaign%3A{numeric})",
                "dateRange": "(start:(year:2026,month:1,day:1),end:(year:2026,month:12,day:31))",
                "timeGranularity": "ALL",
                "fields": "impressions,clicks,costInLocalCurrency,externalWebsiteConversions",
            },
            headers=self._headers,
            idempotent=True,
        )
        elements = data.get("elements") or []
        return {
            "platform": self.platform,
            "source": "linkedin_ad_analytics",
            "measured": elements[0] if elements else {},
            "available": bool(elements),
            "note": "Values are whatever the Marketing API returned; nothing is estimated.",
        }


def _header_id(payload: dict[str, Any]) -> str | None:
    """LinkedIn returns the created id in an ``x-restli-id`` header, not the body."""
    headers = payload.get("_headers") or {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "x-restli-id":
                return str(value)
    return None


_shared: httpx.AsyncClient | None = None


def _shared_client() -> httpx.AsyncClient:
    """One process-wide client instead of leaking one per transport lookup."""
    global _shared
    if _shared is None or _shared.is_closed:
        _shared = httpx.AsyncClient(follow_redirects=True)
    return _shared


def get_ads_transport(
    platform: str, settings: Settings, *, client: httpx.AsyncClient | None = None
) -> AdsTransport:
    """Resolve one transport. Always returns an object; ``available`` decides use."""
    credentials = resolve_credentials(settings)
    http = client or _shared_client()
    if platform == AdPlatform.META_ADS.value:
        return MetaAdsTransport(settings, credentials, http)
    if platform == AdPlatform.GOOGLE_ADS.value:
        return GoogleAdsTransport(settings, credentials, http)
    if platform == AdPlatform.LINKEDIN_ADS.value:
        return LinkedInAdsTransport(settings, credentials, http)
    raise ValueError(f"unsupported ads platform {platform!r}")


def ads_transport_report(settings: Settings) -> list[dict[str, Any]]:
    return [get_ads_transport(p.value, settings).describe() for p in AdPlatform]


def preview_fingerprint(preview: dict[str, Any]) -> str:
    """Stable hash of a preview, for comparing what was approved to what runs."""
    canonical = json.dumps(preview.get("requests", []), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
