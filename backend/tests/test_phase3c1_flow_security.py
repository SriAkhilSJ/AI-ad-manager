"""Phase 3C.1 — DeepX Flow bridge security hardening.

Everything here is mocked or local: no Google account, no Flow tab, no paid model
call, no image or video generation. The extension is simulated by a WebSocket
client speaking ``flow.bridge.v1``, and "media bytes" are PNGs built in-process.

The tests are grouped to mirror the acceptance checklist:

A. transport hardening   — binding, authentication, Host/Origin, limits, redaction
B. command allowlist     — what Crusor can and cannot ask the extension to do
C. generation safety     — one attempt, one live job, idempotency, UNKNOWN_SUBMISSION
D. approval gate         — every real generation needs a bound founder approval
E. asset export contract — bytes verified before an asset exists
F. HTTP surface          — status, companion lifecycle, generation refusal, tickets
G. leak checks           — no token, no secret, no arbitrary URL anywhere
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import websockets

from app.core.config import get_settings
from app.creative.media import encode_png
from app.flow import protocol as proto
from app.flow.bridge import BridgeConfig, BridgeConfigError, FlowBridge
from app.flow.coordinator import (
    ApprovalRequired,
    CommandTransport,
    FlowJob,
    GenerationCoordinator,
    JobInProgress,
    LiveGenerationDisabled,
    UnknownSubmissionRemains,
)
from app.flow.security import (
    ConnectionLimiter,
    HostOriginPolicy,
    LoopbackBindError,
    RedactingFilter,
    assert_loopback_bind,
    constant_time_equals,
    entropy_bits,
    generate_pairing_id,
    generate_pairing_secret,
    is_loopback_host,
    redact,
    secret_fingerprint,
)
from app.flow.protocol import (
    ALLOWED_COMMANDS,
    DENIED_COMMANDS,
    GenerationCommand,
    ResultEnvelope,
    SubmissionState,
)

EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
FLOW_API = "/api/v1/flow"


def _png(width: int = 24, height: int = 30, shade: int = 40) -> bytes:
    return encode_png(width, height, bytes([shade, shade, shade]) * (width * height))


# --------------------------------------------------------------------------- A
# A. transport hardening
# --------------------------------------------------------------------------- #
@pytest.fixture
def pairing() -> dict[str, str]:
    return {"id": generate_pairing_id(), "secret": generate_pairing_secret()}


@pytest.fixture
async def bridge(pairing: dict[str, str]):  # type: ignore[no-untyped-def]
    instance = FlowBridge(
        BridgeConfig(
            pairing_id=pairing["id"],
            pairing_secret=pairing["secret"],
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
            handshake_timeout=2.0,
            request_timeout=5.0,
            command_timeout=3.0,
        )
    )
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
def auth_headers(pairing: dict[str, str]):  # type: ignore[no-untyped-def]
    def _headers(secret: str | None = None, **extra: str) -> dict[str, str]:
        headers = {
            "X-Flow-Pairing": f"{pairing['id']}.{secret or pairing['secret']}",
            **extra,
        }
        return headers

    return _headers


async def _extension(bridge: FlowBridge, pairing: dict[str, str], **hello_extras: Any):  # type: ignore[no-untyped-def]
    """Open an authenticated extension socket and return the ack."""
    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws")
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "protocol": proto.PROTOCOL_VERSION,
                "pairing_id": pairing["id"],
                "pairing_secret": pairing["secret"],
                "extension_id": EXT_ID,
                "extension_version": "1.3.0",
                # Phase 3C.2 requires proven provenance, not a claimed version.
                "service_worker_version": "1.3.0",
                "mcp_version": "1.3.0",
                "source_commit": "0343884",
                "build_id": "acceptance-build-1",
                "flow_tab_present": True,
                **hello_extras,
            }
        )
    )
    ack = json.loads(await asyncio.wait_for(ws.recv(), 3))
    return ws, ack


def test_non_loopback_bind_is_refused_before_any_socket_exists():
    for host in ("0.0.0.0", "::", "", "192.168.1.10", "example.com", "0.0.0.0:8793"):
        assert is_loopback_host(host) is False
        with pytest.raises(LoopbackBindError):
            assert_loopback_bind(host)
        with pytest.raises(LoopbackBindError):
            FlowBridge(
                BridgeConfig(
                    host=host,
                    pairing_id="p",
                    pairing_secret="s" * 43,
                    http_port=0,
                    ws_port=0,
                )
            )


def test_loopback_hosts_are_accepted():
    for host in ("127.0.0.1", "::1", "localhost"):
        assert is_loopback_host(host) is True
        assert assert_loopback_bind(host) == host


def test_bridge_refuses_missing_or_weak_pairing_credentials():
    for secret, pid in (("", "id"), ("short", "id"), ("x" * 32, "")):
        with pytest.raises(BridgeConfigError):
            BridgeConfig(
                pairing_id=pid, pairing_secret=secret, http_port=0, ws_port=0
            ).validate()


def test_bridge_refuses_identical_listener_ports():
    with pytest.raises(BridgeConfigError):
        BridgeConfig(
            pairing_id="id", pairing_secret="s" * 43, http_port=8793, ws_port=8793
        ).validate()
    # Port 0 means "pick an ephemeral port" and is always allowed.
    BridgeConfig(pairing_id="id", pairing_secret="s" * 43, http_port=0, ws_port=0).validate()


async def test_actual_listening_address_is_loopback(bridge: FlowBridge):
    """Acceptance requires the *real* socket address, not the configured one."""
    http_sockets = bridge._http_server.sockets  # type: ignore[union-attr]
    ws_sockets = bridge._ws_server.sockets
    assert http_sockets and ws_sockets
    for sock in [*http_sockets, *ws_sockets]:
        # ``websockets`` wraps the asyncio transport socket; both expose getsockname().
        assert hasattr(sock, "getsockname")
        host, port = sock.getsockname()[:2]
        assert host in ("127.0.0.1", "::ffff:127.0.0.1"), host
        assert port > 0
    assert bridge.snapshot()["listen"]["loopback_only"] is True
    assert bridge.snapshot()["live_generation_enabled"] is False


async def test_bridge_refuses_to_bind_a_public_interface_at_runtime():
    """The check runs again in ``start()``, so a mutated config cannot slip through."""
    instance = FlowBridge(
        BridgeConfig(pairing_id="id", pairing_secret="s" * 43, http_port=0, ws_port=0)
    )
    object.__setattr__(instance.config, "host", "0.0.0.0")
    with pytest.raises(LoopbackBindError):
        await instance.start()
    assert instance._http_server is None


async def test_http_requires_a_pairing_credential(bridge: FlowBridge, auth_headers):
    base = f"http://127.0.0.1:{bridge.http_port}"
    async with httpx.AsyncClient() as client:
        missing = await client.get(f"{base}/api/status")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "unauthenticated"

        wrong_secret = await client.get(
            f"{base}/api/status", headers=auth_headers("wrong-secret-value")
        )
        assert wrong_secret.status_code == 401

        wrong_id = await client.get(
            f"{base}/api/status",
            headers={"X-Flow-Pairing": f"not-{bridge.config.pairing_id}.{bridge.config.pairing_secret}"},
        )
        assert wrong_id.status_code == 401

        no_dot = await client.get(
            f"{base}/api/status", headers={"X-Flow-Pairing": bridge.config.pairing_secret}
        )
        assert no_dot.status_code == 401

        ok = await client.get(f"{base}/api/status", headers=auth_headers())
        assert ok.status_code == 200


async def test_constant_time_comparison_handles_mismatched_lengths():
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
    assert constant_time_equals("abc", "abcd") is False
    assert constant_time_equals("", "") is True
    assert constant_time_equals(None, "abc") is False


def test_pairing_secret_has_256_bits_of_entropy():
    secret = generate_pairing_secret()
    assert len(secret) >= 43
    assert entropy_bits(secret) >= 256
    assert generate_pairing_secret() != generate_pairing_secret()
    assert generate_pairing_id() != generate_pairing_id()
    assert len(secret_fingerprint(secret)) == 16


async def test_bridge_rejects_foreign_host_and_origin(bridge: FlowBridge, auth_headers):
    base = f"http://127.0.0.1:{bridge.http_port}"
    async with httpx.AsyncClient() as client:
        bad_host = await client.post(
            f"{base}/api/command",
            headers=auth_headers(Host="evil.example.com"),
            json={"command": "status", "payload": {}},
        )
        assert bad_host.status_code == 421

        bad_origin = await client.post(
            f"{base}/api/command",
            headers=auth_headers(Origin="https://evil.example.com"),
            json={"command": "status", "payload": {}},
        )
        assert bad_origin.status_code == 403

        good_origin = await client.post(
            f"{base}/api/command",
            headers=auth_headers(Origin=f"chrome-extension://{EXT_ID}"),
            json={"command": "status", "payload": {}},
        )
        # Origin is fine; the extension simply is not paired yet.
        assert good_origin.status_code == 503
        assert good_origin.json()["error"]["code"] == "flow_not_paired"


def test_no_cors_headers_are_ever_advertised():
    policy = HostOriginPolicy(port=8793, extension_ids=(EXT_ID,))
    assert policy.allowed_hosts() == (
        "127.0.0.1:8793",
        "[::1]:8793",
        "localhost:8793",
    )
    assert policy.check_host("127.0.0.1:8793")[0] is True
    assert policy.check_host("10.0.0.5:8793")[0] is False
    assert policy.check_host(None)[0] is False
    # Unknown extensions are not trusted; a missing Origin (non-browser) is allowed.
    assert policy.check_origin(None)[0] is True
    assert policy.check_origin(f"chrome-extension://{EXT_ID}")[0] is True
    assert policy.check_origin("chrome-extension://someoneelse")[0] is False
    assert policy.check_origin("https://flow.google.com")[0] is False
    assert "*" not in " ".join(policy.allowed_origins() + policy.allowed_hosts())


async def test_request_limits_are_enforced(bridge: FlowBridge, auth_headers):
    base = f"http://127.0.0.1:{bridge.http_port}"
    async with httpx.AsyncClient() as client:
        oversize = await client.post(
            f"{base}/api/command",
            headers=auth_headers(),
            content=b"x" * (bridge.config.max_request_bytes + 1024),
        )
        assert oversize.status_code == 413

        wrong_type = await client.post(
            f"{base}/api/command",
            headers={**auth_headers(), "Content-Type": "text/plain"},
            content=b'{"command":"status"}',
        )
        assert wrong_type.status_code == 415

        unknown_route = await client.post(
            f"{base}/api/eval", headers=auth_headers(), json={"code": "1+1"}
        )
        assert unknown_route.status_code == 404

        wrong_method = await client.delete(f"{base}/api/status", headers=auth_headers())
        assert wrong_method.status_code == 405


async def test_connection_limiter_refuses_extra_connections():
    limiter = ConnectionLimiter(limit=2)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    limiter.release()
    assert limiter.try_acquire() is True
    snapshot = limiter.snapshot()
    assert snapshot == {"limit": 2, "active": 2, "rejected": 1}


def test_secrets_are_redacted_from_logs_and_text():
    secret = "S" * 43
    message = f"pairing credential {secret} rejected"
    assert secret not in redact(message, secrets_to_scrub=[secret])
    assert "[redacted]" in redact(message, secrets_to_scrub=[secret])
    # Pattern-based redaction catches a bearer token even without the secret list.
    assert "ya29." not in redact("Authorization: Bearer ya29.abcdefghijklmnop")

    record = logging.LogRecord(
        "app.flow.bridge", logging.INFO, __file__, 1, "hello with %s", (secret,), None
    )
    RedactingFilter(secrets_to_scrub=[secret]).filter(record)
    assert secret not in record.getMessage()


async def test_unauth_websocket_is_closed_with_4401(bridge: FlowBridge, pairing):
    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws")
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "protocol": proto.PROTOCOL_VERSION,
                    "pairing_id": pairing["id"],
                    "pairing_secret": "nope",
                    "extension_id": EXT_ID,
                }
            )
        )
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await asyncio.wait_for(ws.recv(), 3)
        assert excinfo.value.rcvd.code == 4401
    finally:
        await ws.close()


async def test_websocket_hello_without_a_frame_times_out(bridge: FlowBridge):
    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws")
    try:
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await asyncio.wait_for(ws.recv(), 5)
        assert excinfo.value.rcvd.code == 4401
    finally:
        await ws.close()


async def test_untrusted_extension_id_is_refused(bridge: FlowBridge, pairing):
    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws")
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "protocol": proto.PROTOCOL_VERSION,
                    "pairing_id": pairing["id"],
                    "pairing_secret": pairing["secret"],
                    "extension_id": "some-other-extension-id",
                }
            )
        )
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await asyncio.wait_for(ws.recv(), 3)
        assert excinfo.value.rcvd.code == 4401
    finally:
        await ws.close()


async def test_hello_ack_is_honest_and_allowlisted(bridge: FlowBridge, pairing):
    ws, ack = await _extension(bridge, pairing)
    try:
        assert ack["type"] == "hello_ack"
        assert ack["protocol"] == proto.PROTOCOL_VERSION
        assert ack["live_generation"] is False
        assert ack["allowed_commands"] == list(ALLOWED_COMMANDS)
        assert denied_keys(bridge.snapshot()) == set()
        assert bridge.paired is True
    finally:
        await ws.close()


def denied_keys(snapshot: dict[str, Any]) -> set[str]:
    """No denied command may appear in the allowlist presented to the extension."""
    allowed = set(snapshot["allowed_commands"])
    return allowed & set(DENIED_COMMANDS)


async def test_unexpected_frame_after_hello_closes_the_socket(bridge: FlowBridge, pairing):
    ws, _ = await _extension(bridge, pairing)
    try:
        await ws.send(json.dumps({"type": "eval_script", "code": "fetch('/x')"}))
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await asyncio.wait_for(ws.recv(), 3)
        assert excinfo.value.rcvd.code == 4400
    finally:
        await ws.close()


async def test_second_extension_socket_supersedes_the_first(bridge: FlowBridge, pairing):
    first, _ = await _extension(bridge, pairing)
    second, _ = await _extension(bridge, pairing)
    try:
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await asyncio.wait_for(first.recv(), 5)
        assert excinfo.value.rcvd.code == 4000
        assert bridge.session is not None
        assert bridge.session.extension_id == EXT_ID
        assert bridge.stats["ws_superseded"] == 1
    finally:
        await second.close()
        await first.close()


# --------------------------------------------------------------------------- B
# B. command allowlist
# --------------------------------------------------------------------------- #
def test_allowlist_is_exactly_the_five_read_or_generate_operations():
    assert ALLOWED_COMMANDS == (
        "status",
        "generate_image",
        "generate_video",
        "check_video_status",
        "export_asset",
    )


@pytest.mark.parametrize("command", sorted(DENIED_COMMANDS))
def test_every_dangerous_command_is_denied(command: str):
    with pytest.raises(proto.CommandDenied):
        proto.ensure_allowed(command)
    assert proto.command_allowed(command) is False
    assert proto.deny_reason(command)


@pytest.mark.parametrize(
    "command",
    [
        "",
        "STATUS",
        " status",
        "status;rm -rf /",
        "__proto__",
        "eval",
        "execute_js",
        "native_mouse_move",
        "mcp.call",
    ],
)
def test_unknown_and_dangerous_commands_are_denied(command: str):
    assert proto.command_allowed(command) is False
    with pytest.raises(proto.CommandDenied):
        proto.ensure_allowed(command)


async def test_denied_command_is_rejected_over_http(bridge: FlowBridge, auth_headers):
    base = f"http://127.0.0.1:{bridge.http_port}"
    async with httpx.AsyncClient() as client:
        for command in ("eval_script", "native_click", "batch_generate", "api_post", "get_token"):
            response = await client.post(
                f"{base}/api/command",
                headers=auth_headers(),
                json={"command": command, "payload": {}},
            )
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "command_not_allowed"


async def test_allowlisted_command_round_trip_through_the_bridge(
    bridge: FlowBridge, pairing, auth_headers
):
    ws, _ = await _extension(bridge, pairing)
    try:
        async def serve() -> None:
            frame = json.loads(await ws.recv())
            assert frame["command"] == "status"
            await ws.send(
                json.dumps(
                    {
                        "type": "result",
                        "id": frame["id"],
                        "ok": True,
                        "job_id": frame["job_id"],
                        "result": {"ready": True, "flow_tab": True},
                    }
                )
            )

        server = asyncio.create_task(serve())
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url(bridge)}/api/command",
                headers=auth_headers(),
                json={"command": "status", "payload": {}},
            )
        await server
        assert response.status_code == 200
        assert response.json()["result"]["ready"] is True
    finally:
        await ws.close()


def base_url(bridge: FlowBridge) -> str:
    return f"http://127.0.0.1:{bridge.http_port}"


# --------------------------------------------------------------------------- C
# C. generation safety
# --------------------------------------------------------------------------- #
class ScriptedTransport(CommandTransport):
    """Deterministic, in-memory extension. Records every envelope it is sent."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.sent: list[proto.CommandEnvelope] = []
        self._results = list(results or [])
        self._pending: dict[str, asyncio.Future[Any]] = {}

    async def send(self, envelope: proto.CommandEnvelope) -> None:
        self.sent.append(envelope)
        outcome = self._results.pop(0) if self._results else None
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        if isinstance(outcome, Exception):
            future.set_exception(outcome)
        elif outcome is not None:
            future.set_result(outcome)
        self._pending[envelope.id] = future

    async def await_result(self, command_id: str, timeout: float) -> Any:
        return await asyncio.wait_for(self._pending[command_id], timeout)


