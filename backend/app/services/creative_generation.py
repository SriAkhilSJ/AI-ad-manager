"""Creative generation jobs: submit, poll, retry, cancel, persist assets.

The service owns every policy a provider must not: authorisation, budget
reservation, idempotency, timeouts, retries and storage. Providers only generate.

Safety properties enforced here (and covered by tests):

* **Paid generation is off unless explicitly authorised.** A billable provider with
  ``creative_allow_paid_generation=False`` is refused before a request is built.
* **Unknown pricing is not free.** A billable request whose cost cannot be verified
  is refused rather than reserved at zero.
* **Zero budget is not unlimited.** ``creative_campaign_budget_usd=0`` blocks every
  billable job; the mock stays allowed because it genuinely costs nothing.
* **An ambiguous timeout is never auto-resubmitted.** The job keeps ``ambiguous``
  set and a retry requires an explicit founder acknowledgement.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.config import Settings, get_settings
from app.core.errors import (
    IntegrationUnavailableError,
    InvalidStateTransition,
    NotFoundError,
    ValidationProblem,
)
from app.creative.contracts import (
    CreativeProvider,
    CreativeProviderError,
    GenerationRequest,
    GenerationStatus,
)
from app.creative.media import dimensions_for, validate_upload
from app.creative.providers import get_creative_provider
from app.models.content import ContentVersion
from app.models.creative import CreativeAsset, CreativeAssetStatus, CreativeKind
from app.models.creative_brief import CreativeBrief
from app.models.creative_job import CreativeGenerationJob
from app.storage.base import StorageBackend, get_storage, storage_key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; compare everything in UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CreativeGenerationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        provider: CreativeProvider | None = None,
        storage: StorageBackend | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.provider = provider or get_creative_provider(self.settings)
        self.storage = storage or get_storage(self.settings)

    # ------------------------------------------------------------------ reads
    async def get(self, job_id: uuid.UUID) -> CreativeGenerationJob:
        job = await self.session.get(CreativeGenerationJob, job_id)
        if job is None:
            raise NotFoundError(f"Generation job {job_id} not found.")
        return job

    async def list(
        self,
        *,
        brief_id: uuid.UUID | None = None,
        status: str | None = None,
        campaign_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[CreativeGenerationJob]:
        stmt = select(CreativeGenerationJob)
        if brief_id:
            stmt = stmt.where(CreativeGenerationJob.brief_id == brief_id)
        if status:
            stmt = stmt.where(CreativeGenerationJob.status == status)
        if campaign_id:
            stmt = stmt.where(CreativeGenerationJob.campaign_id == campaign_id)
        stmt = stmt.order_by(CreativeGenerationJob.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    # ----------------------------------------------------------------- submit
    async def submit(
        self,
        *,
        brief: CreativeBrief,
        job_type: str | None = None,
        parameters: dict[str, Any] | None = None,
        actor_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        auto_advance: bool = True,
    ) -> tuple[CreativeGenerationJob, bool]:
        """Create and submit a generation job. Returns ``(job, created)``."""
        job_type = (job_type or brief.asset_type or "image").lower()
        if job_type not in ("image", "video"):
            raise ValidationProblem("Only image and video generation are supported.")

        capabilities = await self.provider.get_capabilities()
        if not capabilities.supports_image and job_type == "image":
            raise IntegrationUnavailableError(
                f"The '{capabilities.provider}' provider cannot generate images."
            )
        if not capabilities.supports_video and job_type == "video":
            raise IntegrationUnavailableError(
                f"The '{capabilities.provider}' provider cannot generate video."
            )

        # --- authorisation + budget, before any request exists --------------
        estimate, price_verified = self._estimate_cost(brief, job_type, capabilities)
        if capabilities.billable:
            from app.creative.providers import configured_provider_name

            configured = configured_provider_name(self.settings)
            if configured != capabilities.provider.lower():
                # A billable provider must be the one the operator configured by name,
                # otherwise we cannot say who would be charged.
                raise IntegrationUnavailableError(
                    f"Paid creative generation refused: the responding provider "
                    f"'{capabilities.provider}' is not the configured provider "
                    f"'{configured}'. Provider identity must be verified before paying."
                )
            if not self.settings.creative_allow_paid_generation:
                raise IntegrationUnavailableError(
                    "Paid creative generation is disabled. Set "
                    "CREATIVE_ALLOW_PAID_GENERATION only after pricing is verified and a "
                    "provider is authorised."
                )
            if not price_verified:
                raise IntegrationUnavailableError(
                    "Paid creative generation refused: this provider's price is unknown, "
                    "and unknown pricing must never be treated as zero."
                )
            budget = float(getattr(self.settings, "creative_campaign_budget_usd", 0.0) or 0.0)
            if budget <= 0:
                raise IntegrationUnavailableError(
                    "Creative budget is zero, which blocks every billable generation "
                    "(zero is not unlimited)."
                )
            if estimate is not None and estimate > budget:
                raise IntegrationUnavailableError(
                    f"Estimated cost ${estimate:.4f} exceeds the creative budget "
                    f"${budget:.4f}."
                )

        request_params = dict(parameters or {})
        request_hash = _hash(
            {
                "brief_id": str(brief.id),
                "brief_version": brief.version_number,
                "job_type": job_type,
                "prompt": brief.image_prompt,
                "parameters": request_params,
            }
        )

        # --- idempotency ----------------------------------------------------
        if idempotency_key:
            existing = (
                await self.session.execute(
                    select(CreativeGenerationJob).where(
                        CreativeGenerationJob.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise InvalidStateTransition(
                        "This Idempotency-Key was already used for a different generation "
                        "request.",
                        details={"job_id": str(existing.id)},
                    )
                return existing, False
        else:
            duplicate = await self._find_duplicate(brief, request_hash)
            if duplicate is not None:
                return duplicate, False

        job = CreativeGenerationJob(
            brief_id=brief.id,
            brief_version=brief.version_number,
            brand_profile_id=brief.brand_profile_id,
            campaign_id=brief.campaign_id,
            content_item_id=brief.content_item_id,
            content_version_id=brief.content_version_id,
            job_type=job_type,
            provider=capabilities.provider,
            status=GenerationStatus.QUEUED.value,
            parameters=request_params,
            attempts=0,
            max_attempts=int(getattr(self.settings, "creative_job_max_attempts", 2) or 2),
            timeout_seconds=int(getattr(self.settings, "creative_job_timeout_seconds", 300) or 300),
            is_mock=capabilities.is_mock,
            cost_estimate_usd=estimate,
            price_verified=price_verified,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            created_by=actor_id,
            audit_trail=[
                {
                    "at": _now().isoformat(),
                    "event": "created",
                    "status": GenerationStatus.QUEUED.value,
                    "provider": capabilities.provider,
                    "is_mock": capabilities.is_mock,
                }
            ],
        )
        self.session.add(job)
        await self.session.flush()

        await self._submit_to_provider(job, brief)
        if auto_advance and job.status not in {s.value for s in GenerationStatus.terminal()}:
            await self.advance(job, brief=brief)

        await record_audit(
            self.session,
            action="creative.job_submitted",
            entity_type="creative_generation_job",
            entity_id=job.id,
            actor_id=actor_id,
            after={
                "provider": job.provider,
                "status": job.status,
                "is_mock": job.is_mock,
                "job_type": job.job_type,
                "brief_id": str(brief.id),
            },
        )
        return job, True

    async def _submit_to_provider(
        self, job: CreativeGenerationJob, brief: CreativeBrief
    ) -> None:
        request = GenerationRequest(
            job_type=job.job_type,
            prompt=brief.image_prompt,
            negative_prompt=", ".join(brief.negative_prompt),
            aspect_ratio=brief.aspect_ratio,
            resolution=brief.resolution,
            duration_seconds=int(brief.duration_seconds or 0),
            seed=int(job.parameters.get("seed") or 0),
            parameters=dict(job.parameters),
            brief_id=str(brief.id),
            content_item_id=str(brief.content_item_id or ""),
            content_version_id=str(brief.content_version_id or ""),
            idempotency_key=job.idempotency_key or "",
        )
        job.attempts += 1
        job.submitted_prompt = request.prompt
        job.negative_prompt = request.negative_prompt
        job.submitted_at = _now()
        try:
            submission = (
                await self.provider.submit_video_generation(request)
                if job.job_type == "video"
                else await self.provider.submit_image_generation(request)
            )
        except CreativeProviderError as exc:
            self._fail(job, code=exc.code, message=str(exc), retryable=exc.retryable)
            await self.session.flush()
            return
        except IntegrationUnavailableError:
            self._fail(
                job,
                code="provider_unavailable",
                message="The creative provider is unavailable.",
                retryable=False,
            )
            await self.session.flush()
            raise

        job.external_job_id = submission.job_id
        job.provider = submission.provider or job.provider
        job.status = submission.status or GenerationStatus.SUBMITTED.value
        job.ambiguous = submission.ambiguous
        job.parameters = {**job.parameters, **dict(submission.parameters or {})}
        self.session.add(job)
        await self.session.flush()
        self._audit(
            job,
            "submitted",
            {
                "external_job_id": submission.job_id,
                "status": job.status,
                "ambiguous": job.ambiguous,
            },
        )

    # ----------------------------------------------------------------- poll
    async def advance(
        self,
        job: CreativeGenerationJob,
        *,
        brief: CreativeBrief | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> CreativeGenerationJob:
        """Poll the provider once and apply the result (including asset storage)."""
        terminal = {s.value for s in GenerationStatus.terminal()}
        if job.status in terminal:
            return job

        if job.submitted_at is not None:
            elapsed = (_now() - _aware(job.submitted_at)).total_seconds()
            if elapsed > job.timeout_seconds:
                # Ask the provider once more before declaring a timeout: a slow job
                # may simply have finished after our deadline.
                state = await self._safe_status(job)
                if state is not None and state.status == GenerationStatus.SUCCEEDED.value:
                    return await self._complete(job, brief, actor_id)
                job.status = GenerationStatus.TIMED_OUT.value
                job.ambiguous = True if (state is None or state.ambiguous) else False
                job.error_code = "timeout"
                job.error_message = (
                    f"No result within {job.timeout_seconds}s."
                    + (
                        " The provider cannot confirm whether the job ran, so it will not "
                        "be resubmitted automatically."
                        if job.ambiguous
                        else ""
                    )
                )
                job.error_retryable = not job.ambiguous
                job.cancel_requested = False
                self.session.add(job)
                await self.session.flush()
                self._audit(job, "timed_out", {"ambiguous": job.ambiguous})
                await record_audit(
                    self.session,
                    action="creative.job_timed_out",
                    entity_type="creative_generation_job",
                    entity_id=job.id,
                    actor_id=actor_id,
                    after={"ambiguous": job.ambiguous},
                )
                return job

        state = await self._safe_status(job)
        if state is None:
            return job

        if state.ambiguous:
            job.ambiguous = True
            self._audit(job, "ambiguous", {"status": state.status})
            self.session.add(job)
            await self.session.flush()
            return job

        if state.status == GenerationStatus.SUCCEEDED.value:
            return await self._complete(job, brief, actor_id)

        if state.status == GenerationStatus.FAILED.value:
            self._fail(
                job,
                code=state.error_code or "provider_failure",
                message=state.error_message or "The provider reported a failure.",
                retryable=state.retryable,
            )
            await self.session.flush()
            self._audit(job, "failed", {"code": job.error_code, "retryable": job.error_retryable})
            return job

        if state.status != job.status:
            job.status = state.status
            job.started_at = job.started_at or _now()
            self.session.add(job)
            await self.session.flush()
            self._audit(job, "status_changed", {"status": job.status})
        return job

    async def _safe_status(self, job: CreativeGenerationJob):  # type: ignore[no-untyped-def]
        if not job.external_job_id:
            return None
        try:
            return await self.provider.get_generation_status(job.external_job_id)
        except CreativeProviderError as exc:
            job.ambiguous = exc.ambiguous
            self._audit(job, "status_error", {"code": exc.code, "ambiguous": exc.ambiguous})
            return None
        except IntegrationUnavailableError:
            job.ambiguous = True
            self._audit(job, "status_unavailable", {"ambiguous": True})
            return None

    # ------------------------------------------------------------- completion
    async def _complete(
        self,
        job: CreativeGenerationJob,
        brief: CreativeBrief | None,
        actor_id: uuid.UUID | None,
    ) -> CreativeGenerationJob:
        if brief is None:
            brief = await self.session.get(CreativeBrief, job.brief_id)
        if brief is None:  # pragma: no cover - FK guarantees this
            raise NotFoundError("The brief for this job no longer exists.")

        try:
            media = await self.provider.retrieve_asset(job.external_job_id)
        except CreativeProviderError as exc:
            self._fail(job, code=exc.code, message=str(exc), retryable=exc.retryable)
            await self.session.flush()
            return job
        except IntegrationUnavailableError:
            job.ambiguous = True
            self._audit(job, "retrieval_unavailable", {"ambiguous": True})
            await self.session.flush()
            return job

        max_bytes = int(getattr(self.settings, "creative_max_upload_bytes", 25 * 1024 * 1024))
        info, warnings, error = validate_upload(
            media.data,
            declared_mime=media.mime_type,
            filename=media.filename,
            max_bytes=max_bytes,
            strict=True,
        )
        if error:
            self._fail(
                job,
                code="invalid_media",
                message=f"The provider returned bytes that failed validation: {error}",
                retryable=False,
            )
            await self.session.flush()
            self._audit(job, "invalid_media", {"error": error})
            return job

        key = storage_key(media.filename or "asset.png", prefix="creatives")
        stored = await self.storage.put(
            key, media.data, content_type=info.mime_type or media.mime_type
        )
        # Verify the bytes are actually retrievable before recording success.
        try:
            verification = await self.storage.get(stored.key)
        except Exception as exc:  # noqa: BLE001
            raise IntegrationUnavailableError(
                "Generated bytes could not be read back from storage."
            ) from exc
        if verification != media.data:
            raise IntegrationUnavailableError(
                "Storage round-trip mismatch: the persisted bytes differ from the generated "
                "bytes."
            )

        asset = await self._create_asset(
            job=job,
            brief=brief,
            data=media.data,
            mime_type=info.mime_type or media.mime_type,
            detected_mime=info.mime_type or "",
            width=info.width or media.width,
            height=info.height or media.height,
            duration_seconds=media.duration_seconds or brief.duration_seconds,
            storage_key_ext=stored.key,
            public_url=stored.public_url,
            media_metadata=dict(media.metadata or {}),
            actor_id=actor_id,
        )

        job.status = GenerationStatus.SUCCEEDED.value
        job.completed_at = _now()
        job.asset_id = asset.id
        job.cost_actual_usd = media.cost_actual_usd if media.cost_actual_usd is not None else 0.0
        job.error_code = ""
        job.error_message = ""
        job.error_retryable = False
        job.ambiguous = False
        self.session.add(job)
        await self.session.flush()
        self._audit(
            job,
            "succeeded",
            {
                "asset_id": str(asset.id),
                "bytes": len(media.data),
                "checksum": asset.checksum,
                "mime": asset.mime_type,
                "warnings": warnings,
            },
        )
        await record_audit(
            self.session,
            action="creative.job_succeeded",
            entity_type="creative_generation_job",
            entity_id=job.id,
            actor_id=actor_id,
            after={
                "asset_id": str(asset.id),
                "provider": job.provider,
                "is_mock": job.is_mock,
                "bytes": len(media.data),
            },
        )
        return job

    async def _create_asset(
        self,
        *,
        job: CreativeGenerationJob,
        brief: CreativeBrief,
        data: bytes,
        mime_type: str,
        detected_mime: str,
        width: int | None,
        height: int | None,
        duration_seconds: float | None,
        storage_key_ext: str,
        public_url: str | None,
        media_metadata: dict[str, Any],
        actor_id: uuid.UUID | None,
    ) -> CreativeAsset:
        checksum = hashlib.sha256(data).hexdigest()
        previous = (
            await self.session.execute(
                select(CreativeAsset)
                .where(
                    CreativeAsset.content_item_id == job.content_item_id,
                    CreativeAsset.content_version_id == job.content_version_id,
                    CreativeAsset.kind == job.job_type,
                )
                .order_by(CreativeAsset.asset_version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        asset = CreativeAsset(
            brand_profile_id=brief.brand_profile_id,
            campaign_id=brief.campaign_id,
            brief_id=brief.id,
            brief_version=brief.version_number,
            generation_job_id=job.id,
            content_item_id=job.content_item_id,
            content_version_id=job.content_version_id,
            kind=job.job_type if job.job_type in {k.value for k in CreativeKind} else "image",
            status=CreativeAssetStatus.READY.value,
            filename=f"{job.job_type}-{job.id.hex[:12]}.png",
            mime_type=mime_type,
            detected_mime=detected_mime,
            metadata_verified=True,
            size_bytes=len(data),
            storage_key=storage_key_ext,
            public_url=public_url,
            checksum=checksum,
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            aspect_ratio=brief.aspect_ratio,
            is_mock=job.is_mock,
            asset_version=int(previous.asset_version) + 1 if previous else 1,
            supersedes_asset_id=previous.id if previous else None,
            root_asset_id=(
                previous.root_asset_id
                if previous and previous.root_asset_id
                else (previous.id if previous else None)
            ),
            selected=False,
            prompt=brief.image_prompt,
            provider=job.provider,
            generated_by_agent="creative",
            metadata_json={
                **media_metadata,
                "is_mock": job.is_mock,
                "provider": job.provider,
                "brief_id": str(brief.id),
                "brief_version": brief.version_number,
                "generation_job_id": str(job.id),
                "content_excerpt": _content_excerpt(brief),
                "platform": brief.platform,
                "requested_resolution": brief.resolution,
            },
        )
        self.session.add(asset)
        await self.session.flush()

        # Previous versions stay on disk and in the table; only their status changes.
        if previous is not None and previous.status != CreativeAssetStatus.SUPERSEDED.value:
            previous.status = CreativeAssetStatus.SUPERSEDED.value
            self.session.add(previous)

        # Automated QA runs immediately, so an obviously broken asset never reaches
        # an approval screen looking fine.
        from app.services.creative_qa import CreativeQAService

        await CreativeQAService(self.session, self.storage).evaluate(
            asset=asset, brief=brief, brand=None, actor_id=actor_id
        )
        return asset

    # ----------------------------------------------------------------- cancel
    async def cancel(
        self, job: CreativeGenerationJob, *, actor_id: uuid.UUID | None = None
    ) -> CreativeGenerationJob:
        if job.status in {s.value for s in GenerationStatus.terminal()}:
            raise InvalidStateTransition(
                f"A job in status '{job.status}' cannot be cancelled.",
                details={"status": job.status},
            )
        supported = True
        if job.external_job_id:
            try:
                supported = await self.provider.cancel_generation(job.external_job_id)
            except IntegrationUnavailableError:
                supported = False
        job.cancel_requested = True
        if supported:
            job.status = GenerationStatus.CANCELLED.value
            job.completed_at = _now()
            job.error_code = "cancelled"
            job.error_message = "Cancelled by the operator."
        else:
            job.error_message = (
                "Cancellation was requested but the provider no longer tracks this job."
            )
        self.session.add(job)
        await self.session.flush()
        self._audit(job, "cancel_requested", {"provider_supported": supported})
        await record_audit(
            self.session,
            action="creative.job_cancelled",
            entity_type="creative_generation_job",
            entity_id=job.id,
            actor_id=actor_id,
            after={"status": job.status, "supported": supported},
        )
        return job

    # ------------------------------------------------------------------ retry
    async def retry(
        self,
        job: CreativeGenerationJob,
        *,
        brief: CreativeBrief | None = None,
        actor_id: uuid.UUID | None = None,
        authorize_ambiguous: bool = False,
    ) -> CreativeGenerationJob:
        if job.status not in (
            GenerationStatus.FAILED.value,
            GenerationStatus.TIMED_OUT.value,
        ):
            raise InvalidStateTransition(
                f"Only failed or timed-out jobs can be retried (status '{job.status}').",
                details={"status": job.status},
            )
        if job.status == GenerationStatus.TIMED_OUT.value and job.ambiguous and not authorize_ambiguous:
            raise InvalidStateTransition(
                "This job timed out ambiguously: the provider may already have generated "
                "(and charged for) this asset. Retrying automatically could pay twice. "
                "Re-submit with explicit authorisation after checking the provider.",
                details={"job_id": str(job.id), "ambiguous": True},
            )
        if job.attempts >= job.max_attempts:
            raise InvalidStateTransition(
                f"Retry limit reached ({job.attempts}/{job.max_attempts} attempts).",
                details={"attempts": job.attempts, "max_attempts": job.max_attempts},
            )
        if brief is None:
            brief = await self.session.get(CreativeBrief, job.brief_id)
        if brief is None:  # pragma: no cover
            raise NotFoundError("The brief for this job no longer exists.")

        job.status = GenerationStatus.QUEUED.value
        job.error_code = ""
        job.error_message = ""
        job.error_retryable = False
        job.ambiguous = False
        job.completed_at = None
        self.session.add(job)
        await self.session.flush()
        self._audit(job, "retry_requested", {"attempts": job.attempts})

        await self._submit_to_provider(job, brief)
        if job.status not in {s.value for s in GenerationStatus.terminal()}:
            await self.advance(job, brief=brief, actor_id=actor_id)
        await record_audit(
            self.session,
            action="creative.job_retried",
            entity_type="creative_generation_job",
            entity_id=job.id,
            actor_id=actor_id,
            after={"status": job.status, "attempts": job.attempts},
        )
        return job

    # ----------------------------------------------------------------- helpers
    def _fail(
        self, job: CreativeGenerationJob, *, code: str, message: str, retryable: bool
    ) -> None:
        job.status = GenerationStatus.FAILED.value
        job.error_code = code
        job.error_message = message
        job.error_retryable = retryable
        job.completed_at = _now()
        self.session.add(job)

    def _audit(self, job: CreativeGenerationJob, event: str, detail: dict[str, Any]) -> None:
        trail = list(job.audit_trail or [])
        trail.append(
            {
                "at": _now().isoformat(),
                "event": event,
                "status": job.status,
                "attempt": job.attempts,
                **detail,
            }
        )
        job.audit_trail = trail[-40:]
        self.session.add(job)

    def _estimate_cost(
        self, brief: CreativeBrief, job_type: str, capabilities: Any | None = None
    ) -> tuple[float | None, bool]:
        """Return ``(estimate_usd, price_verified)``.

        The mock is free *by construction*, which is different from "we do not know
        the price". A billable provider with no verified price returns
        ``(None, False)`` and is refused upstream.
        """
        provider_name = getattr(capabilities, "provider", None)
        is_mock = getattr(capabilities, "is_mock", None)
        if is_mock is None:
            is_mock = (getattr(self.settings, "creative_provider", "mock") or "mock") == "mock"
        if is_mock or provider_name == "mock":
            return 0.0, True
        pricing = getattr(self.settings, "creative_pricing_json", None) or {}
        price_per_unit = pricing.get(job_type) if isinstance(pricing, dict) else None
        if isinstance(price_per_unit, (int, float)) and price_per_unit > 0:
            return float(price_per_unit), True
        return None, False

    async def _find_duplicate(
        self, brief: CreativeBrief, request_hash: str
    ) -> CreativeGenerationJob | None:
        """Same brief, same request, still live or already succeeded: reuse it."""
        stmt = (
            select(CreativeGenerationJob)
            .where(
                CreativeGenerationJob.brief_id == brief.id,
                CreativeGenerationJob.request_hash == request_hash,
                CreativeGenerationJob.status.in_(
                    [
                        GenerationStatus.QUEUED.value,
                        GenerationStatus.SUBMITTED.value,
                        GenerationStatus.RUNNING.value,
                        GenerationStatus.SUCCEEDED.value,
                    ]
                ),
            )
            .order_by(CreativeGenerationJob.created_at.desc())
            .limit(1)
        )
        job = (await self.session.execute(stmt)).scalar_one_or_none()
        if job is None:
            return None
        # A succeeded job older than a day is history, not a duplicate to reuse.
        cutoff = _now() - timedelta(days=1)
        created = job.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                return None
        return job


def _content_excerpt(brief: CreativeBrief, limit: int = 240) -> str:
    parts = [brief.creative_objective, brief.visual_concept]
    text = " ".join(p for p in parts if p)
    return text[:limit]


def expected_dimensions(brief: CreativeBrief) -> tuple[int, int]:
    return dimensions_for(brief.aspect_ratio or "4:5", 960)

