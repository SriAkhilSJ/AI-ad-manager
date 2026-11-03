"""Research persistence: projects, sources, findings, pain points.

The service is the only place findings are written, which is what makes the
anti-fabrication guarantees hold:

* every finding is normalised through :class:`RawFinding`, so a source-less
  "verified" claim is downgraded before it ever reaches the database;
* findings are deduplicated per project by ``dedupe_key``, so re-running research
  cannot inflate the corpus;
* sources are deduplicated per project by URL;
* the project row carries the cost envelope and the observed spend, so a partial
  run reports exactly what it bought and what it did not.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.errors import NotFoundError, ValidationProblem
from app.models.research import (
    RESEARCH_CATEGORIES,
    AudiencePainPoint,
    AudienceSegment,
    ProductFact,
    ResearchFinding,
    ResearchProject,
    ResearchSource,
    ResearchStatus,
    VerificationStatus,
)
from app.research.base import RawFinding, RawSource, normalise_dedupe_key


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResearchService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ project
    async def create_project(
        self,
        *,
        brand_profile_id: uuid.UUID,
        objective: str,
        categories: Sequence[str] | None = None,
        provider: str = "",
        max_cost_usd: float = 0.0,
        estimated_cost_usd: float = 0.0,
        workflow_thread_id: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> ResearchProject:
        requested = [c for c in (categories or RESEARCH_CATEGORIES)]
        unknown = [c for c in requested if c not in RESEARCH_CATEGORIES]
        if unknown:
            raise ValidationProblem(
                f"Unknown research categor{'y' if len(unknown) == 1 else 'ies'}: "
                f"{', '.join(unknown)}",
                details={"allowed": list(RESEARCH_CATEGORIES)},
            )
        if not objective.strip():
            raise ValidationProblem("A research objective is required.")
        if max_cost_usd < 0 or estimated_cost_usd < 0:
            raise ValidationProblem("Budget values must be non-negative.")

        project = ResearchProject(
            brand_profile_id=brand_profile_id,
            objective=objective.strip(),
            categories=requested,
            provider=provider,
            max_cost_usd=round(float(max_cost_usd), 6),
            estimated_cost_usd=round(float(estimated_cost_usd), 6),
            workflow_thread_id=workflow_thread_id,
            created_by=created_by,
            status=ResearchStatus.QUEUED.value,
        )
        self.session.add(project)
        await self.session.flush()
        await record_audit(
            self.session,
            action="research.project_created",
            entity_type="research_project",
            entity_id=project.id,
            actor_id=created_by,
            after={
                "objective": project.objective,
                "categories": requested,
                "max_cost_usd": project.max_cost_usd,
            },
        )
        return project

    async def get_project(self, project_id: uuid.UUID) -> ResearchProject:
        project = await self.session.get(ResearchProject, project_id)
        if project is None:
            raise NotFoundError(f"Research project {project_id} not found.")
        return project

    async def list_projects(
        self,
        *,
        brand_profile_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ResearchProject], int]:
        conditions = []
        if brand_profile_id:
            conditions.append(ResearchProject.brand_profile_id == brand_profile_id)
        if status:
            conditions.append(ResearchProject.status == status)
        stmt = select(ResearchProject)
        count_stmt = select(func.count()).select_from(ResearchProject)
        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        stmt = (
            stmt.order_by(ResearchProject.created_at.desc(), ResearchProject.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def mark_running(self, project: ResearchProject) -> ResearchProject:
        project.status = ResearchStatus.RUNNING.value
        project.started_at = _utcnow()
        self.session.add(project)
        await self.session.flush()
        return project

    async def finish_project(
        self,
        project: ResearchProject,
        *,
        status: str,
        summary: str = "",
        error_code: str = "",
        error_message: str = "",
        actual_cost_usd: float | None = None,
        tokens_used: int | None = None,
        tool_calls: int | None = None,
        iterations: int | None = None,
    ) -> ResearchProject:
        if status not in ResearchStatus.values():
            raise ValidationProblem(f"Unknown research status '{status}'.")
        project.status = status
        project.summary = summary[:4000]
        project.error_code = error_code[:64]
        project.error_message = error_message[:2000]
        if actual_cost_usd is not None:
            project.actual_cost_usd = round(max(0.0, float(actual_cost_usd)), 6)
        if tokens_used is not None:
            project.tokens_used = max(0, int(tokens_used))
        if tool_calls is not None:
            project.tool_calls = max(0, int(tool_calls))
        if iterations is not None:
            project.iterations = max(0, int(iterations))
        project.finished_at = _utcnow()
        self.session.add(project)
        await self.session.flush()
        await record_audit(
            self.session,
            action=f"research.project_{status}",
            entity_type="research_project",
            entity_id=project.id,
            actor_id=project.created_by,
            after={
                "status": status,
                "findings": project.finding_count,
                "actual_cost_usd": project.actual_cost_usd,
            },
        )
        return project

    # ------------------------------------------------------------------ sources
    async def upsert_source(
        self,
        project: ResearchProject,
        source: RawSource,
        *,
        provider: str = "",
    ) -> ResearchSource:
        normalised = source.normalised()
        existing = (
            await self.session.execute(
                select(ResearchSource).where(
                    ResearchSource.research_project_id == project.id,
                    ResearchSource.url == normalised.url,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Keep the freshest metadata; never duplicate the row.
            if normalised.title:
                existing.title = normalised.title
            if normalised.excerpt:
                existing.excerpt = normalised.excerpt
            existing.retrieved_at = normalised.retrieved_at
            self.session.add(existing)
            await self.session.flush()
            return existing

        row = ResearchSource(
            brand_profile_id=project.brand_profile_id,
            research_project_id=project.id,
            url=normalised.url,
            title=normalised.title,
            publisher=normalised.publisher,
            retrieved_at=normalised.retrieved_at,
            content_hash=normalised.content_hash,
            credibility=normalised.credibility,
            provider=provider or project.provider,
            excerpt=normalised.excerpt,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    # ----------------------------------------------------------------- findings
    async def add_findings(
        self,
        project: ResearchProject,
        findings: Iterable[RawFinding],
        *,
        agent_run_id: uuid.UUID | None = None,
    ) -> list[ResearchFinding]:
        """Persist findings, skipping duplicates already in this project."""
        existing_keys = set(
            (
                await self.session.execute(
                    select(ResearchFinding.dedupe_key).where(
                        ResearchFinding.research_project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )

        created: list[ResearchFinding] = []
        for raw in findings:
            key = raw.dedupe_key()
            if key in existing_keys:
                continue

            # Normalise once: this guarantees a retrieval timestamp and a content
            # hash whether or not the caller remembered to normalise.
            source = None
            if raw.source and raw.source.url.strip():
                source = raw.source.normalised()

            source_row: ResearchSource | None = None
            if source is not None:
                source_row = await self.upsert_source(project, source, provider=raw.provider)

            status = raw.resolved_status()
            if status == VerificationStatus.VERIFIED.value and source is None:
                # Belt-and-braces: the CHECK constraint would also catch this.
                status = VerificationStatus.UNVERIFIED.value

            row = ResearchFinding(
                research_project_id=project.id,
                brand_profile_id=project.brand_profile_id,
                source_id=source_row.id if source_row else None,
                category=raw.category,
                finding=raw.finding,
                evidence_summary=raw.evidence_summary,
                source_url=(source.url if source else "")[:1000],
                source_title=(source.title if source else "")[:500],
                retrieved_at=(source.retrieved_at if source else None),
                confidence=round(float(raw.confidence), 4),
                verification_status=status,
                competitor_name=raw.competitor_name[:200],
                tags=list(raw.tags)[:20],
                dedupe_key=key,
                provider=raw.provider or project.provider,
                agent_run_id=agent_run_id,
            )
            self.session.add(row)
            created.append(row)
            existing_keys.add(key)

        await self.session.flush()
        await self._refresh_counts(project)
        return created

    async def _refresh_counts(self, project: ResearchProject) -> None:
        findings = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(ResearchFinding)
                    .where(ResearchFinding.research_project_id == project.id)
                )
            ).scalar_one()
        )
        sources = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_project_id == project.id)
                )
            ).scalar_one()
        )
        facts = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(ResearchFinding)
                    .where(
                        ResearchFinding.research_project_id == project.id,
                        ResearchFinding.verification_status
                        != VerificationStatus.VERIFIED.value,
                    )
                )
            ).scalar_one()
        )
        project.finding_count = findings
        project.source_count = sources
        project.gap_count = facts
        self.session.add(project)
        await self.session.flush()

    async def list_findings(
        self,
        *,
        project_id: uuid.UUID | None = None,
        brand_profile_id: uuid.UUID | None = None,
        category: str | None = None,
        verification_status: str | None = None,
        min_confidence: float | None = None,
        competitor: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ResearchFinding], int]:
        conditions = []
        if project_id:
            conditions.append(ResearchFinding.research_project_id == project_id)
        if brand_profile_id:
            conditions.append(ResearchFinding.brand_profile_id == brand_profile_id)
        if category:
            conditions.append(ResearchFinding.category == category)
        if verification_status:
            conditions.append(ResearchFinding.verification_status == verification_status)
        if min_confidence is not None:
            conditions.append(ResearchFinding.confidence >= min_confidence)
        if competitor:
            conditions.append(ResearchFinding.competitor_name == competitor)
        if since:
            conditions.append(ResearchFinding.created_at >= since)

        stmt = select(ResearchFinding)
        count_stmt = select(func.count()).select_from(ResearchFinding)
        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        stmt = (
            stmt.order_by(
                ResearchFinding.confidence.desc(), ResearchFinding.created_at.desc()
            )
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def category_summary(self, project_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(
                    ResearchFinding.category,
                    func.count().label("total"),
                    func.avg(ResearchFinding.confidence).label("confidence"),
                )
                .where(ResearchFinding.research_project_id == project_id)
                .group_by(ResearchFinding.category)
            )
        ).all()
        return [
            {
                "category": row.category,
                "findings": int(row.total),
                "avg_confidence": round(float(row.confidence or 0.0), 3),
            }
            for row in rows
        ]

    async def missing_categories(self, project: ResearchProject) -> list[str]:
        present = set(
            (
                await self.session.execute(
                    select(ResearchFinding.category)
                    .where(ResearchFinding.research_project_id == project.id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return [c for c in project.categories if c not in present]

    # -------------------------------------------------------------- competitors
    async def competitor_snapshot(self, brand_profile_id: uuid.UUID) -> list[dict[str, Any]]:
        """Competitors plus how much sourced evidence backs each one."""
        from app.models.brand import CompetitorProfile

        competitors = list(
            (
                await self.session.execute(
                    select(CompetitorProfile).where(
                        CompetitorProfile.brand_profile_id == brand_profile_id
                    )
                )
            )
            .scalars()
            .all()
        )
        out: list[dict[str, Any]] = []
        for competitor in competitors:
            findings = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(ResearchFinding)
                        .where(
                            ResearchFinding.brand_profile_id == brand_profile_id,
                            ResearchFinding.competitor_name == competitor.name,
                        )
                    )
                ).scalar_one()
            )
            out.append(
                {
                    "name": competitor.name,
                    "website": competitor.website,
                    "positioning": competitor.positioning,
                    "source": competitor.source,
                    "findings": findings,
                }
            )
        return out

    # ------------------------------------------------------------ pain points
    async def record_pain_points(
        self, project: ResearchProject, findings: Sequence[ResearchFinding]
    ) -> list[AudiencePainPoint]:
        """Promote pain-point findings into the reusable audience model."""
        segments = list(
            (
                await self.session.execute(
                    select(AudienceSegment).where(
                        AudienceSegment.brand_profile_id == project.brand_profile_id
                    )
                )
            )
            .scalars()
            .all()
        )
        created: list[AudiencePainPoint] = []
        for finding in findings:
            if finding.category != "developer_pain_points":
                continue
            key = normalise_dedupe_key("pain_point", finding.finding)
            already = (
                await self.session.execute(
                    select(AudiencePainPoint).where(
                        AudiencePainPoint.brand_profile_id == project.brand_profile_id,
                        AudiencePainPoint.dedupe_key == key,
                    )
                )
            ).scalar_one_or_none()
            if already is not None:
                continue
            segment_match = next(
                (
                    s
                    for s in segments
                    if s.name.lower() in finding.finding.lower()
                    or any(t.lower() in s.name.lower() for t in finding.tags)
                ),
                None,
            )
            row = AudiencePainPoint(
                brand_profile_id=project.brand_profile_id,
                audience_segment_id=segment_match.id if segment_match else None,
                statement=finding.finding,
                severity="medium",
                source_id=finding.source_id,
                source_url=finding.source_url,
                confidence=finding.confidence,
                verification_status=finding.verification_status,
                tags=list(finding.tags),
                dedupe_key=key,
            )
            self.session.add(row)
            created.append(row)
        await self.session.flush()
        return created

    # ------------------------------------------------------------------- facts
    async def unverified_facts(self, brand_profile_id: uuid.UUID) -> list[ProductFact]:
        """Everything the founder still needs to confirm before it can be used."""
        return list(
            (
                await self.session.execute(
                    select(ProductFact)
                    .where(
                        ProductFact.brand_profile_id == brand_profile_id,
                        ProductFact.verification_status
                        != VerificationStatus.VERIFIED.value,
                    )
                    .order_by(ProductFact.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def verify_fact(
        self,
        fact_id: uuid.UUID,
        *,
        source_url: str,
        actor_id: uuid.UUID | None = None,
    ) -> ProductFact:
        """Mark a fact verified. Requires a real source — no exceptions."""
        fact = await self.session.get(ProductFact, fact_id)
        if fact is None:
            raise NotFoundError(f"Product fact {fact_id} not found.")
        if not (source_url or "").strip():
            raise ValidationProblem(
                "Verifying a product fact requires a source URL.",
                details={"fact_id": str(fact_id)},
            )
        before = {
            "statement": fact.statement,
            "verification_status": fact.verification_status,
        }
        fact.source_url = source_url.strip()[:1000]
        fact.verification_status = VerificationStatus.VERIFIED.value
        fact.verified_by = actor_id
        fact.verified_at = _utcnow()
        fact.is_public = True
        self.session.add(fact)
        await self.session.flush()
        await record_audit(
            self.session,
            action="product_fact.verified",
            entity_type="product_fact",
            entity_id=fact.id,
            actor_id=actor_id,
            before=before,
            after={
                "verification_status": fact.verification_status,
                "source_url": fact.source_url,
            },
        )
        return fact


__all__ = ["ResearchService"]