def _request(prompt: str = "A charcoal frame with a terminal glow") -> GenerationCommand:
    return GenerationCommand(
        prompt=prompt, aspect_ratio="4:5", model="imagen-3", count=1
    )


def _ok_frame(job_id: str = "", media_id: str = "media-0001") -> ResultEnvelope:
    return ResultEnvelope(
        type="result", id="cmd", ok=True, job_id=job_id, result={"media_id": media_id}
    )


async def test_generation_is_impossible_while_disabled():
    transport = ScriptedTransport()
    coordinator = GenerationCoordinator(transport, live_generation_enabled=False)
    with pytest.raises(LiveGenerationDisabled):
        await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert transport.sent == [], "no command may be written while live generation is off"


async def test_submit_requires_an_approval_binding():
    transport = ScriptedTransport()
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    with pytest.raises(ApprovalRequired):
        await coordinator.submit("image", _request(), approval_binding="")
    assert transport.sent == []


async def test_one_request_is_one_attempt():
    transport = ScriptedTransport([_ok_frame(media_id="media-0001")])
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state is SubmissionState.SUBMITTED
    assert job.attempts == 1
    assert len(transport.sent) == 1
    assert transport.sent[0].payload["count"] == 1
    assert transport.sent[0].payload["one_attempt_only"] is True


