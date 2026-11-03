"""Research Centre API.

Everything here is read/write over the research tables plus one action that
actually runs research. Nothing publishes, sends, spends ad money or touches a
live account.

Authorisation follows the Phase 1 convention: reads need any authenticated user,
writes need marketer-or-above, and provider configuration is founder-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from app.agents.base import AgentContext
from app.agents.registry import get_registry
from app.api.deps import DB, CurrentUser, EditorUser, FounderUser
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationProblem
from app.models.research import RESEARCH_CATEGORIES, VerificationStatus
from app.research.base import research_status
from app.schemas.research import (
    CompetitorSnapshotOut,
    ResearchFindingOut,
    ResearchFindingPage,
    ResearchProjectCreate,
    ResearchProjectDetail,
    ResearchProjectOut,
    ResearchRunResult,
    ResearchSourceOut,
)
from app.services.brand import BrandService
from app.services.research import ResearchService

router = APIRouter(prefix="/research", tags=["research"])


# --------------------------------------------------------------------------- #
# capability
# --------------------------------------------------------------------------- #
@router.get("/capabilities", summary="What research can actually do here")
async def capabilities(user: CurrentUser) -> dict[str, Any]:
    """Honest capability report: the same pattern as Phase 1 storage/llm."""
    settings = get_settings()
    return {
        "provider": research_status(settings),
        "categories": list(RESEARCH_CATEGORIES),
        "verification_statuses": VerificationStatus.values(),
        "max_sources": settings.research_max_sources,
        "budget_usd": settings.research_budget_usd,
        "note": (
            "Findings are only ever as good as the configured provider. Without a live "
            "endpoint the provider derives findings from stored brand knowledge and "
            "labels open questions as gaps instead of inventing answers."
        ),
    }


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #
@router.post(
    "/projects",
    response_model=ResearchRunResult,
    status_code=status.HTTP_201_CREATED,
    summary="Start a research project (and optionally run it)",
)
async def create_project(
    payload: ResearchProjectCreate, session: DB, user: EditorUser
) -> ResearchRunResult:
    settings = get_settings()
    brand = await BrandService(session).get(payload.brand_profile_id)
    if brand is None:
        raise NotFoundError(f"Brand profile {payload.brand_profile_id} not found.")

    service = ResearchService(session)
    project = await service.create_project(
        brand_profile_id=brand.id,
        objective=payload.objective,
        categories=payload.categories or list(RESEARCH_CATEGORIES),
        provider="",
        max_cost_usd=(
            payload.max_cost_usd
            if payload.max_cost_usd is not None
            else settings.research_budget_usd
        ),
        created_by=user.id,
    )
    await session.commit()

    if not payload.run_now:
        await session.refresh(project)
        return ResearchRunResult(
            project=ResearchProjectOut.model_validate(project),
            findings_created=0,
            sources_created=0,
            gaps=[],
            warnings=["Project created but not run (run_now=false)."],
            provider="",
            provider_status="queued",
            cost={},
            limits={},
            stop_reason="not_started",
        )

    return await _execute(session, brand, project, actor_id=user.id)


@router.post(
    "/projects/{project_id}/run",
    response_model=ResearchRunResult,
    summary="Run (or re-run) a research project",
)
async def run_project(
    project_id: uuid.UUID, session: DB, user: EditorUser
) -> ResearchRunResult:
    service = ResearchService(session)
    project = await service.get_project(project_id)
    brand = await BrandService(session).get(project.brand_profile_id)
    if brand is None:
        raise NotFoundError("Brand profile for this project no longer exists.")
    return await _execute(session, brand, project, actor_id=user.id)


async def _execute(session, brand, project, *, actor_id):  # type: ignore[no-untyped-def]
    """Delegate execution to the Research Agent — no duplicated research logic."""
    user_id = actor_id
    ctx = AgentContext(
        session=session,
        brand=brand,
        actor_id=user_id,
        workflow_thread_id=project.workflow_thread_id,
    )
    agent = get_registry().get("research")

    try:
        result = await agent.run(
            ctx,
            {
                "brand_profile_id": str(brand.id),
                "request": project.objective,
                "mode": "evidence",
                "categories": list(project.categories),
                "research_project_id": str(project.id),
                "max_cost_usd": project.max_cost_usd,
            },
        )
    except Exception as exc:  # noqa: BLE001 - API boundary
        await session.rollback()
        raise ValidationProblem(
            f"Research run failed: {str(exc)[:300]}",
            details={"project_id": str(project.id)},
        ) from exc

    await session.commit()
    await session.refresh(project)
    dump = result.model_dump(mode="json")
    return ResearchRunResult(
        project=ResearchProjectOut.model_validate(project),
        findings_created=dump.get("findings_created", 0),
        sources_created=dump.get("sources_created", 0),
        gaps=dump.get("gaps", []),
        warnings=dump.get("warnings", []),
        provider=dump.get("provider", ""),
        provider_status=dump.get("provider_status", ""),
        cost=dump.get("cost", {}),
        limits=dump.get("limits", {}),
        stop_reason=dump.get("stop_reason", ""),
    )


@router.get(
    "/projects",
    response_model=list[ResearchProjectOut],
    summary="List research projects",
)
async def list_projects(
    session: DB,
    user: CurrentUser,
    brand_profile_id: uuid.UUID | None = None,
    project_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ResearchProjectOut]:
    rows, _ = await ResearchService(session).list_projects(
        brand_profile_id=brand_profile_id, status=project_status, limit=limit, offset=offset
    )
    return [ResearchProjectOut.model_validate(row) for row in rows]


@router.get(
    "/projects/{project_id}",
    response_model=ResearchProjectDetail,
    summary="A project with its findings, sources and coverage",
)
async def get_project(
    project_id: uuid.UUID,
    session: DB,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> ResearchProjectDetail:
    service = ResearchService(session)
    project = await service.get_project(project_id)
    findings, _ = await service.list_findings(project_id=project.id, limit=limit)

    from sqlalchemy import select

    from app.models.research import ResearchSource

    sources = list(
        (
            await session.execute(
                select(ResearchSource).where(ResearchSource.research_project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    return ResearchProjectDetail(
        project=ResearchProjectOut.model_validate(project),
        findings=[ResearchFindingOut.model_validate(f) for f in findings],
        sources=[ResearchSourceOut.model_validate(s) for s in sources],
        category_coverage=await service.category_summary(project.id),
        missing_categories=await service.missing_categories(project),
    )


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #
@router.get(
    "/findings",
    response_model=ResearchFindingPage,
    summary="Search findings across projects",
)
async def list_findings(
    session: DB,
    user: CurrentUser,
    brand_profile_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    category: str | None = None,
    verification_status: str | None = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    competitor: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResearchFindingPage:
    rows, total = await ResearchService(session).list_findings(
        project_id=project_id,
        brand_profile_id=brand_profile_id,
        category=category,
        verification_status=verification_status,
        min_confidence=min_confidence,
        competitor=competitor,
        since=since,
        limit=limit,
        offset=offset,
    )
    return ResearchFindingPage(
        items=[ResearchFindingOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/findings/{finding_id}/verify",
    response_model=ResearchFindingOut,
    summary="Mark a finding verified (requires a source URL)",
)
async def verify_finding(
    finding_id: uuid.UUID,
    session: DB,
    user: FounderUser,
    source_url: Annotated[str, Query(min_length=4, max_length=1000)],
) -> ResearchFindingOut:
    """Verifying is a founder act and demands a real citation."""
    from app.models.research import ResearchFinding
    from sqlalchemy import select

    finding = await session.get(ResearchFinding, finding_id)
    if finding is None:
        raise NotFoundError(f"Finding {finding_id} not found.")
    if not source_url.strip().startswith(("http://", "https://", "brand://")):
        raise ValidationProblem(
            "A finding can only be verified against a resolvable URL.",
            details={"received": source_url[:120]},
        )
    finding.verification_status = VerificationStatus.VERIFIED.value
    finding.source_url = source_url.strip()[:1000]
    session.add(finding)
    await session.commit()
    await session.refresh(finding)
    return ResearchFindingOut.model_validate(finding)


# --------------------------------------------------------------------------- #
# competitors
# --------------------------------------------------------------------------- #
@router.get(
    "/competitors",
    response_model=list[CompetitorSnapshotOut],
    summary="Competitor profiles with their supporting evidence count",
)
async def competitors(
    session: DB,
    user: CurrentUser,
    brand_profile_id: Annotated[uuid.UUID, Query()],
) -> list[CompetitorSnapshotOut]:
    rows = await ResearchService(session).competitor_snapshot(brand_profile_id)
    return [CompetitorSnapshotOut(**row) for row in rows]
