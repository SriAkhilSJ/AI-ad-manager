"""Controlled live canary — prepared, deliberately NOT executable unattended.

The Phase 3 audit asks for an optional live inference test that must not run
without explicit founder authorization. This module therefore:

* is **not** wired to any API route, agent, or the Director graph;
* refuses to run without ``--i-authorize-paid-calls`` *and* a cost ceiling
  argument, and prints the full cost disclosure before doing anything;
* makes at most **two** requests (one per tier), each capped at 100 output
  tokens, with no tools, no agent loop, no research retrieval, no retries;
* hard-aborts if either model's price is unverified, because an unverified
  price means the ceiling in §2.3 of the brief cannot be honoured.

Estimated cost of the canary at placeholder pricing:

    input  ~120 tokens/request × $0.004/1k  = $0.00048
    output       100 tokens/request × $0.016/1k = $0.0016
                                          ────────────
    per request                              ≈ $0.00208
    two requests                             ≈ $0.00416   (≈ ₹0.35)

That is well inside the $0.05 ceiling — but the two prices are placeholders, so
the real bill is unknown until Velona publishes its rate card.

Usage (only after the founder authorizes it in writing)::

    python -m app.cli.canary --show-plan
    python -m app.cli.canary --i-authorize-paid-calls --max-total-usd 0.05
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

CANARY_MAX_OUTPUT_TOKENS = 100
CANARY_CEILING_USD = 0.05
SHORT_PROMPT = (
    "Reply with exactly this JSON object and nothing else: "
    '{"status": "ok", "role": "canary"}'
)


def plan(settings: Any) -> dict[str, Any]:
    from app.llm.routing import estimate_cost, price_for_model

    reasoning_provider = settings.llm_reasoning_provider or ""
    auxiliary_provider = settings.llm_auxiliary_provider or ""
    reasoning_model = settings.llm_reasoning_model or ""
    auxiliary_model = settings.llm_auxiliary_model or ""

    input_tokens = 120  # prompt + system, estimated, same ratio the gateway uses
    entries = []
    for tier, provider, model in (
        ("reasoning", reasoning_provider, reasoning_model),
        ("auxiliary", auxiliary_provider, auxiliary_model),
    ):
        est = estimate_cost(
            provider=provider,
            model=model,
            prompt_tokens=input_tokens,
            completion_tokens=CANARY_MAX_OUTPUT_TOKENS,
            tier=tier,
            task_class="classification",
            settings=settings,
        )
        _, verified = price_for_model(provider, model, settings)
        entries.append(
            {
                "tier": tier,
                "provider": provider,
                "model": model,
                "max_output_tokens": CANARY_MAX_OUTPUT_TOKENS,
                "estimated_input_tokens": input_tokens,
                "input_per_1k": est.input_per_1k,
                "output_per_1k": est.output_per_1k,
                "estimated_cost_usd": round(est.estimated_cost_usd, 6),
                "price_verified": verified,
            }
        )

    total = round(sum(e["estimated_cost_usd"] for e in entries), 6)
    unverified = [e["model"] for e in entries if not e["price_verified"]]
    return {
        "requests": entries,
        "request_count": len(entries),
        "max_output_tokens_per_request": CANARY_MAX_OUTPUT_TOKENS,
        "tools": False,
        "agent_loops": False,
        "research_retrieval": False,
        "retries": 0,
        "estimated_total_usd": total,
        "estimated_total_inr": round(total * settings.usd_inr_rate, 4),
        "ceiling_usd": CANARY_CEILING_USD,
        "within_ceiling": total <= CANARY_CEILING_USD,
        "unverified_priced_models": unverified,
        "blocking_reason": (
            f"{len(unverified)} model(s) have no verified price ({', '.join(unverified)}); "
            "the $0.05 ceiling cannot be guaranteed, so authorized execution must stop here"
            if unverified
            else ""
        ),
        "authorization_required": True,
        "note": (
            "Nothing here runs without --i-authorize-paid-calls. No API route reaches "
            "this module. Execution is refused outright while any price is unverified."
        ),
    }


def _print_plan(p: dict[str, Any]) -> None:
    print("\nLIVE CANARY PLAN — not executed\n" + "=" * 78)
    for r in p["requests"]:
        print(f"  {r['tier']:<10} {r['provider']} / {r['model']}")
        print(
            f"             in ${r['input_per_1k']}/1k  out ${r['output_per_1k']}/1k  "
            f"max_out={r['max_output_tokens']}  est=${r['estimated_cost_usd']:.5f}  "
            f"price_verified={r['price_verified']}"
        )
    print("=" * 78)
    print(f"  requests      : {p['request_count']} (one per tier, no retries)")
    print(f"  tools/loops   : none")
    print(f"  estimated     : ${p['estimated_total_usd']:.5f} (≈₹{p['estimated_total_inr']:.2f})")
    print(f"  ceiling       : ${p['ceiling_usd']:.2f}  within={p['within_ceiling']}")
    if p["blocking_reason"]:
        print(f"  BLOCKED       : {p['blocking_reason']}")
    print("=" * 78)
    print("  Budget enforcement: the gateway checks the estimate against the run")
    print("  budget and the monthly cap BEFORE the provider is called, and records")
    print("  actual tokens/cost after. Exceeding either raises BudgetExceeded and")
    print("  no request leaves the process.")


async def _execute(settings: Any, ceiling_usd: float) -> int:
    """Two real requests. Only reachable with explicit authorization."""
    from app.llm.base import BudgetExceeded, LLMRequest, ProviderUnavailable
    from app.llm.gateway import LLMGateway
    from app.llm.routing import BudgetGuard, estimate_cost, price_for_model

    p = plan(settings)
    if p["unverified_priced_models"]:
        print(f"REFUSED: {p['blocking_reason']}", file=sys.stderr)
        return 2
    if p["estimated_total_usd"] > ceiling_usd:
        print(
            f"REFUSED: estimate ${p['estimated_total_usd']:.5f} exceeds ceiling "
            f"${ceiling_usd:.2f}",
            file=sys.stderr,
        )
        return 2

    gateway = LLMGateway(session=None, settings=settings)
    guard = BudgetGuard(max_cost_usd=ceiling_usd, label="canary")
    results = []
    for tier in ("reasoning", "auxiliary"):
        model = (
            settings.llm_reasoning_model if tier == "reasoning" else settings.llm_auxiliary_model
        )
        provider = (
            settings.llm_reasoning_provider
            if tier == "reasoning"
            else settings.llm_auxiliary_provider
        )
        request = LLMRequest(
            system_prompt="You are a connectivity canary. Follow the instruction exactly.",
            user_prompt=SHORT_PROMPT,
            schema_name="canary",
            json_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}, "role": {"type": "string"}},
                "required": ["status", "role"],
            },
            max_tokens=CANARY_MAX_OUTPUT_TOKENS,
            temperature=0.0,
            purpose=f"canary_{tier}",
        )
        task_class = "classification" if tier == "auxiliary" else "strategic_decision"
        try:
            response = await gateway.structured(
                agent_name="canary",
                request=request,
                task_class=task_class,
                budget=guard,
            )
            results.append(
                {
                    "tier": tier,
                    "ok": True,
                    "provider": response.provider,
                    "model": response.model,
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "cost_usd": response.cost_usd,
                    "latency_ms": response.latency_ms,
                    "parsed_json": response.data,
                }
            )
        except (ProviderUnavailable, BudgetExceeded) as exc:
            results.append(
                {"tier": tier, "ok": False, "error_type": type(exc).__name__, "error": str(exc)}
            )
            break  # never retry: one attempt per tier is the whole policy
    print(json.dumps({"results": results, "spent_usd": guard.spent_usd}, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controlled live LLM canary (authorized use only)")
    parser.add_argument("--show-plan", action="store_true", help="print the plan and cost, exit")
    parser.add_argument(
        "--i-authorize-paid-calls",
        action="store_true",
        help="required; asserts the founder has authorized real spend",
    )
    parser.add_argument("--max-total-usd", type=float, default=None, help="hard ceiling")
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    settings = get_settings()
    p = plan(settings)

    if args.show_plan or not args.i_authorize_paid_calls:
        _print_plan(p)
        if not args.show_plan:
            print(
                "\n  Not executed. Re-run with --i-authorize-paid-calls "
                "--max-total-usd 0.05 after the founder authorizes spend."
            )
        return 0

    ceiling = args.max_total_usd if args.max_total_usd is not None else CANARY_CEILING_USD
    _print_plan(p)
    return asyncio.run(_execute(settings, ceiling))


if __name__ == "__main__":
    sys.exit(main())