async def test_repeated_identical_request_is_idempotent():
    transport = ScriptedTransport(
        [_ok_frame(media_id="media-0001")]
    )
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    first = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    second = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert first.job_id == second.job_id
    assert len(transport.sent) == 1, "a replay must not reach the extension twice"


async def test_only_one_live_generation_at_a_time():
    transport = ScriptedTransport(
        [
            ResultEnvelope(
                type="result", id="c1", ok=True, job_id="", result={"media_id": "media-0001"}
            )
        ]
    )
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    await coordinator.submit("image", _request("first"), approval_binding="a" * 64)
    # The first job is SUBMITTED (live until exported/terminal).
    with pytest.raises(JobInProgress):
        await coordinator.submit("video", _request("second"), approval_binding="b" * 64)
    assert len(coordinator.live_jobs) == 1


async def test_ambiguous_submission_never_resubmits():
    transport = ScriptedTransport([asyncio.TimeoutError()])
    coordinator = GenerationCoordinator(
        transport, live_generation_enabled=True, command_timeout=0.05
    )
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state is SubmissionState.UNKNOWN_SUBMISSION
    assert job.error is not None
    assert job.error.code is proto.FlowErrorCode.UNKNOWN_SUBMISSION
    assert job.error.retryable is False

    sent_before = len(transport.sent)
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="a" * 64)
    assert len(transport.sent) == sent_before, "an unknown submission must never be retried"


