"""Hardened local Flow bridge (Phase 3C.1).

Two listeners, both loopback-only and separately authenticated:

* ``http://127.0.0.1:<port>/api/...`` — command API, guarded by a pairing header,
  Host/Origin checks, size limits, a read timeout and a hard connection ceiling;
* ``ws://127.0.0.1:<ws_port>/ws`` — the extension socket. Host/Origin are validated
  at the handshake and the pairing credential must arrive in an authenticated
  ``hello`` frame within ``handshake_timeout``; nothing is dispatched before that.

Why two ports: the command API and the extension socket have different trust
boundaries. Keeping them apart means an HTTP command can never be smuggled onto
the extension's socket, and the extension socket can be firewalled independently.

What this module deliberately does **not** do in Phase 3C.1: dispatch a real
generation. ``FlowBridge.submit_generation`` refuses unless
``live_generation_enabled`` is set, and that flag is false by default and unused by
any route. The generation state machine is exercised through
:class:`app.flow.coordinator.GenerationCoordinator` with mocked transports.

Note on the third-party extension: it cannot set request headers from a service
worker, so the credential is presented in the ``hello`` frame. An unmodified DeepX
build sends no credential and is therefore refused — see ``UPSTREAM_BLOCKERS``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from app.flow import protocol as proto
from app.flow.provenance import (
    COMPONENT_COMPANION,
    COMPONENT_EXTENSION,
    COMPONENT_MCP,
    ExtensionProvenance,
    ProvenanceRejected,
    TrustAnchor,
    TrustPolicy,
    UnsupportedProtocol,
    provenance_from_hello,
    resolve_trust_anchor,
    verify_provenance,
)
from app.flow.trust import (
    AttestationRejected,
    ChallengeStore,
    ComponentReport,
    SessionAttestation,
    TrustDecision,
    TrustRegistry,
    UNVERIFIED,
    UntrustedArtifact,
    VerificationSubmission,
    aggregate_trust,
    empty_decision,
    evaluate_live_generation_guard,
    get_registry,
    trust_status_payload,
)
from app.flow.coordinator import (
    CoordinatorError,
    FlowJob,
    GenerationCoordinator,
    LiveGenerationDisabled,
    NotPaired,
)
from app.flow.security import (
    ConnectionLimiter,
    HostOriginPolicy,
    assert_loopback_bind,
    constant_time_equals,
    install_redaction,
    is_loopback_host,
    secret_fingerprint,
)

logger = logging.getLogger("app.flow.bridge")

BRIDGE_VERSION = "1.0-3c1"

#: Response header on every HTTP reply. No CORS headers are ever emitted: a
#: browser page must not be able to read these responses.
_SAFE_HEADERS = (
    "X-Content-Type-Options: nosniff\r\n"
    "Cache-Control: no-store\r\n"
    "Referrer-Policy: no-referrer\r\n"
    "Connection: close\r\n"
)

MAX_HEADER_BYTES = 8 * 1024
REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Payload Too Large",
    421: "Misdirected Request",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class BridgeConfigError(RuntimeError):
    """The bridge was asked to start in an unsafe configuration."""


@dataclass(frozen=True)
class BridgeConfig:
    host: str = "127.0.0.1"
    http_port: int = 8793
    ws_port: int = 8794
    pairing_id: str = ""
    pairing_secret: str = ""
    extension_ids: tuple[str, ...] = ()
    max_request_bytes: int = 64 * 1024
    max_ws_message_bytes: int = 32 * 1024 * 1024
    max_connections: int = 8
    handshake_timeout: float = 5.0
    request_timeout: float = 15.0
    command_timeout: float = 120.0
    max_live_jobs: int = 1
    export_max_bytes: int = 25 * 1024 * 1024
    live_generation_enabled: bool = False
    #: Accept a package whose components sit on different release lines. Off by
    #: default: that is exactly the drift the audit found upstream. Accepting it
    #: never yields a *trusted* session — see :class:`TrustPolicy`.
    allow_component_drift: bool = False
    #: Phase 3C.2.1 trust anchors. Without one of these the bridge can verify that
    #: a HELLO is self-consistent but never that the installed bytes are the bytes
    #: Crusor expects, so the session is reported UNVERIFIED.
    trusted_artifact_sha256: str = ""
    trusted_release: str = ""
    trusted_source_commit: str = ""
    release_manifest_path: str = ""
    release_manifest_public_key: str = ""
    #: Phase 3C.2.2 — require a challenge-bound measurement of the installed
    #: extension before the session can be trusted. Off by default so an existing
    #: pairing still connects; live generation requires it regardless.
    require_installed_measurement: bool = False
    challenge_ttl_seconds: float = 120.0
    measurement_freshness_seconds: float = 300.0
    required_components: tuple[str, ...] = (COMPONENT_EXTENSION,)

    def validate(self) -> "BridgeConfig":
        assert_loopback_bind(self.host)  # raises LoopbackBindError
        if not self.pairing_id or len(self.pairing_secret) < 32:
            raise BridgeConfigError(
                "The bridge refuses to start without a high-entropy pairing credential. "
                "Generate one with `python -m app.flow.pairing --print`."
            )
        if self.http_port and self.http_port == self.ws_port:
            raise BridgeConfigError("HTTP and WebSocket listeners must use different ports.")
        if self.max_request_bytes <= 0 or self.max_connections < 1:
            raise BridgeConfigError("Limits must be positive.")
        if self.require_installed_measurement and not self.trust_policy.has_anchor:
            raise BridgeConfigError(
                "Installed-artifact measurement requires a trusted release anchor: a "
                "measurement with nothing to compare against proves nothing."
            )
        if self.live_generation_enabled and not self.trust_policy.has_anchor:
            # Turning on live generation without an independent artifact anchor
            # would let an unverified build spend real money.
            raise BridgeConfigError(
                "Live generation requires a trusted artifact anchor "
                "(FLOW_TRUSTED_ARTIFACT_SHA256 or FLOW_RELEASE_MANIFEST_PATH)."
            )
        return self

    @property
    def trust_policy(self) -> TrustPolicy:
        return TrustPolicy(
            trusted_artifact_sha256=self.trusted_artifact_sha256,
            trusted_release=self.trusted_release,
            trusted_source_commit=self.trusted_source_commit,
            release_manifest_path=self.release_manifest_path,
            release_manifest_public_key=self.release_manifest_public_key,
            allow_component_drift=self.allow_component_drift,
        )

    @property
    def origins(self) -> HostOriginPolicy:
        return HostOriginPolicy(port=self.http_port, extension_ids=self.extension_ids)


@dataclass
class ExtensionSession:
    session_id: str
    websocket: Any
    extension_id: str = ""
    extension_version: str = ""
    flow_tab_present: bool = False
    authenticated: bool = False
    busy: bool = False
    provenance: ExtensionProvenance | None = None
    provenance_trusted: bool = False
    provenance_state: str = "UNVERIFIED"
    trust: TrustDecision | None = None
    challenge_id: str = ""
    claims_well_formed: bool = False
    components_consistent: bool = False
    artifact_integrity_verified: bool = False
    provenance_checks: tuple[str, ...] = ()
    anchor_source: str = "none"
    provenance_warnings: tuple[str, ...] = ()
    pending: dict[str, asyncio.Future[proto.ResultEnvelope]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "extension_id": self.extension_id,
            "extension_version": self.extension_version,
            "service_worker_version": self.provenance.service_worker_version if self.provenance else "",
            "mcp_version": self.provenance.mcp_version if self.provenance else "",
            "source_commit": self.provenance.source_commit if self.provenance else "",
            "build_id": self.provenance.build_id if self.provenance else "",
            "provenance_trusted": self.provenance_trusted,
            "flow_tab_present": self.flow_tab_present,
            "authenticated": self.authenticated,
            "busy": self.busy,
            "pending_commands": len(self.pending),
        }


class BridgeTransport:
    """``CommandTransport`` over the live extension WebSocket."""

    def __init__(self, bridge: "FlowBridge") -> None:
        self.bridge = bridge

    async def send(self, envelope: proto.CommandEnvelope) -> None:
        await self.bridge.send_command(envelope)

    async def await_result(
        self, command_id: str, timeout: float
    ) -> proto.ResultEnvelope:
        return await self.bridge.await_result(command_id, timeout)


class FlowBridge:
    """Loopback-only bridge with authentication, limits and an allowlist."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config.validate()
        install_redaction(logger, [config.pairing_secret])
        self.policy = config.origins
        #: Rebuilt after binding so an ephemeral port (0) is still validated
        #: against the address actually in use.
        self.http_policy = HostOriginPolicy(port=config.http_port, extension_ids=config.extension_ids)
        self.ws_policy = HostOriginPolicy(port=config.ws_port, extension_ids=config.extension_ids)
        self.limiter = ConnectionLimiter(config.max_connections)
        self.session: ExtensionSession | None = None
        self._http_server: asyncio.AbstractServer | None = None
        self._ws_server: Any = None
        self._sockets: list[Any] = []
        self._stopping = asyncio.Event()
        self.stats: dict[str, int] = {
            "http_requests": 0,
            "http_rejected_auth": 0,
            "http_rejected_origin": 0,
            "http_rejected_host": 0,
            "http_rejected_size": 0,
            "http_rejected_limit": 0,
            "http_rejected_type": 0,
            "ws_connections": 0,
            "ws_rejected_auth": 0,
            "ws_rejected_origin": 0,
            "ws_rejected_provenance": 0,
            "ws_rejected_protocol": 0,
            "ws_rejected_measurement": 0,
            "ws_superseded": 0,
            "commands_sent": 0,
            "commands_denied": 0,
        }
        self.denied_commands: list[str] = []
        self.challenges = ChallengeStore(ttl_seconds=config.challenge_ttl_seconds)
        self.trust_registry = get_registry()
        self.last_trust: TrustDecision | None = None
        self.coordinator = GenerationCoordinator(
            BridgeTransport(self),
            live_generation_enabled=False,  # Phase 3C.1: never live.
            max_live_jobs=config.max_live_jobs,
            command_timeout=config.command_timeout,
            export_max_bytes=config.export_max_bytes,
            # Phase 3C.2.2 §9: the coordinator refuses to dispatch while the
            # installed artifact is anything but VERIFIED, whatever a flag says.
            trust_provider=self.current_trust,
        )

    # ------------------------------------------------------------------ life
    async def start(self) -> list[tuple[str, int]]:
        """Bind both listeners and return the addresses actually in use."""
        # Defence in depth: ``__init__`` already validated, but a config swapped in
        # at runtime must not be able to expose the bridge on a public interface.
        assert_loopback_bind(self.config.host)
        self._http_server = await asyncio.start_server(
            self._handle_http,
            host=self.config.host,
            port=self.config.http_port,
            limit=MAX_HEADER_BYTES,
            reuse_address=True,
        )
        from websockets.asyncio.server import serve as ws_serve

        self._ws_server = await ws_serve(
            self._handle_ws,
            host=self.config.host,
            port=self.config.ws_port,
            process_request=self._process_ws_request,
            max_size=self.config.max_ws_message_bytes,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            server_header="crusor-flow-bridge",
        )
        self._sockets = list(self._http_server.sockets)
        addresses: list[tuple[str, int]] = []
        for sock in self._sockets:
            name = sock.getsockname()
            addresses.append((str(name[0]), int(name[1])))
        for server in [self._ws_server]:
            for sock in getattr(server, "sockets", []) or []:
                name = sock.getsockname()
                addresses.append((str(name[0]), int(name[1])))
        self.http_policy = HostOriginPolicy(
            port=self.http_port, extension_ids=self.config.extension_ids
        )
        self.ws_policy = HostOriginPolicy(
            port=self.ws_port, extension_ids=self.config.extension_ids
        )
        for host, _port in addresses:
            if not is_loopback_host(host):
                await self.stop()
                raise BridgeConfigError(
                    f"The bridge tried to bind a non-loopback address ({host}); refusing."
                )
        logger.info(
            "flow_bridge_started http=%s:%s ws=%s:%s pairing_id=%s pairing=%s",
            self.config.host,
            self.config.http_port,
            self.config.host,
            self.config.ws_port,
            self.config.pairing_id,
            secret_fingerprint(self.config.pairing_secret),
        )
        return addresses

    @property
    def bound_addresses(self) -> list[tuple[str, int]]:
        return list(self._sockets and [(s.getsockname()[0], s.getsockname()[1]) for s in self._sockets] or [])

    @property
    def http_port(self) -> int:
        if self._sockets:
            return int(self._sockets[0].getsockname()[1])
        return self.config.http_port

    @property
    def ws_port(self) -> int:
        sockets = getattr(self._ws_server, "sockets", None) or []
        if sockets:
            return int(sockets[0].getsockname()[1])
        return self.config.ws_port

    async def stop(self) -> None:
        self._stopping.set()
        if self.session and self.session.websocket is not None:
            try:
                await self.session.websocket.close(code=1001, reason="bridge stopping")
            except Exception:  # noqa: BLE001
                pass
            self.session = None
        if self._ws_server is not None:
            try:
                self._ws_server.close()
                await self._ws_server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._ws_server = None
        if self._http_server is not None:
            self._http_server.close()
            try:
                await self._http_server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._http_server = None
        self._sockets = []

    # ------------------------------------------------------------- HTTP side
    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self.limiter.try_acquire():
            self.stats["http_rejected_limit"] += 1
            await self._respond(writer, 429, {"error": {"code": "too_many_connections"}})
            return
        peer = writer.get_extra_info("peername") or ("", 0)
        try:
            if not is_loopback_host(str(peer[0])):
                self.stats["http_rejected_host"] += 1
                await self._respond(writer, 403, {"error": {"code": "non_loopback_peer"}})
                return
            try:
                request = await asyncio.wait_for(
                    self._read_request(reader, writer), timeout=self.config.request_timeout
                )
            except asyncio.TimeoutError:
                await self._respond(writer, 408, {"error": {"code": "request_timeout"}})
                return
            except _HttpProtocolError as exc:
                await self._respond(writer, exc.status, {"error": {"code": exc.code}})
                return

            if request is None:  # already answered
                return
            await self._route(writer, request)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            return
        except Exception:  # noqa: BLE001 - never leak internals to the socket
            logger.exception("flow_bridge_http_error")
            await self._safe_respond(writer, 500, {"error": {"code": "internal_error"}})
        finally:
            self.limiter.release()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _read_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> dict[str, Any] | None:
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > MAX_HEADER_BYTES:
            raise _HttpProtocolError(413, "headers_too_large")
        text = raw.decode("latin-1")
        lines = text.split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError as exc:
            raise _HttpProtocolError(400, "malformed_request_line") from exc
        if not version.startswith("HTTP/1."):
            raise _HttpProtocolError(400, "unsupported_http_version")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise _HttpProtocolError(400, "malformed_header")
            name, value = line.split(":", 1)
            key = name.strip().lower()
            if key in headers:
                raise _HttpProtocolError(400, "duplicate_header")
            headers[key] = value.strip()

        ok, _reason = self.http_policy.check_host(headers.get("host"))
        if not ok:
            self.stats["http_rejected_host"] += 1
            await self._respond(writer, 421, {"error": {"code": "bad_host"}})
            return None

        ok, _reason = self.http_policy.check_origin(headers.get("origin"))
        if not ok:
            self.stats["http_rejected_origin"] += 1
            await self._respond(writer, 403, {"error": {"code": "bad_origin"}})
            return None

        if "transfer-encoding" in headers:
            raise _HttpProtocolError(411, "length_required")

        body = b""
        length = headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError as exc:
                raise _HttpProtocolError(400, "bad_content_length") from exc
            if size < 0:
                raise _HttpProtocolError(400, "bad_content_length")
            if size > self.config.max_request_bytes:
                self.stats["http_rejected_size"] += 1
                raise _HttpProtocolError(413, "body_too_large")
            if size:
                body = await reader.readexactly(size)

        self.stats["http_requests"] += 1
        return {"method": method.upper(), "target": target, "headers": headers, "body": body}

    async def _route(self, writer: asyncio.StreamWriter, request: dict[str, Any]) -> None:
        path = urlsplit(request["target"]).path.rstrip("/") or "/"
        method = request["method"]

        if path not in {"/api/status", "/api/command"}:
            await self._respond(writer, 404, {"error": {"code": "not_found"}})
            return

        if not self._http_authenticated(request["headers"]):
            self.stats["http_rejected_auth"] += 1
            logger.info("flow_bridge_auth_rejected path=%s", path)
            await self._respond(writer, 401, {"error": {"code": "unauthenticated"}})
            return

        if path == "/api/status" and method == "GET":
            await self._respond(writer, 200, self.snapshot())
            return
        if path == "/api/command" and method == "POST":
            # A JSON-only surface removes a whole class of parser-confusion and
            # form-post tricks; the extension only ever sends JSON.
            content_type = request["headers"].get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self.stats["http_rejected_type"] += 1
                await self._respond(
                    writer, 415, {"error": {"code": "unsupported_media_type"}}
                )
                return
            await self._handle_command(writer, request["body"])
            return
        await self._respond(writer, 405, {"error": {"code": "method_not_allowed"}})

    async def _handle_command(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            await self._respond(writer, 400, {"error": {"code": "invalid_json"}})
            return
        if not isinstance(payload, dict):
            await self._respond(writer, 400, {"error": {"code": "invalid_payload"}})
            return

        command = str(payload.get("command") or "")
        if not proto.command_allowed(command):
            self.stats["commands_denied"] += 1
            if len(self.denied_commands) < 50:
                self.denied_commands.append(command[:64])
            logger.info("flow_bridge_command_denied command=%s", command[:64])
            await self._respond(
                writer,
                403,
                {
                    "ok": False,
                    "error": {
                        "code": proto.CommandDenied.code,
                        "message": proto.deny_reason(command),
                        "allowed": list(proto.ALLOWED_COMMANDS),
                    },
                },
            )
            return

        try:
            result = await self.dispatch(
                command,
                dict(payload.get("payload") or {}),
                job_id=str(payload.get("job_id") or ""),
            )
        except proto.CommandDenied as exc:
            await self._respond(writer, 403, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
        except asyncio.TimeoutError:
            await self._respond(
                writer, 504, {"ok": False, "error": {"code": "flow_command_timeout", "message": "The extension did not answer in time."}}
            )
        except CoordinatorError as exc:
            await self._respond(writer, 503, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
        except Exception:  # noqa: BLE001
            logger.exception("flow_bridge_dispatch_error")
            await self._respond(writer, 500, {"ok": False, "error": {"code": "internal_error", "message": "Internal error."}})
        else:
            await self._respond(writer, 200, {"ok": True, "result": result})

    def _http_authenticated(self, headers: dict[str, str]) -> bool:
        presented = headers.get("x-flow-pairing", "")
        if not presented or "." not in presented:
            return False
        presented_id, _, presented_secret = presented.partition(".")
        id_ok = constant_time_equals(presented_id, self.config.pairing_id)
        secret_ok = constant_time_equals(presented_secret, self.config.pairing_secret)
        # Both comparisons always run: no early exit on the id.
        return bool(id_ok and secret_ok)

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {REASONS.get(status, 'Unknown')}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{_SAFE_HEADERS}\r\n"
        ).encode("ascii")
        writer.write(head + body)
        await writer.drain()

    async def _safe_respond(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]
    ) -> None:
        try:
            await self._respond(writer, status, payload)
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- WS side
    def _process_ws_request(self, connection: Any, request: Any) -> Any:
        path = urlsplit(request.path).path.rstrip("/")
        headers = {k.lower(): v for k, v in request.headers.items()}
        if path != "/ws":
            return connection.respond(404, "Not Found")
        ok, _ = self.ws_policy.check_host(headers.get("host"))
        if not ok:
            self.stats["ws_rejected_origin"] += 1
            return connection.respond(421, "Misdirected Request")
        ok, _ = self.ws_policy.check_origin(headers.get("origin"))
        if not ok:
            self.stats["ws_rejected_origin"] += 1
            return connection.respond(403, "Forbidden")
        if not self.limiter.try_acquire():
            self.stats["http_rejected_limit"] += 1
            return connection.respond(429, "Too Many Requests")
        connection.crusor_limit_acquired = True
        return None

    async def _handle_ws(self, websocket: Any) -> None:
        acquired = bool(getattr(websocket, "crusor_limit_acquired", False))
        session = ExtensionSession(session_id=proto.new_id(), websocket=websocket)
        try:
            if not is_loopback_host(str((websocket.remote_address or ("", 0))[0])):
                await websocket.close(code=4403, reason="loopback only")
                return
            self.stats["ws_connections"] += 1
            try:
                first = await asyncio.wait_for(
                    websocket.recv(), timeout=self.config.handshake_timeout
                )
            except asyncio.TimeoutError:
                self.stats["ws_rejected_auth"] += 1
                logger.info("flow_bridge_ws_hello_timeout")
                await websocket.close(code=4401, reason="pairing required")
                return

            ok, close_code, reason = self._authenticate_hello(session, first)
            if not ok:
                if close_code == proto.WS_CLOSE_PROVENANCE:
                    self.stats["ws_rejected_provenance"] += 1
                elif close_code == proto.WS_CLOSE_PROTOCOL:
                    self.stats["ws_rejected_protocol"] += 1
                else:
                    self.stats["ws_rejected_auth"] += 1
                logger.info("flow_bridge_ws_refused code=%s reason=%s", close_code, reason)
                await websocket.close(code=close_code, reason=reason[:120])
                return

            session.authenticated = True
            if self.session is not None and self.session.websocket is not websocket:
                # One extension connection at a time; the newest pairing wins and
                # the old socket is retired rather than left half-authenticated.
                try:
                    await self.session.websocket.close(code=4000, reason="superseded")
                except Exception:  # noqa: BLE001
                    pass
                self.stats["ws_superseded"] += 1
            self.session = session
            logger.info(
                "flow_bridge_session_open session=%s extension=%s version=%s flow_tab=%s",
                session.session_id,
                session.extension_id or "-",
                session.extension_version or "-",
                session.flow_tab_present,
            )
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello_ack",
                        "protocol": proto.PROTOCOL_VERSION,
                        "session_id": session.session_id,
                        "bridge_version": BRIDGE_VERSION,
                        "max_live_jobs": self.config.max_live_jobs,
                        "allowed_commands": list(proto.ALLOWED_COMMANDS),
                        "live_generation": False,
                        "provenance_trusted": session.provenance_trusted,
                        "provenance_state": session.provenance_state,
                        # Three separate facts, reported separately: a well-formed
                        # HELLO is not an integrity check, and the ack must not imply
                        # one by reusing the conjunction for the narrower name.
                        "claims_well_formed": session.claims_well_formed,
                        "components_consistent": session.components_consistent,
                        "artifact_integrity_verified": session.artifact_integrity_verified,
                        "trust_anchor": session.anchor_source,
                        "measurement_required": self._measurement_required(),
                        "required_components": list(self.config.required_components),
                        "provenance": session.provenance.as_dict() if session.provenance else {},
                    }
                )
            )
            await self._issue_challenge(session)
            await self._ws_loop(session)
        except Exception:  # noqa: BLE001
            logger.exception("flow_bridge_ws_error")
        finally:
            for future in session.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("extension disconnected"))
            session.pending.clear()
            if self.session is session:
                self.session = None
            if acquired:
                self.limiter.release()
            logger.info("flow_bridge_session_closed session=%s", session.session_id)

    def _authenticate_hello(
        self, session: ExtensionSession, first: Any
    ) -> tuple[bool, int, str]:
        """Authenticate *and* prove provenance. Returns ``(ok, close_code, reason)``."""
        try:
            payload = json.loads(first if isinstance(first, str) else first.decode("utf-8"))
        except Exception:  # noqa: BLE001
            logger.info("flow_bridge_ws_bad_hello")
            return False, proto.WS_CLOSE_UNAUTHORIZED, "pairing required"

        # Protocol first, so an old build gets an unambiguous "unsupported protocol"
        # rather than a generic pairing failure.
        presented = str(payload.get("protocol") or "") if isinstance(payload, dict) else ""
        if presented not in proto.SUPPORTED_PROTOCOLS:
            return False, proto.WS_CLOSE_PROTOCOL, "unsupported protocol"

        try:
            hello = proto.HelloMessage.model_validate(payload)
        except Exception:  # noqa: BLE001
            logger.info("flow_bridge_ws_bad_hello")
            return False, proto.WS_CLOSE_UNAUTHORIZED, "pairing required"

        id_ok = constant_time_equals(hello.pairing_id, self.config.pairing_id)
        secret_ok = constant_time_equals(hello.pairing_secret, self.config.pairing_secret)
        if not (id_ok and secret_ok):
            return False, proto.WS_CLOSE_UNAUTHORIZED, "pairing required"

        if self.config.extension_ids:
            if hello.extension_id not in self.config.extension_ids:
                logger.info("flow_bridge_ws_extension_not_allowed extension=%s", hello.extension_id or "-")
                return False, proto.WS_CLOSE_UNAUTHORIZED, "pairing required"
        elif hello.extension_id:
            # No allowlist configured means no extension is trusted yet.
            logger.info("flow_bridge_ws_extension_unlisted extension=%s", hello.extension_id[:32])
            return False, proto.WS_CLOSE_UNAUTHORIZED, "pairing required"

        # Phase 3C.2 §10: a claimed version is not a proven one.
        provenance = provenance_from_hello(hello)
        policy = self.config.trust_policy
        try:
            report = verify_provenance(
                provenance,
                policy=policy,
                allow_component_drift=self.config.allow_component_drift,
            )
        except UnsupportedProtocol as exc:
            return False, proto.WS_CLOSE_PROTOCOL, exc.code
        except ProvenanceRejected as exc:
            return False, proto.WS_CLOSE_PROVENANCE, exc.code

        if policy.has_anchor and not report.artifact_integrity_verified:
            # An anchor exists, so "cannot prove these bytes" is a refusal, not a
            # warning: this is the case Phase 3C.2 wrongly called trusted.
            return False, proto.WS_CLOSE_PROVENANCE, "artifact integrity unverified"

        session.extension_id = hello.extension_id
        session.extension_version = hello.extension_version
        session.flow_tab_present = hello.flow_tab_present
        session.provenance = provenance
        session.provenance_trusted = report.trusted
        session.provenance_warnings = report.warnings
        session.provenance_state = report.trust_state
        session.claims_well_formed = report.claims_well_formed
        session.components_consistent = report.components_consistent
        session.artifact_integrity_verified = report.artifact_integrity_verified
        session.provenance_checks = report.checks
        session.anchor_source = report.anchor.source
        return True, 0, ""

    async def _ws_loop(self, session: ExtensionSession) -> None:
        async for message in session.websocket:
            try:
                payload = json.loads(message if isinstance(message, str) else message.decode("utf-8"))
            except Exception:  # noqa: BLE001
                await session.websocket.close(code=4400, reason="malformed frame")
                return
            kind = str(payload.get("type") or "")
            if kind == "heartbeat":
                session.flow_tab_present = bool(payload.get("flow_tab_present"))
                session.busy = bool(payload.get("busy"))
                continue
            if kind == "verification_report":
                if not await self._apply_verification_report(session, payload):
                    return
                continue
            if kind == "result":
                envelope_id = str(payload.get("id") or "")
                future = session.pending.pop(envelope_id, None)
                if future is None or future.done():
                    continue
                try:
                    future.set_result(proto.ResultEnvelope.model_validate(payload))
                except Exception:  # noqa: BLE001
                    future.set_exception(proto.FlowProtocolError("invalid result frame"))
                continue
            await session.websocket.close(code=4400, reason="unknown frame type")
            return

    # --------------------------------------------------- installed artifact
    def _measurement_required(self) -> bool:
        """A measurement is required when an anchor exists and either the operator
        asked for one or the policy makes it mandatory."""
        return bool(self.config.require_installed_measurement and self.config.trust_policy.has_anchor)

    def current_trust(self) -> TrustDecision:
        """The session's trust decision, or an explicit UNVERIFIED one."""
        decision = self.session.trust if self.session else None
        return decision or empty_decision("no_session")

    async def _issue_challenge(self, session: ExtensionSession) -> None:
        if not self._measurement_required():
            return
        attestation = SessionAttestation(
            store=self.challenges,
            anchor=resolve_trust_anchor(self.config.trust_policy),
            required_components=tuple(self.config.required_components),
            freshness_seconds=self.config.measurement_freshness_seconds,
        )
        try:
            challenge = attestation.issue(
                session_id=session.session_id,
                companion_id=session.extension_id or "",
                extension_id=session.extension_id or "",
            )
        except AttestationRejected as exc:
            self.trust_registry.audit("flow.trust.challenge_refused", code=exc.code)
            return
        session.challenge_id = challenge.challenge_id
        frame = proto.VerificationChallengeMessage(
            challenge_id=challenge.challenge_id,
            nonce=challenge.nonce,
            session_id=challenge.session_id,
            expires_at=proto.iso(
                datetime.fromtimestamp(challenge.expires_at, tz=timezone.utc)
            ),
            expected_extension_id=attestation.anchor.extension_id,
            required_components=list(self.config.required_components),
        )
        self.trust_registry.audit(
            "flow.trust.measurement_started",
            session_id=session.session_id,
            challenge_id=challenge.challenge_id,
        )
        try:
            await session.websocket.send(frame.model_dump_json())
        except Exception:  # noqa: BLE001 - the socket may already be gone
            logger.info("flow_bridge_challenge_send_failed session=%s", session.session_id)

    async def _apply_verification_report(
        self, session: ExtensionSession, payload: dict[str, Any]
    ) -> bool:
        """Validate a challenge-bound measurement. Returns False to close the socket."""
        try:
            message = proto.VerificationReportMessage.model_validate(payload)
        except Exception:  # noqa: BLE001
            self.stats["ws_rejected_measurement"] += 1
            await self._close_measurement(session, "malformed verification report")
            return False

        submission = VerificationSubmission(
            challenge_id=message.challenge_id,
            nonce=message.nonce,
            components=tuple(
                ComponentReport(
                    name=item.name,
                    digest=item.digest,
                    extension_id=item.extension_id,
                    version=item.version,
                    measurement_id=item.measurement_id,
                    measured_at=item.measured_at,
                    limitations=tuple(item.limitations),
                    checks=tuple(item.checks),
                )
                for item in message.components
            ),
        )
        anchor = resolve_trust_anchor(self.config.trust_policy)
        attestation = SessionAttestation(
            store=self.challenges,
            anchor=anchor,
            required_components=tuple(self.config.required_components),
            freshness_seconds=self.config.measurement_freshness_seconds,
        )
        provenance = session.provenance
        try:
            evidence = attestation.accept(
                submission,
                session_id=session.session_id,
                companion_id=session.extension_id or "",
                hello_extension_id=session.extension_id or "",
                hello_version=session.extension_version or "",
                hello_digest=(provenance.package_sha256 if provenance else "") or "",
            )
        except AttestationRejected as exc:
            self.stats["ws_rejected_measurement"] += 1
            self.trust_registry.audit(
                f"flow.trust.{exc.code}",
                session_id=session.session_id,
                challenge_id=message.challenge_id,
            )
            self.trust_registry.record(
                session.session_id,
                empty_decision(exc.code),
            )
            await self._close_measurement(session, exc.code)
            return False

        report = verify_provenance(
            provenance,
            policy=self.config.trust_policy,
            allow_component_drift=self.config.allow_component_drift,
        ) if provenance else None
        extension = submission.component(COMPONENT_EXTENSION)
        decision = aggregate_trust(
            source_gate_status=self._source_gate_status(),
            anchor=anchor,
            provenance=report,
            evidence=evidence,
            session_id=session.session_id,
            measured_extension_id=extension.extension_id if extension else "",
            measured_version=extension.version if extension else "",
            measured_digest=extension.digest if extension else "",
        )
        session.trust = self.trust_registry.record(session.session_id, decision)
        self.last_trust = session.trust
        logger.info(
            "flow_bridge_measurement state=%s session=%s measured=%s",
            decision.state,
            session.session_id,
            (decision.measured_digest or "-")[:16],
        )
        return True

    def _source_gate_status(self) -> str:
        """The upstream gate, read once per decision. Absent source is the norm."""
        try:
            from app.flow.source_gate import evaluate_source_gate

            return evaluate_source_gate(os.getenv("FLOW_SOURCE_PACKAGE_PATH", "")).status
        except Exception:  # noqa: BLE001 - a gate that cannot answer must not authorize
            return "LICENSING_BLOCKED"

    async def _close_measurement(self, session: ExtensionSession, reason: str) -> None:
        try:
            await session.websocket.close(
                code=proto.WS_CLOSE_MEASUREMENT, reason=reason[:120]
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- dispatch
    @property
    def paired(self) -> bool:
        return bool(self.session and self.session.authenticated)

    def snapshot(self) -> dict[str, Any]:
        return {
            "bridge_version": BRIDGE_VERSION,
            "protocol": proto.PROTOCOL_VERSION,
            "listen": {
                "host": self.config.host,
                "http_port": self.http_port,
                "ws_port": self.ws_port,
                "loopback_only": True,
            },
            "paired": self.paired,
            "session": self.session.as_dict() if self.session else None,
            "provenance": self.session.provenance.as_dict() if self.session and self.session.provenance else None,
            "provenance_trusted": bool(self.session and self.session.provenance_trusted),
            "provenance_state": self.session.provenance_state if self.session else "UNVERIFIED",
            "claims_well_formed": bool(self.session and self.session.claims_well_formed),
            "components_consistent": bool(self.session and self.session.components_consistent),
            "artifact_integrity_verified": bool(
                self.session and self.session.artifact_integrity_verified
            ),
            "trust_anchor": self.session.anchor_source if self.session else "none",
            "trust": trust_status_payload(
                policy_anchor=resolve_trust_anchor(self.config.trust_policy)
                if self.config.trust_policy.has_anchor
                else TrustAnchor(),
                decision=self.session.trust if self.session else self.last_trust,
                registry=self.trust_registry,
                source_gate_status=self._source_gate_status(),
                required_components=tuple(self.config.required_components),
            ),
            "trust_policy": {
                "anchor_configured": self.config.trust_policy.has_anchor,
                "anchor_kind": (
                    "signed_release_manifest"
                    if self.config.release_manifest_path
                    else ("pinned_digest" if self.config.trusted_artifact_sha256 else "none")
                ),
                "component_drift_allowed": self.config.allow_component_drift,
            },
            "supported_protocols": list(proto.SUPPORTED_PROTOCOLS),
            "allowed_commands": list(proto.ALLOWED_COMMANDS),
            "denied_commands": sorted(proto.DENIED_COMMANDS),
            "denied_requests": list(self.denied_commands),
            "live_generation_enabled": False,
            "max_live_jobs": self.config.max_live_jobs,
            "limits": {
                "max_request_bytes": self.config.max_request_bytes,
                "max_ws_message_bytes": self.config.max_ws_message_bytes,
                "handshake_timeout_seconds": self.config.handshake_timeout,
                "request_timeout_seconds": self.config.request_timeout,
                "command_timeout_seconds": self.config.command_timeout,
                "connections": self.limiter.snapshot(),
            },
            "stats": dict(self.stats),
            "jobs": self.coordinator.snapshot(),
            "upstream_blockers": list(proto.UPSTREAM_BLOCKERS),
        }

    async def dispatch(
        self, command: str, payload: dict[str, Any], *, job_id: str = ""
    ) -> dict[str, Any]:
        """Send one allowlisted command and return its structured result."""
        proto.ensure_allowed(command)
        if not self.paired:
            raise NotPaired()
        envelope = proto.CommandEnvelope(
            id=proto.new_id(),
            command=command,
            job_id=job_id,
            payload=dict(payload),
            issued_at=proto.iso(),
            expires_at=proto.iso(),
        )
        await self.send_command(envelope)
        result = await self.await_result(envelope.id, self.config.command_timeout)
        if not result.ok:
            descriptor = proto.normalise_error(result.error)
            raise CoordinatorError(descriptor.message, details={"code": descriptor.code.value})
        return dict(result.result)

    async def send_command(self, envelope: proto.CommandEnvelope) -> None:
        proto.ensure_allowed(envelope.command)
        session = self.session
        if session is None or not session.authenticated:
            raise NotPaired()
        loop = asyncio.get_running_loop()
        session.pending[envelope.id] = loop.create_future()
        self.stats["commands_sent"] += 1
        try:
            await session.websocket.send(envelope.model_dump_json())
        except Exception:
            session.pending.pop(envelope.id, None)
            raise

    async def await_result(self, command_id: str, timeout: float) -> proto.ResultEnvelope:
        session = self.session
        if session is None:
            raise ConnectionError("extension disconnected")
        future = session.pending.get(command_id)
        if future is None:
            raise ConnectionError("command was not sent")
        return await asyncio.wait_for(future, timeout=timeout)

    async def submit_generation(
        self, kind: str, request: proto.GenerationCommand, *, approval_binding: str
    ) -> FlowJob:
        """Refused unless every §9 prerequisite holds — which today it never does."""
        guard = evaluate_live_generation_guard(
            live_generation_flag=bool(self.config.live_generation_enabled),
            source_gate_status=self._source_gate_status(),
            decision=self.current_trust(),
            approval_binding=approval_binding,
            budget_configured=True,
            idempotency_enforced=True,
        )
        if not guard.allowed:
            raise UntrustedArtifact(
                "Live generation is blocked: " + ", ".join(guard.blockers),
                details={"blockers": list(guard.blockers)},
            )
        if not self.config.live_generation_enabled:
            raise LiveGenerationDisabled()
        return await self.coordinator.submit(
            kind,  # type: ignore[arg-type]
            request,
            approval_binding=approval_binding,
        )


class _HttpProtocolError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def bridge_config_from_settings(settings: Any) -> BridgeConfig:
    return BridgeConfig(
        host=settings.flow_bridge_host,
        http_port=settings.flow_bridge_port,
        ws_port=settings.flow_bridge_ws_port,
        pairing_id=settings.flow_pairing_id,
        pairing_secret=settings.flow_pairing_secret,
        extension_ids=tuple(settings.flow_extension_id_list),
        max_request_bytes=settings.flow_max_request_bytes,
        max_ws_message_bytes=settings.flow_max_ws_message_bytes,
        max_connections=settings.flow_max_connections,
        handshake_timeout=settings.flow_handshake_timeout_seconds,
        request_timeout=settings.flow_request_timeout_seconds,
        command_timeout=settings.flow_command_timeout_seconds,
        max_live_jobs=settings.flow_max_live_jobs,
        export_max_bytes=settings.flow_export_max_bytes,
        live_generation_enabled=False,
        allow_component_drift=bool(getattr(settings, "flow_allow_component_drift", False)),
        trusted_artifact_sha256=getattr(settings, "flow_trusted_artifact_sha256", ""),
        trusted_release=getattr(settings, "flow_trusted_release", ""),
        trusted_source_commit=getattr(settings, "flow_trusted_source_commit", ""),
        release_manifest_path=getattr(settings, "flow_release_manifest_path", ""),
        release_manifest_public_key=getattr(settings, "flow_release_manifest_public_key", ""),
        require_installed_measurement=bool(
            getattr(settings, "flow_require_installed_measurement", False)
        ),
        challenge_ttl_seconds=float(
            getattr(settings, "flow_measurement_challenge_ttl_seconds", 120.0)
        ),
        measurement_freshness_seconds=float(
            getattr(settings, "flow_measurement_freshness_seconds", 300.0)
        ),
        required_components=tuple(
            item.strip()
            for item in str(getattr(settings, "flow_required_components", "chrome_extension")).split(",")
            if item.strip()
        ) or ("chrome_extension",),
    )


async def run_bridge(config: BridgeConfig | None = None) -> None:
    """Run the bridge in the foreground until interrupted (pairing/debugging aid)."""
    from app.core.config import get_settings

    settings = get_settings()
    config = config or bridge_config_from_settings(settings)
    install_redaction(logger, [config.pairing_secret])
    bridge = FlowBridge(config)
    await bridge.start()
    logger.info(
        "flow_bridge_listening http=%s ws=%s extension_ids=%d live_generation=%s",
        bridge.http_port,
        bridge.ws_port,
        len(config.extension_ids),
        False,
    )
    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await bridge.stop()


def main() -> int:
    """``python -m app.flow.bridge`` — starts the loopback bridge only."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run_bridge())
    except LoopbackBindError as exc:
        logger.error("flow_bridge_refused: %s", exc)
        return 2
    except BridgeConfigError as exc:
        logger.error("flow_bridge_misconfigured: %s", exc)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
