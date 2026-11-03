"""Versioned API aggregation (`/api/v1`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import rate_limit
from app.api.v1 import (
    agents,
    analytics,
    auth,
    brand,
    campaigns,
    content,
    content_studio,
    creative,
    creative_studio,
    flow,
    director,
    research,
    social,
    strategy,
)

api_router = APIRouter(dependencies=[Depends(rate_limit)])

api_router.include_router(auth.router)
api_router.include_router(brand.router)
api_router.include_router(campaigns.router)
api_router.include_router(content.router)
api_router.include_router(content_studio.router)
api_router.include_router(creative.router)
api_router.include_router(creative_studio.router)
api_router.include_router(flow.router)
api_router.include_router(director.router)
api_router.include_router(agents.router)
api_router.include_router(social.router)
api_router.include_router(analytics.router)
api_router.include_router(research.router)
api_router.include_router(strategy.router)
api_router.include_router(strategy.director_router)

__all__ = ["api_router"]