async def test_failure_after_dispatch_is_treated_as_unknown():
    transport = ScriptedTransport(
        [
            ResultEnvelope(
                type="result",
                id="c1",
                ok=False,
                job_id="",
                error={"code": "command_failed", "message": "the tab crashed"},
            )
        ]
    )
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state is SubmissionState.UNKNOWN_SUBMISSION
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="a" * 64)


async def test_proven_no_dispatch_failure_is_a_distinct_retryable_state():
    transport = ScriptedTransport(
        [
            ResultEnvelope(
                type="result",
                id="c1",
                ok=False,
                job_id="",
                error={
                    "code": "command_failed",
                    "message": "the extension refused before submitting",
                    "dispatched": False,
                },
            ),
            ResultEnvelope(
                type="result", id="c2", ok=True, job_id="", result={"media_id": "media-0002"}
            ),
        ]
    )
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state is SubmissionState.FAILED
    assert job.error is not None and job.error.retryable is True
    retried = await coordinator.retry(job.job_id, approval_binding="c" * 64)
    assert retried.attempts == 2
    assert retried.state is SubmissionState.SUBMITTED


async def test_foreign_job_id_cannot_satisfy_a_job():
    transport = ScriptedTransport(
        [ResultEnvelope(type="result", id="c1", ok=True, job_id="someone-else", result={"media_id": "media-9"})]
    )
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state is SubmissionState.UNKNOWN_SUBMISSION
    assert job.media_id == ""


async def test_a_stale_tile_cannot_be_claimed_by_two_jobs():
    coordinator = GenerationCoordinator(ScriptedTransport(), live_generation_enabled=False)
    job = FlowJob(
        job_id="job-1",
        kind="image",
        request=_request("first prompt"),
        approval_binding="a" * 64,
    )
    coordinator._jobs[job.job_id] = job
    coordinator._claim_media(job, "media-0001")
    other = FlowJob(
        job_id="job-2",
        kind="image",
        request=_request("second prompt"),
        approval_binding="b" * 64,
    )
    with pytest.raises(Exception) as excinfo:
        coordinator._claim_media(other, "media-0001")
    assert "already belongs to another job" in str(excinfo.value)


async def test_image_jobs_are_not_polled_into_a_false_completion():
    transport = ScriptedTransport([_ok_frame(media_id="media-0001")])
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    sent_before = len(transport.sent)
    polled = await coordinator.check_status(job.job_id)
    assert polled.state is SubmissionState.SUBMITTED
    assert len(transport.sent) == sent_before


async def test_export_requires_a_correlated_media_id():
    transport = ScriptedTransport()
    coordinator = GenerationCoordinator(transport, live_generation_enabled=True)
    job = FlowJob(job_id="job-x", kind="image", request=_request(), approval_binding="a" * 64)
    coordinator._jobs[job.job_id] = job
    with pytest.raises(Exception) as excinfo:
        await coordinator.export(job.job_id)
    assert "no correlated media id" in str(excinfo.value).lower()


def _export_frame(job_id: str, media: bytes, *, sha: str | None = None, size: int | None = None) -> ResultEnvelope:
    return ResultEnvelope(
        type="result",
        id="export-cmd",
        ok=True,
        job_id=job_id,
        result={
            "job_id": job_id,
            "media_id": "media-0001",
            "content_type": "image/png",
            "sha256": sha or hashlib.sha256(media).hexdigest(),
            "size_bytes": size if size is not None else len(media),
            "bytes_b64": base64.b64encode(media).decode(),
        },
    )


def _job_with_media(job_id: str = "job-1", media_id: str = "media-0001") -> tuple[GenerationCoordinator, FlowJob]:
    coordinator = GenerationCoordinator(ScriptedTransport(), live_generation_enabled=True)
    job = FlowJob(job_id=job_id, kind="image", request=_request(), approval_binding="a" * 64)
    coordinator._jobs[job_id] = job
    coordinator._claim_media(job, media_id)
    return coordinator, job


async def test_export_returns_verified_bytes():
    media = _png()
    coordinator, job = _job_with_media()
    coordinator.transport = ScriptedTransport([_export_frame(job.job_id, media)])
    payload = await coordinator.export(job.job_id)
    assert payload.sha256 == hashlib.sha256(media).hexdigest()
    assert payload.size_bytes == len(media)
    assert payload.content_type == "image/png"
    assert payload.data == media


async def test_export_rejects_bytes_that_do_not_match_the_checksum():
    media = _png()
    coordinator, job = _job_with_media()
    coordinator.transport = ScriptedTransport(
        [_export_frame(job.job_id, media, sha=hashlib.sha256(b"other bytes").hexdigest())]
    )
    with pytest.raises(Exception) as excinfo:
        await coordinator.export(job.job_id)
    assert "hash" in str(excinfo.getrepr()).lower() or "hash" in str(excinfo.value).lower()


async def test_export_rejects_a_size_or_type_mismatch():
    media = _png()
    for frame in (
        _export_frame("job-1", media, size=len(media) + 10),
        ResultEnvelope(
            type="result",
            id="x",
            ok=True,
            job_id="job-1",
            result={
                "job_id": "job-1",
                "media_id": "media-0001",
                "content_type": "text/html",
                "sha256": hashlib.sha256(media).hexdigest(),
                "size_bytes": len(media),
                "bytes_b64": base64.b64encode(media).decode(),
            },
        ),
        # Bytes for a different job are never accepted.
        _export_frame("job-2", media),
    ):
        coordinator, job = _job_with_media()
        coordinator.transport = ScriptedTransport([frame])
        with pytest.raises(Exception):
            await coordinator.export(job.job_id)
        assert job.result.get("export_verified") is None


# --------------------------------------------------------------------------- D
# D. approval gate
# --------------------------------------------------------------------------- #
async def _content_with_brief(session, users, brand_row):  # type: ignore[no-untyped-def]
    """A DRAFT content item, its first version, and a creative brief for it."""
    from app.core.config import Settings
    from app.models.content import ContentItem, ContentVersion
    from app.services.creative_briefs import CreativeBriefService

    item = ContentItem(
        brand_profile_id=brand_row.id,
        title="Flow gated post",
        platform="instagram",
        format="social_post",
        status="DRAFT",
        created_by_agent=None,
    )
    session.add(item)
    await session.flush()
    version = ContentVersion(
        content_item_id=item.id,
        version_number=1,
        headline="Ship fast",
        body="Crusor AI is an AI-powered IDE. Build and ship with ease.",
        call_to_action="Try it.",
        content_hash="flow-content-hash-1",
    )
    session.add(version)
    await session.flush()
    item.current_version_id = version.id
    await session.flush()
    brief, _created = await CreativeBriefService(
        session, Settings(environment="test")
    ).create(content_version_id=version.id, asset_type="image")
    await session.flush()
    return item, version, brief


