"""Agent foundation.

Every agent is a *typed function*, not a chat participant:

* it declares a Pydantic ``Input`` and ``Output`` schema,
* it has execution limits (attempts, token budget, wall-clock timeout),
* it writes an :class:`AgentRun` ledger row with a trace per attempt,
* it records model usage through the gateway (never estimated),
* it raises typed errors that the Director can route on.

Agents do **not** call each other. The Director passes structured outputs between
them, which keeps the dependency graph acyclic and auditable.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm.base import BudgetExceeded, LLMRequest, ProviderUnavailable
from app.llm.gateway import LLMGateway
from app.models.agent import AgentRun, AgentRunStatus
from app.models.brand import BrandProfile

logger = get_logger("crusor.agents")

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentError(RuntimeError):
    code = "agent_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class AgentInputInvalid(AgentError):
    code = "agent_input_invalid"


class AgentTimeout(AgentError):
    code = "agent_timeout"


class AgentBudgetExceeded(AgentError):
    code = "agent_budget_exceeded"


class AgentOutputInvalid(AgentError):
    code = "agent_output_invalid"


class AgentPayload(BaseModel):
    """Base for every agent input."""

    model_config = ConfigDict(extra="forbid")

    brand_profile_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None
    request: str = ""


class AgentResult(BaseModel):
    """Base for every agent output.

    ``data_source`` and ``generation_mode`` are mandatory so the UI can always
    state how a result was produced, and so no result can masquerade as
    model-generated when it was produced deterministically.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str
    generation_mode: str = Field(
        default="deterministic",
        description="'llm' when a provider produced the content, 'deterministic' otherwise.",
    )
    data_source: str = Field(
        default="brand_profile",
        description="Provenance of the facts used: brand_profile | database | analytics | none",
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    requires_human_review: bool = True


@dataclass
class AgentContext:
    """Everything an agent is allowed to touch."""

    session: AsyncSession
    brand: BrandProfile | None = None
    gateway: LLMGateway | None = None
    actor_id: uuid.UUID | None = None
    workflow_thread_id: str | None = None
    workflow_node: str | None = None
    run: AgentRun | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    tokens_used: int = 0

    @property
    def remaining_tokens(self) -> int:
        settings = get_settings()
        return max(settings.agent_max_tokens_per_run - self.tokens_used, 0)


class BaseAgent(Generic[InputT, OutputT]):
    name: ClassVar[str] = "base_agent"
    description: ClassVar[str] = ""
    responsibilities: ClassVar[list[str]] = []
    input_schema: ClassVar[type[BaseModel]] = AgentPayload
    output_schema: ClassVar[type[BaseModel]] = AgentResult

    # execution limits (overridable per agent / per settings)
    max_attempts: ClassVar[int] = 2
    timeout_seconds: ClassVar[int] = 120
    max_tokens: ClassVar[int] = 2000
    retry_backoff_seconds: ClassVar[float] = 0.2

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ spec
    @classmethod
    def spec(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "description": cls.description,
            "responsibilities": cls.responsibilities,
            "input_schema": cls.input_schema.model_json_schema(),
            "output_schema": cls.output_schema.model_json_schema(),
            "limits": {
                "max_attempts": cls.max_attempts,
                "timeout_seconds": cls.timeout_seconds,
                "max_tokens": cls.max_tokens,
            },
        }

    # ------------------------------------------------------------- lifecycle
    async def run(
        self,
        ctx: AgentContext,
        payload: InputT | dict[str, Any],
    ) -> OutputT:
        """Public entry point: validate → execute with limits → ledger → result."""
        run = await self._open_run(ctx, payload)
        ctx.run = run
        started = time.perf_counter()

        try:
            validated = self.input_schema.model_validate(payload)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            await self._finish_run(
                ctx, run, AgentRunStatus.FAILED, error_code=AgentInputInvalid.code,
                error_message=f"input validation failed: {exc}",
            )
            raise AgentInputInvalid(
                f"{self.name}: input validation failed.",
                details={"errors": str(exc)[:500]},
            ) from exc

        run.input_payload = validated.model_dump(mode="json")
        await ctx.session.flush()

        last_error: Exception | None = None
        attempts = max(1, min(self.max_attempts, self.settings.agent_max_attempts))
        timeout = min(self.timeout_seconds, self.settings.agent_timeout_seconds)

        for attempt in range(1, attempts + 1):
            run.attempts = attempt
            self._trace(ctx, run, "attempt_started", attempt=attempt, timeout=timeout)
            try:
                result = await asyncio.wait_for(
                    self.execute(ctx, validated), timeout=timeout
                )
                if not isinstance(result, self.output_schema):
                    result = self.output_schema.model_validate(result)
                self._check_budget(ctx)
                run.output_payload = result.model_dump(mode="json")
                await self._finish_run(
                    ctx,
                    run,
                    AgentRunStatus.SUCCEEDED,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
                logger.info(
                    "agent_succeeded",
                    agent=self.name,
                    attempt=attempt,
                    mode=result.generation_mode,
                    tokens=ctx.tokens_used,
                )
                return result  # type: ignore[return-value]

            except asyncio.TimeoutError as exc:
                last_error = AgentTimeout(f"{self.name} exceeded {timeout}s")
                self._trace(ctx, run, "attempt_timeout", attempt=attempt)
            except (AgentBudgetExceeded, BudgetExceeded) as exc:
                last_error = AgentBudgetExceeded(str(exc))
                self._trace(ctx, run, "attempt_budget_exceeded", attempt=attempt, error=str(exc))
                break  # budget failures are not retryable
            except (AgentError, ProviderUnavailable) as exc:
                last_error = exc
                self._trace(
                    ctx, run, "attempt_failed", attempt=attempt,
                    error=type(exc).__name__, message=str(exc)[:300],
                )
            except Exception as exc:  # noqa: BLE001 - agent boundary
                last_error = exc
                logger.exception("agent_attempt_crashed", agent=self.name, attempt=attempt)
                self._trace(
                    ctx, run, "attempt_crashed", attempt=attempt, error=type(exc).__name__
                )
            if attempt < attempts:
                await asyncio.sleep(self.retry_backoff_seconds * attempt)

        status = (
            AgentRunStatus.BUDGET_EXCEEDED
            if isinstance(last_error, (AgentBudgetExceeded, BudgetExceeded))
            else AgentRunStatus.FAILED
        )
        await self._finish_run(
            ctx,
            run,
            status,
            error_code=getattr(last_error, "code", "agent_error"),
            error_message=str(last_error) if last_error else "unknown",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise AgentError(
            f"{self.name} failed after {attempts} attempt(s): {last_error}",
            details={"agent": self.name, "attempts": attempts},
        ) from last_error

    async def execute(self, ctx: AgentContext, payload: InputT) -> OutputT:
        raise NotImplementedError

    # -------------------------------------------------------------- LLM helper
    async def try_llm(
        self,
        ctx: AgentContext,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
        schema_name: str,
        purpose: str,
    ) -> tuple[BaseModel, bool]:
        """Attempt a structured model call.

        Returns ``(parsed, used_llm)``. Any absence of a provider, budget stop or
        malformed response degrades to ``(_, False)`` so the caller's
        deterministic path runs. A degradation is recorded as a warning, never
        hidden.
        """
        gateway = ctx.gateway
        if gateway is None:
            return schema.model_construct(), False
        budget = min(self.max_tokens, ctx.remaining_tokens)
        if budget <= 0:
            return schema.model_construct(), False
        request = LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name=schema_name,
            json_schema=schema.model_json_schema(),
            max_tokens=budget,
            purpose=purpose,
        )
        try:
            response = await gateway.structured(
                agent_name=self.name,
                request=request,
                agent_run_id=ctx.run.id if ctx.run else None,
                attempt=ctx.run.attempts if ctx.run else 1,
                model_budget_tokens=budget,
            )
        except ProviderUnavailable:
            return schema.model_construct(), False
        except BudgetExceeded as exc:
            raise AgentBudgetExceeded(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_call_failed", agent=self.name, error=type(exc).__name__)
            return schema.model_construct(), False

        ctx.tokens_used += response.total_tokens
        self._trace(
            ctx,
            ctx.run,
            "llm_call",
            provider=response.provider,
            model=response.model,
            tokens=response.total_tokens,
            cost_usd=response.cost_usd,
        )
        try:
            parsed = schema.model_validate(response.data)
        except Exception:  # noqa: BLE001 - schema drift in model output
            logger.warning("llm_output_schema_mismatch", agent=self.name)
            return schema.model_construct(), False
        return parsed, True

    def _check_budget(self, ctx: AgentContext) -> None:
        if ctx.tokens_used > self.settings.agent_max_tokens_per_run:
            raise AgentBudgetExceeded(
                f"{self.name}: token budget {self.settings.agent_max_tokens_per_run} exceeded "
                f"({ctx.tokens_used} used)"
            )

    # ---------------------------------------------------------------- ledger
    async def _open_run(
        self, ctx: AgentContext, payload: Any
    ) -> AgentRun:
        payload_dict = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else dict(payload or {})
        )
        run = AgentRun(
            agent_name=self.name,
            status=AgentRunStatus.RUNNING.value,
            workflow_thread_id=ctx.workflow_thread_id,
            workflow_node=ctx.workflow_node or self.name,
            campaign_id=payload_dict.get("campaign_id"),
            brand_profile_id=payload_dict.get("brand_profile_id"),
            triggered_by=ctx.actor_id,
            input_payload=payload_dict,
            started_at=datetime.now(timezone.utc),
            trace=[],
        )
        ctx.session.add(run)
        await ctx.session.flush()
        self._trace(ctx, run, "run_opened")
        return run

    async def _finish_run(
        self,
        ctx: AgentContext,
        run: AgentRun,
        status: AgentRunStatus,
        *,
        error_code: str = "",
        error_message: str = "",
        latency_ms: int | None = None,
    ) -> None:
        run.status = status.value
        run.error_code = error_code
        run.error_message = error_message[:2000]
        run.finished_at = datetime.now(timezone.utc)
        if latency_ms is not None:
            run.latency_ms = latency_ms
        self._trace(ctx, run, "run_finished", status=status.value, tokens=ctx.tokens_used)
        ctx.session.add(run)
        await ctx.session.flush()

    def _trace(self, ctx: AgentContext, run: AgentRun | None, event: str, **fields: Any) -> None:
        if run is None:
            return
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "agent": self.name,
            **fields,
        }
        run.trace = [*(run.trace or []), entry]
