"""Growth Analyst — reads real analytics and proposes measurable next steps.

It refuses to fabricate baselines: with no snapshots it returns
``data_source: "none"`` and a measurement plan, not invented benchmarks.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.services.analytics import AnalyticsService


class GrowthInput(AgentPayload):
    window_days: int = 30
    platform: str | None = None
    focus: str = "engagement"


class Recommendation(BaseModel):
    action: str
    rationale: str
    expected_effect: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class GrowthOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "none"
    measured: dict[str, object] = Field(default_factory=dict)
    observations: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    measurement_plan: list[str] = Field(default_factory=list)


class GrowthAnalystAgent(BaseAgent[GrowthInput, GrowthOutput]):
    name = "growth_analyst"
    description = "Interprets measured performance and proposes grounded optimisations."
    responsibilities = [
        "Read analytics snapshots from the database",
        "Report only measured metrics; never estimate or project as if measured",
        "Produce prioritised, falsifiable recommendations",
        "State what must be instrumented before conclusions can be drawn",
    ]
    input_schema = GrowthInput
    output_schema = GrowthOutput
    max_tokens = 1000

    async def execute(self, ctx: AgentContext, payload: GrowthInput) -> GrowthOutput:
        if ctx.brand is None:
            return GrowthOutput(
                summary="No brand profile; analytics cannot be scoped.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile."],
            )

        analytics = AnalyticsService(ctx.session)
        overview = await analytics.overview(
            brand_profile_id=ctx.brand.id,
            days=payload.window_days,
            platform=payload.platform,
        )

        if not overview["has_data"]:
            return GrowthOutput(
                summary=(
                    "No analytics snapshots in the selected window. No performance "
                    "conclusions can be drawn yet."
                ),
                data_source="none",
                measured={"window_days": payload.window_days, "snapshots": 0},
                observations=[
                    "No published-post metrics have been collected.",
                    "No platform analytics connector is configured.",
                ],
                recommendations=[
                    Recommendation(
                        action="Instrument the publishing pipeline before optimising.",
                        rationale="Optimisation without measurement is guesswork.",
                        expected_effect="Enables attributable, per-post performance data.",
                        confidence=0.9,
                    )
                ],
                measurement_plan=[
                    "Link at least one social account and complete OAuth.",
                    "Enable the analytics collector (Phase 2) to write analytics_snapshots.",
                    "Define the primary KPI on the campaign before publishing.",
                ],
                confidence=0.85,
                warnings=["Analytics unavailable: results are a measurement plan, not an assessment."],
            )

        observations: list[str] = []
        recommendations: list[Recommendation] = []
        totals = overview["totals"]  # type: ignore[index]
        engagement_rate = overview["engagement_rate"]

        if isinstance(engagement_rate, float):
            observations.append(
                f"Engagement rate is {engagement_rate:.2%} across {totals['impressions']} impressions."  # type: ignore[index]
            )
            if engagement_rate < 0.02:
                recommendations.append(
                    Recommendation(
                        action="Raise hook quality in the first line of copy.",
                        rationale=f"Measured engagement rate {engagement_rate:.2%} is below 2%.",
                        expected_effect="Higher stop-rate on feed scrolling.",
                        confidence=0.6,
                    )
                )
        by_platform = overview.get("by_platform") or []
        if isinstance(by_platform, list) and by_platform:
            best = max(by_platform, key=lambda row: row.get("engagements", 0))
            observations.append(
                f"{best['platform']} leads on measured engagements ({best.get('engagements', 0)})."
            )
            recommendations.append(
                Recommendation(
                    action=f"Shift cadence toward {best['platform']} while measurement is thin.",
                    rationale="Highest measured engagements in the window.",
                    expected_effect="More efficient use of creative effort.",
                    confidence=0.5,
                )
            )

        return GrowthOutput(
            summary=f"Analysed {overview['window_days']} days of measured performance.",
            data_source="analytics_snapshots",
            measured={
                "window_days": overview["window_days"],
                "totals": totals,
                "engagement_rate": engagement_rate,
                "by_platform": by_platform,
            },
            observations=observations,
            recommendations=recommendations,
            measurement_plan=[
                "Continue collecting snapshots at a consistent cadence for trend validity."
            ],
            confidence=0.55,
        )