def _binding_for(brief, prompt: str = "A charcoal frame", **overrides):  # type: ignore[no-untyped-def]
    from app.services.flow_approval import FlowGenerationBinding

    request = GenerationCommand(
        prompt=prompt,
        aspect_ratio=overrides.pop("aspect_ratio", "4:5"),
        model=overrides.pop("model", "imagen-3"),
        duration_seconds=overrides.pop("duration_seconds", 0),
        count=1,
    )
    return FlowGenerationBinding.from_brief(
        brief, request=request, provider="flow", model="imagen-3"
    )


async def test_flow_generation_is_refused_without_an_approval(session, users, brand_row):
    from app.services.flow_approval import ApprovalRequiredError, FlowApprovalGate

    _, _, brief = await _content_with_brief(session, users, brand_row)
    with pytest.raises(ApprovalRequiredError):
        await FlowApprovalGate(session).authorize(_binding_for(brief))


async def test_a_3b_approval_does_not_authorise_a_flow_generation(session, users, brand_row):
    from app.models.approval import ApprovalRecord
    from app.services.flow_approval import ApprovalRequiredError, FlowApprovalGate

    item, version, brief = await _content_with_brief(session, users, brand_row)
    session.add(
        ApprovalRecord(
            content_item_id=item.id,
            content_version_id=version.id,
            action="approve",
            actor_id=users["founder"].id,
            actor_role="founder",
            reason="3B approval, no flow binding",
        )
    )
    await session.flush()
    with pytest.raises(ApprovalRequiredError) as excinfo:
        await FlowApprovalGate(session).authorize(_binding_for(brief))
    assert "does not cover a Flow generation" in str(excinfo.value)


async def _stamp_approval(session, users, item, version, binding):  # type: ignore[no-untyped-def]
    from app.models.approval import ApprovalRecord

    record = ApprovalRecord(
        content_item_id=item.id,
        content_version_id=version.id,
        action="approve",
        actor_id=users["founder"].id,
        actor_role="founder",
        reason="founder approved this exact Flow combination",
        flow_binding_hash=binding.hash(),
        flow_prompt_hash=binding.prompt_hash,
        flow_provider=binding.provider,
        flow_model=binding.model,
        flow_aspect_ratio=binding.aspect_ratio,
        flow_duration_seconds=binding.duration_seconds,
        flow_output_count=binding.output_count,
    )
    session.add(record)
    await session.flush()
    return record


async def test_a_matching_approval_authorises_exactly_that_binding(session, users, brand_row):
    from datetime import datetime, timezone

    from app.services.flow_approval import ApprovalRequiredError, FlowApprovalGate

    item, version, brief = await _content_with_brief(session, users, brand_row)
    binding = _binding_for(brief)
    record = await _stamp_approval(session, users, item, version, binding)
    authorised = await FlowApprovalGate(session).authorize(binding)
    assert authorised.id == record.id
    assert authorised.flow_binding_hash == binding.hash()

    # Any change to the combination breaks the approval.
    for changed in (
        _binding_for(brief, prompt="A different prompt"),
        _binding_for(brief, aspect_ratio="16:9"),
        _binding_for(brief, model="veo-3"),
        _binding_for(brief, duration_seconds=8),
    ):
        with pytest.raises(ApprovalRequiredError):
            await FlowApprovalGate(session).authorize(changed)
    assert binding.hash() != _binding_for(brief, prompt="A different prompt").hash()


async def test_an_invalidated_approval_stops_authorising(session, users, brand_row):
    from app.services.flow_approval import ApprovalRequiredError, FlowApprovalGate

    item, version, brief = await _content_with_brief(session, users, brand_row)
    binding = _binding_for(brief)
    record = await _stamp_approval(session, users, item, version, binding)
    record.invalidated_at = datetime.now(timezone.utc)
    record.invalidated_reason = "brief changed"
    await session.flush()
    with pytest.raises(ApprovalRequiredError):
        await FlowApprovalGate(session).authorize(binding)


# --------------------------------------------------------------------------- E
# E. asset export contract
# --------------------------------------------------------------------------- #
@pytest.fixture
def companion_settings(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLOW_COMPANION_ID", "companion-test")
    monkeypatch.setenv("FLOW_COMPANION_SECRET", "c" * 43)
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


async def test_export_ticket_accepts_only_verified_bytes(
    session, users, brand_row, companion_settings, tmp_path
):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path / "assets")
    service = FlowAssetExportService(session, companion_settings, storage)
    media = _png()
    checksum = hashlib.sha256(media).hexdigest()
    _, _, brief = await _content_with_brief(session, users, brand_row)

    ticket, token = await service.create_ticket(
        companion_id="companion-test",
        brief_id=brief.id,
        expected_sha256=checksum,
        content_type="image/png",
        filename="flow-frame.png",
        created_by=users["marketer"].id,
    )
    assert ticket.token_hash != token, "the raw token is never stored"
    assert ticket.status == "pending"

    ticket, asset = await service.accept_upload(
        token=token, data=media, companion_id="companion-test"
    )
    assert ticket.status == "consumed"
    assert ticket.checksum == checksum
    assert ticket.received_bytes == len(media)
    assert asset.id == ticket.asset_id
    assert asset.checksum == checksum
    assert asset.provider == "flow"
    assert asset.is_mock is False
    assert (asset.width, asset.height) == (24, 30)
    assert await storage.get(asset.storage_key) == media
    assert asset.metadata_json["verified_sha256"] == checksum
    assert asset.metadata_json["source"] == "flow_companion"

    # Single use.
    with pytest.raises(Exception):
        await service.accept_upload(token=token, data=media, companion_id="companion-test")


async def test_export_checksum_mismatch_never_creates_an_asset(
    session, users, brand_row, companion_settings, tmp_path
):
    from sqlalchemy import select

    from app.models.creative import CreativeAsset
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path / "assets")
    service = FlowAssetExportService(session, companion_settings, storage)
    media = _png()
    ticket, token = await service.create_ticket(
        companion_id="companion-test",
        expected_sha256=hashlib.sha256(b"expected bytes").hexdigest(),
        content_type="image/png",
        filename="frame.png",
    )
    with pytest.raises(Exception) as excinfo:
        await service.accept_upload(token=token, data=media, companion_id="companion-test")
    assert "checksum" in str(excinfo.value).lower()
    await session.flush()
    assert ticket.status == "failed"
    assert ticket.error_code == "export_checksum_mismatch"
    assets = list((await session.execute(select(CreativeAsset))).scalars().all())
    assert assets == [], "an unverified upload must not create an asset"


