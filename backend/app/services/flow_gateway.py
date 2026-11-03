"""Crusor side of the Flow integration (Phase 3C.1).

Two services, no generation:

``FlowCompanionGateway``
    Authenticates a *local* companion over an outbound connection (the companion
    dials Crusor; Crusor never dials the user's loopback port), tracks its session,
    and queues the five allowlisted commands for it to collect. Commands are queued
    in process memory on purpose: losing a queued command on restart must fail
    closed, and the caller is told the outcome is unknown rather than retried.

``FlowAssetExportService``
    One-time, short-lived, checksum-pinned upload tickets. An asset is created only
    after the bytes arrive, match the declared checksum, sniff as real media, and are
    read back from storage. A URL or a Chrome download id is never treated as
    evidence that an asset exists.

Neither service can dispatch a generation: ``flow_live_generation_enabled`` is false
and :class:`app.flow.coordinator.GenerationCoordinator` refuses even if it were
called.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.config import Settings, get_settings
from app.core.errors import IntegrationUnavailableError, NotFoundError, ValidationProblem
from app.creative.media import inspect_media
from app.flow import protocol as proto
from app.flow.security import constant_time_equals, secret_fingerprint
from app.models.content import ContentItem
from app.models.creative import CreativeAsset, CreativeKind
from app.models.creative_brief import CreativeBrief
from app.models.flow import FlowAssetExport, FlowCompanionSession
from app.services.creative import CreativeService
from app.storage.base import StorageBackend, get_storage, storage_key


class CompanionAuthError(Exception):
    """The companion credential is missing, wrong, or the gateway is not configured."""

    code = "flow_companion_auth_failed"
    status_code = 401

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else "The companion credential was rejected."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass
class QueuedCommand:
    command_id: str
    command: str
    payload: dict[str, Any]
    job_id: str = ""
    created_at: datetime = field(default_factory=_now)
    delivered_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.command_id,
            "command": self.command,
            "job_id": self.job_id,
            "payload": self.payload,
            "issued_at": self.created_at.isoformat(),
        }


class FlowCompanionGateway:
    """Session registry and the (small) command queue."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._queues: dict[str, list[QueuedCommand]] = {}

    # ------------------------------------------------------------------ auth
    def authenticate(
        self, companion_id: str | None, companion_secret: str | None
    ) -> str:
        """Constant-time check of the configured companion credential."""
        expected_id = self.settings.flow_companion_id or ""
        expected_secret = self.settings.flow_companion_secret or ""
        if not expected_id or len(expected_secret) < 32:
            raise CompanionAuthError(
                "The Flow companion is not configured on this deployment "
                "(FLOW_COMPANION_ID / FLOW_COMPANION_SECRET are unset or too short)."
            )
        if not companion_id or not companion_secret:
            raise CompanionAuthError("Missing companion credential.")
        id_ok = constant_time_equals(companion_id, expected_id)
        secret_ok = constant_time_equals(companion_secret, expected_secret)
        if not (id_ok and secret_ok):
            raise CompanionAuthError("The companion credential was rejected.")
        return companion_id

    # --------------------------------------------------------------- session
    async def register(
        self,
        *,
        companion_id: str,
        label: str = "",
        extension_id: str = "",
        extension_version: str = "",
        flow_tab_present: bool = False,
        bridge_paired: bool = False,
        capabilities: dict[str, Any] | None = None,
        client_host: str = "",
    ) -> FlowCompanionSession:
        row = (
            await self.session.execute(
                select(FlowCompanionSession)
                .where(FlowCompanionSession.companion_id == companion_id)
                .order_by(FlowCompanionSession.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = FlowCompanionSession(companion_id=companion_id, connected_at=_now())
            self.session.add(row)
        row.label = label[:128]
        row.status = "connected"
        row.extension_id = extension_id[:128]
        row.extension_version = extension_version[:64]
        row.flow_tab_present = flow_tab_present
        row.bridge_paired = bridge_paired
        row.live_generation_enabled = bool(self.settings.flow_live_generation_enabled)
        row.client_host = client_host[:64]
        row.secret_fingerprint = secret_fingerprint(self.settings.flow_companion_secret or "")
        row.capabilities = capabilities or {}
        row.last_seen_at = _now()
        row.disconnected_at = None
        await self.session.flush()
        self._queues.setdefault(companion_id, [])
        await record_audit(
            self.session,
            action="flow.companion_connected",
            entity_type="flow_companion_session",
            entity_id=row.id,
            actor_id=None,
            after={
                "companion_id": companion_id,
                "extension_version": extension_version,
                "flow_tab_present": flow_tab_present,
                "bridge_paired": bridge_paired,
                "live_generation_enabled": False,
            },
        )
        return row

    async def heartbeat(
        self,
        companion_id: str,
        *,
        flow_tab_present: bool | None = None,
        bridge_paired: bool | None = None,
        busy: bool | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> FlowCompanionSession:
        row = await self._latest(companion_id)
        row.status = "connected"
        row.last_seen_at = _now()
        if flow_tab_present is not None:
            row.flow_tab_present = flow_tab_present
        if bridge_paired is not None:
            row.bridge_paired = bridge_paired
        if capabilities is not None:
            row.capabilities = {**row.capabilities, **capabilities, "busy": bool(busy)}
        await self.session.flush()
        return row

    async def disconnect(self, companion_id: str) -> FlowCompanionSession:
        row = await self._latest(companion_id)
        row.status = "disconnected"
        row.disconnected_at = _now()
        row.last_seen_at = _now()
        self._queues.pop(companion_id, None)
        await self.session.flush()
        await record_audit(
            self.session,
            action="flow.companion_disconnected",
            entity_type="flow_companion_session",
            entity_id=row.id,
            actor_id=None,
            after={"companion_id": companion_id},
        )
        return row

    async def latest_session(self, companion_id: str) -> FlowCompanionSession | None:
        return await self._latest(companion_id, required=False)

    async def _latest(
        self, companion_id: str, *, required: bool = True
    ) -> FlowCompanionSession:
        row = (
            await self.session.execute(
                select(FlowCompanionSession)
                .where(FlowCompanionSession.companion_id == companion_id)
                .order_by(FlowCompanionSession.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None and required:
            raise NotFoundError("This companion has not registered with Crusor.")
        return row  # type: ignore[return-value]

    # --------------------------------------------------------------- commands
    async def enqueue(
        self, companion_id: str, command: str, payload: dict[str, Any], *, job_id: str = ""
    ) -> QueuedCommand:
        proto.ensure_allowed(command)  # CommandDenied for anything else
        queued = QueuedCommand(
            command_id=proto.new_id(),
            command=command,
            payload=dict(payload),
            job_id=job_id,
        )
        self._queues.setdefault(companion_id, []).append(queued)
        await record_audit(
            self.session,
            action="flow.command_queued",
            entity_type="flow_companion_session",
            entity_id=uuid.uuid4(),
            actor_id=None,
            after={"companion_id": companion_id, "command": command, "job_id": job_id},
        )
        return queued

    def drain(self, companion_id: str) -> list[QueuedCommand]:
        queue = self._queues.get(companion_id, [])
        self._queues[companion_id] = []
        for item in queue:
            item.delivered_at = _now()
        return queue

    def pending(self, companion_id: str) -> list[QueuedCommand]:
        return list(self._queues.get(companion_id, []))

    # ----------------------------------------------------------------- status
    async def status(self) -> dict[str, Any]:
        rows = list(
            (
                await self.session.execute(
                    select(FlowCompanionSession).order_by(FlowCompanionSession.created_at.desc()).limit(10)
                )
            )
            .scalars()
            .all()
        )
        now = _now()
        sessions = []
        for row in rows:
            last_seen = _aware(row.last_seen_at)
            stale = bool(
                row.status == "connected"
                and last_seen is not None
                and (now - last_seen) > timedelta(seconds=90)
            )
            sessions.append(
                {
                    "companion_id": row.companion_id,
                    "label": row.label,
                    "status": "stale" if stale else row.status,
                    "extension_id": row.extension_id,
                    "extension_version": row.extension_version,
                    "flow_tab_present": row.flow_tab_present,
                    "bridge_paired": row.bridge_paired,
                    "live_generation_enabled": False,
                    "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                    "client_host": row.client_host,
                    "queued_commands": len(self._queues.get(row.companion_id, [])),
                }
            )
        return {
            "configured": bool(self.settings.flow_companion_id)
            and len(self.settings.flow_companion_secret or "") >= 32,
            "sessions": sessions,
            "queue_depth": sum(len(q) for q in self._queues.values()),
        }


class FlowAssetExportService:
    """One-time upload tickets that end in a verified, immutable asset."""

    TOKEN_BYTES = 32

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        storage: StorageBackend | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.storage = storage or get_storage()

    # --------------------------------------------------------------- tickets
    async def create_ticket(
        self,
        *,
        companion_id: str,
        content_item_id: uuid.UUID | None = None,
        brief_id: uuid.UUID | None = None,
        generation_job_id: uuid.UUID | None = None,
        expected_sha256: str = "",
        content_type: str = "",
        filename: str = "flow-asset",
        max_bytes: int | None = None,
        created_by: uuid.UUID | None = None,
    ) -> tuple[FlowAssetExport, str]:
        token = secrets.token_urlsafe(self.TOKEN_BYTES)
        campaign_id = None
        content_version_id = None
        brief_version = 0
        if content_item_id is not None:
            item = await self.session.get(ContentItem, content_item_id)
            if item is None:
                raise NotFoundError(f"Content item {content_item_id} not found.")
            campaign_id = item.campaign_id
            content_version_id = item.current_version_id
        if brief_id is not None:
            brief = await self.session.get(CreativeBrief, brief_id)
            if brief is None:
                raise NotFoundError(f"Creative brief {brief_id} not found.")
            brief_version = int(brief.version_number or 0)
            content_version_id = content_version_id or brief.content_version_id
            campaign_id = campaign_id or brief.campaign_id

        ttl = int(self.settings.flow_export_ttl_seconds or 900)
        limit = int(max_bytes or self.settings.flow_export_max_bytes or 25 * 1024 * 1024)
        ticket = FlowAssetExport(
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            status="pending",
            companion_id=companion_id,
            generation_job_id=generation_job_id,
            campaign_id=campaign_id,
            content_item_id=content_item_id,
            content_version_id=content_version_id,
            brief_id=brief_id,
            brief_version=brief_version,
            expected_sha256=(expected_sha256 or "").lower(),
            declared_content_type=(content_type or "")[:128],
            filename=(filename or "flow-asset")[:255],
            max_bytes=limit,
            expires_at=_now() + timedelta(seconds=ttl),
            created_by=created_by,
        )
        self.session.add(ticket)
        await self.session.flush()
        await record_audit(
            self.session,
            action="flow.export_ticket_created",
            entity_type="flow_asset_export",
            entity_id=ticket.id,
            actor_id=created_by,
            after={
                "companion_id": companion_id,
                "expires_at": ticket.expires_at.isoformat() if ticket.expires_at else None,
                "expected_sha256": bool(expected_sha256),
                "max_bytes": limit,
            },
        )
        return ticket, token

    async def accept_upload(
        self,
        *,
        token: str,
        data: bytes,
        companion_id: str,
        declared_content_type: str = "",
        actor_id: uuid.UUID | None = None,
    ) -> tuple[FlowAssetExport, CreativeAsset]:
        ticket = await self._ticket_for_token(token)
        now = _now()

        if ticket.status != "pending":
            raise ValidationProblem(
                "This export ticket has already been used.", 
            )
        expires = _aware(ticket.expires_at)
        if expires is not None and expires <= now:
            ticket.status = "expired"
            ticket.error_code = "export_ticket_expired"
            ticket.error_message = "The upload window closed before the bytes arrived."
            await self.session.flush()
            raise ValidationProblem("This export ticket has expired.")
        if ticket.companion_id and ticket.companion_id != companion_id:
            raise ValidationProblem("This export ticket belongs to a different companion.")
        if len(data) > int(ticket.max_bytes or 0) > 0:
            ticket.status = "failed"
            ticket.error_code = "export_too_large"
            ticket.error_message = f"Uploaded {len(data)} bytes, limit {ticket.max_bytes}."
            await self.session.flush()
            raise ValidationProblem("The upload exceeds the ticket's size limit.")

        info = inspect_media(data)
        if not info.recognized or not info.mime_type:
            ticket.status = "failed"
            ticket.error_code = "export_unrecognised_media"
            ticket.error_message = "The uploaded bytes are not a recognised media format."
            await self.session.flush()
            raise ValidationProblem(
                "The uploaded bytes are not a recognised image or video container."
            )
        if ticket.declared_content_type and info.mime_type != ticket.declared_content_type:
            ticket.status = "failed"
            ticket.error_code = "export_content_type_mismatch"
            ticket.error_message = (
                f"declared {ticket.declared_content_type}, detected {info.mime_type}"
            )
            await self.session.flush()
            raise ValidationProblem(
                "The uploaded media type does not match the ticket's declared type."
            )

        checksum = hashlib.sha256(data).hexdigest()
        if ticket.expected_sha256 and checksum != ticket.expected_sha256:
            ticket.status = "failed"
            ticket.error_code = "export_checksum_mismatch"
            ticket.error_message = "The uploaded bytes do not match the expected checksum."
            await self.session.flush()
            raise ValidationProblem(
                "The uploaded bytes do not match the checksum declared when the ticket was created."
            )

        key = storage_key(ticket.filename or "flow-asset.png", prefix="flow")
        stored = await self.storage.put(
            key,
            data,
            content_type=info.mime_type,
            metadata={"source": "flow_companion", "ticket": str(ticket.id)},
        )
        # Read back before anything is recorded as stored.
        read_back = await self.storage.get(stored.key)
        if hashlib.sha256(read_back).hexdigest() != checksum:
            ticket.status = "failed"
            ticket.error_code = "export_readback_failed"
            ticket.error_message = "Stored bytes did not survive a read-back check."
            await self.session.flush()
            raise ValidationProblem("The asset could not be stored and verified.")

        kind = CreativeKind.VIDEO.value if (info.mime_type or "").startswith("video/") else CreativeKind.IMAGE.value
        asset = await CreativeService(self.session).register(
            content_item_id=ticket.content_item_id,
            content_version_id=ticket.content_version_id,
            brand_profile_id=None,
            campaign_id=ticket.campaign_id,
            kind=kind,
            filename=ticket.filename,
            mime_type=info.mime_type,
            detected_mime=info.mime_type,
            metadata_verified=True,
            content_bytes=data,
            storage_key=stored.key,
            public_url=stored.public_url,
            width=info.width,
            height=info.height,
            prompt="",
            provider="flow",
            is_mock=False,
            metadata={
                "source": "flow_companion",
                "export_id": str(ticket.id),
                "generation_job_id": str(ticket.generation_job_id) if ticket.generation_job_id else None,
                "verified_sha256": checksum,
                "declared_content_type": declared_content_type or ticket.declared_content_type,
            },
            actor_id=actor_id,
        )

        ticket.status = "consumed"
        ticket.consumed_at = now
        ticket.received_bytes = len(data)
        ticket.checksum = checksum
        ticket.storage_key = stored.key
        ticket.asset_id = asset.id
        ticket.width = info.width
        ticket.height = info.height
        ticket.error_code = ""
        ticket.error_message = ""
        await self.session.flush()
        await record_audit(
            self.session,
            action="flow.asset_exported",
            entity_type="flow_asset_export",
            entity_id=ticket.id,
            actor_id=actor_id,
            after={
                "asset_id": str(asset.id),
                "checksum": checksum,
                "size_bytes": len(data),
                "mime_type": info.mime_type,
                "verified": True,
            },
        )
        return ticket, asset

    async def get_ticket(self, ticket_id: uuid.UUID) -> FlowAssetExport:
        ticket = await self.session.get(FlowAssetExport, ticket_id)
        if ticket is None:
            raise NotFoundError(f"Export ticket {ticket_id} not found.")
        return ticket

    async def _ticket_for_token(self, token: str) -> FlowAssetExport:
        if not token:
            raise ValidationProblem("A ticket token is required.")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ticket = (
            await self.session.execute(
                select(FlowAssetExport).where(FlowAssetExport.token_hash == digest)
            )
        ).scalar_one_or_none()
        if ticket is None:
            raise NotFoundError("Unknown export ticket.")
        return ticket


def flow_status_payload(
    settings: Settings, gateway_status: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The honest status block used by ``/flow/status`` and ``/system/status``."""
    return {
        "integration": "google_flow_connector",
        "phase": "3C.1",
        "available": False,
        "enabled": False,
        "live_generation_enabled": bool(settings.flow_live_generation_enabled),
        "provider": "flow",
        "bridge": {
            "mode": "local_companion",
            "bind_host": settings.flow_bridge_host,
            "loopback_only": settings.flow_bridge_is_loopback,
            "http_port": settings.flow_bridge_port,
            "ws_port": settings.flow_bridge_ws_port,
            "enabled": bool(settings.flow_bridge_enabled),
            "authenticated": True,
            "command_allowlist": list(proto.ALLOWED_COMMANDS),
            "denied_commands": sorted(proto.DENIED_COMMANDS),
        },
        "token_policy": {
            "page_visible_tokens": False,
            "arbitrary_api_urls": False,
            "approved_endpoints_only": True,
            "logging": "redacted",
        },
        "generation": {
            "max_live_jobs": int(settings.flow_max_live_jobs),
            "one_attempt_per_request": True,
            "unknown_submission_policy": "never_resubmitted_automatically",
            "output_count": 1,
        },
        "asset_export": {
            "contract": "one_time_ticket_then_verified_bytes",
            "ttl_seconds": int(settings.flow_export_ttl_seconds),
            "max_bytes": int(settings.flow_export_max_bytes),
            "checksum_verified": True,
            "readback_verified": True,
            "assets_created_before_verification": False,
        },
        "companion": gateway_status or {"configured": False, "sessions": []},
        "upstream_blockers": list(proto.UPSTREAM_BLOCKERS),
        "note": (
            "Phase 3C.1 hardens the bridge, the command surface, the token boundary and "
            "the asset-export contract. Live Flow generation stays disabled until the "
            "upstream extension changes are delivered and Phase 3C.2 is accepted."
        ),
    }


def ensure_live_generation_disabled(settings: Settings) -> None:
    """Hard stop used by every route that could otherwise dispatch a generation."""
    if not settings.flow_live_generation_enabled:
        raise IntegrationUnavailableError(
            "Live Flow generation is disabled in Phase 3C.1. The bridge, command "
            "allowlist, approval gate and asset-export contract are in place, but no "
            "generation is dispatched until Phase 3C.2 is accepted and a founder "
            "authorises a canary."
        )
