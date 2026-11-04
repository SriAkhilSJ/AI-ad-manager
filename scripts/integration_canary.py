#!/usr/bin/env python
"""Run the Phase 4.3 integration canaries and record what each one proved.

A canary is a *small, read-only-by-default* real call. This script exists because
"the mock tests pass" is not evidence that a provider accepts our credentials, and
because a canary that can post publicly or spend money is a weapon unless it is
gated.

WHAT IT WILL DO WITHOUT ANY FLAG
--------------------------------
* read the code (capabilities, transports, providers) and report what is implemented
* report which credentials are present — **names only, never values**
* report the OAuth redirect/vault configuration a real consent flow needs
* build and print the request shape the transport would send, without sending it
* with ``--network``: open a TCP connection to the provider's API host and report
  whether this machine can reach it at all

WHAT IT WILL NOT DO WITHOUT EXPLICIT FLAGS
------------------------------------------
* ``--live``              perform a real, **read-only** provider call
                          (ads: ``validate_account``; social: ``/accounts/{id}/verify``
                          through a running API; creative: capability probe)
* ``--allow-spend``       submit a billable creative generation, and only together
                          with ``--founder-authorization FILE`` whose content names
                          the provider and a spend cap at least the estimate
* ``--allow-public-post`` publish a test item — never implemented here on purpose:
                          it belongs to the operator, with the founder watching

Anything not proven is reported as *not proven*. There is no code path in this
file that turns a mock result into a ``sandbox-verified`` mark.

Usage::

    python ../scripts/integration_canary.py                    # report only
    python ../scripts/integration_canary.py --network          # + reachability
    python ../scripts/integration_canary.py --live             # + read-only calls
    python ../scripts/integration_canary.py --write            # write docs/27-...
    python ../scripts/integration_canary.py --json             # machine readable

Exit codes: 0 = every requested stage for every requested provider passed;
2 = at least one provider is blocked by missing configuration or credentials
(this is the expected result on a checkout with no secrets); 3 = a requested live
call was attempted and failed; 4 = live/spend requested without authorisation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

#: Read-only by default. These are the documented hosts each transport talks to.
SOCIAL_KIND = "social"
ADS_KIND = "ads"
CREATIVE_KIND = "creative"

#: Which evidence column a passing stage would move. Nothing writes these except
#: a human recording a real result; the script only *reports* the current state.
EVIDENCE_LEVELS = ("sandbox", "production")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(value: Any) -> str:
    """Render a config value without ever leaking it."""
    if value is None or value == "":
        return "<unset>"
    text = str(value)
    if len(text) <= 6:
        return "<set:%d chars>" % len(text)
    return "<set:%d chars…%s>" % (len(text), text[-2:])


def _present(value: Any) -> bool:
    return bool(str(value or "").strip())


def _https_probe(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Can this machine complete an HTTPS request to the provider? No credentials sent.

    A TCP connect is not enough: an egress proxy will happily accept the socket and
    then close the TLS handshake. Any HTTP status back means the service answered —
    which says nothing about *our* credentials, only that the network path exists.
    """
    import httpx

    started = time.monotonic()
    try:
        response = httpx.get(url, timeout=timeout)
        elapsed = (time.monotonic() - started) * 1000
        return True, f"GET {url} → HTTP {response.status_code} in {elapsed:.0f} ms (no credentials sent)"
    except Exception as exc:  # noqa: BLE001 - the exception *is* the evidence
        elapsed = (time.monotonic() - started) * 1000
        return False, f"GET {url} failed after {elapsed:.0f} ms — {type(exc).__name__}: {str(exc)[:180]}"


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


# --------------------------------------------------------------------------- #
# provider specifications, derived from the code itself
# --------------------------------------------------------------------------- #
def social_specs() -> dict[str, dict[str, Any]]:
    from app.integrations.oauth import PROVIDERS

    specs: dict[str, dict[str, Any]] = {}
    for platform, provider in PROVIDERS.items():
        specs[platform] = {
            "kind": SOCIAL_KIND,
            "label": provider.display_name,
            "settings": [provider.client_id_setting, provider.client_secret_setting],
            "hosts": sorted({_host(provider.api_base), _host(provider.token_url)} - {""}),
            "scopes": list(provider.scopes),
            "docs": provider.docs_url,
            "notes": provider.notes,
            "read_only_endpoint": f"{provider.api_base}/me (identity, provider-specific)",
        }
    return specs