async def test_export_rejects_unrecognised_bytes_and_wrong_types(
    session, companion_settings, tmp_path
):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "a"))
    _, token = await service.create_ticket(companion_id="companion-test", filename="x.png")
    with pytest.raises(Exception) as excinfo:
        await service.accept_upload(
            token=token, data=b"<html>not media</html>", companion_id="companion-test"
        )
    assert "not a recognised" in str(excinfo.value).lower()

    service2 = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "b"))
    # Declared as video, uploaded a PNG: the declared type must match the bytes.
    _, token2 = await service2.create_ticket(
        companion_id="companion-test", content_type="video/mp4", filename="x.mp4"
    )
    with pytest.raises(Exception) as excinfo:
        await service2.accept_upload(token=token2, data=_png(), companion_id="companion-test")
    assert "does not match" in str(excinfo.value).lower()

    service3 = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "c"))
    _, token3 = await service3.create_ticket(
        companion_id="companion-test", filename="x.png", max_bytes=64
    )
    with pytest.raises(Exception) as excinfo:
        await service3.accept_upload(token=token3, data=_png(), companion_id="companion-test")
    assert "size limit" in str(excinfo.value).lower()


async def test_export_ticket_expiry_and_foreign_companion(
    session, companion_settings, tmp_path
):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "s"))
    ticket, token = await service.create_ticket(companion_id="companion-test", filename="x.png")
    with pytest.raises(Exception) as excinfo:
        await service.accept_upload(token=token, data=_png(), companion_id="another-companion")
    assert "different companion" in str(excinfo.value).lower()

    ticket.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.flush()
    with pytest.raises(Exception) as excinfo:
        await service.accept_upload(token=token, data=_png(), companion_id="companion-test")
    assert "expired" in str(excinfo.value).lower()

    with pytest.raises(Exception):
        await service.accept_upload(token="not-a-token", data=_png(), companion_id="companion-test")


# --------------------------------------------------------------------------- F
# F. HTTP surface
# --------------------------------------------------------------------------- #
def companion_headers(companion_id: str = "companion-test", secret: str = "c" * 43) -> dict[str, str]:
    return {"X-Companion-Id": companion_id, "X-Companion-Secret": secret}


async def test_flow_status_requires_authentication(client: httpx.AsyncClient):
    response = await client.get(f"{FLOW_API}/status")
    assert response.status_code == 401


async def test_flow_status_reports_the_hardened_posture(client, auth, companion_settings):
    response = await client.get(f"{FLOW_API}/status", headers=auth())
    assert response.status_code == 200, response.text
    body = response.json()
    # The Phase 3C.1 boundary is still asserted; only the milestone label moves as
    # later phases harden the same payload.
    assert body["phase"] == "3C.2.1"
    assert body["live_generation_enabled"] is False
    assert body["bridge"]["loopback_only"] is True
    assert body["bridge"]["bind_host"] == "127.0.0.1"
    assert body["token_policy"] == {
        "page_visible_tokens": False,
        "arbitrary_api_urls": False,
        "approved_endpoints_only": True,
        "logging": "redacted",
    }
    assert body["generation"]["max_live_jobs"] == 1
    assert body["generation"]["unknown_submission_policy"] == "never_resubmitted_automatically"
    assert body["asset_export"]["assets_created_before_verification"] is False
    assert {item["id"] for item in body["upstream_blockers"]} >= {"U1", "U2", "U3"}
    assert body["bridge"]["command_allowlist"] == list(ALLOWED_COMMANDS)


async def test_system_status_carries_the_flow_posture(client, auth, companion_settings):
    response = await client.get("/api/v1/system/status", headers=auth())
    assert response.status_code == 200, response.text
    assert response.json()["flow"]["live_generation_enabled"] is False


async def test_capabilities_list_the_denied_commands(client, auth):
    response = await client.get(f"{FLOW_API}/capabilities", headers=auth())
    assert response.status_code == 200
    body = response.json()
    denied = {item["command"] for item in body["denied_commands"]}
    assert {"eval_script", "native_click", "native_key", "batch_generate"}.issubset(denied)
    assert {"rapid_batch_dispatch", "autonomous_storyboard_pipeline"}.issubset(denied)
    assert {"rename_project", "open_new_project", "api_post", "get_token"}.issubset(denied)
    assert body["allowed_commands"] == list(ALLOWED_COMMANDS)


async def test_companion_registration_requires_the_shared_credential(client, companion_settings):
    payload = {"companion_id": "companion-test", "label": "laptop", "bridge_paired": False}
    assert (await client.post(f"{FLOW_API}/companion/hello", json=payload)).status_code == 401
    bad = await client.post(
        f"{FLOW_API}/companion/hello", json=payload, headers=companion_headers(secret="d" * 43)
    )
    assert bad.status_code == 401
    wrong_id = await client.post(
        f"{FLOW_API}/companion/hello",
        json={**payload, "companion_id": "someone-else"},
        headers=companion_headers(),
    )
    assert wrong_id.status_code == 422

    ok = await client.post(f"{FLOW_API}/companion/hello", json=payload, headers=companion_headers())
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["live_generation_enabled"] is False
    assert body["allowed_commands"] == list(ALLOWED_COMMANDS)
    assert body["session_id"]


