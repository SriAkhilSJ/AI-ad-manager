"""Phase 4.3: the canary harness must refuse, redact, and never spend on its own.

There are no provider credentials in CI, so the canaries themselves stop at
"blocked". What *can* be proven here is the part that decides whether a canary is
allowed to run at all — which is the part that would hurt if it were wrong.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "integration_canary.py"


def _load():
    spec = importlib.util.spec_from_file_location("integration_canary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["integration_canary"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def canary():
    return _load()


# ---------------------------------------------------------------- redaction
def test_redact_never_returns_the_value(canary):
    secret = "sk-live-0123456789abcdefghijklmnop"
    rendered = canary.redact(secret)
    assert secret not in rendered
    assert "set:" in rendered


def test_redact_handles_unset_and_short_values(canary):
    assert canary.redact(None) == "<unset>"
    assert canary.redact("") == "<unset>"
    assert canary.redact("abc") == "<set:3 chars>"


# ----------------------------------------------------- authorisation parsing
def test_authorization_missing_fields_is_refused(canary, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"founder": "F", "provider": "openai"}))
    payload, problem = canary.load_authorization(str(path))
    assert payload is None
    assert "missing" in (problem or "")


def test_authorization_zero_cap_is_refused(canary, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "founder": "F",
                "granted_at": "2026-11-04T00:00:00Z",
                "provider": "openai",
                "max_spend_usd": 0,
                "reference": "ticket",
            }
        )
    )
    payload, problem = canary.load_authorization(str(path))
    assert payload is None
    assert "> 0" in (problem or "")


def test_authorization_complete_is_accepted(canary, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "founder": "Founder",
                "granted_at": "2026-11-04T00:00:00Z",
                "provider": "openai",
                "max_spend_usd": 0.5,
                "reference": "ticket/4.3",
            }
        )
    )
    payload, problem = canary.load_authorization(str(path))
    assert problem is None
    assert payload and payload["max_spend_usd"] == 0.5


# ------------------------------------------------------------- spend gating
def test_allow_spend_without_authorization_exits_4(canary, monkeypatch):
    monkeypatch.delenv("FOUNDER_AUTHORIZATION", raising=False)
    assert canary.main(["--allow-spend", "--provider", "openai_image"]) == 4


def test_invalid_authorization_exits_4(canary, tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDER_AUTHORIZATION", raising=False)
    path = tmp_path / "auth.json"
    path.write_text("{}")
    assert (
        canary.main(["--allow-spend", "--founder-authorization", str(path), "--provider", "openai_image"])
        == 4
    )


def test_no_live_call_happens_without_flags(canary):
    report = canary.run(
        providers=["openai_image"],
        network=False,
        live=False,
        allow_spend=False,
        authorization=None,
        api_base=None,
        api_token=None,
    )
    stages = {s["stage"]: s for s in report["providers"][0]["stages"]}
    assert stages["live"]["status"] == "skipped"
    assert stages["network"]["status"] == "skipped"
    assert stages["evidence"]["sandbox_verified"] == "no"
    assert stages["evidence"]["production_verified"] == "no"


# ------------------------------------------------------------- credentials
def test_missing_credentials_are_named_not_valued(canary, monkeypatch):
    for name in ("OPENAI_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    spec = {"kind": "creative", "settings": ["OPENAI_API_KEY"]}
    stage = canary.stage_credentials(spec)
    assert stage["status"] == "blocked"
    assert "OPENAI_API_KEY" in stage["missing"]


def test_present_credential_is_not_echoed(canary, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value-that-must-not-appear")
    spec = {"kind": "creative", "settings": ["OPENAI_API_KEY"]}
    stage = canary.stage_credentials(spec)
    assert stage["status"] == "pass"
    assert "sk-test-value-that-must-not-appear" not in json.dumps(stage)


# ------------------------------------------------------------ network probe
def test_https_probe_reports_a_transport_failure(canary, monkeypatch):
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("TLS/SSL connection has been closed (EOF)")

    monkeypatch.setattr(httpx, "get", boom)
    ok, detail = canary._https_probe("https://api.openai.com/")
    assert ok is False
    assert "ConnectError" in detail


def test_https_probe_accepts_an_error_status_as_reachable(canary, monkeypatch):
    import httpx

    class Response:
        status_code = 401

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())
    ok, detail = canary._https_probe("https://api.openai.com/")
    assert ok is True
    assert "401" in detail


# ---------------------------------------------------------- report plumbing
def test_report_never_upgrades_evidence_without_a_live_call(canary):
    report = canary.run(
        providers=sorted(canary.all_specs()),
        network=False,
        live=False,
        allow_spend=False,
        authorization=None,
        api_base=None,
        api_token=None,
    )
    for row in report["providers"]:
        stages = {s["stage"]: s for s in row["stages"]}
        assert stages["evidence"]["sandbox_verified"] == "no"
        assert stages["evidence"]["production_verified"] == "no"


def test_unknown_provider_is_rejected(canary):
    with pytest.raises(SystemExit):
        canary.main(["--provider", "not-a-provider"])


def test_specs_come_from_the_code(canary):
    specs = canary.all_specs()
    from app.integrations.oauth import PROVIDERS

    for platform in PROVIDERS:
        assert platform in specs
    for platform in ("meta_ads", "google_ads", "linkedin_ads"):
        assert platform in specs
    assert "openai_image" in specs and "replicate_video" in specs


# ------------------------------------------------------- lifecycle evidence
def test_lifecycle_evidence_refuses_to_run_without_credentials(monkeypatch):
    """The evidence runner takes credentials from the environment, never argv."""
    import importlib.util

    script = REPO_ROOT / "scripts" / "lifecycle_evidence.py"
    spec = importlib.util.spec_from_file_location("lifecycle_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.delenv("LIFECYCLE_EMAIL", raising=False)
    monkeypatch.delenv("LIFECYCLE_PASSWORD", raising=False)
    assert module.main([]) == 2
