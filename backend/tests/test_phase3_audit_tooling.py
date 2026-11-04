"""Guard tests for the Phase 3 audit tooling.

These do not test inference — there is no inference to test here. They test the
*safety properties* the audit claims:

1. the preflight makes no network call and cannot invoke a model;
2. the canary refuses to run without explicit authorization;
3. the canary refuses to run while any price is unverified;
4. neither CLI is reachable from the running application.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from app.cli import canary, preflight


@pytest.fixture()
def settings(monkeypatch):
    """Clean settings with no credentials and unverified model prices."""
    from app.core.config import Settings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return Settings(
        llm_reasoning_provider="openrouter",
        llm_reasoning_model="deepseek-v4.1-flash",
        llm_auxiliary_provider="openrouter",
        llm_auxiliary_model="qwen-3.7-flash",
    )


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def test_preflight_offline_checks_never_touch_the_network(settings, monkeypatch):
    """Offline mode must not construct an HTTP client at all."""
    import httpx

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("preflight offline mode must not use the network")

    monkeypatch.setattr(httpx, "AsyncClient", explode)
    results = preflight.run_offline_checks(settings)

    assert results, "preflight produced no checks"
    ids = {c.id for c in results}
    assert "config.valid" in ids
    assert "credentials.present" in ids
    assert "accounting.pricing" in ids


def test_preflight_reports_missing_credentials_as_failure(settings):
    results = {c.id: c for c in preflight.run_offline_checks(settings)}
    assert results["credentials.present"].status == preflight.FAIL
    assert results["credentials.tier_match"].status == preflight.FAIL
    # evidence names each variable with its state, never a value
    evidence = results["credentials.present"].evidence
    assert all(state == "absent" for state in evidence.values())


def test_preflight_confirms_configurable_base_url_after_phase_2_5(settings):
    """Phase 2.5 fixed the hardcoded-base-URL defect; the check must now pass.

    This test previously asserted FAIL. It was updated only because the defect it
    pinned was actually repaired — not to make a failure go away.
    """
    results = {c.id: c for c in preflight.run_offline_checks(settings)}
    assert results["endpoint.configurable"].status == preflight.PASS
    assert "VELONA_BASE_URL" in results["endpoint.configurable"].detail


def test_preflight_flags_unverified_pricing(settings):
    results = {c.id: c for c in preflight.run_offline_checks(settings)}
    check = results["accounting.pricing"]
    assert check.status == preflight.FAIL
    assert check.evidence["reasoning"]["verified"] is False
    assert check.evidence["auxiliary"]["verified"] is False


def test_preflight_module_has_no_inference_path():
    """A static guarantee: the module never calls a model."""
    from pathlib import Path

    source = Path(preflight.__file__).read_text()
    # No call into the gateway's inference entrypoint anywhere in the module.
    assert ".structured(" not in source
    assert "LLMGateway(session=" in source  # it only inspects availability


def test_preflight_rejects_an_inference_flag():
    """There is no flag that could turn the preflight into a paid call."""
    with pytest.raises(SystemExit):
        preflight.main(["--generate"])


def test_preflight_secret_redaction_never_returns_the_value():
    key = "sk-super-secret-value-that-must-not-print"
    rendered = preflight._secret_state(key)
    assert key not in rendered
    assert "len=" in rendered


# --------------------------------------------------------------------------- #
# canary
# --------------------------------------------------------------------------- #
def test_canary_plan_blocks_while_prices_are_unverified(settings):
    plan = canary.plan(settings)
    assert plan["request_count"] == 2
    assert plan["max_output_tokens_per_request"] == 100
    assert plan["tools"] is False
    assert plan["agent_loops"] is False
    assert plan["research_retrieval"] is False
    assert plan["retries"] == 0
    assert plan["unverified_priced_models"], "placeholder prices must be flagged"
    assert plan["blocking_reason"]
    assert plan["authorization_required"] is True


def test_canary_stays_within_its_own_ceiling(settings):
    plan = canary.plan(settings)
    assert plan["estimated_total_usd"] <= canary.CANARY_CEILING_USD
    assert plan["within_ceiling"] is True


def test_canary_refuses_without_authorization_and_makes_no_call(settings, monkeypatch):
    """The default path must print a plan and return, with zero network activity."""
    import app.core.config as config_module

    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    async def explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("canary must not execute without authorization")

    monkeypatch.setattr(canary, "_execute", explode)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = canary.main([])

    assert code == 0, "an unauthorized invocation is a no-op, not an error"
    output = buffer.getvalue()
    assert "not executed" in output.lower()


async def test_canary_execution_refuses_unverified_pricing(settings):
    """Even with authorization, an unverified price must stop it before any request.

    Declared async on purpose: ``asyncio_mode = auto`` gives this test its own
    loop. Calling ``asyncio.run()`` from a sync test would close the default
    event loop and break every later async test in the session.
    """
    code = await canary._execute(settings, canary.CANARY_CEILING_USD)
    assert code == 2, "must refuse, not attempt a paid request"


def test_canary_is_not_wired_into_the_application():
    """No API route or agent may reach paid inference through the canary."""
    from pathlib import Path

    app_root = Path(canary.__file__).resolve().parents[1]  # backend/app
    offenders = []
    for path in app_root.rglob("*.py"):
        if "cli" in path.parts:
            continue
        text = path.read_text()
        if "app.cli.canary" in text or "from app.cli import canary" in text:
            offenders.append(str(path))
    assert not offenders, f"canary is reachable from: {offenders}"
