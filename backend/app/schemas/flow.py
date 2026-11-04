"""Phase 3C.1 request/response models for the Flow bridge and companion."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FlowBindingIn(BaseModel):
    """The exact combination a founder approves for a real generation."""

    model_config = ConfigDict(extra="forbid")

    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["flow"] = "flow"
    model: str = Field(default="", max_length=64)
    aspect_ratio: str = Field(default="4:5", max_length=16)
    duration_seconds: int = Field(default=0, ge=0, le=120)
    output_count: Literal[1] = 1


class GenerationRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_id: uuid.UUID
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=2000)
    output_kind: Literal["image", "video"] = "image"
    model: str = Field(default="", max_length=64)
    aspect_ratio: str = Field(default="4:5", max_length=16)
    duration_seconds: int = Field(default=0, ge=0, le=120)
    reference_media_ids: list[str] = Field(default_factory=list, max_length=4)
    #: Duplicate-paid-generation guard. One key maps to one request; reuse with a
    #: different payload is refused with 409 ``flow_idempotency_conflict``.
    idempotency_key: str = Field(default="", max_length=128)
    dry_run: bool = True


class GenerationDecisionOut(BaseModel):
    """What the backend decided, and why nothing was dispatched."""

    authorized: bool
    dispatched: bool = False
    lifecycle: str = "NOT_SUBMITTED"
    idempotency_key: str = ""
    live_generation_enabled: bool = False
    approval_id: uuid.UUID | None = None
    approval_hash: str = ""
    prompt_hash: str = ""
    binding: dict[str, Any] = Field(default_factory=dict)
    command: dict[str, Any] | None = None
    refusal: str = ""
    reason: str = ""


class CompanionHelloIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companion_id: str = Field(min_length=1, max_length=128)
    label: str = Field(default="", max_length=128)
    extension_id: str = Field(default="", max_length=128)
    extension_version: str = Field(default="", max_length=64)
    flow_tab_present: bool = False
    bridge_paired: bool = False
    capabilities: dict[str, Any] = Field(default_factory=dict)


class CompanionHeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companion_id: str = Field(min_length=1, max_length=128)
    flow_tab_present: bool | None = None
    bridge_paired: bool | None = None
    busy: bool | None = None
    status: str = Field(default="", max_length=32)
    capabilities: dict[str, Any] | None = None


class CompanionDisconnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companion_id: str = Field(min_length=1, max_length=128)


class CommandResultIn(BaseModel):
    """A companion's report about one command it handled.

    Phase 3C.2.3.3: transport delivery and provider dispatch are separate events and
    travel separately. Both are optional (an older companion simply omits them, which
    the backend records as *unknown*, never as non-delivery), and the two must not
    contradict each other.
    """

    model_config = ConfigDict(extra="forbid")

    companion_id: str = Field(default="", max_length=128)
    command_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(default="", max_length=64)
    ok: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    #: Crusor → extension delivery of this command.
    transport: Literal["", "definitely_not_sent", "may_have_been_sent", "sent"] = ""
    #: extension → provider dispatch. ``unknown`` is the safe default when unstated.
    provider_dispatch: Literal["", "not_dispatched", "dispatched", "unknown"] = ""

    @model_validator(mode="after")
    def _states_do_not_contradict(self) -> "CommandResultIn":
        # A provider can only be "not dispatched" if the frame either never left or
        # was delivered and *explicitly* refused by a correlated provider answer —
        # never while delivery itself is uncertain.
        if self.provider_dispatch == "not_dispatched" and self.transport == "may_have_been_sent":
            raise ValueError(
                "provider_dispatch 'not_dispatched' contradicts transport "
                "'may_have_been_sent': an ambiguous send cannot establish that no "
                "generation was submitted."
            )
        if self.provider_dispatch == "dispatched" and self.transport == "definitely_not_sent":
            raise ValueError(
                "provider_dispatch 'dispatched' contradicts transport "
                "'definitely_not_sent'."
            )
        error = self.error if isinstance(self.error, dict) else {}
        if error.get("dispatched") is False and self.transport == "may_have_been_sent":
            raise ValueError(
                "A result may not claim 'dispatched: false' while its transport "
                "delivery is 'may_have_been_sent'."
            )
        return self


class CompanionSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    companion_id: str
    label: str = ""
    status: str
    extension_id: str = ""
    extension_version: str = ""
    flow_tab_present: bool = False
    bridge_paired: bool = False
    last_seen_at: datetime | None = None


class ExportTicketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companion_id: str = Field(default="", max_length=128)
    content_item_id: uuid.UUID | None = None
    brief_id: uuid.UUID | None = None
    generation_job_id: uuid.UUID | None = None
    expected_sha256: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    content_type: str = Field(default="", max_length=128)
    filename: str = Field(default="flow-asset", max_length=255)
    max_bytes: int = Field(default=0, ge=0, le=100 * 1024 * 1024)


class ExportTicketOut(BaseModel):
    id: uuid.UUID
    status: str
    expires_at: datetime | None
    upload_url: str
    #: Shown exactly once, in this response. Send it back in ``X-Export-Token``.
    upload_token: str = ""
    expected_sha256: str = ""
    declared_content_type: str = ""
    max_bytes: int = 0
    #: Bounded chunked transfer (Phase 3C.2 §9). ``upload_mode`` is ``single`` until
    #: the first chunk arrives.
    upload_mode: str = "single"
    chunk_bytes: int = 0
    max_chunks: int = 0
    one_time: bool = True
    note: str = (
        "The ticket is single-use and expires. The asset only exists after the bytes "
        "arrive, match the expected checksum and survive a storage read-back."
    )


class ExportStatusOut(BaseModel):
    id: uuid.UUID
    status: str
    asset_id: uuid.UUID | None = None
    checksum: str = ""
    received_bytes: int = 0
    upload_mode: str = "single"
    chunk_count: int = 0
    chunks_received: int = 0
    complete: bool = False
    width: int | None = None
    height: int | None = None
    error_code: str = ""
    error_message: str = ""
    consumed_at: datetime | None = None


class FlowChunkHeaders(BaseModel):
    """Documentation-only model: the headers a chunk must carry."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    total: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FlowStatusOut(BaseModel):
    integration: str
    phase: str
    available: bool
    enabled: bool
    live_generation_enabled: bool
    provider: str
    bridge: dict[str, Any]
    token_policy: dict[str, Any]
    generation: dict[str, Any]
    asset_export: dict[str, Any]
    companion: dict[str, Any]
    source_gate: dict[str, Any]
    protocol: dict[str, Any]
    lifecycle: dict[str, Any]
    upstream_blockers: list[dict[str, Any]]
    note: str
