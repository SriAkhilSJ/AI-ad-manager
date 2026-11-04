#!/usr/bin/env python
"""Phase 4.4 preflight: can this deployment actually reach the providers?

Phase 4.3 discovered that provider calls fail from the authoring environment. This
script turns that discovery into a measurement: for every provider it resolves
DNS, opens TCP, completes a *verified* TLS handshake, and makes one credential-free
HTTPS request — and reports exactly where it stopped.

TLS verification is never disabled. If a handshake fails because of a certificate
the deployment does not trust, that is the finding, not something to work around.

It also prints the *names* of the environment variables each integration needs and
whether they are present. Values are never read into the output.

Usage::

    python ../scripts/environment_preflight.py                 # human summary
    python ../scripts/environment_preflight.py --json
    python ../scripts/environment_preflight.py --write         # docs/preflight.json
    python ../scripts/environment_preflight.py --only openai   # one provider

Exit codes: 0 = every checked endpoint is reachable; 2 = at least one is blocked
(the expected result when a network policy is in force); 1 = bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

#: provider -> (hosts to probe, environment variable names it needs).
#: Names only: this file must never learn a value.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI (images / LLM)",
        "hosts": ["api.openai.com"],
        "env": ["OPENAI_API_KEY", "OPENAI_IMAGE_MODEL"],
        "kind": "creative",
    },
    "replicate": {
        "label": "Replicate (async video)",
        "hosts": ["api.replicate.com"],
        "env": ["REPLICATE_API_TOKEN", "REPLICATE_VIDEO_MODEL"],
        "kind": "creative",
    },
    "facebook": {
        "label": "Meta Graph (Facebook / Instagram)",
        "hosts": ["graph.facebook.com", "www.facebook.com"],
        "env": ["META_APP_ID", "META_APP_SECRET"],
        "kind": "social",
    },
    "linkedin": {
        "label": "LinkedIn",
        "hosts": ["api.linkedin.com", "www.linkedin.com"],
        "env": ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"],
        "kind": "social",
    },
    "x": {
        "label": "X",
        "hosts": ["api.twitter.com", "twitter.com"],
        "env": ["X_CLIENT_ID", "X_CLIENT_SECRET"],
        "kind": "social",
    },
    "youtube": {
        "label": "Google / YouTube",
        "hosts": ["oauth2.googleapis.com", "www.googleapis.com"],
        "env": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        "kind": "social",
    },
    "reddit": {
        "label": "Reddit",
        "hosts": ["oauth.reddit.com", "www.reddit.com"],
        "env": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
        "kind": "social",
    },
    "meta_ads": {
        "label": "Meta Marketing API",
        "hosts": ["graph.facebook.com"],
        "env": ["META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"],
        "kind": "ads",
    },
    "google_ads": {
        "label": "Google Ads API",
        "hosts": ["googleads.googleapis.com", "oauth2.googleapis.com"],
        "env": [
            "GOOGLE_ADS_ACCESS_TOKEN",
            "GOOGLE_ADS_DEVELOPER_TOKEN",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        ],
        "kind": "ads",
    },
    "linkedin_ads": {
        "label": "LinkedIn Marketing API",
        "hosts": ["api.linkedin.com"],
        "env": ["LINKEDIN_ADS_ACCESS_TOKEN", "LINKEDIN_ADS_ACCOUNT_URN"],
        "kind": "ads",
    },
}

#: Core settings the app needs before any integration can be activated.
CORE_ENV = [
    "SECRET_KEY",
    "DATABASE_URL",
    "TOKEN_VAULT_BACKEND",
    "TOKEN_VAULT_KEY",
    "OAUTH_REDIRECT_BASE_URL",
    "PUBLISHING_ENABLED",
    "CREATIVE_ALLOW_PAID_GENERATION",
    "CREATIVE_CAMPAIGN_BUDGET_USD",
    "CREATIVE_PRICING_JSON",
    "LLM_PROVIDER",
    "FLOW_LIVE_GENERATION_ENABLED",
]

PROXY_VARS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
]
CA_VARS = ["SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"]

TIMEOUT = 8.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_host(host: str, *, timeout: float = TIMEOUT) -> dict[str, Any]:
    """DNS → TCP → verified TLS → one credential-free HTTPS request."""
    result: dict[str, Any] = {"host": host}
    started = time.monotonic()

    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        result["dns"] = "ok"
        result["addresses"] = sorted({info[4][0] for info in infos})
    except OSError as exc:
        result |= {
            "dns": "fail",
            "dns_error": f"{type(exc).__name__}: {exc}",
            "tcp": "skipped",
            "tls": "skipped",
            "https": "skipped",
            "reachable": False,
            "ms": round((time.monotonic() - started) * 1000, 1),
        }
        return result

    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        result["tcp"] = "ok"
    except OSError as exc:
        result |= {
            "tcp": "fail",
            "tcp_error": f"{type(exc).__name__}: {exc}",
            "tls": "skipped",
            "https": "skipped",
            "reachable": False,
            "ms": round((time.monotonic() - started) * 1000, 1),
        }
        return result

    # Verification is ON. A trusting-but-wrong CA must surface as a failure.
    context = ssl.create_default_context()
    try:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}
            issuer = dict(x[0] for x in cert.get("issuer", []))
            subject = dict(x[0] for x in cert.get("subject", []))
            result |= {
                "tls": "ok",
                "tls_verified": True,
                "cert_issuer": issuer.get("organizationName") or issuer.get("commonName") or "",
                "cert_subject": subject.get("commonName") or "",
                "cert_not_after": cert.get("notAfter", ""),
                "tls_version": tls.version(),
            }
    except ssl.SSLError as exc:
        try:
            raw.close()
        except OSError:
            pass
        result |= {
            "tls": "fail",
            "tls_verified": False,
            "tls_error": f"{type(exc).__name__}: {exc}",
            "https": "skipped",
            "reachable": False,
            "ms": round((time.monotonic() - started) * 1000, 1),
            # A peer that closes the handshake without a certificate is a policy
            # block, not a certificate problem. Distinguish the two.
            "blocked_by": (
                "no TLS response (SNI/policy block or proxy refusal)"
                if isinstance(exc, ssl.SSLZeroReturnError)
                else "certificate or protocol failure"
            ),
        }
        return result

    try:
        import httpx

        response = httpx.get(f"https://{host}/", timeout=timeout)
        result["https"] = f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001 - the exception is the finding
        result["https"] = f"failed: {type(exc).__name__}: {str(exc)[:120]}"

    result["reachable"] = result.get("tls") == "ok"
    result["ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def env_report(names: list[str]) -> dict[str, str]:
    """Presence only. No value is read into the report."""
    out: dict[str, str] = {}
    for name in names:
        raw = os.environ.get(name)
        out[name] = "unset" if raw is None or raw == "" else "set"
    return out


def _settings_env(names: list[str]) -> dict[str, str]:
    """Fall back to Settings, so a .env-driven deployment is reported honestly."""
    from app.core.config import get_settings

    settings = get_settings()
    out: dict[str, str] = {}
    for name in names:
        if os.environ.get(name):
            out[name] = "set"
            continue
        value = getattr(settings, name.lower(), None)
        out[name] = "set" if value not in (None, "", 0, False) else "unset"
    return out


def build(only: list[str] | None = None) -> dict[str, Any]:
    selected = {
        key: spec for key, spec in PROVIDERS.items() if not only or key in only
    }
    if only:
        unknown = [name for name in only if name not in PROVIDERS]
        if unknown:
            raise SystemExit(f"unknown provider(s): {', '.join(unknown)}")

    providers: list[dict[str, Any]] = []
    for key, spec in selected.items():
        hosts = [probe_host(host) for host in spec["hosts"]]
        providers.append(
            {
                "provider": key,
                "label": spec["label"],
                "kind": spec["kind"],
                "hosts": hosts,
                "reachable": all(h.get("reachable") for h in hosts),
                "env": _settings_env(spec["env"]),
                "configured": all(v == "set" for v in _settings_env(spec["env"]).values()),
            }
        )

    proxy = {name: ("set" if os.environ.get(name) else "unset") for name in PROXY_VARS}
    ca = {name: ("set" if os.environ.get(name) else "unset") for name in CA_VARS}
    return {
        "generated_at": _now(),
        "python": sys.version.split()[0],
        "openssl": ssl.OPENSSL_VERSION,
        "clock_utc": _now(),
        "proxy_env": proxy,
        "ca_env": ca,
        "core_env": env_report(CORE_ENV),
        "providers": providers,
        "summary": {
            "providers_checked": len(providers),
            "reachable": sum(1 for p in providers if p["reachable"]),
            "blocked": sum(1 for p in providers if not p["reachable"]),
        },
    }


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Integration preflight")
    add("")
    add(f"Generated {report['generated_at']} · Python {report['python']} · {report['openssl']}")
    add("")
    proxy_set = [name for name, state in report["proxy_env"].items() if state == "set"]
    add(f"Proxy environment: {', '.join(proxy_set) if proxy_set else 'none set'}")
    ca_set = [name for name, state in report["ca_env"].items() if state == "set"]
    add(f"Custom CA variables: {', '.join(ca_set) if ca_set else 'none set (system trust store)'}")
    add("")
    # One row per probed host: a provider with several hosts must not hide a
    # failing one behind whichever host happened to be probed first.
    add("| Provider | Host | DNS | TCP | TLS (verified) | HTTPS | Reachable |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for provider in report["providers"]:
        for host in provider["hosts"]:
            add(
                f"| {provider['label']} | {host['host']} | {host.get('dns', '-')} | "
                f"{host.get('tcp', '-')} | {host.get('tls', '-')} | "
                f"{host.get('https', '-')} | {'yes' if host.get('reachable') else '**no**'} |"
            )
    add("")
    add("## Failure detail")
    add("")
    blocked = 0
    for provider in report["providers"]:
        for host in provider["hosts"]:
            if host.get("reachable"):
                continue
            blocked += 1
            add(
                f"- **{host['host']}** ({host.get('ms')} ms): "
                f"{host.get('dns_error') or host.get('tcp_error') or host.get('tls_error')}"
                + (f" — {host['blocked_by']}" if host.get("blocked_by") else "")
            )
    if not blocked:
        add("None — every probed host completed a verified TLS handshake.")
    add("")
    add("## Environment variables (names only)")
    add("")
    for provider in report["providers"]:
        state = ", ".join(f"`{name}` {value}" for name, value in provider["env"].items())
        add(f"- {provider['label']}: {state}")
    add("")
    add("## Core settings")
    add("")
    for name, state in report["core_env"].items():
        add(f"- `{name}`: {state}")
    add("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", help="restrict to one provider (repeatable)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true", help="write docs/preflight.json")
    args = parser.parse_args(argv)

    report = build(args.only)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    if args.write:
        target = ROOT / "docs" / "preflight.json"
        target.write_text(json.dumps(report, indent=2, default=str), "utf-8")
        print(f"wrote {target}")
    return 0 if report["summary"]["blocked"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
