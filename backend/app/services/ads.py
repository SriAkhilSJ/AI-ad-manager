"""Paid advertising governance.

THE INVARIANT
-------------
An external ad mutation happens only when **all** of these hold:

1. the actor is a founder (enforced in the API layer),
2. an ``AdBudgetAuthorization`` is ``active``, unexpired, belongs to this
   campaign, lists this operation, and covers the spend,
3. the campaign's *current* proposal hash equals the hash that was authorised,
4. an ads transport for the platform is actually implemented and configured.

Condition 3 is the one that is easy to get wrong. Approving "the Meta campaign"
and then editing the budget is not an approval of the edited campaign, so the
hash is recomputed from the live row at execution time and compared to the value
frozen into the authorisation. Any edit invalidates it.

Condition 4 is checked **before** the authorisation is consumed, so a launch that
cannot proceed does not burn the founder's approval.

Everything that is attempted — including refusals — is written to
``ad_operations`` with the authorisation reference. The ledger is the evidence
that the gate held, so a refusal is recorded as faithfully as a success.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.config import Settings
from app.core.errors import (
    ApprovalRequiredError,
    IntegrationUnavailableError,
    NotFoundError,
    ValidationProblem,
)
from app.integrations.ads import get_ads_transport
from app.models.ads import (
    AdAuthorizationStatus,
    AdBudgetAuthorization,
    AdCampaign,
    AdCampaignStatus,
    AdOperation,
    AdOperationKind,
    AdPlatform,
)

MAX_AUTHORIZATION_TTL = timedelta(days=7)


def proposal_fingerprint(
    *,
    platform: str,
    name: str,
    objective: str,
    plan: dict[str, Any],
    targeting: dict[str, Any],
    creatives: list[Any],
    daily_budget_cents: int,
    total_budget_cents: int,
    currency: str,
) -> str:
    """Stable hash of everything a founder is being asked to authorise."""
    payload = {
        "platform": platform,
        "name": name,
        "objective": objective,
        "plan": plan,
        "targeting": targeting,
        "creatives": creatives,
        "daily_budget_cents": int(daily_budget_cents),
        "total_budget_cents": int(total_budget_cents),
        "currency": currency.upper(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint(*parts: Any) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AdsService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # ------------------------------------------------------------- proposals
    async def propose(
        self,
        *,
        brand_profile_id: uuid.UUID,
        platform: str,
        name: str,
        objective: str,
        plan: dict[str, Any],
        targeting: dict[str, Any],
        creatives: list[Any],
        daily_budget_cents: int,
        total_budget_cents: int,
        currency: str = "USD",
        campaign_id: uuid.UUID | None = None,
        created_by_agent: str | None = None,
    ) -> AdCampaign:
        try:
            AdPlatform(platform)
        except ValueError as exc:
            raise ValidationProblem(
                f"Unsupported ads platform {platform!r}; expected one of "
                f"{AdPlatform.values()}."
            ) from exc
        if daily_budget_cents < 0 or total_budget_cents < 0:
            raise ValidationProblem("Budgets cannot be negative.")
        if daily_budget_cents and total_budget_cents and daily_budget_cents > total_budget_cents:
            raise ValidationProblem("The daily budget cannot exceed the total budget.")

        digest = proposal_fingerprint(
            platform=platform,
            name=name,
            objective=objective,
            plan=plan,
            targeting=targeting,
            creatives=creatives,
            daily_budget_cents=daily_budget_cents,
            total_budget_cents=total_budget_cents,
            currency=currency,
        )
        campaign = AdCampaign(
            brand_profile_id=brand_profile_id,
            campaign_id=campaign_id,
            platform=platform,
            name=name,
            objective=objective,
            status=AdCampaignStatus.PROPOSED.value,
            plan=plan,
            targeting=targeting,
            creatives=creatives,
            daily_budget_cents=daily_budget_cents,
            total_budget_cents=total_budget_cents,
            currency=currency.upper(),
            proposal_hash=digest,
            created_by_agent=created_by_agent,
        )
        self.session.add(campaign)
        await self.session.flush()
        await record_audit(
            self.session,
            action="ads.proposed",
            entity_type="ad_campaign",
            entity_id=campaign.id,
            after={"proposal_hash": digest, "platform": platform},
        )
        return campaign

    async def get(self, campaign_id: uuid.UUID) -> AdCampaign:
        campaign = await self.session.get(AdCampaign, campaign_id)
        if campaign is None:
            raise NotFoundError(f"Ad campaign {campaign_id} not found.")
        return campaign

    def current_hash(self, campaign: AdCampaign) -> str:
        return proposal_fingerprint(
            platform=campaign.platform,
            name=campaign.name,
            objective=campaign.objective,
            plan=campaign.plan or {},
            targeting=campaign.targeting or {},
            creatives=campaign.creatives or [],
            daily_budget_cents=campaign.daily_budget_cents,
            total_budget_cents=campaign.total_budget_cents,
            currency=campaign.currency,
        )

    # ----------------------------------------------------------- authorisation
    async def authorize(
        self,
        campaign_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        actor_role: str,
        max_spend_cents: int,
        allowed_operations: list[str],
        ttl_hours: int = 24,
        note: str = "",
    ) -> AdBudgetAuthorization:
        if actor_role != "founder":
            raise ApprovalRequiredError(
                "Only a founder may authorise advertising spend.",
                details={"actor_role": actor_role},
            )
        campaign = await self.get(campaign_id)
        if campaign.status not in {
            AdCampaignStatus.PROPOSED.value,
            AdCampaignStatus.AUTHORIZED.value,
        }:
            raise ValidationProblem(
                f"Campaign is '{campaign.status}'; only a proposal can be authorised."
            )
        if max_spend_cents <= 0:
            raise ValidationProblem("An authorisation must cover a positive amount.")
        if max_spend_cents > int(self.settings.ads_max_authorized_spend_cents):
            raise ValidationProblem(
                "The requested ceiling exceeds the configured per-authorisation maximum "
                f"({self.settings.ads_max_authorized_spend_cents} cents)."
            )
        if int(campaign.total_budget_cents) > max_spend_cents:
            raise ValidationProblem(
                "The authorisation does not cover the campaign's total budget.",
                details={
                    "total_budget_cents": campaign.total_budget_cents,
                    "max_spend_cents": max_spend_cents,
                },
            )
        valid_kinds = set(AdOperationKind.values())
        unknown = [op for op in allowed_operations if op not in valid_kinds]
        if unknown:
            raise ValidationProblem(f"Unknown operations in authorisation: {unknown}.")

        digest = self.current_hash(campaign)
        ttl = timedelta(hours=max(1, min(int(ttl_hours), 24 * 7)))
        if ttl > MAX_AUTHORIZATION_TTL:  # pragma: no cover - clamped above
            ttl = MAX_AUTHORIZATION_TTL

        authorization = AdBudgetAuthorization(
            ad_campaign_id=campaign.id,
            proposal_hash=digest,
            max_spend_cents=max_spend_cents,
            allowed_operations=sorted(set(allowed_operations)),
            currency=campaign.currency,
            status=AdAuthorizationStatus.ACTIVE.value,
            expires_at=datetime.now(timezone.utc) + ttl,
            authorized_by=actor_id,
            note=note[:2000],
        )
        self.session.add(authorization)
        campaign.status = AdCampaignStatus.AUTHORIZED.value
        campaign.proposal_hash = digest
        self.session.add(campaign)
        await self.session.flush()
        await record_audit(
            self.session,
            action="ads.authorized",
            entity_type="ad_budget_authorization",
            entity_id=authorization.id,
            actor_id=actor_id,
            after={
                "ad_campaign_id": str(campaign.id),
                "proposal_hash": digest,
                "max_spend_cents": max_spend_cents,
                "allowed_operations": authorization.allowed_operations,
            },
        )
        return authorization

    async def reject(
        self, campaign_id: uuid.UUID, *, actor_id: uuid.UUID, actor_role: str, reason: str = ""
    ) -> AdCampaign:
        if actor_role != "founder":
            raise ApprovalRequiredError("Only a founder may reject an advertising proposal.")
        campaign = await self.get(campaign_id)
        campaign.status = AdCampaignStatus.REJECTED.value
        self.session.add(campaign)
        await self.session.flush()
        await record_audit(
            self.session,
            action="ads.rejected",
            entity_type="ad_campaign",
            entity_id=campaign.id,
            actor_id=actor_id,
            after={"reason": reason[:500]},
        )
        return campaign

    # -------------------------------------------------------------- execution
    async def execute(
        self,
        campaign_id: uuid.UUID,
        *,
        kind: str,
        actor_id: uuid.UUID,
        actor_role: str,
        authorization_id: uuid.UUID | None,
        params: dict[str, Any] | None = None,
    ) -> AdOperation:
        """Attempt one external mutation. Refusals are recorded, not raised, once
        they are governance refusals — the caller sees the operation outcome."""
        campaign = await self.get(campaign_id)
        params = params or {}
        fingerprint = _fingerprint(campaign_id, kind, self.current_hash(campaign), params)

        existing = (
            await self.session.execute(
                select(AdOperation).where(
                    AdOperation.ad_campaign_id == campaign_id,
                    AdOperation.kind == kind,
                    AdOperation.request_fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        def _record(outcome: str, detail: str, auth_id: uuid.UUID | None, evidence: dict[str, Any] | None = None) -> AdOperation:
            op = AdOperation(
                ad_campaign_id=campaign_id,
                authorization_id=auth_id,
                kind=kind,
                outcome=outcome,
                detail=detail[:2000],
                request_fingerprint=fingerprint,
                evidence=evidence or {},
                actor_id=actor_id,
                actor_role=actor_role,
            )
            self.session.add(op)
            return op

        if actor_role != "founder":
            op = _record("refused", "only a founder may mutate a paid campaign", None)
            await self.session.flush()
            return op

        if kind not in set(AdOperationKind.values()):
            raise ValidationProblem(f"Unknown operation {kind!r}.")

        authorization = None
        if authorization_id is not None:
            authorization = await self.session.get(AdBudgetAuthorization, authorization_id)
        if authorization is None:
            op = _record("refused", "no founder authorisation references this operation", None)
            await self.session.flush()
            return op
        if authorization.ad_campaign_id != campaign_id:
            op = _record(
                "refused",
                "the authorisation belongs to a different campaign",
                authorization.id,
            )
            await self.session.flush()
            return op

        now = datetime.now(timezone.utc)
        expires = authorization.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if authorization.status != AdAuthorizationStatus.ACTIVE.value:
            op = _record("refused", f"authorisation is {authorization.status}", authorization.id)
            await self.session.flush()
            return op
        if expires < now:
            authorization.status = AdAuthorizationStatus.EXPIRED.value
            self.session.add(authorization)
            op = _record("refused", "the authorisation has expired", authorization.id)
            await self.session.flush()
            return op
        if kind not in (authorization.allowed_operations or []):
            op = _record(
                "refused",
                f"operation '{kind}' is not in the authorised set",
                authorization.id,
            )
            await self.session.flush()
            return op

        digest = self.current_hash(campaign)
        if digest != authorization.proposal_hash or digest != campaign.proposal_hash:
            op = _record(
                "refused",
                "the proposal changed after it was authorised (hash mismatch)",
                authorization.id,
                {"authorized_hash": authorization.proposal_hash, "current_hash": digest},
            )
            await self.session.flush()
            return op

        if int(campaign.total_budget_cents) > int(authorization.max_spend_cents):
            op = _record(
                "refused",
                "the campaign budget exceeds the authorised ceiling",
                authorization.id,
            )
            await self.session.flush()
            return op

        transport = get_ads_transport(campaign.platform, self.settings)
        if not transport.available:
            # Checked before consuming the authorisation so a blocked launch does
            # not burn the founder's approval.
            op = _record(
                "refused",
                f"ads transport unavailable: {transport.reason}",
                authorization.id,
                {"transport": transport.describe()},
            )
            await self.session.flush()
            return op

        # Consume the authorisation atomically: exactly one operation may use it.
        consumed = await self.session.execute(
            update(AdBudgetAuthorization)
            .where(
                AdBudgetAuthorization.id == authorization.id,
                AdBudgetAuthorization.status == AdAuthorizationStatus.ACTIVE.value,
                AdBudgetAuthorization.consumed_at.is_(None),
            )
            .values(
                status=AdAuthorizationStatus.CONSUMED.value,
                consumed_at=now,
            )
        )
        if int(consumed.rowcount or 0) != 1:
            op = _record("refused", "the authorisation was consumed concurrently", authorization.id)
            await self.session.flush()
            return op

        try:
            result = await transport.execute(
                campaign=campaign, kind=kind, params=params
            )
        except Exception as exc:  # noqa: BLE001 - transport boundary
            op = _record(
                "failed",
                f"{type(exc).__name__}: {exc}"[:500],
                authorization.id,
            )
            campaign.status = AdCampaignStatus.FAILED.value
            self.session.add(campaign)
            await self.session.flush()
            return op

        if not result.get("verified"):
            op = _record(
                "unverified",
                "the platform did not confirm the change; the campaign is not marked live",
                authorization.id,
                result,
            )
            await self.session.flush()
            return op

        op = _record("succeeded", f"{kind} confirmed by the platform", authorization.id, result)
        await self.session.flush()
        op_id = op.id
        await self.session.execute(
            update(AdBudgetAuthorization)
            .where(AdBudgetAuthorization.id == authorization.id)
            .values(consumed_by_operation_id=op_id)
        )
        campaign.external_campaign_id = result.get("external_campaign_id") or campaign.external_campaign_id
        campaign.status = (
            AdCampaignStatus.ACTIVE.value
            if kind in {AdOperationKind.CREATE.value, AdOperationKind.LAUNCH.value, AdOperationKind.RESUME.value}
            else AdCampaignStatus.PAUSED.value
            if kind == AdOperationKind.PAUSE.value
            else campaign.status
        )
        campaign.last_synced_at = now
        self.session.add(campaign)
        await self.session.flush()
        await record_audit(
            self.session,
            action=f"ads.{kind}_committed",
            entity_type="ad_operation",
            entity_id=op.id,
            actor_id=actor_id,
            after={
                "ad_campaign_id": str(campaign.id),
                "authorization_id": str(authorization.id),
                "outcome": "succeeded",
            },
        )
        return op

    # ------------------------------------------------------------------ reads
    async def list_campaigns(self, brand_profile_id: uuid.UUID) -> list[AdCampaign]:
        return list(
            (
                await self.session.execute(
                    select(AdCampaign)
                    .where(AdCampaign.brand_profile_id == brand_profile_id)
                    .order_by(AdCampaign.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def operations(self, campaign_id: uuid.UUID) -> list[AdOperation]:
        return list(
            (
                await self.session.execute(
                    select(AdOperation)
                    .where(AdOperation.ad_campaign_id == campaign_id)
                    .order_by(AdOperation.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

    def require_transport(self, platform: str) -> None:
        transport = get_ads_transport(platform, self.settings)
        if not transport.available:
            raise IntegrationUnavailableError(transport.reason, details={"platform": platform})
