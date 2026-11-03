"""Strategy persistence, versioning and approval.

The guarantee this module implements, mirroring Phase 1's content approval:

    an approval authorises **one exact strategy version**, and editing the
    strategy invalidates that approval.

Versions are immutable. "Editing" always means creating version *n+1*, which
clears the current version pointer's approval and moves the strategy back to
``DRAFT``. Every decision is written to :class:`StrategyApproval` (append-only)
*and* to the audit log, so the history of who approved what, and when it stopped
being valid, is reconstructible without trusting the strategy row itself.

A strategy may **never** be executed here. Nothing in this module publishes,
sends, spends or touches a live account; it only records intent for a founder
to approve and for a human to act on.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.errors import ConflictError, NotFoundError, ValidationProblem
from app.models.research import BUSINESS_OBJECTIVES
from app.models.strategy import (
    GrowthExperiment,
    MarketingStrategy,
    StrategyApproval,
    StrategyStatus,
    StrategyVersion,
)
from app.services.brand import BrandService

# The payload fields that make up a strategy version's identity. Approval binds
# to the hash of exactly these, so a change to any of them invalidates it.
VERSION_PAYLOAD_FIELDS = (
    "positioning_recommendations",
    "audience_segmentation",
    "competitor_differentiation",
    "content_pillars",
    "campaign_concepts",
    "platform_strategies",
    "weekly_objectives",
    "publishing_frequency",
    "lead_generation",
    "growth_experiments",
    "kpis",
    "measurement_plan",
    "business_objectives",
    "objective_alignment",
    "research_summary",
    "rationale",
)


def compute_strategy_hash(payload: dict[str, Any]) -> str:
    """Deterministic fingerprint of the strategy payload."""
    body = {field: payload.get(field) for field in VERSION_PAYLOAD_FIELDS}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrategyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ read
    async def get_strategy(self, strategy_id: uuid.UUID) -> MarketingStrategy:
        strategy = await self.session.get(MarketingStrategy, strategy_id)
        if strategy is None:
            raise NotFoundError(f"Strategy {strategy_id} not found.")
        return strategy

    async def get_version(self, version_id: uuid.UUID) -> StrategyVersion:
        version = await self.session.get(StrategyVersion, version_id)
        if version is None:
            raise NotFoundError(f"Strategy version {version_id} not found.")
        return version

    async def current_version(self, strategy: MarketingStrategy) -> StrategyVersion | None:
        if strategy.current_version_id is None:
            return None
        return await self.session.get(StrategyVersion, strategy.current_version_id)

    async def list_strategies(
        self,
        *,
        brand_profile_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MarketingStrategy], int]:
        conditions = []
        if brand_profile_id:
            conditions.append(MarketingStrategy.brand_profile_id == brand_profile_id)
        if status:
            conditions.append(MarketingStrategy.status == status)
        stmt = select(MarketingStrategy)
        count_stmt = select(func.count()).select_from(MarketingStrategy)
        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        stmt = (
            stmt.order_by(
                MarketingStrategy.created_at.desc(), MarketingStrategy.id.desc()
            )
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def list_versions(self, strategy_id: uuid.UUID) -> list[StrategyVersion]:
        return list(
            (
                await self.session.execute(
                    select(StrategyVersion)
                    .where(StrategyVersion.strategy_id == strategy_id)
                    .order_by(StrategyVersion.version_number.desc())
                )
            )
            .scalars()
            .all()
        )

    async def diff_versions(
        self, left: StrategyVersion, right: StrategyVersion
    ) -> dict[str, Any]:
        """Field-level comparison for the Strategy Studio version view."""
        changes: dict[str, Any] = {}
        for field in VERSION_PAYLOAD_FIELDS:
            before = getattr(left, field)
            after = getattr(right, field)
            if before != after:
                changes[field] = {"before": before, "after": after}
        return {
            "left": {"id": str(left.id), "version_number": left.version_number},
            "right": {"id": str(right.id), "version_number": right.version_number},
            "changed_fields": sorted(changes),
            "changes": changes,
            "identical": not changes,
        }

    # ---------------------------------------------------------------- create
    async def create_strategy(
        self,
        *,
        brand_profile_id: uuid.UUID,
        name: str,
        objective: str,
        payload: dict[str, Any],
        primary_objective_type: str = "developer_acquisition",
        research_project_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
        workflow_thread_id: str | None = None,
        created_by: uuid.UUID | None = None,
        created_by_agent: str | None = None,
        generation_mode: str = "deterministic",
        confidence: float = 0.5,
        cost_usd: float = 0.0,
        tokens_used: int = 0,
        change_note: str = "Initial version",
    ) -> tuple[MarketingStrategy, StrategyVersion]:
        if not name.strip():
            raise ValidationProblem("A strategy name is required.")
        self._validate_payload(payload)

        strategy = MarketingStrategy(
            brand_profile_id=brand_profile_id,
            research_project_id=research_project_id,
            campaign_id=campaign_id,
            workflow_thread_id=workflow_thread_id,
            name=name.strip()[:300],
            objective=objective,
            status=StrategyStatus.DRAFT.value,
            primary_objective_type=primary_objective_type,
            created_by_agent=created_by_agent,
            created_by=created_by,
            version_count=0,
        )
        self.session.add(strategy)
        await self.session.flush()

        version = await self._append_version(
            strategy,
            payload=payload,
            version_number=1,
            change_note=change_note,
            created_by=created_by,
            created_by_agent=created_by_agent,
            generation_mode=generation_mode,
            confidence=confidence,
            cost_usd=cost_usd,
            tokens_used=tokens_used,
        )
        strategy.current_version_id = version.id

        await record_audit(
            self.session,
            action="strategy.created",
            entity_type="marketing_strategy",
            entity_id=strategy.id,
            actor_id=created_by,
            after={
                "name": strategy.name,
                "objective_type": primary_objective_type,
                "version": version.version_number,
                "business_objectives": payload.get("business_objectives", []),
            },
        )
        await self.session.flush()
        return strategy, version

    async def add_version(
        self,
        strategy: MarketingStrategy,
        *,
        payload: dict[str, Any],
        change_note: str = "",
        created_by: uuid.UUID | None = None,
        created_by_agent: str | None = None,
        generation_mode: str = "deterministic",
        confidence: float = 0.5,
        cost_usd: float = 0.0,
        tokens_used: int = 0,
    ) -> StrategyVersion:
        """Create version *n+1* and invalidate any approval for the old one."""
        self._validate_payload(payload)

        next_number = int(strategy.version_count) + 1
        version = await self._append_version(
            strategy,
            payload=payload,
            version_number=next_number,
            change_note=change_note or f"Version {next_number}",
            created_by=created_by,
            created_by_agent=created_by_agent,
            generation_mode=generation_mode,
            confidence=confidence,
            cost_usd=cost_usd,
            tokens_used=tokens_used,
        )

        previous_version_id = strategy.current_version_id
        strategy.current_version_id = version.id
        # An edit always returns the strategy to review: approval is per version.
        strategy.status = StrategyStatus.DRAFT.value
        self.session.add(strategy)
        await self.session.flush()

        invalidated = 0
        if previous_version_id is not None:
            invalidated = await self.invalidate_approvals(
                strategy,
                version_id=previous_version_id,
                reason=f"superseded by version {version.version_number}",
                actor_id=created_by,
            )

        await record_audit(
            self.session,
            action="strategy.version_created",
            entity_type="marketing_strategy",
            entity_id=strategy.id,
            actor_id=created_by,
            after={
                "version": version.version_number,
                "version_id": str(version.id),
                "change_note": version.change_note,
                "approvals_invalidated": invalidated,
            },
        )
        return version

    async def _append_version(
        self,
        strategy: MarketingStrategy,
        *,
        payload: dict[str, Any],
        version_number: int,
        change_note: str,
        created_by: uuid.UUID | None,
        created_by_agent: str | None,
        generation_mode: str,
        confidence: float,
        cost_usd: float,
        tokens_used: int,
    ) -> StrategyVersion:
        data = {field: payload.get(field) for field in VERSION_PAYLOAD_FIELDS}
        content_hash = compute_strategy_hash(data)

        version = StrategyVersion(
            strategy_id=strategy.id,
            version_number=version_number,
            # defaults for the structured blocks
            positioning_recommendations=data["positioning_recommendations"] or [],
            audience_segmentation=data["audience_segmentation"] or [],
            competitor_differentiation=data["competitor_differentiation"] or [],
            content_pillars=data["content_pillars"] or [],
            campaign_concepts=data["campaign_concepts"] or [],
            platform_strategies=data["platform_strategies"] or [],
            weekly_objectives=data["weekly_objectives"] or [],
            publishing_frequency=data["publishing_frequency"] or {},
            lead_generation=data["lead_generation"] or [],
            growth_experiments=data["growth_experiments"] or [],
            kpis=data["kpis"] or [],
            measurement_plan=data["measurement_plan"] or {},
            business_objectives=data["business_objectives"] or [],
            objective_alignment=data["objective_alignment"] or {},
            research_summary=data["research_summary"] or "",
            rationale=data["rationale"] or "",
            source_finding_ids=[
                str(fid) for fid in (payload.get("source_finding_ids") or [])
            ][:200],
            brand_check=payload.get("brand_check") or {},
            missing_information=payload.get("missing_information") or [],
            warnings=[str(w) for w in (payload.get("warnings") or [])][:50],
            generation_mode=generation_mode,
            confidence=round(max(0.0, min(1.0, float(confidence))), 4),
            cost_usd=round(max(0.0, float(cost_usd)), 6),
            tokens_used=max(0, int(tokens_used)),
            content_hash=content_hash,
            change_note=change_note[:2000],
            created_by_agent=created_by_agent,
            created_by=created_by,
        )
        self.session.add(version)
        await self.session.flush()

        strategy.version_count = version_number
        self.session.add(strategy)
        await self.session.flush()
        return version

    # -------------------------------------------------------------- validate
    def _validate_payload(self, payload: dict[str, Any]) -> None:
        objectives = payload.get("business_objectives") or []
        if not objectives:
            raise ValidationProblem(
                "A strategy must map to at least one business objective.",
                details={"allowed": list(BUSINESS_OBJECTIVES)},
            )
        unknown = [o for o in objectives if o not in BUSINESS_OBJECTIVES]
        if unknown:
            raise ValidationProblem(
                f"Unknown business objective(s): {', '.join(unknown)}",
                details={"allowed": list(BUSINESS_OBJECTIVES)},
            )
        if not payload.get("kpis"):
            raise ValidationProblem(
                "A strategy must define KPIs — engagement vanity metrics do not count "
                "as measurement."
            )

    # -------------------------------------------------------------- approval
    async def valid_approval(self, strategy: MarketingStrategy) -> StrategyApproval | None:
        """The approval that currently authorises *this exact* current version."""
        if strategy.current_version_id is None:
            return None
        row = (
            await self.session.execute(
                select(StrategyApproval)
                .where(
                    StrategyApproval.strategy_id == strategy.id,
                    StrategyApproval.strategy_version_id == strategy.current_version_id,
                    StrategyApproval.action == "approve",
                    StrategyApproval.invalidated_at.is_(None),
                )
                .order_by(StrategyApproval.created_at.desc())
            )
        ).scalars().first()
        return row

    def stored_hash(self, version: StrategyVersion) -> str:
        """Hash of the payload as it exists *now*.

        Computed on demand rather than trusting the stored column, so an
        out-of-band modification to a version is visible.
        """
        return compute_strategy_hash({f: getattr(version, f) for f in VERSION_PAYLOAD_FIELDS})

    async def require_approved(self, strategy_id: uuid.UUID) -> tuple[MarketingStrategy, StrategyVersion, StrategyApproval]:
        """Gate for anything that would *execute* a strategy.

        Raises ``ConflictProblem`` when there is no live approval, when the
        approval names a different version, or when the stored hash no longer
        matches the version payload.
        """
        strategy = await self.get_strategy(strategy_id)
        version = await self.current_version(strategy)
        if version is None:
            raise ConflictError("Strategy has no version to approve.")
        approval = await self.valid_approval(strategy)
        if approval is None:
            raise ConflictError(
                "This strategy has no valid approval. A founder must approve the "
                "current version before anything can be executed.",
                details={
                    "strategy_id": str(strategy.id),
                    "current_version": version.version_number,
                    "strategy_status": strategy.status,
                },
            )
        if approval.strategy_version_id != version.id:
            raise ConflictError("The approval does not match the current strategy version.")
        current = self.stored_hash(version)
        if approval.content_hash and approval.content_hash != current:
            raise ConflictError(
                "The approved strategy content no longer matches the stored version; "
                "approval is stale.",
                details={
                    "approved_hash": approval.content_hash,
                    "current_hash": current,
                },
            )
        if version.content_hash and version.content_hash != current:
            raise ConflictError(
                "The stored strategy has been modified since it was written; "
                "its integrity hash does not match its payload."
            )
        return strategy, version, approval

    async def decide(
        self,
        strategy: MarketingStrategy,
        *,
        action: str,
        actor_id: uuid.UUID | None,
        actor_role: str,
        reason: str = "",
        version_id: uuid.UUID | None = None,
    ) -> StrategyApproval:
        """Record a founder decision. Only ``approve`` authorises execution."""
        if action not in ("submit", "approve", "reject", "request_changes", "revoke"):
            raise ValidationProblem(f"Unknown strategy decision '{action}'.")
        if action in ("approve", "reject", "request_changes", "revoke") and actor_role not in (
            "founder",
        ):
            raise ConflictError(
                "Only the founder may decide on a strategy.",
                details={"actor_role": actor_role, "required": "founder"},
            )

        version = (
            await self.get_version(version_id)
            if version_id is not None
            else await self.current_version(strategy)
        )
        if version is None:
            raise ValidationProblem("Strategy has no version to act on.")
        if version.strategy_id != strategy.id:
            raise ValidationProblem("The version does not belong to this strategy.")

        if action == "approve":
            if strategy.current_version_id != version.id:
                raise ConflictError(
                    "Only the current version can be approved; "
                    f"version {version.version_number} is not current."
                )
            if version.content_hash != self.stored_hash(version):
                raise ConflictError(
                    "Stored strategy content hash does not match its payload; "
                    "refusing to approve a modified version."
                )

        record = StrategyApproval(
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            action=action,
            actor_id=actor_id,
            actor_role=actor_role or "",
            reason=reason[:4000],
            content_hash=version.content_hash,
            request_payload={
                "version_number": version.version_number,
                "action": action,
            },
            decided_at=_utcnow(),
        )
        self.session.add(record)

        transition = {
            "submit": StrategyStatus.IN_REVIEW.value,
            "approve": StrategyStatus.APPROVED.value,
            "reject": StrategyStatus.REJECTED.value,
            "request_changes": StrategyStatus.CHANGES_REQUESTED.value,
            "revoke": StrategyStatus.DRAFT.value,
        }[action]
        before_status = strategy.status
        strategy.status = transition
        self.session.add(strategy)
        await self.session.flush()

        if action in ("reject", "request_changes", "revoke"):
            await self.invalidate_approvals(
                strategy, version_id=version.id, reason=f"{action}: {reason}"[:255],
                actor_id=actor_id,
            )

        if action == "approve":
            await self._materialise_experiments(strategy, version, actor_id=actor_id)

        await record_audit(
            self.session,
            action=f"strategy.{action}",
            entity_type="marketing_strategy",
            entity_id=strategy.id,
            actor_id=actor_id,
            before={"status": before_status},
            after={
                "status": strategy.status,
                "version": version.version_number,
                "version_id": str(version.id),
                "reason": reason[:500],
                "actor_role": actor_role,
            },
        )
        await self.session.flush()
        return record

    async def invalidate_approvals(
        self,
        strategy: MarketingStrategy,
        *,
        version_id: uuid.UUID | None = None,
        reason: str = "",
        actor_id: uuid.UUID | None = None,
    ) -> int:
        conditions = [
            StrategyApproval.strategy_id == strategy.id,
            StrategyApproval.invalidated_at.is_(None),
        ]
        if version_id is not None:
            conditions.append(StrategyApproval.strategy_version_id == version_id)
        rows = list(
            (await self.session.execute(select(StrategyApproval).where(*conditions)))
            .scalars()
            .all()
        )
        invalidated = 0
        for row in rows:
            row.invalidated_at = _utcnow()
            row.invalidated_reason = reason[:255]
            self.session.add(row)
            invalidated += 1
        if invalidated:
            await self.session.flush()
            await record_audit(
                self.session,
                action="strategy.approval_invalidated",
                entity_type="marketing_strategy",
                entity_id=strategy.id,
                actor_id=actor_id,
                after={"invalidated": invalidated, "reason": reason[:500]},
            )
        return invalidated

    async def approval_history(self, strategy_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = list(
            (
                await self.session.execute(
                    select(StrategyApproval)
                    .where(StrategyApproval.strategy_id == strategy_id)
                    .order_by(StrategyApproval.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": str(row.id),
                "action": row.action,
                "strategy_version_id": str(row.strategy_version_id),
                "actor_id": str(row.actor_id) if row.actor_id else None,
                "actor_role": row.actor_role,
                "reason": row.reason,
                "content_hash": row.content_hash,
                "is_valid": row.is_valid,
                "invalidated_at": row.invalidated_at.isoformat() if row.invalidated_at else None,
                "invalidated_reason": row.invalidated_reason,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    # ------------------------------------------------------------ experiments
    async def _materialise_experiments(
        self,
        strategy: MarketingStrategy,
        version: StrategyVersion,
        *,
        actor_id: uuid.UUID | None,
    ) -> list[GrowthExperiment]:
        """Turn the approved version's experiments into trackable rows.

        Only happens on approval: an unapproved idea is not a commitment.
        """
        existing = set(
            (
                await self.session.execute(
                    select(GrowthExperiment.name).where(
                        GrowthExperiment.strategy_id == strategy.id
                    )
                )
            )
            .scalars()
            .all()
        )
        created: list[GrowthExperiment] = []
        for item in version.growth_experiments or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in existing:
                continue
            objective_type = str(item.get("objective_type") or strategy.primary_objective_type)
            if objective_type not in BUSINESS_OBJECTIVES:
                objective_type = strategy.primary_objective_type
            effort = str(item.get("expected_effort") or "medium")
            if effort not in ("low", "medium", "high"):
                effort = "medium"
            row = GrowthExperiment(
                brand_profile_id=strategy.brand_profile_id,
                strategy_id=strategy.id,
                strategy_version_id=version.id,
                name=name[:300],
                hypothesis=str(item.get("hypothesis") or "")[:4000],
                channel=str(item.get("channel") or "")[:64],
                objective_type=objective_type,
                primary_metric=str(item.get("primary_metric") or "")[:200],
                expected_effort=effort,
                status="proposed",
            )
            self.session.add(row)
            created.append(row)
            existing.add(name)
        if created:
            await self.session.flush()
        return created

    async def list_experiments(self, strategy_id: uuid.UUID) -> list[GrowthExperiment]:
        return list(
            (
                await self.session.execute(
                    select(GrowthExperiment)
                    .where(GrowthExperiment.strategy_id == strategy_id)
                    .order_by(GrowthExperiment.created_at.asc())
                )
            )
            .scalars()
            .all()
        )


__all__ = [
    "StrategyService",
    "VERSION_PAYLOAD_FIELDS",
    "compute_strategy_hash",
]
