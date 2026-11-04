"""Agent contracts: schemas, limits, ledger, error handling, usage accounting."""

from __future__ import annotations

import pytest

from app.agents.base import (
    AgentContext,
    AgentError,
    AgentInputInvalid,
    AgentPayload,
    AgentResult,
    BaseAgent,
)
from app.agents.registry import get_registry
from app.models.agent import AgentRun, AgentRunStatus


async def test_registry_exposes_all_six_agents():
    names = set(get_registry().names())
    assert names == {
        "research",
        "strategy",
        "content",
        "creative",
        "brand_guardian",
        "growth_analyst",
    }


async def test_every_agent_declares_schemas_and_limits():
    for spec in get_registry().specs():
        assert spec["description"], spec["name"]
        assert spec["responsibilities"], spec["name"]
        assert spec["input_schema"]["properties"] is not None
        assert spec["output_schema"]["properties"] is not None
        limits = spec["limits"]
        assert limits["max_attempts"] >= 1
        assert limits["timeout_seconds"] > 0
        assert limits["max_tokens"] > 0


async def test_input_schema_rejects_unknown_fields(session, brand_row):
    agent = get_registry().get("research")
    ctx = AgentContext(session=session, brand=brand_row)
    with pytest.raises(AgentInputInvalid):
        await agent.run(ctx, {"brand_profile_id": str(brand_row.id), "surprise": 1})


async def test_agent_run_is_ledgered_with_trace(session, brand_row):
    agent = get_registry().get("research")
    ctx = AgentContext(session=session, brand=brand_row)
    result = await agent.run(ctx, {"brand_profile_id": str(brand_row.id)})
    await session.commit()

    run = ctx.run
    assert run is not None
    assert run.status == AgentRunStatus.SUCCEEDED.value
    assert run.attempts == 1
    assert run.input_payload["brand_profile_id"] == str(brand_row.id)
    assert run.output_payload["summary"]
    events = [event["event"] for event in run.trace]
    assert "run_opened" in events
    assert "attempt_started" in events
    assert "run_finished" in events
    assert result.requires_human_review is True


async def test_outputs_are_structured_not_free_text(session, brand_row):
    ctx = AgentContext(session=session, brand=brand_row)
    research = await get_registry().get("research").run(ctx, {})
    assert research.audience_insights
    assert all(hasattr(i, "segment") for i in research.audience_insights)

    content = await get_registry().get("content").run(
        ctx, {"platform": "linkedin", "angle": "shipping velocity"}
    )
    assert content.headline and content.body and content.call_to_action
    assert content.platform == "linkedin"


async def test_generation_mode_is_reported_honestly(session, brand_row):
    """With no provider configured every agent must label itself deterministic."""
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    for name, payload in (
        ("research", {}),
        ("strategy", {"platforms": ["linkedin"]}),
        ("content", {"platform": "linkedin"}),
        ("creative", {"platform": "instagram"}),
        ("growth_analyst", {}),
    ):
        result = await get_registry().get(name).run(ctx, payload)
        assert result.generation_mode == "deterministic", name


async def test_brand_guardian_blocks_unverifiable_claims(session, brand_row):
    ctx = AgentContext(session=session, brand=brand_row)
    result = await get_registry().get("brand_guardian").run(
        ctx,
        {
            "platform": "linkedin",
            "body": "We are trusted by 5000 developers and our IDE is 10x faster. "
            "Guaranteed results, revolutionary workflow.",
            "headline": "Best IDE",
        },
    )
    assert result.verdict == "blocked"
    rules = {v.rule for v in result.violations}
    assert "unverifiable_claim" in rules
    assert "prohibited_phrase" in rules
    assert result.score < 1.0


async def test_brand_guardian_enforces_platform_length(session, brand_row):
    ctx = AgentContext(session=session, brand=brand_row)
    result = await get_registry().get("brand_guardian").run(
        ctx, {"platform": "x", "body": "a" * 1200, "call_to_action": "Follow"}
    )
    assert result.verdict == "blocked"
    assert any(v.rule == "platform_length" for v in result.violations)


async def test_failing_agent_retries_then_marks_run_failed(session, brand_row):
    class ExplodingAgent(BaseAgent[AgentPayload, AgentResult]):
        name = "exploding"
        max_attempts = 2
        retry_backoff_seconds = 0

        async def execute(self, ctx, payload):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    agent = ExplodingAgent()
    ctx = AgentContext(session=session, brand=brand_row)
    with pytest.raises(AgentError):
        await agent.run(ctx, {})
    await session.commit()
    assert ctx.run is not None
    assert ctx.run.status == AgentRunStatus.FAILED.value
    assert ctx.run.attempts == 2
    assert ctx.run.error_message


async def test_agent_run_endpoint(client, auth, brand):
    resp = await client.post(
        "/api/v1/agents/research/run",
        json={"payload": {"request": "audience"}},
        headers=auth("marketer"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["output"]["generation_mode"] == "deterministic"
    assert body["run_id"]

    viewer = await client.post(
        "/api/v1/agents/research/run", json={"payload": {}}, headers=auth("viewer")
    )
    assert viewer.status_code == 403

    unknown = await client.post(
        "/api/v1/agents/nope/run", json={"payload": {}}, headers=auth("marketer")
    )
    assert unknown.status_code == 404


async def test_agent_specs_endpoint_matches_registry(client, auth):
    resp = await client.get("/api/v1/agents", headers=auth("viewer"))
    assert resp.status_code == 200
    assert {a["name"] for a in resp.json()} == set(get_registry().names())
