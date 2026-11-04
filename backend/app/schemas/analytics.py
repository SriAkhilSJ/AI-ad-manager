from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MetricTotals(BaseModel):
    impressions: int = 0
    reach: int = 0
    engagements: int = 0
    clicks: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0


class PlatformBreakdown(BaseModel):
    platform: str
    impressions: int = 0
    engagements: int = 0
    clicks: int = 0
    snapshots: int = 0


class SeriesPoint(BaseModel):
    date: str
    impressions: int = 0
    engagements: int = 0


class AnalyticsOverviewOut(BaseModel):
    has_data: bool
    data_source: str | list[str] | Literal["none"]
    window_days: int
    platform: str | None = None
    message: str | None = None
    totals: MetricTotals
    engagement_rate: float | None = None
    by_platform: list[PlatformBreakdown] = Field(default_factory=list)
    series: list[SeriesPoint] = Field(default_factory=list)
    published_posts: int = 0


class UsageTotals(BaseModel):
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class ProviderUsage(BaseModel):
    provider: str
    model: str
    calls: int
    total_tokens: int
    cost_usd: float


class AgentUsage(BaseModel):
    agent: str
    calls: int
    total_tokens: int
    cost_usd: float


class DailyUsage(BaseModel):
    date: str
    total_tokens: int
    cost_usd: float
    calls: int


class UsageSummaryOut(BaseModel):
    has_data: bool
    window_days: int
    message: str | None = None
    totals: UsageTotals
    by_provider: list[ProviderUsage] = Field(default_factory=list)
    by_agent: list[AgentUsage] = Field(default_factory=list)
    by_day: list[DailyUsage] = Field(default_factory=list)
