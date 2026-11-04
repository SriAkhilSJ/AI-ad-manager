"""Local Flow Companion (Phase 3C.1).

The companion is the only component that can talk to the browser extension, and it
lives on the user's machine. It connects **outbound** to Crusor, so a cloud
deployment never dials the user's loopback port and no inbound path into a user's
network exists.

Lifecycle::

    STOPPED -> STARTING -> UNPAIRED (bridge up, no extension)
                       -> PAIRED   (extension authenticated)
                       -> CONNECTED (Crusor accepted the companion)
                       -> DEGRADED  (a few consecutive failures, retrying)
                       -> DISCONNECTED (gave up or asked to stop)

Two rules the loop enforces:

* generation commands are **refused** unless ``allow_generation_commands`` is set,
  which the companion only does in Phase 3C.2 after the backend says so — today the
  backend never says so;
* every outbound request carries the companion credential in a header, never in a
  URL, and the credential is redacted from logs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from app.flow import protocol as proto
from app.flow.delivery import (
    DeliveryOutcome,
    DispatchFailed,
    ProviderDispatchOutcome,
    classify_send_exception,
)
from app.flow.bridge import BridgeConfig, FlowBridge
from app.flow.security import install_redaction

logger = logging.getLogger("app.flow.companion")

COMPANION_PROTOCOL = "flow.companion.v1"
GENERATION_COMMANDS = frozenset({"generate_image", "generate_video", "export_asset"})


class CompanionState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    UNPAIRED = "unpaired"
    PAIRED = "paired"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


@dataclass
class CompanionConfig:
    backend_url: str = "http://127.0.0.1:8000"
    companion_id: str = ""
    companion_secret: str = ""
    label: str = "local-flow-companion"
    poll_wait_seconds: int = 20
    heartbeat_seconds: float = 20.0
    request_timeout: float = 30.0
    max_backoff_seconds: float = 20.0
    #: Phase 3C.1 never executes a generation; 3C.2 flips this after acceptance.
    allow_generation_commands: bool = False

    def validate(self) -> "CompanionConfig":
        if not self.companion_id or len(self.companion_secret) < 32:
            raise ValueError(
                "The companion needs FLOW_COMPANION_ID and a 256-bit "
                "FLOW_COMPANION_SECRET before it can register."
            )
        return self


@dataclass
class CompanionStatus:
    state: CompanionState = CompanionState.STOPPED
    paired: bool = False
    registered: bool = False
    last_error: str = ""
    commands_handled: int = 0
    commands_refused: int = 0
    consecutive_failures: int = 0
    last_heartbeat_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "paired": self.paired,
            "registered": self.registered,
            "last_error": self.last_error,
            "commands_handled": self.commands_handled,
            "commands_refused": self.commands_refused,
            "consecutive_failures": self.consecutive_failures,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None,
        }


@dataclass
class LocalFlowCompanion:
    """Runs the bridge and keeps Crusor informed over an outbound connection."""

    config: CompanionConfig
    bridge_config: BridgeConfig
    bridge: FlowBridge | None = None
    settings: Any = None
    client: httpx.AsyncClient | None = None
    status: CompanionStatus = field(default_factory=CompanionStatus)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _owns_bridge: bool = False
    _owns_client: bool = False

    def __post_init__(self) -> None:
        self.config.validate()
        install_redaction(logger, [self.config.companion_secret, self.bridge_config.pairing_secret])

    # ------------------------------------------------------------------ life
    async def start(self) -> None:
        self.status.state = CompanionState.STARTING
        if self.bridge is None:
            self.bridge = FlowBridge(self.bridge_config, settings=self.settings)
            self._owns_bridge = True
        await self.bridge.start()
        self.status.paired = self.bridge.paired
        self.status.state = CompanionState.PAIRED if self.bridge.paired else CompanionState.UNPAIRED
        if self.client is None:
            self.client = httpx.AsyncClient(
                base_url=self.config.backend_url.rstrip("/"),
                timeout=self.config.request_timeout,
            )
            self._owns_client = True
        self._stop.clear()
        logger.info(
            "flow_companion_started backend=%s companion=%s bridge_http=%s bridge_ws=%s",
            self.config.backend_url,
            self.config.companion_id,
            self.bridge.http_port,
            self.bridge.ws_port,
        )

    async def stop(self) -> None:
        self._stop.set()
        # A rejected credential is not retried and not announced: the backend has
        # already told us to stop.
        if self.client is not None and self.status.state is not CompanionState.DISCONNECTED:
            try:
                await self._post(
                    "/api/v1/flow/companion/disconnect",
                    {"companion_id": self.config.companion_id},
                )
            except Exception:  # noqa: BLE001 - stopping is best effort
                pass
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None
        if self._owns_bridge and self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None
        # A credential rejection stays DISCONNECTED so the reason survives shutdown.
        if self.status.state is not CompanionState.DISCONNECTED:
            self.status.state = CompanionState.STOPPED
        self.status.registered = False

    # ------------------------------------------------------------------ wire
    def _headers(self) -> dict[str, str]:
        return {
            "X-Companion-Id": self.config.companion_id,
            "X-Companion-Secret": self.config.companion_secret,
            "X-Companion-Protocol": COMPANION_PROTOCOL,
            "Accept": "application/json",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.client is not None
        response = await self.client.post(path, json=payload, headers=self._headers())
        if response.status_code >= 400:
            raise CompanionRequestError(response.status_code, _safe_error(response))
        return response.json() if response.content else {}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.client is not None
        response = await self.client.get(path, params=params, headers=self._headers())
        if response.status_code >= 400:
            raise CompanionRequestError(response.status_code, _safe_error(response))
        return response.json() if response.content else {}

    # --------------------------------------------------------------- payload
    def capability_payload(self) -> dict[str, Any]:
        bridge = self.bridge
        snapshot = bridge.snapshot() if bridge else {}
        return {
            "protocol": COMPANION_PROTOCOL,
            "bridge_version": snapshot.get("bridge_version", ""),
            "allowed_commands": list(proto.ALLOWED_COMMANDS),
            "live_generation": False,
            "generation_commands_executable": bool(self.config.allow_generation_commands),
            "measures_installed_artifacts": bool(
                self.bridge_config.require_installed_measurement
            ),
            "measurement_components": [spec.component for spec in self.bridge_config.install_components],
            "loopback_only": True,
            "limits": snapshot.get("limits", {}),
        }

    def session_payload(self) -> dict[str, Any]:
        snapshot = self.bridge.snapshot() if self.bridge else {}
        session = snapshot.get("session") or {}
        return {
            "companion_id": self.config.companion_id,
            "label": self.config.label,
            "extension_id": session.get("extension_id", ""),
            "extension_version": session.get("extension_version", ""),
            "flow_tab_present": bool(session.get("flow_tab_present")),
            "bridge_paired": bool(self.bridge.paired) if self.bridge else False,
            "capabilities": self.capability_payload(),
        }

    async def hello(self) -> dict[str, Any]:
        result = await self._post("/api/v1/flow/companion/hello", self.session_payload())
        self.status.registered = True
        self.status.state = CompanionState.CONNECTED
        self.status.consecutive_failures = 0
        self.status.paired = bool(self.bridge.paired) if self.bridge else False
        return result

    async def heartbeat(self) -> None:
        payload = self.session_payload()
        payload.update({"busy": False, "status": self.status.state.value})
        await self._post("/api/v1/flow/companion/heartbeat", payload)
        self.status.last_heartbeat_at = datetime.now(timezone.utc)

    async def poll_once(self) -> list[dict[str, Any]]:
        response = await self._get(
            "/api/v1/flow/companion/commands", {"wait": self.config.poll_wait_seconds}
        )
        commands = response.get("commands") or []
        for command in commands:
            await self._handle_command(command)
        return commands

    async def _handle_command(self, command: dict[str, Any]) -> None:
        command_id = str(command.get("id") or "")
        name = str(command.get("command") or "")
        payload = dict(command.get("payload") or {})
        job_id = str(command.get("job_id") or "")
        # Phase 3C.2.3.4: echoed back with the result so the backend can correlate it.
        request_hash = str(command.get("request_hash") or "")
        # A local refusal is real, but `not_dispatched` is *evidence*: without the
        # command's request hash it cannot be tied to one attempt, so it is reported
        # as unknown and can never authorize a retry.
        refusal_provider = (
            ProviderDispatchOutcome.NOT_DISPATCHED
            if request_hash
            else ProviderDispatchOutcome.UNKNOWN
        )

        try:
            proto.ensure_allowed(name)
        except proto.CommandDenied as exc:
            self.status.commands_refused += 1
            await self._post_result(
                command_id,
                ok=False,
                job_id=job_id,
                error={
                    "code": proto.CommandDenied.code,
                    "message": exc.message,
                    "dispatched": False,
                    "transport": DeliveryOutcome.DEFINITELY_NOT_SENT.value,
                    "provider_dispatch": refusal_provider.value,
                },
                transport=DeliveryOutcome.DEFINITELY_NOT_SENT,
                provider=refusal_provider,
                request_hash=request_hash,
            )
            return

        if name in GENERATION_COMMANDS and not self.config.allow_generation_commands:
            # Defence in depth: the backend never queues these in 3C.1, and the
            # companion refuses them even if it did.
            self.status.commands_refused += 1
            await self._post_result(
                command_id,
                ok=False,
                job_id=job_id,
                error={
                    "code": "flow_live_generation_disabled",
                    "message": "The companion does not execute generation commands in Phase 3C.1.",
                    # Explicit, and true here: the command never reached the socket.
                    "dispatched": False,
                    "transport": DeliveryOutcome.DEFINITELY_NOT_SENT.value,
                    "provider_dispatch": refusal_provider.value,
                },
                transport=DeliveryOutcome.DEFINITELY_NOT_SENT,
                provider=refusal_provider,
                request_hash=request_hash,
            )
            return

        if self.bridge is None:
            await self._post_result(
                command_id,
                ok=False,
                job_id=job_id,
                error={
                    "code": "flow_bridge_unavailable",
                    "message": "The local bridge is not running.",
                    "dispatched": False,
                    "transport": DeliveryOutcome.DEFINITELY_NOT_SENT.value,
                    "provider_dispatch": refusal_provider.value,
                },
                transport=DeliveryOutcome.DEFINITELY_NOT_SENT,
                provider=refusal_provider,
                request_hash=request_hash,
            )
            return

        self.status.paired = self.bridge.paired
        try:
            result = await self.bridge.dispatch(
                name, payload, job_id=job_id, request_hash=request_hash
            )
        except asyncio.CancelledError:
            # No result is posted: the request is being torn down. Silence is honest
            # here — the backend treats the job as unknown, never as non-delivery.
            raise
        except Exception as exc:  # noqa: BLE001 - normalised before it crosses the wire
            # Phase 3C.2.3.3: `dispatched: false` is only truthful when the transport
            # itself proved the frame never left. An exception after the socket send
            # (timeout, reset, decode error) leaves the provider state unknown, and
            # saying otherwise would invite a duplicate paid generation.
            code = getattr(exc, "code", "flow_command_failed")
            delivery = classify_send_exception(exc)
            if isinstance(exc, DispatchFailed):
                # The bridge correlated this answer with the command it sent, so the
                # pair it reports is evidence, not speculation — except that a
                # `not_dispatched` claim still needs the request hash to be
                # attributable to one attempt.
                provider = exc.provider
                if provider is ProviderDispatchOutcome.NOT_DISPATCHED and not request_hash:
                    provider = ProviderDispatchOutcome.UNKNOWN
            elif delivery is DeliveryOutcome.DEFINITELY_NOT_SENT:
                provider = refusal_provider
            else:
                provider = ProviderDispatchOutcome.UNKNOWN
            error: dict[str, Any] = {
                "code": str(code),
                "message": getattr(exc, "message", "The bridge could not complete the command."),
                "transport": delivery.value,
                "provider_dispatch": provider.value,
            }
            if delivery is DeliveryOutcome.DEFINITELY_NOT_SENT:
                error["dispatched"] = False
            await self._post_result(
                command_id,
                ok=False,
                job_id=job_id,
                error=error,
                transport=delivery,
                provider=provider,
                request_hash=request_hash,
            )
            self.status.commands_refused += 1
            return

        self.status.commands_handled += 1
        await self._post_result(
            command_id, ok=True, job_id=job_id, result=result, request_hash=request_hash
        )

    async def _post_result(
        self,
        command_id: str,
        *,
        ok: bool,
        job_id: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        transport: DeliveryOutcome | None = None,
        provider: ProviderDispatchOutcome | None = None,
        request_hash: str = "",
    ) -> None:
        payload: dict[str, Any] = {
            "command_id": command_id,
            "job_id": job_id,
            "ok": ok,
            "result": result or {},
            "error": error,
        }
        # Phase 3C.2.3.4: the echo of the request hash the backend issued with the
        # command. It is what ties this report to one attempt; a report that cannot
        # carry it may not claim `not_dispatched`.
        if request_hash:
            payload["request_hash"] = request_hash
        # Phase 3C.2.3.3: the two events travel separately. Omitted means "not
        # stated", which the backend records as unknown — never as non-delivery.
        if transport is not None:
            payload["transport"] = transport.value
        if provider is not None:
            payload["provider_dispatch"] = provider.value
        await self._post("/api/v1/flow/companion/results", payload)

    # ------------------------------------------------------------------ loop
    async def run_forever(self, *, max_iterations: int | None = None) -> None:
        """Register, then heartbeat + poll until stopped (or ``max_iterations``)."""
        await self.start()
        iteration = 0
        backoff = 1.0
        try:
            while not self._stop.is_set():
                if max_iterations is not None and iteration >= max_iterations:
                    break
                iteration += 1
                try:
                    if not self.status.registered:
                        await self.hello()
                    else:
                        await self.heartbeat()
                        await self.poll_once()
                    self.status.state = CompanionState.CONNECTED
                    self.status.consecutive_failures = 0
                    backoff = 1.0
                except CompanionRequestError as exc:
                    self.status.consecutive_failures += 1
                    self.status.last_error = exc.message
                    self.status.registered = exc.status_code in (401, 403)
                    self.status.state = (
                        CompanionState.DISCONNECTED
                        if exc.status_code in (401, 403)
                        else CompanionState.DEGRADED
                    )
                    if exc.status_code in (401, 403):
                        # A rejected credential is not retried: stop and let a human
                        # re-pair instead of hammering the backend.
                        logger.warning("flow_companion_auth_rejected status=%s", exc.status_code)
                        break
                    await asyncio.sleep(min(backoff, self.config.max_backoff_seconds))
                    backoff = min(backoff * 2, self.config.max_backoff_seconds)
                except (httpx.HTTPError, OSError) as exc:
                    self.status.consecutive_failures += 1
                    self.status.last_error = type(exc).__name__
                    self.status.state = CompanionState.DEGRADED
                    await asyncio.sleep(min(backoff, self.config.max_backoff_seconds))
                    backoff = min(backoff * 2, self.config.max_backoff_seconds)
                else:
                    await asyncio.sleep(min(self.config.heartbeat_seconds, 0.05 if max_iterations else self.config.heartbeat_seconds))
        finally:
            await self.stop()


class CompanionRequestError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _safe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        return str(payload.get("error", {}).get("message") or f"HTTP {response.status_code}")
    except Exception:  # noqa: BLE001
        return f"HTTP {response.status_code}"


def companion_from_settings() -> "LocalFlowCompanion":
    from app.core.config import get_settings
    from app.flow.bridge import bridge_config_from_settings

    settings = get_settings()
    config = CompanionConfig(
        backend_url=settings.flow_backend_url,
        companion_id=settings.flow_companion_id,
        companion_secret=settings.flow_companion_secret,
        label=f"crusor-companion-{settings.environment}",
        poll_wait_seconds=int(settings.flow_request_timeout_seconds or 20),
        heartbeat_seconds=float(settings.flow_handshake_timeout_seconds or 20) * 4,
    )
    return LocalFlowCompanion(
        config=config,
        bridge_config=bridge_config_from_settings(settings),
        settings=settings,
    )


def main() -> int:
    """``python -m app.flow.companion`` — dials out to Crusor from the user's machine."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        companion = companion_from_settings()
    except ValueError as exc:
        logger.error("flow_companion_misconfigured: %s", exc)
        return 2
    try:
        asyncio.run(companion.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
