"""Creative QA — automated checks with explicit limits.

Three things this service refuses to do:

1. **Read text in an image.** There is no OCR here, so ``text_readability`` is
   reported as ``not_performed`` — never as a pass.
2. **Judge aesthetics.** Composition, mood and brand "feel" require a human. Every
   report ends with ``human_visual_review_required: true``.
3. **Treat missing data as success.** A check that could not run is recorded with
   ``performed: false`` and does not contribute to a PASS.

What it *does* do is real: it re-reads the stored bytes, verifies the checksum,
parses the image header, decodes PNG pixels and measures the actual colour
distribution against the Crusor palette, and scans the brief and prompt for
unsupported claims. Formats it cannot decode are reported as not analysed rather
than guessed at.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.content.platforms import get_spec
from app.core.audit import record_audit
from app.creative.media import PngStats, analyze_png, inspect_media
from app.models.brand import BrandProfile
from app.models.creative import CreativeAsset, CreativeValidationStatus
from app.models.creative_brief import CreativeBrief
from app.storage.base import StorageBackend

VERDICTS = CreativeValidationStatus

#: Minimum sensible dimensions per platform for a feed asset.
PLATFORM_MIN_DIMENSION = {
    "instagram": 320,
    "linkedin": 400,
    "youtube": 640,
    "x": 400,
    "facebook": 400,
    "reddit": 400,
}

PLATFORM_ASPECTS = {
    "instagram": {"4:5", "1:1", "9:16"},
    "linkedin": {"1.91:1", "1:1", "4:5"},
    "youtube": {"16:9", "9:16"},
    "x": {"16:9", "1:1", "4:5"},
    "facebook": {"1.91:1", "1:1", "4:5"},
    "reddit": {"16:9", "1:1", "4:5"},
}

CLAIM_PATTERNS: list[tuple[str, str]] = [
    (r"\d+\s*%", "percentage claim"),
    (r"\b\d[\d,]{3,}\+?\s+(developers|users|customers|teams|companies)\b", "user-count claim"),
    (r"[$₹€]\s?\d", "price claim"),
    (r"\b(\d+(\.\d+)?x)\s+(faster|better|cheaper)\b", "multiple claim"),
    (r"\b(guarantee[ds]?|guaranteed|risk[- ]free)\b", "guarantee claim"),
    (r"\b(best|fastest|leading|number one|#1)\b", "superlative claim"),
    (r"\b(used by|trusted by|backed by)\b", "social-proof claim"),
]

LOGO_REPRODUCTION_PATTERNS = [
    r"recreate (the )?(crusor )?logo",
    r"redraw (the )?(crusor )?logo",
    r"approximate (the )?logo",
    r"draw the crusor logo",
]

SCREENSHOT_PATTERNS = [
    r"screenshot of (the )?crusor",
    r"exact ui of",
    r"replica of the (crusor )?(ide|editor)",
]


class CreativeQAService:
    def __init__(self, session: AsyncSession, storage: StorageBackend | None = None) -> None:
        self.session = session
        self.storage = storage

    # ------------------------------------------------------------------ public
    async def evaluate(
        self,
        *,
        asset: CreativeAsset,
        brief: CreativeBrief | None = None,
        brand: BrandProfile | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []

        data: bytes | None = None
        if self.storage is not None and asset.storage_key:
            try:
                data = await self.storage.get(asset.storage_key)
            except Exception:  # noqa: BLE001 - absence is a finding, not a crash
                data = None

        checks.append(
            self._check(
                "bytes_present",
                "integrity",
                passed=bool(data),
                performed=True,
                detail=(
                    f"{len(data)} bytes read back from storage"
                    if data
                    else "Stored object could not be read back from storage"
                ),
            )
        )
        if not data:
            issues.append(
                self._issue(
                    "bytes_missing",
                    "integrity",
                    "blocking",
                    "The asset's stored bytes could not be read, so nothing can be verified.",
                    suggestion="Re-upload the asset or re-run generation.",
                )
            )

        if data:
            import hashlib

            checksum = hashlib.sha256(data).hexdigest()
            checksum_ok = bool(asset.checksum) and checksum == asset.checksum
            checks.append(
                self._check(
                    "checksum_matches",
                    "integrity",
                    passed=checksum_ok,
                    performed=True,
                    detail=(
                        "SHA-256 of stored bytes matches the recorded checksum"
                        if checksum_ok
                        else "Stored bytes do not match the recorded checksum"
                    ),
                )
            )
            if not checksum_ok:
                issues.append(
                    self._issue(
                        "checksum_mismatch",
                        "integrity",
                        "blocking",
                        "The stored bytes no longer match the checksum recorded at upload.",
                        suggestion="Treat this asset as corrupted and regenerate it.",
                    )
                )

            info = inspect_media(data)
            detected = info.mime_type or ""
            mime_ok = bool(detected) and (
                not asset.mime_type or detected == asset.mime_type
            )
            checks.append(
                self._check(
                    "media_type",
                    "format",
                    passed=mime_ok,
                    performed=True,
                    detail=(
                        f"content is {detected}"
                        if detected
                        else "content signature not recognised"
                    ),
                )
            )
            if detected and asset.mime_type and detected != asset.mime_type:
                issues.append(
                    self._issue(
                        "mime_mismatch",
                        "format",
                        "needs_revision",
                        f"Recorded type '{asset.mime_type}' does not match the bytes ('{detected}').",
                        suggestion="Correct the asset's MIME type or re-upload the correct file.",
                    )
                )

            width = info.width or asset.width
            height = info.height or asset.height
            dimension_ok = bool(width and height)
            checks.append(
                self._check(
                    "dimensions_known",
                    "format",
                    passed=dimension_ok,
                    performed=True,
                    detail=f"{width}x{height}" if dimension_ok else "dimensions unavailable",
                )
            )

            platform = (brief.platform if brief else "") or (
                asset.metadata_json.get("platform", "") if asset.metadata_json else ""
            )
            spec_min = PLATFORM_MIN_DIMENSION.get(platform, 320)
            if dimension_ok:
                small = min(int(width), int(height)) < spec_min  # type: ignore[arg-type]
                checks.append(
                    self._check(
                        "minimum_dimensions",
                        "format",
                        passed=not small,
                        performed=True,
                        detail=f"short edge {min(int(width), int(height))}px vs {spec_min}px minimum for {platform or 'generic'}",
                    )
                )
                if small:
                    issues.append(
                        self._issue(
                            "dimensions_too_small",
                            "format",
                            "needs_revision",
                            f"Short edge is {min(int(width), int(height))}px; {platform or 'the platform'} needs at least {spec_min}px.",
                            suggestion="Regenerate at a larger size.",
                        )
                    )

                ratio = self._ratio_label(int(width), int(height))
                allowed = PLATFORM_ASPECTS.get(platform)
                if allowed:
                    ratio_ok = ratio in allowed
                    checks.append(
                        self._check(
                            "platform_aspect_ratio",
                            "format",
                            passed=ratio_ok,
                            performed=True,
                            detail=f"actual {ratio} {'in' if ratio_ok else 'not in'} {sorted(allowed)} for {platform}",
                        )
                    )
                    if not ratio_ok:
                        issues.append(
                            self._issue(
                                "aspect_ratio_mismatch",
                                "format",
                                "needs_revision",
                                f"Frame is {ratio}; {platform} accepts {', '.join(sorted(allowed))}.",
                                suggestion="Regenerate at a supported aspect ratio for the platform.",
                            )
                        )
                else:
                    checks.append(
                        self._check(
                            "platform_aspect_ratio",
                            "format",
                            passed=False,
                            performed=False,
                            detail="no platform specified, ratio rules not applied",
                        )
                    )

                if brief and brief.resolution and brief.resolution != f"{width}x{height}":
                    checks.append(
                        self._check(
                            "requested_resolution_met",
                            "format",
                            passed=False,
                            performed=True,
                            detail=f"brief asked {brief.resolution}; asset is {width}x{height}",
                        )
                    )
                    issues.append(
                        self._issue(
                            "resolution_reduced",
                            "format",
                            "warning",
                            f"The brief requested {brief.resolution} but the asset is {width}x{height}.",
                            suggestion="Accept for mock output, or regenerate with a provider that honours the full size.",
                        )
                    )
                else:
                    checks.append(
                        self._check(
                            "requested_resolution_met",
                            "format",
                            passed=True,
                            performed=True,
                            detail="asset matches the requested resolution"
                            if brief
                            else "no resolution requested",
                        )
                    )

            # ---- real pixel analysis (PNG only) -----------------------------
            stats: PngStats | None = None
            if detected == "image/png":
                try:
                    stats = analyze_png(data, sample_step=9)
                    checks.append(
                        self._check(
                            "pixel_analysis",
                            "brand",
                            passed=True,
                            performed=True,
                            detail=(
                                f"mean luma {stats.mean_luma:.1f}, {stats.dark_ratio:.0%} dark, "
                                f"{stats.purple_ratio:.0%} purple, {stats.metallic_ratio:.1%} metallic"
                            ),
                        )
                    )
                except ValueError as exc:
                    checks.append(
                        self._check(
                            "pixel_analysis",
                            "brand",
                            passed=False,
                            performed=False,
                            detail=f"PNG could not be decoded ({exc})",
                        )
                    )
            else:
                checks.append(
                    self._check(
                        "pixel_analysis",
                        "brand",
                        passed=False,
                        performed=False,
                        detail="pixel analysis supports PNG only; this asset was not analysed",
                    )
                )

            if stats is not None:
                dark_ok = stats.dark_ratio >= 0.45
                checks.append(
                    self._check(
                        "brand_dark_base",
                        "brand",
                        passed=dark_ok,
                        performed=True,
                        detail=f"{stats.dark_ratio:.0%} of sampled pixels are dark (charcoal base)",
                    )
                )
                if not dark_ok:
                    issues.append(
                        self._issue(
                            "brand_dark_base_missing",
                            "brand",
                            "warning",
                            "Less than 45% of the frame is dark; the Crusor system is charcoal-first.",
                            suggestion="Darken the background toward #0A0A0B.",
                        )
                    )
                purple_ok = stats.purple_ratio <= 0.35
                checks.append(
                    self._check(
                        "brand_purple_restraint",
                        "brand",
                        passed=purple_ok,
                        performed=True,
                        detail=f"{stats.purple_ratio:.0%} of the frame reads as purple (ceiling 35%)",
                    )
                )
                if not purple_ok:
                    issues.append(
                        self._issue(
                            "brand_purple_overuse",
                            "brand",
                            "warning",
                            "Purple covers too much of the frame; it is an accent, not a wash.",
                            suggestion="Reduce the glow radius or intensity.",
                        )
                    )
            else:
                checks.append(
                    self._check(
                        "brand_dark_base",
                        "brand",
                        passed=False,
                        performed=False,
                        detail="not measurable without decoded pixels",
                    )
                )
                checks.append(
                    self._check(
                        "brand_purple_restraint",
                        "brand",
                        passed=False,
                        performed=False,
                        detail="not measurable without decoded pixels",
                    )
                )

        # ---- text-based checks (brief + prompt) -----------------------------
        prompt_text = " ".join(
            part
            for part in (
                asset.prompt or "",
                brief.image_prompt if brief else "",
                brief.visual_concept if brief else "",
                " ".join(brief.scenes) if brief else "",
            )
            if part
        )

        claim_hits = self._claims_in(prompt_text, brand)
        checks.append(
            self._check(
                "unsupported_claims",
                "claims",
                passed=not claim_hits,
                performed=True,
                detail=(
                    "no claim patterns in the brief or prompt"
                    if not claim_hits
                    else "claim patterns found: " + ", ".join(sorted({c[1] for c in claim_hits}))
                ),
            )
        )
        for phrase, kind in claim_hits:
            issues.append(
                self._issue(
                    "unsupported_claim",
                    "claims",
                    "blocking",
                    f"The brief or prompt contains a {kind}: '{phrase}'.",
                    evidence=phrase,
                    suggestion="Remove the claim. Unverified numbers and superlatives are not publishable.",
                )
            )

        logo_hits = [p for p in LOGO_REPRODUCTION_PATTERNS if re.search(p, prompt_text, re.I)]
        checks.append(
            self._check(
                "logo_handling",
                "brand",
                passed=not logo_hits,
                performed=True,
                detail=(
                    "no instruction to redraw or approximate the logo"
                    if not logo_hits
                    else "prompt instructs recreating the logo"
                ),
            )
        )
        for hit in logo_hits:
            issues.append(
                self._issue(
                    "logo_recreation_requested",
                    "brand",
                    "blocking",
                    "The prompt asks for the Crusor logo to be recreated or approximated.",
                    evidence=hit,
                    suggestion="Use the official brand asset, or omit the logo entirely.",
                )
            )

        screenshot_hits = [p for p in SCREENSHOT_PATTERNS if re.search(p, prompt_text, re.I)]
        checks.append(
            self._check(
                "product_ui_distinction",
                "claims",
                passed=not screenshot_hits,
                performed=True,
                detail=(
                    "visual is conceptual; no instruction to reproduce real UI"
                    if not screenshot_hits
                    else "prompt asks for a screenshot-like reproduction of Crusor UI"
                ),
            )
        )
        for hit in screenshot_hits:
            issues.append(
                self._issue(
                    "fabricated_product_ui",
                    "claims",
                    "blocking",
                    "The prompt implies a screenshot of Crusor software that does not exist.",
                    evidence=hit,
                    suggestion="Describe the concept as an abstract illustration instead.",
                )
            )

        relevance = self._relevance(asset, brief)
        checks.append(
            self._check(
                "content_to_asset_relevance",
                "relevance",
                passed=relevance["passed"],
                performed=relevance["performed"],
                detail=relevance["detail"],
            )
        )
        if relevance["performed"] and not relevance["passed"]:
            issues.append(
                self._issue(
                    "low_content_relevance",
                    "relevance",
                    "warning",
                    "The visual brief shares little vocabulary with the content it is meant to support.",
                    suggestion="Align the brief's subject with the post's actual claim.",
                )
            )

        # ---- checks this service cannot perform -----------------------------
        checks.append(
            self._check(
                "text_readability",
                "readability",
                passed=False,
                performed=False,
                detail="no OCR in Phase 3B; on-image text legibility requires human review",
            )
        )
        checks.append(
            self._check(
                "watermark_detection",
                "integrity",
                passed=False,
                performed=False,
                detail=(
                    "mock provider adds no watermark by construction; third-party "
                    "watermarks are not detectable"
                )
                if asset.is_mock
                else "watermark detection is not implemented; requires human review",
            )
        )
        checks.append(
            self._check(
                "human_visual_review",
                "human",
                passed=False,
                performed=False,
                detail="not performed by this service — a human must look at the asset",
            )
        )

        verdict, score = self._verdict(issues)
        report = {
            "verdict": verdict.value,
            "score": score,
            "checks": checks,
            "issues": issues,
            "blocking_count": sum(1 for i in issues if i["severity"] == "blocking"),
            "warning_count": sum(1 for i in issues if i["severity"] == "warning"),
            "performed_checks": sum(1 for c in checks if c["performed"]),
            "not_performed_checks": sum(1 for c in checks if not c["performed"]),
            "pixel_stats": stats.as_dict() if stats is not None else None,
            "visual_verification": "not_performed",
            "human_visual_review_required": True,
            "note": (
                "Automated checks only. No OCR, no aesthetic judgement, no human review. "
                "A PASS here means the machine checks passed, not that the asset is good."
            ),
        }

        asset.validation_status = verdict.value
        asset.qa_report = report
        from datetime import datetime, timezone

        asset.validation_checked_at = datetime.now(timezone.utc)
        self.session.add(asset)
        await self.session.flush()
        await record_audit(
            self.session,
            action="creative.qa_evaluated",
            entity_type="creative_asset",
            entity_id=asset.id,
            actor_id=actor_id,
            after={
                "verdict": verdict.value,
                "score": score,
                "blocking": report["blocking_count"],
                "warnings": report["warning_count"],
            },
        )
        return report

    # -------------------------------------------------------------- internals
    def _check(
        self,
        rule: str,
        category: str,
        *,
        passed: bool,
        performed: bool,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "rule": rule,
            "category": category,
            "passed": bool(passed),
            "performed": bool(performed),
            "automated": True,
            "detail": detail,
        }

    def _issue(
        self,
        rule: str,
        category: str,
        severity: str,
        message: str,
        *,
        evidence: str = "",
        suggestion: str = "",
    ) -> dict[str, Any]:
        return {
            "rule": rule,
            "category": category,
            "severity": severity,
            "message": message,
            "evidence": evidence,
            "suggestion": suggestion,
        }

    def _verdict(self, issues: list[dict[str, Any]]) -> tuple[CreativeValidationStatus, int]:
        blocking = sum(1 for i in issues if i["severity"] == "blocking")
        warnings = sum(1 for i in issues if i["severity"] == "warning")
        revision = sum(1 for i in issues if i["severity"] == "needs_revision")
        if blocking:
            return CreativeValidationStatus.BLOCKED, max(0, 20 - blocking * 5)
        if revision:
            return CreativeValidationStatus.NEEDS_REVISION, max(30, 70 - revision * 10)
        if warnings:
            return CreativeValidationStatus.PASS_WITH_WARNINGS, max(50, 90 - warnings * 5)
        return CreativeValidationStatus.PASS, 95

    def _claims_in(
        self, text: str, brand: BrandProfile | None
    ) -> list[tuple[str, str]]:
        hits: list[tuple[str, str]] = []
        if not text:
            return hits
        for pattern, kind in CLAIM_PATTERNS:
            for match in re.finditer(pattern, text, re.I):
                hits.append((match.group(0).strip(), kind))
        for phrase in (getattr(brand, "prohibited_phrases", []) or []) if brand else []:
            if phrase and phrase.lower() in text.lower():
                hits.append((phrase, "prohibited phrase"))
        return hits[:12]

    def _relevance(self, asset: CreativeAsset, brief: CreativeBrief | None) -> dict[str, Any]:
        if brief is None:
            return {
                "performed": False,
                "passed": False,
                "detail": "no brief attached; relevance cannot be assessed",
            }
        source = " ".join(
            part
            for part in (
                brief.creative_objective,
                brief.visual_concept,
                brief.content_hash,
            )
            if part
        )
        if (asset.metadata_json or {}).get("content_excerpt"):
            source += " " + str(asset.metadata_json.get("content_excerpt"))
        words = {w for w in re.findall(r"[a-z]{4,}", source.lower())}
        prompt_words = {w for w in re.findall(r"[a-z]{4,}", (asset.prompt or "").lower())}
        if not words or not prompt_words:
            return {
                "performed": False,
                "passed": False,
                "detail": "not enough text to compare",
            }
        overlap = len(words & prompt_words) / max(1, len(words))
        return {
            "performed": True,
            "passed": overlap >= 0.15,
            "detail": f"{overlap:.0%} of the brief's vocabulary appears in the generation prompt",
        }

    @staticmethod
    def _ratio_label(width: int, height: int) -> str:
        from math import gcd

        divisor = gcd(width, height) or 1
        w, h = width // divisor, height // divisor
        if w > 40 or h > 40:
            # normalise odd pixel sizes to the closest known ratio
            target = width / height
            for label, (rw, rh) in {
                "1:1": (1, 1),
                "4:5": (4, 5),
                "9:16": (9, 16),
                "16:9": (16, 9),
                "1.91:1": (191, 100),
                "3:2": (3, 2),
                "2:3": (2, 3),
            }.items():
                if abs(target - rw / rh) < 0.01:
                    return label
            return f"{w}:{h}"
        return f"{w}:{h}"


def platform_spec_summary(platform: str) -> dict[str, Any]:
    """Small helper so QA findings can quote the platform spec they used."""
    try:
        spec = get_spec(platform)
    except Exception:  # noqa: BLE001 - unknown platform is not an error here
        return {}
    return {
        "platform": spec.platform,
        "body_max_chars": spec.body_max_chars,
        "hashtag_max": spec.hashtag_max,
        "supported_types": list(spec.supported_types),
    }