async def test_companion_lifecycle_heartbeat_drain_and_disconnect(client, companion_settings):
    headers = companion_headers()
    hello = await client.post(
        f"{FLOW_API}/companion/hello",
        json={"companion_id": "companion-test", "label": "laptop"},
        headers=headers,
    )
    assert hello.status_code == 200, hello.text

    beat = await client.post(
        f"{FLOW_API}/companion/heartbeat",
        json={"companion_id": "companion-test", "flow_tab_present": True, "busy": False},
        headers=headers,
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["ok"] is True

    drained = await client.get(f"{FLOW_API}/companion/commands", headers=headers)
    assert drained.status_code == 200
    assert drained.json()["commands"] == []

    bye = await client.post(
        f"{FLOW_API}/companion/disconnect",
        json={"companion_id": "companion-test"},
        headers=headers,
    )
    assert bye.status_code == 200
    assert bye.json()["status"] == "disconnected"

    status = await client.get(f"{FLOW_API}/status", headers=companion_headers(), **{})
    assert status.status_code in (200, 401)  # /status is user-authenticated only


async def test_companion_endpoints_reject_anonymous_callers(client, companion_settings):
    for path, payload in (
        ("/companion/heartbeat", {"companion_id": "companion-test"}),
        ("/companion/disconnect", {"companion_id": "companion-test"}),
        (
            "/companion/results",
            {"command_id": "abc", "ok": True},
        ),
    ):
        response = await client.post(f"{FLOW_API}{path}", json=payload)
        assert response.status_code == 401, path
    assert (await client.get(f"{FLOW_API}/companion/commands")).status_code == 401


async def test_generation_request_without_approval_is_refused(
    client, auth, session, users, brand_row
):
    _, _, brief = await _content_with_brief(session, users, brand_row)
    await session.commit()
    response = await client.post(
        f"{FLOW_API}/generations",
        json={"brief_id": str(brief.id), "prompt": "A charcoal frame", "dry_run": True},
        headers=auth("marketer"),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "flow_approval_required"


async def test_approved_generation_is_authorised_but_never_dispatched(
    client, auth, session, users, brand_row
):
    item, version, brief = await _content_with_brief(session, users, brand_row)
    request = GenerationCommand(
        prompt="A charcoal frame", aspect_ratio="4:5", model="imagen-3", count=1
    )
    binding = _binding_for(brief, "A charcoal frame")
    await _stamp_approval(session, users, item, version, binding)
    await session.commit()

    response = await client.post(
        f"{FLOW_API}/generations",
        json={
            "brief_id": str(brief.id),
            "prompt": "A charcoal frame",
            "model": "imagen-3",
            "aspect_ratio": "4:5",
            "dry_run": True,
        },
        headers=auth("marketer"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorized"] is True
    assert body["dispatched"] is False
    assert body["live_generation_enabled"] is False
    assert body["refusal"] == "live_generation_disabled"
    assert body["approval_hash"] == binding.hash()
    assert body["command"]["payload"]["count"] == 1
    assert request.prompt_hash() == body["prompt_hash"]


async def test_real_generation_request_returns_503_and_dispatches_nothing(
    client, auth, session, users, brand_row
):
    item, version, brief = await _content_with_brief(session, users, brand_row)
    binding = _binding_for(brief, "A charcoal frame")
    await _stamp_approval(session, users, item, version, binding)
    await session.commit()

    response = await client.post(
        f"{FLOW_API}/generations",
        json={
            "brief_id": str(brief.id),
            "prompt": "A charcoal frame",
            "model": "imagen-3",
            "dry_run": False,
        },
        headers=auth("marketer"),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "integration_unavailable"


async def test_binding_endpoint_explains_what_must_be_approved(
    client, auth, session, users, brand_row
):
    _, _, brief = await _content_with_brief(session, users, brand_row)
    await session.commit()
    response = await client.post(
        f"{FLOW_API}/binding",
        json={"brief_id": str(brief.id), "prompt": "A charcoal frame", "model": "imagen-3"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["binding"]["output_count"] == 1
    assert len(body["binding_hash"]) == 64
    assert len(body["prompt_hash"]) == 64


async def test_export_ticket_flow_accepts_verified_bytes(
    client, auth, session, users, brand_row, companion_settings
):
    _, _, brief = await _content_with_brief(session, users, brand_row)
    await session.commit()
    media = _png()
    checksum = hashlib.sha256(media).hexdigest()

    created = await client.post(
        f"{FLOW_API}/exports",
        json={
            "companion_id": "companion-test",
            "brief_id": str(brief.id),
            "expected_sha256": checksum,
            "content_type": "image/png",
            "filename": "flow-frame.png",
        },
        headers=auth("marketer"),
    )
    assert created.status_code == 201, created.text
    ticket = created.json()
    assert ticket["one_time"] is True
    assert ticket["upload_token"]

    anonymous = await client.put(
        f"{FLOW_API}/exports/{ticket['id']}/bytes",
        content=media,
        headers={"Content-Type": "image/png", "X-Export-Token": ticket["upload_token"]},
    )
    assert anonymous.status_code == 401

    uploaded = await client.put(
        f"{FLOW_API}/exports/{ticket['id']}/bytes",
        content=media,
        headers={
            **companion_headers(),
            "Content-Type": "image/png",
            "X-Export-Token": ticket["upload_token"],
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert body["status"] == "consumed"
    assert body["checksum"] == checksum
    assert body["received_bytes"] == len(media)
    assert body["asset_id"]

    reused = await client.put(
        f"{FLOW_API}/exports/{ticket['id']}/bytes",
        content=media,
        headers={
            **companion_headers(),
            "Content-Type": "image/png",
            "X-Export-Token": ticket["upload_token"],
        },
    )
    assert reused.status_code == 422

    status = await client.get(f"{FLOW_API}/exports/{ticket['id']}", headers=auth())
    assert status.status_code == 200
    assert status.json()["status"] == "consumed"


async def test_export_upload_requires_a_token_and_a_known_ticket(
    client, auth, session, users, brand_row, companion_settings
):
    _, _, brief = await _content_with_brief(session, users, brand_row)
    await session.commit()
    created = await client.post(
        f"{FLOW_API}/exports",
        json={"companion_id": "companion-test", "brief_id": str(brief.id), "filename": "f.png"},
        headers=auth("marketer"),
    )
    ticket = created.json()
    missing = await client.put(
        f"{FLOW_API}/exports/{ticket['id']}/bytes",
        content=_png(),
        headers=companion_headers(),
    )
    assert missing.status_code == 422
    unknown = await client.put(
        f"{FLOW_API}/exports/{uuid.uuid4()}/bytes",
        content=_png(),
        headers={**companion_headers(), "X-Export-Token": "nope"},
    )
    assert unknown.status_code in (404, 422)


# --------------------------------------------------------------------------- G
# G. leak checks
# --------------------------------------------------------------------------- #
FORBIDDEN_ROUTE_TOKENS = (
    "eval",
    "api_post",
    "get_token",
    "native_",
    "batch_generate",
    "storyboard",
    "open_new_project",
    "rename_project",
    "script",
)


async def test_no_route_exposes_a_dangerous_capability(client):
    schema = (await client.get("/openapi.json")).json()
    paths = " ".join(schema["paths"])
    for token in FORBIDDEN_ROUTE_TOKENS:
        assert token not in paths, f"route surface exposes {token}"


async def test_flow_responses_never_contain_credentials(client, auth, companion_settings):
    for path in (f"{FLOW_API}/status", f"{FLOW_API}/capabilities"):
        text = (await client.get(path, headers=auth())).text
        assert companion_settings.flow_companion_secret not in text
        if companion_settings.flow_pairing_secret:
            assert companion_settings.flow_pairing_secret not in text
        # The header name and the JSON *value* must never appear; the words may
        # appear inside the blocker prose that explains what the extension needs.
        assert "X-Flow-Pairing" not in text
        assert '"pairing_secret":' not in text


async def test_status_does_not_leak_google_endpoints_or_bearer_tokens(client, auth, companion_settings):
    body = (await client.get(f"{FLOW_API}/status", headers=auth())).json()
    text = json.dumps(body)
    for needle in ("googleapis.com", "Bearer ", "access_token", "ya29."):
        assert needle not in text


def test_error_normalisation_never_echoes_raw_payloads():
    descriptor = proto.normalise_error(
        {"code": "command_failed", "message": "boom", "token": "ya29.secret", "url": "https://x"}
    )
    assert "ya29" not in json.dumps(descriptor.details)
    assert descriptor.code is proto.FlowErrorCode.COMMAND_FAILED
    unknown = proto.normalise_error({"code": "totally_made_up"})
    assert unknown.code is proto.FlowErrorCode.COMMAND_FAILED
    assert proto.normalise_error(None).code is proto.FlowErrorCode.COMMAND_FAILED
    # Token-looking values in upstream details are scrubbed before storage.
    assert "ya29" not in json.dumps(descriptor.details)


def test_upstream_blockers_are_recorded_not_claimed_fixed():
    ids = {item["id"] for item in proto.UPSTREAM_BLOCKERS}
    assert ids == {f"U{i}" for i in range(1, 8)}
    for item in proto.UPSTREAM_BLOCKERS:
        assert item["area"] and item["finding"] and item["required_change"]
        assert item["blocked_because"], "every blocker names why it cannot be fixed here"


# --------------------------------------------------------------------------- H
# H. local companion (dials out; never listens for Crusor)
# --------------------------------------------------------------------------- #
class FakeBridge:
    """Stands in for :class:`FlowBridge` so the companion loop can be driven."""

    def __init__(self, *, paired: bool = True) -> None:
        self.paired = paired
        self.http_port = 8793
        self.ws_port = 8794
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "bridge_version": "1.0-3c1",
            "limits": {"max_request_bytes": 65536},
            "session": {
                "extension_id": EXT_ID,
                "extension_version": "1.3.0",
                "flow_tab_present": True,
            },
        }

    async def dispatch(self, command: str, payload: dict[str, Any], *, job_id: str = "") -> dict[str, Any]:
        self.calls.append((command, payload, job_id))
        return {"ready": True}


def _companion_config(**overrides: Any):  # type: ignore[no-untyped-def]
    from app.flow.companion import CompanionConfig

    settings = CompanionConfig(
        backend_url="https://crusor.example",
        companion_id="companion-test",
        companion_secret="c" * 43,
        heartbeat_seconds=0.0,
        request_timeout=5.0,
        **overrides,
    )
    return settings


async def _run_companion(handler, *, bridge: FakeBridge | None = None, config=None, iterations: int = 2):  # type: ignore[no-untyped-def]
    from app.flow.companion import LocalFlowCompanion

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url=(config or _companion_config()).backend_url, transport=transport
    )
    companion = LocalFlowCompanion(
        config=config or _companion_config(),
        bridge_config=BridgeConfig(
            pairing_id="pid", pairing_secret="s" * 43, http_port=0, ws_port=0
        ),
        bridge=bridge or FakeBridge(),
        client=client,
    )
    await companion.run_forever(max_iterations=iterations)
    return companion


def test_companion_refuses_to_start_without_a_strong_credential():
    from app.flow.companion import CompanionConfig

    with pytest.raises(ValueError):
        CompanionConfig(companion_id="", companion_secret="c" * 43).validate()
    with pytest.raises(ValueError):
        CompanionConfig(companion_id="companion-test", companion_secret="short").validate()
    CompanionConfig(companion_id="companion-test", companion_secret="c" * 43).validate()


async def test_companion_only_ever_dials_out():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.url.host == "crusor.example"
        assert request.headers["x-companion-id"] == "companion-test"
        assert request.headers["x-companion-secret"] == "c" * 43
        if request.url.path.endswith("/hello"):
            return httpx.Response(200, json={"status": "connected", "session_id": "s1"})
        if request.url.path.endswith("/commands"):
            return httpx.Response(200, json={"commands": []})
        return httpx.Response(200, json={"ok": True})

    bridge = FakeBridge()
    companion = await _run_companion(handler, bridge=bridge)
    assert bridge.started is True, "the companion brings the local bridge up itself"
    assert seen[0] == ("POST", "/api/v1/flow/companion/hello")
    assert ("POST", "/api/v1/flow/companion/heartbeat") in seen
    assert ("GET", "/api/v1/flow/companion/commands") in seen
    assert companion.status.commands_handled == 0


async def test_companion_executes_an_allowlisted_command():
    results: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commands"):
            return httpx.Response(
                200,
                json={
                    "commands": [
                        {"id": "cmd-1", "command": "status", "payload": {}, "job_id": ""}
                    ]
                },
            )
        if request.url.path.endswith("/results"):
            results.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    bridge = FakeBridge()
    companion = await _run_companion(handler, bridge=bridge, iterations=2)
    assert bridge.calls == [("status", {}, "")]
    assert results and results[0]["ok"] is True
    assert results[0]["command_id"] == "cmd-1"
    assert companion.status.commands_handled == 1


async def test_companion_refuses_generation_commands_even_if_queued():
    results: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commands"):
            return httpx.Response(
                200,
                json={
                    "commands": [
                        {"id": "cmd-1", "command": "generate_image", "payload": {}, "job_id": "j1"}
                    ]
                },
            )
        if request.url.path.endswith("/results"):
            results.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    bridge = FakeBridge()
    companion = await _run_companion(handler, bridge=bridge, iterations=2)
    assert bridge.calls == [], "no generation may reach the extension in Phase 3C.1"
    assert results[0]["ok"] is False
    assert results[0]["error"]["code"] == "flow_live_generation_disabled"
    assert results[0]["error"]["dispatched"] is False
    assert companion.status.commands_refused == 1


async def test_companion_refuses_a_denied_command_without_dispatching():
    results: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commands"):
            return httpx.Response(
                200,
                json={"commands": [{"id": "cmd-9", "command": "eval_script", "payload": {"code": "1"}}]},
            )
        if request.url.path.endswith("/results"):
            results.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    bridge = FakeBridge()
    await _run_companion(handler, bridge=bridge, iterations=2)
    assert bridge.calls == []
    assert results[0]["error"]["code"] == "command_not_allowed"


async def test_companion_stops_when_its_credential_is_rejected():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "rejected"}})

    companion = await _run_companion(handler, iterations=5)
    # One hello, then the best-effort disconnect on shutdown: no retry storm.
    assert calls == ["/api/v1/flow/companion/hello"], calls
    assert companion.status.state.value == "disconnected"
    assert companion.status.last_error == "rejected"


async def test_companion_degrades_and_backs_off_on_transport_errors():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("backend unreachable")

    companion = await _run_companion(handler, iterations=2)
    assert calls.count("/api/v1/flow/companion/hello") == 2
    assert companion.status.consecutive_failures >= 1
    assert companion.status.state.value in {"degraded", "stopped"}
    assert companion.status.last_error == "ConnectError"


def test_companion_main_returns_configuration_error_without_starting(monkeypatch):
    """The module entry point fails closed before creating listeners when unconfigured."""
    from app.flow import companion as companion_module

    def fail_configuration():
        raise ValueError("missing companion pairing configuration")

    monkeypatch.setattr(companion_module, "companion_from_settings", fail_configuration)
    monkeypatch.setattr(companion_module.logging, "basicConfig", lambda **_kwargs: None)

    assert companion_module.main() == 2
