"""Phase 4.4: the preflight and provisioning tools must be honest and safe.

There is no network and no credential in CI, so what is proven here is the part
that matters: the tools cannot print a secret, cannot disable TLS verification,
cannot write key material into the repository, and cannot report a shape that is
wrong as if it were fine.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preflight():
    return _load("environment_preflight", "environment_preflight.py")


@pytest.fixture(scope="module")
def provision():
    return _load("provision_credentials", "provision_credentials.py")


# ------------------------------------------------------------------ preflight
def test_env_report_never_returns_values(preflight, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-appear-anywhere")
    report = preflight.env_report(["OPENAI_API_KEY", "REPLICATE_API_TOKEN"])
    assert report == {"OPENAI_API_KEY": "set", "REPLICATE_API_TOKEN": "unset"}
    assert "sk-must-not-appear-anywhere" not in json.dumps(report)


def test_probe_reports_dns_failure_structurally(preflight, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("Name or service not known")

    monkeypatch.setattr(preflight.socket, "getaddrinfo", boom)
    result = preflight.probe_host("api.openai.com")
    assert result["dns"] == "fail"
    assert result["reachable"] is False
    assert "Name or service not known" in result["dns_error"]


def test_probe_distinguishes_a_policy_block_from_a_bad_certificate(preflight, monkeypatch):
    class FakeRaw:
        def close(self):
            pass

    class FakeContext:
        def wrap_socket(self, raw, server_hostname=None):
            import ssl

            raise ssl.SSLZeroReturnError("TLS/SSL connection has been closed (EOF)")

    monkeypatch.setattr(preflight.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("1.2.3.4", 443))])
    monkeypatch.setattr(preflight.socket, "create_connection", lambda *a, **k: FakeRaw())
    monkeypatch.setattr(preflight.ssl, "create_default_context", lambda *a, **k: FakeContext())

    result = preflight.probe_host("api.openai.com")
    assert result["tls"] == "fail"
    assert result["reachable"] is False
    assert "SNI" in result["blocked_by"]


def test_preflight_never_disables_tls_verification():
    """A static guard: the script may only use a verifying default context."""
    source = (REPO_ROOT / "scripts" / "environment_preflight.py").read_text()
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
    assert "create_default_context" in source


def test_unknown_provider_is_rejected(preflight):
    with pytest.raises(SystemExit):
        preflight.build(["not-a-provider"])


def test_render_names_every_probed_host_and_its_failure(monkeypatch, preflight):
    """Rendering is asserted against a synthetic probe, never the live network:
    a suite that only passes where egress is blocked is not a test."""
    failing = {
        "host": "api.openai.com", "dns": "ok", "tcp": "ok", "tls": "fail",
        "reachable": False, "ms": 31,
        "tls_error": "SSLZeroReturnError: TLS/SSL connection has been closed (EOF)",
        "blocked_by": "no TLS response (SNI/policy block or proxy refusal)",
    }
    monkeypatch.setattr(preflight, "probe_host", lambda host: dict(failing, host=host))
    text = preflight.render(preflight.build(["openai"]))
    assert "Reachable" in text
    assert "api.openai.com" in text
    assert "SSLZeroReturnError" in text
    assert "**" in text  # the verdict is visually marked as blocked


def test_render_says_so_when_nothing_is_blocked(monkeypatch, preflight):
    passing = {"host": "api.openai.com", "dns": "ok", "tcp": "ok", "tls": "ok",
               "https": "200", "reachable": True}
    monkeypatch.setattr(preflight, "probe_host", lambda host: dict(passing, host=host))
    text = preflight.render(preflight.build(["openai"]))
    assert "None — every probed host completed a verified TLS handshake." in text


def test_render_shows_each_host_of_a_multi_host_provider(monkeypatch, preflight):
    def probe(host):
        return {"host": host, "dns": "ok", "tcp": "ok", "tls": "ok", "https": "200",
                "reachable": True}

    monkeypatch.setattr(preflight, "probe_host", probe)
    text = preflight.render(preflight.build(["facebook"]))
    assert "graph.facebook.com" in text and "www.facebook.com" in text


# --------------------------------------------------------------- provisioning
def test_keygen_refuses_to_write_inside_the_repository(provision):
    with pytest.raises(SystemExit):
        provision.write_key_file(REPO_ROOT / "vault.key")


def test_keygen_writes_a_0600_file_with_a_valid_key(provision, tmp_path, capsys):
    target = tmp_path / "vault.key"
    path = provision.write_key_file(target)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    content = path.read_text().strip()
    assert content.startswith("TOKEN_VAULT_KEY=")
    key = content.split("=", 1)[1]
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    # the value must never be printed
    assert key not in capsys.readouterr().out


def test_keygen_refuses_to_overwrite(provision, tmp_path):
    target = tmp_path / "vault.key"
    provision.write_key_file(target)
    with pytest.raises(SystemExit):
        provision.write_key_file(target)


def test_vault_key_shape_is_validated(provision):
    problems = provision.shape_problems({"TOKEN_VAULT_KEY": "abc123", "SECRET_KEY": "short"})
    assert "TOKEN_VAULT_KEY" in problems
    assert "SECRET_KEY" in problems


def test_redirect_must_be_https(provision):
    problems = provision.shape_problems({"OAUTH_REDIRECT_BASE_URL": "http://example.com/cb"})
    assert "OAUTH_REDIRECT_BASE_URL" in problems


def test_video_model_must_be_pinned(provision):
    problems = provision.shape_problems({"REPLICATE_VIDEO_MODEL": "owner/model"})
    assert "REPLICATE_VIDEO_MODEL" in problems
    assert not provision.shape_problems({"REPLICATE_VIDEO_MODEL": "owner/model:abc123"})


def test_ads_identifiers_are_validated(provision):
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" in provision.shape_problems(
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "12345"}
    )
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" not in provision.shape_problems(
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "123-456-7890"}
    )
    assert "LINKEDIN_ADS_ACCOUNT_URN" in provision.shape_problems(
        {"LINKEDIN_ADS_ACCOUNT_URN": "123456"}
    )


def test_vault_backend_db_requires_a_key(provision):
    problems = provision.shape_problems({"TOKEN_VAULT_BACKEND": "db"})
    assert problems.get("TOKEN_VAULT_KEY")


def test_check_reports_nothing_ready_on_an_empty_environment(provision):
    report = provision.check({})
    assert report["ready_for_first_social_canary"] is False
    assert report["ready_for_first_creative_canary"] is False
    assert "none" in provision.render(report)


def test_check_is_ready_when_the_required_shapes_are_present(provision, tmp_path):
    env = {
        "SECRET_KEY": "x" * 40,
        "DATABASE_URL": "postgresql+asyncpg://user@host/db",
        "TOKEN_VAULT_BACKEND": "db",
        "TOKEN_VAULT_KEY": provision.generate_key(),
        "OAUTH_REDIRECT_BASE_URL": "https://app.example.com/api/v1",
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
        "CREATIVE_IMAGE_PROVIDER": "openai",
        "CREATIVE_PRICING_JSON": json.dumps({"image": 0.04}),
        "CREATIVE_CAMPAIGN_BUDGET_USD": "1.0",
        "CREATIVE_ALLOW_PAID_GENERATION": "true",
        "PUBLISHING_ENABLED": "false",
        "LINKEDIN_CLIENT_ID": "id",
        "LINKEDIN_CLIENT_SECRET": "secret",
    }
    report = provision.check(env)
    assert report["ready_for_first_creative_canary"] is True
    assert report["ready_for_first_social_canary"] is True
    assert "social:linkedin" in report["social_platforms_ready"]
    # values are never echoed into the report
    assert "sk-test" not in json.dumps(report)
