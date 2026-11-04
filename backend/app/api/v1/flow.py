"""Flow integration API (Phase 3C.1).

Read this module as the security boundary, not as a feature surface:

* the companion authenticates with a 256-bit secret in headers on **every** request;
* the command queue only ever carries the five allowlisted operations, and the
  queue is drained by the companion, never pushed to a loopback address;
* a generation decision is produced by the approval gate and then refused —
  ``live_generation_enabled`` is false, so ``POST /flow/generations`` can only
  answer "authorised but not dispatched" (dry run) or 503 (real dispatch);
* asset export is a one-time ticket: the bytes must arrive, match the declared
  checksum and survive a read-back before an asset row exists.

There is deliberately no route that returns a Google token, accepts an arbitrary
URL, or evaluates script.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, status

from app.api.deps import DB, CurrentUser, EditorUser
from app.core.audit import record_audit
from app.core.config import get_settings
from app.core.errors import (
    AuthenticationError,
    IntegrationUnavailableError,
    NotFoundError,
    ValidationProblem,
)
from app.flow import protocol as proto
from app.models.creative_brief import CreativeBrief
from app.schemas.flow import (
    CommandResultIn,
    CompanionDisconnectIn,
    CompanionHeartbeatIn,
    CompanionHelloIn,
    ExportStatusOut,
    ExportTicketIn,
    ExportTicketOut,
    GenerationDecisionOut,
    GenerationRequestIn,
)
from app.services.flow_approval import FlowApprovalGate, FlowGenerationBinding
from app.services.flow_gateway import (
    CompanionAuthError,
    FlowAssetExportService,
    FlowCompanionGateway,
    ensure_live_generation_disabled,
    flow_status_payload,
)
from app.storage.base import get_storage

router = APIRouter(prefix="/flow", tags=["flow"])


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@router.get("/status", summary="Flow integration readiness and security posture")
async def flow_status(session: DB, user: CurrentUser) -> dict[str, Any]:
    settings = get_settings()
    gateway_status = await FlowCompanionGateway(session, settings).status()
    return flow_status_payload(settings, gateway_status)


@router.get("/capabilities", summary="Command allowlist and blocked upstream capabilities")
async def flow_capabilities(user: CurrentUser) -> dict[str, Any]:
    return {
        "protocol": proto.PROTOCOL_VERSION,
        "allowed_commands": list(proto.ALLOWED_COMMANDS),
        "denied_commands": [
            {"command": name, "reason": reason} for name, reason in sorted(proto.DENIED_COMMANDS.items())
        ],
        "live_generation_enabled": False,
        "asset_export": "one_time_ticket_with_checksum_verification",
        "upstream_blockers": list(proto.UPSTREAM_BLOCKERS),
    }


# --------------------------------------------------------------------------- #
# companion lifecycle
# --------------------------------------------------------------------------- #
@router.post("/companion/hello", summary="Register a local companion (outbound only)")
async def companion_hello(
    payload: CompanionHelloIn,
    session: DB,
    x_companion_id: Annotated[str | None, Header(alias="X-Companion-Id")] = None,
    x_companion_secret: Annotated[str | None, Header(alias="X-Companion-Secret")] = None,
) -> dict[str, Any]:
    settings = get_settings()
    gateway = FlowCompanionGateway(session, settings)
    try:
        companion_id = gateway.authenticate(x_companion_id, x_companion_secret)
    except CompanionAuthError as exc:
        raise AuthenticationError(exc.message) from exc
    if payload.companion_id != companion_id:
        raise ValidationProblem("The companion id does not match the authenticated credential.")

    row = await gateway.register(
        companion_id=companion_id,
        label=payload.label,
        extension_id=payload.extension_id,
        extension_version=payload.extension_version,
        flow_tab_present=payload.flow_tab_present,
        bridge_paired=payload.bridge_paired,
        capabilities=payload.capabilities,
    )
    await session.commit()
    return {
        "session_id": str(row.id),
        "companion_id": companion_id,
        "protocol": proto.PROTOCOL_VERSION,
        "status": "connected",
        "live_generation_enabled": False,
        "poll_wait_seconds": 20,
        "heartbeat_seconds": 20,
        "allowed_commands": list(proto.ALLOWED_COMMANDS),
        "note": (
            "Registered. Crusor never dials your loopback port: the companion polls for "
            "commands and posts results back on this authenticated connection."
        ),
    }


@router.post("/companion/heartbeat", summary="Companion liveness and extension state")
async def companion_heartbeat(
    payload: CompanionHeartbeatIn,
    session: DB,
    x_companion_id: Annotated[str | None, Header(alias="X-Companion-Id")] = None,
    x_companion_secret: Annotated[str | None, Header(alias="X-Companion-Secret")] = None,
) -> dict[str, Any]:
    settings = get_settings()
    gateway = FlowCompanionGateway(session, settings)
    try:
        companion_id = gateway.authenticate(x_companion_id, x_companion_secret)
    except CompanionAuthError as exc:
        raise AuthenticationError(exc.message) from exc
    row = await gateway.heartbeat(
        companion_id,
        flow_tab_present=payload.flow_tab_present,
        bridge_paired=payload.bridge_paired,
        busy=payload.busy,
        capabilities=payload.capabilities,
    )
    await session.commit()
    return {
        "ok": True,
        "status": row.status,
        "live_generation_enabled": False,
        "queued_commands": len(gateway.pending(companion_id)),
    }


@router.post("/companion/disconnect", summary="Companion going away")
async def companion_disconnect(
    payload: CompanionDisconnectIn,
    session: DB,
    x_companion_id: Annotated[str | None, Header(alias="X-Companion-Id")] = None,
    x_companion_secret: Annotated[str | None, Header(alias="X-Companion-Secret")] = None,
) -> dict[str, Any]:
    settings = get_settings()
    gateway = FlowCompanionGateway(session, settings)
    try:
        companion_id = gateway.authenticate(x_companion_id, x_companion_secret)
    except CompanionAuthError as exc:
        raise AuthenticationError(exc.message) from exc
    row = await gateway.disconnect(companion_id)
    await session.commit()
    return {"ok": True, "status": row.status}


@router.get("/companion/commands", summary="Collect queued commands (allowlisted only)")
async def companion_commands(
    session: DB,
    x_companion_id: Annotated[str | None, Header(alias="X-Companion-Id")] = None,
    x_companion_secret: Annotated[str | None, Header(alias="X-Companion-Secret")] = None,
    wait: int = Query(default=0, ge=0, le=25),
) -> dict[str, Any]:
    import asyncio

    settings = get_settings()
    gateway = FlowCompanionGateway(session, settings)
    try:
        companion_id = gateway.authenticate(x_companion_id, x_companion_secret)
    except CompanionAuthError as exc:
        raise AuthenticationError(exc.message) from exc

    deadline = max(0, min(wait, 25))
    waited = 0.0
    commands = gateway.drain(companion_id)
    while not commands and waited < deadline:
        await asyncio.sleep(0.5)
        waited += 0.5
        commands = gateway.drain(companion_id)
        await session.rollback()  # release the read transaction while idle

    return {
        "commands": [command.as_dict() for command in commands],
        "live_generation_enabled": False,
        "poll_after_seconds": 5,
    }


@router.post("/companion/results", summary="Record the result of a dispatched command")
async def companion_result(
    payload: CommandResultIn,
    session: DB,
    x_companion_id: Annotated[str | None, Header(alias="X-Companion-Id")] = None,
    x_companion_secret: Annotated[str | None, Header(alias="X-Companion-Secret")] = None,
) -> dict[str, Any]:
    settings = get_settings()
    gateway = FlowCompanionGateway(session, settings)
    try:
        companion_id = gateway.authenticate(x_companion_id, x_companion_secret)
    except CompanionAuthError as exc:
        raise AuthenticationError(exc.message) from exc

    await record_audit(
        session,
        action="flow.command_result",
        entity_type="flow_companion_session",
        entity_id=uuid.uuid4(),
        actor_id=None,
        after={
            "companion_id": companion_id,
            "command_id": payload.command_id,
            "job_id": payload.job_id,
            "ok": payload.ok,
            "error_code": str((payload.error or {}).get("code") or ""),
            # Phase 3C.2.3.3: the two events are recorded separately. An empty
            # string means the companion did not state it, which is not the same as
            # "not dispatched" and must never be read that way.
            "transport": payload.transport,
            "provider_dispatch": payload.provider_dispatch,
            "request_hash": payload.request_hash,
        },
    )
    await session.commit()
    return {"ok": True, "live_generation_enabled": False}


# --------------------------------------------------------------------------- #
# generation decisions (never a dispatch in 3C.1)
# --------------------------------------------------------------------------- #
@router.post(
    "/generations",
    response_model=GenerationDecisionOut,
    summary="Evaluate the approval gate; never dispatches in Phase 3C.1",
)
async def request_generation(
    payload: GenerationRequestIn,
    session: DB,
    user: EditorUser,
) -> GenerationDecisionOut:
    settings = get_settings()
    brief = await session.get(CreativeBrief, payload.brief_id)
    if brief is None:
        raise NotFoundError(f"Creative brief {payload.brief_id} not found.")

    command = proto.GenerationCommand(
        prompt=payload.prompt,
        negative_prompt=payload.negative_prompt,
        output_kind=payload.output_kind,
        model=payload.model,
        aspect_ratio=payload.aspect_ratio,
        duration_seconds=payload.duration_seconds,
        reference_media_ids=payload.reference_media_ids,
        count=1,
        idempotency_key=payload.idempotency_key,
    )
    binding = FlowGenerationBinding.from_brief(
        brief, request=command, provider="flow", model=payload.model
    )
    record = await FlowApprovalGate(session).authorize(binding)

    await record_audit(
        session,
        action="flow.generation_authorized",
        entity_type="creative_brief",
        entity_id=brief.id,
        actor_id=user.id,
        after={
            "binding_hash": binding.hash(),
            "approval_id": str(record.id),
            "prompt_hash": binding.prompt_hash,
            "dispatched": False,
        },
    )
    await session.commit()

    envelope = {
        "command": "generate_image" if payload.output_kind == "image" else "generate_video",
        "payload": {
            "prompt": command.prompt,
            "negative_prompt": command.negative_prompt,
            "aspect_ratio": command.aspect_ratio,
            "model": command.model,
            "duration_seconds": command.duration_seconds,
            "count": 1,
        },
        "requires_approval_hash": binding.hash(),
    }

    if not payload.dry_run:
        # A real request is refused here, after the gate ran, so the operator gets
        # the precise reason instead of a silent no-op.
        ensure_live_generation_disabled(settings)
        raise IntegrationUnavailableError(
            "Live Flow generation is disabled in Phase 3C.1."
        )

    return GenerationDecisionOut(
        authorized=True,
        dispatched=False,
        lifecycle=proto.FlowLifecycle.NOT_SUBMITTED.value,
        idempotency_key=payload.idempotency_key,
        live_generation_enabled=bool(settings.flow_live_generation_enabled),
        approval_id=record.id,
        approval_hash=record.flow_binding_hash,
        prompt_hash=binding.prompt_hash,
        binding=binding.as_dict(),
        command=envelope,
        refusal="live_generation_disabled",
        reason=(
            "The approval gate passed. Nothing was dispatched: Phase 3C.1 hardens the "
            "bridge and the contract, and a canary needs Phase 3C.2 acceptance plus "
            "explicit founder authorisation."
        ),
    )


# --------------------------------------------------------------------------- #
# asset export tickets
# --------------------------------------------------------------------------- #
@router.post(
    "/exports",
    response_model=ExportTicketOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a one-time upload ticket for verified media bytes",
)
async def create_export_ticket(
    payload: ExportTicketIn,
    session: DB,
    user: EditorUser,
) -> ExportTicketOut:
    settings = get_settings()
    companion_id = payload.companion_id or settings.flow_companion_id
    if not companion_id:
        raise ValidationProblem(
            "A companion must be registered before an export ticket can be created."
        )
    service = FlowAssetExportService(session, settings, get_storage(settings))
    ticket, token = await service.create_ticket(
        companion_id=companion_id,
        content_item_id=payload.content_item_id,
        brief_id=payload.brief_id,
        generation_job_id=payload.generation_job_id,
        expected_sha256=payload.expected_sha256,
        content_type=payload.content_type,
        filename=payload.filename,
        max_bytes=payload.max_bytes or None,
        created_by=user.id,
    )
    await session.commit()
    return ExportTicketOut(
        id=ticket.id,
        status=ticket.status,
        expires_at=ticket.expires_at,
        upload_url=f"/api/v1/flow/exports/{ticket.id}/bytes",
        upload_token=token,
        expected_sha256=ticket.expected_sha256,
        declared_content_type=ticket.declared_content_type,
        max_bytes=ticket.max_bytes,
        upload_mode=ticket.upload_mode,
        chunk_bytes=int(settings.flow_export_chunk_bytes),
        max_chunks=int(settings.flow_export_max_chunks),
        one_time=True,
    )


@router.put(
    "/exports/{ticket_id}/bytes",
    response_model=ExportStatusOut,
    summary="Upload the media bytes for a ticket (companion only, single use)",
)
async def upload_export_bytes(
    ticket_id: uuid.UUID,
    request: Request,
    session: DB,
    x_companion_id: Annotated[str | None, Header(alias="X-Companion-Id")] = None,
    x_companion_secret: Annotated[str | None, Header(alias="X-Companion-Secret")] = None,
    x_export_token: Annotated[str | None, Header(alias="X-Export-Token")] = None,
) -> ExportStatusOut:
    settings = get_settings()
    gateway = FlowCompanionGateway(session, settings)
    try:
        companion_id = gateway.authenticate(x_companion_id, x_companion_secret)
    except CompanionAuthError as exc:
        raise AuthenticationError(exc.message) from exc

    limit = int(settings.flow_export_max_bytes or 25 * 1024 * 1024)
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > limit:
        raise ValidationProblem("The upload exceeds the maximum export size.")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ValidationProblem("The upload exceeds the maximum export size.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise ValidationProblem("The upload was empty.")

    service = FlowAssetExportService(session, settings, get_storage(settings))
    existing = await service.get_ticket(ticket_id)
    if not x_export_token:
        raise ValidationProblem("The one-time export token is required in X-Export-Token.")
    ticket, asset = await service.accept_upload(
        token=x_export_token or "",
        data=data,
        companion_id=companion_id,
        declared_content_type=request.headers.get("x-media-type", ""),
        expected_ticket_id=ticket_id,
        actor_id=None,
    )
    await session.commit()
    return _export_status(ticket, asset_id=asset.id)


def _export_status(ticket: Any, *, asset_id: uuid.UUID | None = None) -> ExportStatusOut:
    return ExportStatusOut(
        id=ticket.id,
        status=ticket.status,
        asset_id=asset_id or ticket.asset_id,
        checksum=ticket.checksum,
        received_bytes=ticket.received_bytes,
        width=ticket.width,
        height=ticket.height,
        upload_mode=ticket.upload_mode or "single",
        chunk_count=int(ticket.chunk_count or 0),
        chunks_received=int(ticket.chunks_received or 0),
        complete=ticket.status == "consumed",
        error_code=ticket.error_code,
        error_message=ticket.error_message,
        consumed_at=ticket.consumed_at,
    )


@router.post(
    "/exports/{ticket_id}/chunks",
    response_model=ExportStatusOut,
    summary="Upload one bounded, checksummed chunk of a verified media export",
)
async def upload_export_chunk(
    ticket_id: uuid.UUID,
    request: Request,
    session: DB,
    x_companion_id: Annotated[str | None, Header(alias="X-Companion-Id")] = None,
    x_companion_secret: Annotated[str | None, Header(alias="X-Companion-Secret")] = None,
    x_export_token: Annotated[str | None, Header(alias="X-Export-Token")] = None,
    x_chunk_index: Annotated[str | None, Header(alias="X-Chunk-Index")] = None,
    x_chunk_count: Annotated[str | None, Header(alias="X-Chunk-Count")] = None,
    x_chunk_sha256: Annotated[str | None, Header(alias="X-Chunk-Sha256")] = None,
) -> ExportStatusOut:
    settings = get_settings()
    gateway = FlowCompanionGateway(session, settings)
    try:
        companion_id = gateway.authenticate(x_companion_id, x_companion_secret)
    except CompanionAuthError as exc:
        raise AuthenticationError(exc.message) from exc

    if not x_export_token:
        raise ValidationProblem("The one-time export token is required in X-Export-Token.")
    try:
        index = int(x_chunk_index or "")
        total = int(x_chunk_count or "")
    except ValueError as exc:
        raise ValidationProblem("X-Chunk-Index and X-Chunk-Count must be integers.") from exc

    limit = int(settings.flow_export_chunk_bytes or 8 * 1024 * 1024)
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > limit:
        raise ValidationProblem(f"A chunk may not exceed {limit} bytes.")

    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise ValidationProblem(f"A chunk may not exceed {limit} bytes.")
    if not buffer:
        raise ValidationProblem("The chunk was empty.")

    service = FlowAssetExportService(session, settings, get_storage(settings))
    # The ticket named in the URL is part of the authorization decision, so the
    # service resolves the token and compares identity *before* writing anything.
    # A post-mutation comparison (Phase 3C.2) would already have mutated the token's
    # own ticket by the time the mismatch was noticed.
    ticket, _asset = await service.accept_chunk(
        token=x_export_token,
        index=index,
        total=total,
        chunk_sha256=x_chunk_sha256 or "",
        data=bytes(buffer),
        companion_id=companion_id,
        expected_ticket_id=ticket_id,
    )
    await session.commit()
    return _export_status(ticket)


@router.get(
    "/exports/{ticket_id}",
    response_model=ExportStatusOut,
    summary="Export ticket status",
)
async def get_export_ticket(
    ticket_id: uuid.UUID, session: DB, user: CurrentUser
) -> ExportStatusOut:
    service = FlowAssetExportService(session, get_settings(), get_storage(get_settings()))
    return _export_status(await service.get_ticket(ticket_id))


@router.post(
    "/binding",
    summary="Compute the binding hash a founder approval must carry",
)
async def compute_binding(
    payload: GenerationRequestIn,
    session: DB,
    user: CurrentUser,
) -> dict[str, Any]:
    brief = await session.get(CreativeBrief, payload.brief_id)
    if brief is None:
        raise NotFoundError(f"Creative brief {payload.brief_id} not found.")
    command = proto.GenerationCommand(
        prompt=payload.prompt,
        negative_prompt=payload.negative_prompt,
        output_kind=payload.output_kind,
        model=payload.model,
        aspect_ratio=payload.aspect_ratio,
        duration_seconds=payload.duration_seconds,
        reference_media_ids=payload.reference_media_ids,
    )
    binding = FlowGenerationBinding.from_brief(
        brief, request=command, provider="flow", model=payload.model
    )
    return {
        "binding": binding.as_dict(),
        "binding_hash": binding.hash(),
        "prompt_hash": binding.prompt_hash,
        "note": (
            "An approval record must carry exactly this hash for a Flow generation to be "
            "authorised. Any change to prompt, model, ratio, duration or count invalidates it."
        ),
    }