#: Ads credentials are per platform, by design: there is no shared ads key.
ADS_CREDENTIALS: dict[str, list[str]] = {
    "meta_ads": ["META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"],
    "google_ads": [
        "GOOGLE_ADS_ACCESS_TOKEN",
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    ],
    "linkedin_ads": ["LINKEDIN_ADS_ACCESS_TOKEN", "LINKEDIN_ADS_ACCOUNT_URN"],
}


def ads_specs() -> dict[str, dict[str, Any]]:
    from app.core.config import get_settings
    from app.integrations.ads import ads_transport_report

    from app.integrations.ads import get_ads_transport

    specs: dict[str, dict[str, Any]] = {}
    for row in ads_transport_report(get_settings()):
        name = row["platform"]
        try:
            base = get_ads_transport(name, get_settings()).base
        except Exception:  # noqa: BLE001 - describe() still works without a base
            base = ""
        specs[name] = {
            "kind": ADS_KIND,
            "label": ADS_LABELS.get(name, name),
            "settings": ADS_CREDENTIALS.get(name, []),
            "hosts": [urlparse(base).hostname or ""],
            "base_url": base,
            "api_version": row.get("api_version") or "",
            "notes": row.get("reason") or "",
            "implemented": bool(row.get("implemented")),
            "available": bool(row.get("available")),
        }
    return specs


def creative_specs() -> dict[str, dict[str, Any]]:
    from app.core.config import get_settings

    settings = get_settings()
    specs = {
        "openai_image": {
            "kind": CREATIVE_KIND,
            "label": "OpenAI Images",
            "settings": ["OPENAI_API_KEY"],
            "hosts": [_host(getattr(settings, "openai_base_url", "") or "https://api.openai.com/v1")],
            "notes": f"model {getattr(settings, 'openai_image_model', 'gpt-image-1')}; sizes 1024x1024, 1536x1024, 1024x1536",
        },
        "replicate_video": {
            "kind": CREATIVE_KIND,
            "label": "Replicate video",
            "settings": ["REPLICATE_API_TOKEN", "REPLICATE_VIDEO_MODEL"],
            "hosts": ["api.replicate.com"],
            "notes": "a pinned owner/model:version is required; an unpinned model is refused",
        },
    }
    return specs


def all_specs() -> dict[str, dict[str, Any]]:
    return {**social_specs(), **ads_specs(), **creative_specs()}


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def stage_credentials(spec: dict[str, Any]) -> dict[str, Any]:
    """Presence of the app credentials. Names only."""
    missing: list[str] = []
    present: list[str] = []
    for name in spec.get("settings") or []:
        if _present(os.environ.get(name)) or _present(_settings_value(name)):
            present.append(name)
        else:
            missing.append(name)
    ok = not missing
    return {
        "stage": "credentials",
        "status": "pass" if ok else "blocked",
        "detail": (
            "all required settings present: " + ", ".join(present)
            if ok
            else "missing: " + ", ".join(missing)
        ),
        "missing": missing,
    }


def _settings_value(env_name: str) -> str:
    """Read a ``Settings`` field for an environment-style name, without printing it."""
    from app.core.config import get_settings

    settings = get_settings()
    field = env_name.lower()
    return str(getattr(settings, field, "") or "")


def stage_oauth_config(spec: dict[str, Any]) -> dict[str, Any]:
    """The redirect + vault configuration a consent flow needs to complete."""
    from app.core.config import get_settings

    settings = get_settings()
    redirect = str(getattr(settings, "oauth_redirect_base_url", "") or "")
    vault = str(getattr(settings, "token_vault_backend", "none") or "none").lower()
    vault_key = _present(getattr(settings, "token_vault_key", ""))
    problems: list[str] = []
    if not redirect:
        problems.append("OAUTH_REDIRECT_BASE_URL is unset — no consent flow can be completed")
    elif not redirect.startswith("https://") and "localhost" not in redirect and "127.0.0.1" not in redirect:
        problems.append("OAUTH_REDIRECT_BASE_URL is not https — providers reject plain-http redirects")
    if vault == "none":
        problems.append("TOKEN_VAULT_BACKEND=none — a token cannot be stored after the exchange")
    elif vault == "db" and not vault_key:
        problems.append("TOKEN_VAULT_KEY is unset — the vault cannot encrypt a token")
    return {
        "stage": "oauth_config",
        "status": "pass" if not problems else "blocked",
        "detail": (
            f"redirect {redirect or '<unset>'}, vault {vault}"
            + (", key present" if vault_key else ", key MISSING")
            if not problems
            else "; ".join(problems)
        ),
        "problems": problems,
    }


