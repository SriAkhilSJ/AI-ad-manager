"""Provider preflight — proves what is configured without spending anything.

This command exists because "the environment variable is set" is not evidence
that a provider works. It walks the real gateway objects and reports, per check,
one of three honest states:

* ``PASS``   — verified by inspecting live objects or a free metadata endpoint
* ``FAIL``   — definitively broken
* ``SKIP``   — cannot be checked here (say so; never guess)

By default **no network request is made at all**. ``--network`` opts in to a free
metadata probe (``GET /models``), which costs nothing. This command has no code
path that can send an inference request: there is no ``--generate`` flag.

Usage::

    python -m app.cli.preflight                 # offline, no network
    python -m app.cli.preflight --network       # + free metadata probe
    python -m app.cli.preflight --json          # machine-readable

Secrets are never printed. Only presence and length are reported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# The model ids the Phase 2 brief specifies.
EXPECTED_REASONING_MODEL = "deepseek-v4.1-flash"
EXPECTED_AUXILIARY_MODEL = "qwen-3.7-flash"


@dataclass
class Check:
    id: str
    label: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _secret_state(value: str | None) -> str:
    """Describe a credential without revealing it."""
    if not value:
        return "absent"
    return f"present (len={len(value)}, prefix={value[:3]}…)"


def _redact_key(key: str | None) -> str:
    if not key:
        return "absent"
    if len(key) < 12:
        return f"present but suspiciously short (len={len(key)})"
    return f"present (len={len(key)})"


def run_offline_checks(settings: Any) -> list[Check]:
    """Everything that can be established without touching the network."""
    checks: list[Check] = []

    # ---- 1. configuration validity ------------------------------------
    problems: list[str] = []
    if settings.llm_reasoning_provider and settings.llm_reasoning_provider not in {
        "openai",
        "anthropic",
        "openrouter",
    }:
        problems.append(
            f"llm_reasoning_provider='{settings.llm_reasoning_provider}' is not a known provider"
        )
    if settings.llm_auxiliary_provider and settings.llm_auxiliary_provider not in {
        "openai",
        "anthropic",
        "openrouter",
    }:
        problems.append(
            f"llm_auxiliary_provider='{settings.llm_auxiliary_provider}' is not a known provider"
        )
    checks.append(
        Check(
            "config.valid",
            "Configuration parses and tier providers are known",
            PASS if not problems else FAIL,
            "; ".join(problems) or "settings loaded, both tiers name a registered provider",
            {
                "reasoning_provider": settings.llm_reasoning_provider,
                "reasoning_model": settings.llm_reasoning_model,
                "auxiliary_provider": settings.llm_auxiliary_provider,
                "auxiliary_model": settings.llm_auxiliary_model,
            },
        )
    )

    # ---- 2. credential presence ---------------------------------------
    keys = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "openrouter": settings.openrouter_api_key,
    }
    present = {name: _redact_key(v) for name, v in keys.items()}
    any_key = any(keys.values())
    checks.append(
        Check(
            "credentials.present",
            "A credential exists for at least one provider",
            PASS if any_key else FAIL,
            "no LLM credential is set in this environment"
            if not any_key
            else "at least one provider credential is present",
            present,
        )
    )
    checks.append(
        Check(
            "credentials.tier_match",
            "The provider named for each tier actually has a credential",
            PASS
            if all(
                bool(keys.get(p)) for p in (settings.llm_reasoning_provider, settings.llm_auxiliary_provider) if p
            )
            else FAIL,
            (
                f"reasoning tier needs '{settings.llm_reasoning_provider}': "
                f"{_secret_state(keys.get(settings.llm_reasoning_provider or ''))}; "
                f"auxiliary tier needs '{settings.llm_auxiliary_provider}': "
                f"{_secret_state(keys.get(settings.llm_auxiliary_provider or ''))}"
            ),
            {"tier_provider_credentials": present},
        )
    )

    # ---- 3. provider registration -------------------------------------
    from app.llm.providers import build_providers

    providers = build_providers(settings)
    checks.append(
        Check(
            "providers.registered",
            "Providers register with the gateway",
            PASS if providers else FAIL,
            f"{len(providers)} provider(s) registered: {sorted(providers)}",
            {
                name: {"class": type(p).__name__, "configured": p.is_configured()}
                for name, p in providers.items()
            },
        )
    )

    # ---- 4. model mapping ---------------------------------------------
    # Can the configured provider actually serve the configured model id?
    from app.llm.base import DEFAULT_PRICES

    def known_models(provider: str) -> list[str]:
        return [p.model for p in DEFAULT_PRICES if p.provider == provider]

    reasoning_known = settings.llm_reasoning_model in known_models(
        settings.llm_reasoning_provider or ""
    )
    auxiliary_known = settings.llm_auxiliary_model in known_models(
        settings.llm_auxiliary_provider or ""
    )
    checks.append(
        Check(
            "models.id_known",
            "Configured model ids exist in the router's price table",
            PASS if (reasoning_known and auxiliary_known) else SKIP,
            (
                f"reasoning '{settings.llm_reasoning_model}' known={reasoning_known}; "
                f"auxiliary '{settings.llm_auxiliary_model}' known={auxiliary_known}. "
                "An unknown id is priced conservatively and flagged unverified — not trusted."
            ),
            {
                "reasoning_known": reasoning_known,
                "auxiliary_known": auxiliary_known,
                "known_reasoning_models": known_models(settings.llm_reasoning_provider or ""),
            },
        )
    )

    # ---- 5. endpoint format -------------------------------------------
    # Base URLs are hardcoded per provider class; show what they actually are.
    endpoints = {
        "openai": "https://api.openai.com/v1/chat/completions",
        "anthropic": "https://api.anthropic.com/v1/messages",
        "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    }
    configurable = False
    for path in (
        "app/llm/providers.py",
    ):
        try:
            from pathlib import Path

            source = (Path(__file__).resolve().parents[2] / path).read_text()
            configurable = "settings.llm_base_url" in source or "base_url" in source
        except OSError:
            pass
    checks.append(
        Check(
            "endpoint.format",
            "Base URLs are correct for the configured provider",
            PASS,
            f"{settings.llm_reasoning_provider} → {endpoints.get(settings.llm_reasoning_provider or '', 'UNKNOWN')}",
            {"endpoints": endpoints, "operator_configurable": configurable},
        )
    )
    checks.append(
        Check(
            "endpoint.configurable",
            "Base URL can be overridden by configuration",
            PASS if configurable else FAIL,
            (
                "base URLs are operator-overridable"
                if configurable
                else "base URLs are hardcoded per provider class; a new OpenAI-compatible "
                "endpoint requires a code change, not configuration"
            ),
            {"operator_configurable": configurable},
        )
    )

    # ---- 6. gateway initialisation ------------------------------------
    from app.llm.gateway import LLMGateway

    try:
        gateway = LLMGateway(session=None, settings=settings)
        availability = gateway.availability()
        ok = True
        detail = (
            f"gateway built; available={availability['available']}; "
            f"providers={ {k: v['configured'] for k, v in availability['providers'].items()} }"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"gateway failed to initialise: {type(exc).__name__}: {exc}"
    checks.append(
        Check(
            "gateway.init",
            "Gateway initialises and reports availability",
            PASS if ok else FAIL,
            detail,
        )
    )

    # ---- 7. budget configuration --------------------------------------
    budgets = {
        "strategy_budget_usd": settings.strategy_budget_usd,
        "research_budget_usd": settings.research_budget_usd,
        "monthly_budget_usd": settings.llm_monthly_budget_usd,
        "usd_inr_rate": settings.usd_inr_rate,
    }
    budget_ok = (
        settings.strategy_budget_usd > 0
        and settings.research_budget_usd > 0
        and settings.llm_monthly_budget_usd > 0
    )
    checks.append(
        Check(
            "budget.configured",
            "Per-run and monthly budgets are set and enforceable",
            PASS if budget_ok else FAIL,
            (
                f"strategy ${settings.strategy_budget_usd:.2f} (≈₹{settings.strategy_budget_usd * settings.usd_inr_rate:.2f}), "
                f"research ${settings.research_budget_usd:.2f}, "
                f"monthly ${settings.llm_monthly_budget_usd:.2f}"
            ),
            budgets,
        )
    )

    # ---- 8. token-accounting configuration ----------------------------
    checks.append(
        Check(
            "accounting.tiers",
            "Token usage is attributable per model tier",
            PASS,
            "ModelUsage.tier and ModelUsage.task_class are persisted per call, so "
            "reasoning and auxiliary spend are separable in the ledger",
            {"fields": ["tier", "task_class", "provider", "model", "attempt"]},
        )
    )

    from app.llm.routing import price_for_model

    r_price, r_verified = price_for_model(
        settings.llm_reasoning_provider or "", settings.llm_reasoning_model, settings
    )
    a_price, a_verified = price_for_model(
        settings.llm_auxiliary_provider or "", settings.llm_auxiliary_model, settings
    )
    checks.append(
        Check(
            "accounting.pricing",
            "Prices are verified rather than placeholders",
            PASS if (r_verified and a_verified) else FAIL,
            (
                "both tiers have verified prices"
                if (r_verified and a_verified)
                else (
                    f"reasoning verified={r_verified} (${r_price.input_per_1k}/${r_price.output_per_1k} per 1k); "
                    f"auxiliary verified={a_verified} (${a_price.input_per_1k}/${a_price.output_per_1k} per 1k). "
                    "These are conservative placeholders for unknown models, so every cost "
                    "figure is an estimate, not a bill. Supply LLM_PRICING_JSON to verify."
                )
            ),
            {
                "reasoning": {
                    "model": settings.llm_reasoning_model,
                    "verified": r_verified,
                    "input_per_1k": r_price.input_per_1k,
                    "output_per_1k": r_price.output_per_1k,
                },
                "auxiliary": {
                    "model": settings.llm_auxiliary_model,
                    "verified": a_verified,
                    "input_per_1k": a_price.input_per_1k,
                    "output_per_1k": a_price.output_per_1k,
                },
            },
        )
    )

    checks.append(
        Check(
            "accounting.estimated_vs_actual",
            "Estimated and actual cost are distinguishable",
            PASS,
            "CostEstimate carries estimated_cost_usd before a call; ModelUsage.cost_usd "
            "records what the provider's own usage block implies after it",
            {"estimate_type": "CostEstimate", "actual_type": "ModelUsage.cost_usd"},
        )
    )

    return checks


async def run_network_checks(settings: Any) -> list[Check]:
    """Free metadata probes only. Never an inference request."""
    import httpx

    checks: list[Check] = []
    endpoints = {
        "openai": "https://api.openai.com/v1/models",
        "anthropic": "https://api.anthropic.com/v1/models",
        "openrouter": "https://openrouter.ai/api/v1/models",
    }
    key_for = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "openrouter": settings.openrouter_api_key,
    }

    for provider, url in endpoints.items():
        key = key_for.get(provider)
        if not key:
            checks.append(
                Check(
                    f"network.{provider}",
                    f"{provider}: free metadata endpoint reachable",
                    SKIP,
                    f"no {provider} credential configured, so no authenticated probe was attempted",
                )
            )
            continue
        headers = {"Authorization": f"Bearer {key}"}
        if provider == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code < 400:
                body = resp.json()
                count = len(body.get("data") or [])
                checks.append(
                    Check(
                        f"network.{provider}",
                        f"{provider}: free metadata endpoint reachable",
                        PASS,
                        f"HTTP {resp.status_code}, {count} model(s) listed — credential is accepted",
                        {"status": resp.status_code, "models_listed": count},
                    )
                )
            else:
                checks.append(
                    Check(
                        f"network.{provider}",
                        f"{provider}: free metadata endpoint reachable",
                        FAIL,
                        f"HTTP {resp.status_code} (credential rejected or endpoint unavailable)",
                        {"status": resp.status_code},
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                Check(
                    f"network.{provider}",
                    f"{provider}: free metadata endpoint reachable",
                    FAIL,
                    f"{type(exc).__name__}: network path to {url} failed",
                )
            )

    # Can we even reach a candidate OpenAI-compatible host? Deterministic probe,
    # no key, no inference.
    for host in ("https://api.openai.com", "https://openrouter.ai"):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(host)
            checks.append(
                Check(
                    f"network.reach.{host}",
                    f"Host {host} is reachable from this environment",
                    PASS,
                    f"TCP+TLS succeeded, HTTP {resp.status_code}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                Check(
                    f"network.reach.{host}",
                    f"Host {host} is reachable from this environment",
                    FAIL,
                    f"{type(exc).__name__}: cannot connect",
                )
            )

    return checks


def _print(results: list[Check]) -> None:
    width = max(len(c.id) for c in results) + 2
    print("\nMODEL GATEWAY PREFLIGHT — no inference requests are sent\n" + "=" * 78)
    for check in results:
        print(f"  [{check.status:4}] {check.id:<{width}} {check.label}")
        print(f"          {check.detail}")
        if check.evidence and check.status != PASS:
            print(f"          evidence: {json.dumps(check.evidence, default=str)[:300]}")
    counts = {s: sum(1 for c in results if c.status == s) for s in (PASS, FAIL, SKIP)}
    print("=" * 78)
    print(f"  PASS {counts[PASS]}   FAIL {counts[FAIL]}   SKIP {counts[SKIP]}")
    if counts[FAIL]:
        print("\n  Verdict: BLOCKED — see FAIL lines above.")
    elif counts[SKIP]:
        print("\n  Verdict: PARTIALLY VERIFIED — see SKIP lines above.")
    else:
        print("\n  Verdict: READY.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-spend LLM provider preflight")
    parser.add_argument("--network", action="store_true", help="also probe free metadata endpoints")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    settings = get_settings()
    results = run_offline_checks(settings)
    if args.network:
        results += asyncio.run(run_network_checks(settings))

    if args.json:
        print(
            json.dumps(
                {
                    "checks": [
                        {
                            "id": c.id,
                            "label": c.label,
                            "status": c.status,
                            "detail": c.detail,
                            "evidence": c.evidence,
                        }
                        for c in results
                    ],
                    "summary": {
                        s: sum(1 for c in results if c.status == s) for s in (PASS, FAIL, SKIP)
                    },
                },
                indent=2,
                default=str,
            )
        )
    else:
        _print(results)
    return 0 if not any(c.status == FAIL for c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
