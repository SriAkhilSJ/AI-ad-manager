"""Human approval gate for real Flow generations (Phase 3C.1).

The rule from the audit is blunt: *every* real generation needs a founder approval
bound to the campaign, the content version, the creative brief version, the prompt
hash, the provider, the model, the aspect ratio, the duration and the output count.
An agent cannot satisfy this gate, because satisfying it means a founder pressing
approve on that exact combination — the gate reads the single Phase 1 approval
ledger and nothing else.

Two deliberate consequences:

* A 3B-era approval (no ``flow_*`` columns) does **not** authorise a Flow
  generation. Absent evidence is not evidence of approval.
* Any change to the prompt, model, ratio, duration or count changes the binding
  hash, so a previously approved combination cannot be reused for a different one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionDeniedError
from app.flow.protocol import GenerationCommand
from app.models.approval import ApprovalAction, ApprovalRecord
from app.models.content import ContentItem
from app.models.creative_brief import CreativeBrief


class ApprovalRequiredError(PermissionDeniedError):
    """No founder approval covers this exact generation combination."""

    code = "flow_approval_required"
    status_code = 403


@dataclass(frozen=True, slots=True)
class FlowGenerationBinding:
    """The exact combination a founder must have approved."""

    campaign_id: str
    content_version_id: str
    brief_id: str
    brief_version: int
    prompt_hash: str
    provider: str
    model: str
    aspect_ratio: str
    duration_seconds: int
    output_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "content_version_id": self.content_version_id,
            "brief_id": self.brief_id,
            "brief_version": self.brief_version,
            "prompt_hash": self.prompt_hash,
            "provider": self.provider,
            "model": self.model,
            "aspect_ratio": self.aspect_ratio,
            "duration_seconds": self.duration_seconds,
            "output_count": self.output_count,
        }

    def hash(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_brief(
        cls,
        brief: CreativeBrief,
        *,
        request: GenerationCommand,
        provider: str,
        model: str,
    ) -> "FlowGenerationBinding":
        return cls(
            campaign_id=str(brief.campaign_id or ""),
            content_version_id=str(brief.content_version_id or ""),
            brief_id=str(brief.id),
            brief_version=int(brief.version_number or 0),
            prompt_hash=request.prompt_hash(),
            provider=provider,
            model=model or request.model,
            aspect_ratio=request.aspect_ratio,
            duration_seconds=int(request.duration_seconds),
            output_count=int(request.count),
        )


class FlowApprovalGate:
    """Reads the single approval ledger; never writes it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def authorize(
        self, binding: FlowGenerationBinding
    ) -> ApprovalRecord:
        """Return the covering approval or raise :class:`ApprovalRequiredError`."""
        item = await self._item_for_version(binding.content_version_id)
        if item is None:
            raise ApprovalRequiredError(
                "No content item exists for this content version.",
                details={"content_version_id": binding.content_version_id},
            )

        record = await self._live_record(item.id, binding.content_version_id)
        if record is None:
            raise ApprovalRequiredError(
                "This content version has no live founder approval.",
                details={"content_version_id": binding.content_version_id},
            )
        if not record.flow_binding_hash:
            raise ApprovalRequiredError(
                "The live approval does not cover a Flow generation. Approve the exact "
                "prompt, model, aspect ratio, duration and output count first.",
                details={"approval_id": str(record.id)},
            )
        if record.flow_binding_hash != binding.hash():
            raise ApprovalRequiredError(
                "The approval covers a different generation combination (prompt hash, "
                "model, aspect ratio, duration or output count changed).",
                details={
                    "approval_id": str(record.id),
                    "approved_hash": record.flow_binding_hash[:16],
                    "requested_hash": binding.hash()[:16],
                },
            )
        if record.flow_output_count != binding.output_count:
            raise ApprovalRequiredError(
                "The approval covers a different output count.",
                details={"approved": record.flow_output_count, "requested": binding.output_count},
            )
        return record

    async def _item_for_version(self, content_version_id: str) -> ContentItem | None:
        try:
            version_uuid = uuid.UUID(content_version_id)
        except (ValueError, TypeError):
            return None
        stmt = select(ContentItem).where(ContentItem.current_version_id == version_uuid)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _live_record(
        self, item_id: uuid.UUID, content_version_id: str
    ) -> ApprovalRecord | None:
        try:
            version_uuid = uuid.UUID(content_version_id)
        except (ValueError, TypeError):
            return None
        stmt = (
            select(ApprovalRecord)
            .where(
                ApprovalRecord.content_item_id == item_id,
                ApprovalRecord.content_version_id == version_uuid,
                ApprovalRecord.action == ApprovalAction.APPROVE.value,
                ApprovalRecord.invalidated_at.is_(None),
            )
            .order_by(ApprovalRecord.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


def flow_binding_columns(binding: FlowGenerationBinding) -> dict[str, Any]:
    """Column values stamped onto an ``ApprovalRecord`` when a founder approves."""
    return {
        "flow_prompt_hash": binding.prompt_hash,
        "flow_provider": binding.provider,
        "flow_model": binding.model,
        "flow_aspect_ratio": binding.aspect_ratio,
        "flow_duration_seconds": binding.duration_seconds,
        "flow_output_count": binding.output_count,
        "flow_binding_hash": binding.hash(),
    }