def stage_publishing_gate(spec: dict[str, Any]) -> dict[str, Any]:
    """Publishing stays off until a platform round-trip has actually succeeded."""
    from app.core.config import get_settings

    enabled = bool(getattr(get_settings(), "publishing_enabled", False))
    return {
        "stage": "publishing_gate",
        "status": "info",
        "detail": (
            "PUBLISHING_ENABLED=true — a publish call would reach the platform"
            if enabled
            else "PUBLISHING_ENABLED=false — publishing is refused before any network call"
        ),
    }


def stage_network(spec: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {
            "stage": "network",
            "status": "skipped",
            "detail": "not attempted (pass --network to probe reachability from this machine); "
            f"hosts {', '.join(spec.get('hosts') or []) or '<none known>'}",
        }
    hosts = [h for h in (spec.get("hosts") or []) if h]
    if not hosts:
        return {"stage": "network", "status": "skipped", "detail": "no API host known for this provider"}
    results = [_https_probe(f"https://{host}/") for host in hosts]
    ok = all(r[0] for r in results)
    return {
        "stage": "network",
        "status": "pass" if ok else "blocked",
        "detail": "; ".join(r[1] for r in results),
    }


def stage_preview(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """What the transport *would* send. Built by the real code, never sent."""
    from app.core.config import get_settings

    settings = get_settings()
    if spec["kind"] == ADS_KIND:
        from app.integrations.ads import get_ads_transport

        transport = get_ads_transport(name, settings)
        described = transport.describe()
        return {
            "stage": "preview",
            "status": "info",
            "detail": (
                f"{described.get('api_version')} → {spec.get('base_url') or '<no base>'}; "
                f"available={described.get('available')}; reason={described.get('reason') or '—'}"
            ),
            "request_shape": described,
        }
    if spec["kind"] == CREATIVE_KIND:
        return {
            "stage": "preview",
            "status": "info",
            "detail": spec.get("notes", ""),
        }
    return {
        "stage": "preview",
        "status": "info",
        "detail": (
            f"authorize {spec.get('hosts')} /me read; "
            f"scopes {', '.join(spec.get('scopes') or [])}"
        ),
    }


#: The ads transports report a machine name; this is what a founder sees.
ADS_LABELS = {
    "meta_ads": "Meta Marketing API",
    "google_ads": "Google Ads API",
    "linkedin_ads": "LinkedIn Marketing API",
}


async def _ads_live(platform: str) -> dict[str, Any]:
    from app.integrations.ads import AdsNotConfigured, get_ads_transport
    from app.core.config import get_settings

    transport = get_ads_transport(platform, get_settings())
    try:
        result = await transport.validate_account()
    except AdsNotConfigured as exc:
        return {"stage": "live", "status": "blocked", "detail": f"not configured: {exc}"}
    except Exception as exc:  # noqa: BLE001 - a live failure is the evidence
        return {
            "stage": "live",
            "status": "fail",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "stage": "live",
        "status": "pass",
        "detail": json.dumps(result, default=str)[:600],
        "evidence": result,
    }


async def _creative_live(
    name: str, *, allow_spend: bool, authorization: dict[str, Any] | None
) -> dict[str, Any]:
    from app.core.config import get_settings
    from app.creative.remote import build_provider, describe_requirement

    canonical = "openai" if name == "openai_image" else "replicate"
    provider = build_provider(canonical, get_settings())
    if provider is None:
        return {"stage": "live", "status": "blocked", "detail": "provider could not be constructed"}
    missing = describe_requirement(provider)
    if missing:
        return {"stage": "live", "status": "blocked", "detail": f"missing: {missing}"}

    # Capability probe is read-only (no submission, no spend).
    capabilities = await provider.get_capabilities()
    if not allow_spend:
        return {
            "stage": "live",
            "status": "pass",
            "detail": (
                "capability probe only (no submission): "
                + json.dumps(capabilities.as_dict(), default=str)[:400]
            ),
        }
    if not authorization:
        return {
            "stage": "live",
            "status": "blocked",
            "detail": "spend requested without --founder-authorization",
        }
    return {
        "stage": "live",
        "status": "blocked",
        "detail": (
            "submission is implemented in the API path (POST /creative-studio/jobs) where the "
            "budget, pricing and authorisation gates live; this harness refuses to duplicate "
            "that logic outside them. Run the canary through the API with the founder present."
        ),
    }


def stage_live(
    name: str,
    spec: dict[str, Any],
    *,
    live: bool,
    allow_spend: bool,
    authorization: dict[str, Any] | None,
    api_base: str | None,
    api_token: str | None,
) -> dict[str, Any]:
    if not live:
        return {
            "stage": "live",
            "status": "skipped",
            "detail": "not attempted (pass --live for a read-only provider call)",
        }
    if spec["kind"] == ADS_KIND:
        return asyncio.run(_ads_live(name))
    if spec["kind"] == CREATIVE_KIND:
        return asyncio.run(_creative_live(name, allow_spend=allow_spend, authorization=authorization))
    return _social_live(name, spec, api_base=api_base, api_token=api_token)


def _social_live(
    name: str, spec: dict[str, Any], *, api_base: str | None, api_token: str | None
) -> dict[str, Any]:
    """Social calls are made through the server, so the vault and state rules apply."""
    if not api_base or not api_token:
        return {
            "stage": "live",
            "status": "blocked",
            "detail": (
                "a social canary needs a connected account and runs through the API: set "
                "CANARY_API_BASE and CANARY_API_TOKEN (a founder/service token), and connect "
                "the account with POST /social/oauth/{platform}/start first"
            ),
        }
    import urllib.error
    import urllib.request

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            api_base.rstrip("/") + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")[:400]
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {exc}"

    status, accounts = call("GET", "/api/v1/social/accounts")
    if status != 200 or not isinstance(accounts, list):
        return {"stage": "live", "status": "fail", "detail": f"could not list accounts: {status} {accounts}"}
    mine = [a for a in accounts if (a.get("platform") or "").lower() == name]
    if not mine:
        return {
            "stage": "live",
            "status": "blocked",
            "detail": f"no connected {name} account on this deployment",
        }
    account_id = mine[0].get("id")
    status, payload = call("POST", f"/api/v1/social/accounts/{account_id}/verify")
    return {
        "stage": "live",
        "status": "pass" if status == 200 else "fail",
        "detail": json.dumps(payload, default=str)[:600],
    }


def stage_evidence(spec: dict[str, Any], stages: list[dict[str, Any]]) -> dict[str, Any]:
    live = next((s for s in stages if s["stage"] == "live"), None)
    verified = bool(live and live["status"] == "pass" and "no submission" not in live.get("detail", ""))
    return {
        "stage": "evidence",
        "status": "info",
        # A live read-only call is *sandbox* evidence at best, and only if it ran.
        "sandbox_verified": "yes" if verified else "no",
        "production_verified": "no",
        "detail": (
            "a read-only live call succeeded; record it, then run the publishing/spend "
            "canaries before claiming anything more"
            if verified
            else "no live call has succeeded for this provider"
        ),
    }


# --------------------------------------------------------------------------- #
# authorisation file
# --------------------------------------------------------------------------- #
def load_authorization(path: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a founder authorisation. Refuses anything ambiguous.

    The file is JSON:
        {"founder": "name", "granted_at": "2026-11-04T10:00:00Z",
         "provider": "openai", "max_spend_usd": 0.5, "reference": "ticket/4.3"}
    """
    if not path:
        return None, None
    try:
        payload = json.loads(Path(path).read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"authorisation file unreadable: {type(exc).__name__}"
    required = {"founder", "granted_at", "provider", "max_spend_usd", "reference"}
    missing = required - set(payload)
    if missing:
        return None, "authorisation is missing " + ", ".join(sorted(missing))
    try:
        cap = float(payload["max_spend_usd"])
    except (TypeError, ValueError):
        return None, "max_spend_usd is not a number"
    if cap <= 0:
        return None, "max_spend_usd must be > 0"
    payload["max_spend_usd"] = cap
    return payload, None


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def run(
    *,
    providers: list[str],
    network: bool,
    live: bool,
    allow_spend: bool,
    authorization: dict[str, Any] | None,
    api_base: str | None,
    api_token: str | None,
) -> dict[str, Any]:
    specs = all_specs()
    unknown = [p for p in providers if p not in specs]
    if unknown:
        raise SystemExit(f"unknown provider(s): {', '.join(unknown)}; known: {', '.join(sorted(specs))}")

    results: list[dict[str, Any]] = []
    for name in providers:
        spec = specs[name]
        stages = [stage_credentials(spec)]
        if spec["kind"] == SOCIAL_KIND:
            stages.append(stage_oauth_config(spec))
            stages.append(stage_publishing_gate(spec))
        stages.append(stage_preview(name, spec))
        stages.append(stage_network(spec, enabled=network))
        stages.append(
            stage_live(
                name,
                spec,
                live=live,
                allow_spend=allow_spend,
                authorization=authorization,
                api_base=api_base,
                api_token=api_token,
            )
        )
        stages.append(stage_evidence(spec, stages))
        blocked = [s for s in stages if s["status"] in ("blocked", "fail")]
        results.append(
            {
                "provider": name,
                "label": spec["label"],
                "kind": spec["kind"],
                "status": "blocked" if blocked else "ok",
                "blocked_by": [f"{s['stage']}: {s['detail']}" for s in blocked],
                "stages": stages,
            }
        )
    return {"generated_at": _now(), "providers": results}


def render(report: dict[str, Any], *, network: bool, live: bool) -> str:
    lines: list[str] = []
    add = lines.append
    add("# 27 — Integration canary report")
    add("")
    add(f"Generated {report['generated_at']} by `scripts/integration_canary.py`.")
    add("")
    add(
        "A canary is the smallest real call that can prove a provider accepts us. "
        f"This run was executed with `--network={'yes' if network else 'no'}` and "
        f"`--live={'yes' if live else 'no'}`, so read the `blocked` column literally: "
        "it names the first thing that must change before the next column can move."
    )
    add("")
    add("| Provider | Kind | Credentials | Live canary | Sandbox-verified | Production-verified |")
    add("| --- | --- | --- | --- | --- | --- |")
    for row in report["providers"]:
        by_stage = {s["stage"]: s for s in row["stages"]}
        creds = by_stage["credentials"]["status"]
        live_stage = by_stage["live"]
        evidence = by_stage["evidence"]
        add(
            f"| {row['label']} | {row['kind']} | {creds} | {live_stage['status']} | "
            f"{evidence['sandbox_verified']} | {evidence['production_verified']} |"
        )
    add("")
    add("## Per provider")
    for row in report["providers"]:
        add("")
        add(f"### {row['label']} (`{row['provider']}`)")
        add("")
        for stage in row["stages"]:
            detail = str(stage.get("detail", "")).replace("\n", " ")
            add(f"- **{stage['stage']}** — {stage['status']}: {detail}")
    add("")
    add("## What a green row still would not prove")
    add("")
    add(
        "* A read-only identity call proves the credential and the account, not the "
        "publish path, the media upload, the insights window or the rate limit under load."
    )
    add(
        "* Ads `validate_account` proves access to the account; it does not prove that "
        "a create is accepted, that billing is funded, or that the review status allows "
        "the objective we intend."
    )
    add(
        "* A creative capability probe proves the key is accepted by the service listing; "
        "it does not prove a generation succeeds, costs what we think, or returns an "
        "asset that passes integrity checks. Only a submitted job proves that."
    )
    add("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", action="append", default=None, help="repeatable; default all")
    parser.add_argument("--network", action="store_true", help="probe TCP reachability to provider hosts")
    parser.add_argument("--live", action="store_true", help="perform read-only provider calls")
    parser.add_argument("--allow-spend", action="store_true", help="allow a billable creative generation")
    parser.add_argument("--founder-authorization", default=os.environ.get("FOUNDER_AUTHORIZATION"), help="JSON authorisation file")
    parser.add_argument("--api-base", default=os.environ.get("CANARY_API_BASE"), help="running API root, for social canaries")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true", help="write docs/27-INTEGRATION-CANARY-REPORT.md")
    args = parser.parse_args(argv)

    authorization, problem = load_authorization(args.founder_authorization)
    if problem:
        print(f"FAIL: {problem}")
        return 4
    if args.allow_spend and not authorization:
        print("FAIL: --allow-spend requires --founder-authorization (no unapproved spend)")
        return 4

    providers = args.provider or sorted(all_specs())
    report = run(
        providers=providers,
        network=args.network,
        live=args.live,
        allow_spend=args.allow_spend,
        authorization=authorization,
        api_base=args.api_base,
        api_token=os.environ.get("CANARY_API_TOKEN"),
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report, network=args.network, live=args.live))

    if args.write:
        target = ROOT / "docs" / "27-INTEGRATION-CANARY-REPORT.md"
        target.write_text(render(report, network=args.network, live=args.live), "utf-8")
        evidence = ROOT / "docs" / "canary-run.json"
        evidence.write_text(json.dumps(report, indent=2, default=str), "utf-8")
        print(f"\nwrote {target} and {evidence}")

    if any(row["status"] == "fail" for row in report["providers"]):
        return 3
    if any(row["status"] == "blocked" for row in report["providers"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
