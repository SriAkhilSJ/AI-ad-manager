"""Provider-independent platform content specifications.

Every platform difference that matters to *content* lives here: what content
types it supports, how long each field may be, how hashtags are used, how it is
formatted, and what the call to action should look like. Nothing in this module
talks to a network — publishing adapters are explicitly out of scope for Phase
3A.

The table is the single source of truth for both the Content Agent (which is told
what to write) and the Brand Guardian (which enforces it). Keeping one table
prevents the classic drift where the generator and the validator disagree, and it
means a spec change is testable rather than buried in a prompt string.

Overrides
---------
Operators can adjust any field without a code change by supplying
``CONTENT_PLATFORM_OVERRIDES_JSON``, for example::

    {"x": {"body_max_chars": 280}, "instagram": {"hashtag_max": 12}}

Unknown platform keys are rejected rather than ignored, so a typo cannot silently
leave the stock limit in place.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable


class ContentType(str, enum.Enum):
    """The content shapes Crusor produces in Phase 3A."""

    SOCIAL_POST = "social_post"
    CAPTION = "caption"
    HOOK = "hook"
    SHORT_FORM_VIDEO_SCRIPT = "short_form_video_script"
    EDUCATIONAL_POST = "educational_post"
    PRODUCT_ANNOUNCEMENT = "product_announcement"
    FOUNDER_STORY = "founder_story"
    THREAD = "thread"
    CAROUSEL_OUTLINE = "carousel_outline"
    COMMUNITY_DISCUSSION = "community_discussion"
    CALL_TO_ACTION = "call_to_action"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


CONTENT_TYPES: tuple[str, ...] = tuple(ContentType.values())


@dataclass(frozen=True)
class PlatformSpec:
    """Everything the system needs to know about writing for one platform."""

    platform: str
    label: str
    supported_types: tuple[str, ...]
    body_max_chars: int
    title_max_chars: int
    hook_max_chars: int
    cta_max_chars: int
    hashtag_min: int
    hashtag_max: int
    hashtag_guidance: str
    formatting_rules: tuple[str, ...]
    cta_guidance: str
    validation_rules: tuple[str, ...]
    tone: str
    #: True when the platform is discussion-led rather than broadcast. The
    #: planner uses this to keep promotional density down.
    community_first: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["supported_types"] = list(self.supported_types)
        data["formatting_rules"] = list(self.formatting_rules)
        data["validation_rules"] = list(self.validation_rules)
        return data

    def supports(self, content_type: str) -> bool:
        return content_type in self.supported_types


def _spec(**kwargs: Any) -> PlatformSpec:
    return PlatformSpec(**kwargs)


# --------------------------------------------------------------------------- #
# The stock table
# --------------------------------------------------------------------------- #
_SPECS: dict[str, PlatformSpec] = {
    "instagram": _spec(
        platform="instagram",
        label="Instagram",
        supported_types=(
            ContentType.SOCIAL_POST.value,
            ContentType.CAPTION.value,
            ContentType.HOOK.value,
            ContentType.SHORT_FORM_VIDEO_SCRIPT.value,
            ContentType.CAROUSEL_OUTLINE.value,
            ContentType.EDUCATIONAL_POST.value,
            ContentType.PRODUCT_ANNOUNCEMENT.value,
        ),
        body_max_chars=2100,
        title_max_chars=120,
        hook_max_chars=125,
        cta_max_chars=120,
        hashtag_min=3,
        hashtag_max=8,
        hashtag_guidance="3–8 specific, topical tags; no banned or generic #follow4follow style tags.",
        formatting_rules=(
            "Open with a hook in the first line; the caption is truncated after ~125 chars.",
            "Short lines and blank lines between ideas; no dense paragraphs.",
            "Visual direction belongs in the script or carousel outline, not the caption body.",
        ),
        cta_guidance="One low-friction action (try it, join the waitlist, watch the reel). Never stack CTAs.",
        validation_rules=(
            "hook_within_limit",
            "platform_length",
            "hashtag_count",
            "single_cta",
        ),
        tone="visual-first, energetic, concise",
    ),
    "linkedin": _spec(
        platform="linkedin",
        label="LinkedIn",
        supported_types=(
            ContentType.SOCIAL_POST.value,
            ContentType.FOUNDER_STORY.value,
            ContentType.EDUCATIONAL_POST.value,
            ContentType.PRODUCT_ANNOUNCEMENT.value,
            ContentType.SHORT_FORM_VIDEO_SCRIPT.value,
        ),
        body_max_chars=2600,
        title_max_chars=200,
        hook_max_chars=210,
        cta_max_chars=150,
        hashtag_min=0,
        hashtag_max=3,
        hashtag_guidance="0–3 professional tags. Skip hashtag stuffing.",
        formatting_rules=(
            "First two lines must stand alone — that is all most readers see.",
            "Paragraphs of 1–3 sentences, blank line between them.",
            "No emoji walls; at most a few, used structurally.",
        ),
        cta_guidance="Invite a concrete next step: comment, read the write-up, or try the build.",
        validation_rules=(
            "hook_within_limit",
            "platform_length",
            "hashtag_count",
            "no_corporate_filler",
        ),
        tone="professional, personal, insight-led",
    ),
    "youtube": _spec(
        platform="youtube",
        label="YouTube",
        supported_types=(
            ContentType.SHORT_FORM_VIDEO_SCRIPT.value,
            ContentType.HOOK.value,
            ContentType.EDUCATIONAL_POST.value,
            ContentType.PRODUCT_ANNOUNCEMENT.value,
        ),
        body_max_chars=5000,
        title_max_chars=100,
        hook_max_chars=160,
        cta_max_chars=150,
        hashtag_min=0,
        hashtag_max=3,
        hashtag_guidance="0–3 tags relevant to the topic; they belong in the description.",
        formatting_rules=(
            "Scene-by-scene outline with visual and voiceover columns as plain text.",
            "Keep the spoken hook under 8 seconds.",
            "Text scripts only in Phase 3A — no rendering or media production.",
        ),
        cta_guidance="Ask for one action: subscribe, try the build, or watch the next episode.",
        validation_rules=(
            "title_within_limit",
            "hook_within_limit",
            "scene_outline_present",
        ),
        tone="demo-oriented, clear, paced",
    ),
    "x": _spec(
        platform="x",
        label="X",
        supported_types=(
            ContentType.SOCIAL_POST.value,
            ContentType.THREAD.value,
            ContentType.PRODUCT_ANNOUNCEMENT.value,
            ContentType.EDUCATIONAL_POST.value,
            ContentType.FOUNDER_STORY.value,
            ContentType.HOOK.value,
        ),
        body_max_chars=280,
        title_max_chars=100,
        hook_max_chars=120,
        cta_max_chars=100,
        hashtag_min=0,
        hashtag_max=2,
        hashtag_guidance="0–2 tags. On X, tags are a search aid, not a decoration.",
        formatting_rules=(
            "One idea per post. 280 characters is a ceiling, not a target.",
            "Threads are numbered and each post stands alone.",
            "No link-only posts; always give the reader something first.",
        ),
        cta_guidance="Ask one direct question or point to a single link.",
        validation_rules=("platform_length", "thread_numbering", "hashtag_count"),
        tone="crisp, technical, direct",
    ),
    "facebook": _spec(
        platform="facebook",
        label="Facebook",
        supported_types=(
            ContentType.SOCIAL_POST.value,
            ContentType.PRODUCT_ANNOUNCEMENT.value,
            ContentType.EDUCATIONAL_POST.value,
            ContentType.SHORT_FORM_VIDEO_SCRIPT.value,
            ContentType.CALL_TO_ACTION.value,
        ),
        body_max_chars=1200,
        title_max_chars=200,
        hook_max_chars=125,
        cta_max_chars=120,
        hashtag_min=0,
        hashtag_max=2,
        hashtag_guidance="0–2 tags; Facebook reach is not tag-driven.",
        formatting_rules=(
            "Conversational, community-facing tone.",
            "Short paragraphs; the first line must earn the 'see more' click.",
            "Product demonstrations are described as text briefs, not claimed as demos.",
        ),
        cta_guidance="Point to a single next step such as the site or a discussion thread.",
        validation_rules=("hook_within_limit", "platform_length", "hashtag_count"),
        tone="conversational, plain, welcoming",
    ),
    "reddit": _spec(
        platform="reddit",
        label="Reddit",
        supported_types=(
            ContentType.COMMUNITY_DISCUSSION.value,
            ContentType.EDUCATIONAL_POST.value,
            ContentType.SOCIAL_POST.value,
            ContentType.FOUNDER_STORY.value,
        ),
        body_max_chars=4000,
        title_max_chars=300,
        hook_max_chars=200,
        cta_max_chars=150,
        hashtag_min=0,
        hashtag_max=0,
        hashtag_guidance="No hashtags. They read as advertising on Reddit.",
        formatting_rules=(
            "Lead with community value; the product is context, not the subject.",
            "State the affiliation plainly and early — no astroturfing.",
            "No marketing cadence: no urgency, no scarcity, no engagement bait.",
        ),
        cta_guidance="Invite a genuine discussion or feedback, not a signup.",
        validation_rules=(
            "no_hashtags",
            "affiliation_disclosed",
            "no_engagement_bait",
            "platform_length",
        ),
        tone="plain, candid, non-promotional",
        community_first=True,
    ),
}

#: Fields an operator may override, per platform.
_OVERRIDABLE = frozenset(
    {
        "body_max_chars",
        "title_max_chars",
        "hook_max_chars",
        "cta_max_chars",
        "hashtag_min",
        "hashtag_max",
        "supported_types",
        "cta_guidance",
        "tone",
    }
)


class PlatformSpecError(ValueError):
    """An override is malformed or names an unknown platform/field."""


def _apply_overrides(overrides: dict[str, Any] | None) -> dict[str, PlatformSpec]:
    specs = dict(_SPECS)
    if not overrides:
        return specs
    if not isinstance(overrides, dict):
        raise PlatformSpecError("platform overrides must be a mapping of platform -> fields")
    for platform_key, fields in overrides.items():
        key = str(platform_key).lower()
        if key not in specs:
            raise PlatformSpecError(
                f"unknown platform in overrides: {platform_key!r} "
                f"(known: {sorted(specs)})"
            )
        if not isinstance(fields, dict):
            raise PlatformSpecError(f"overrides for {key} must be a mapping")
        unknown = set(fields) - _OVERRIDABLE
        if unknown:
            raise PlatformSpecError(
                f"unsupported override field(s) for {key}: {sorted(unknown)} "
                f"(allowed: {sorted(_OVERRIDABLE)})"
            )
        cleaned: dict[str, Any] = {}
        for name, value in fields.items():
            if name == "supported_types":
                if not isinstance(value, (list, tuple)):
                    raise PlatformSpecError(f"{key}.supported_types must be a list")
                bad = [t for t in value if t not in CONTENT_TYPES]
                if bad:
                    raise PlatformSpecError(f"{key}.supported_types has unknown types: {bad}")
                cleaned[name] = tuple(value)
            elif name in {"cta_guidance", "tone"}:
                cleaned[name] = str(value)
            else:
                try:
                    number = int(value)
                except (TypeError, ValueError) as exc:
                    raise PlatformSpecError(f"{key}.{name} must be an integer") from exc
                if number < 0:
                    raise PlatformSpecError(f"{key}.{name} may not be negative")
                cleaned[name] = number
        specs[key] = replace(specs[key], **cleaned)
    return specs


def _configured_overrides() -> dict[str, Any] | None:
    """Read overrides from settings, tolerating a partially-built app at import."""
    try:
        from app.core.config import get_settings

        return getattr(get_settings(), "content_platform_overrides_json", None) or None
    except Exception:  # pragma: no cover - settings always exist in practice
        return None


def available_platforms() -> list[str]:
    return list(_apply_overrides(_configured_overrides()))


def get_spec(platform: str, *, overrides: dict[str, Any] | None = None) -> PlatformSpec:
    specs = _apply_overrides(
        overrides if overrides is not None else _configured_overrides()
    )
    key = (platform or "").lower()
    if key not in specs:
        raise PlatformSpecError(
            f"unsupported platform {platform!r} (known: {sorted(specs)})"
        )
    return specs[key]


def spec_dict(platforms: Iterable[str] | None = None) -> list[dict[str, Any]]:
    specs = _apply_overrides(_configured_overrides())
    keys = [p.lower() for p in platforms] if platforms else list(specs)
    return [specs[k].as_dict() for k in keys if k in specs]


# --------------------------------------------------------------------------- #
# Mechanical validation against a spec
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpecIssue:
    rule: str
    severity: str  # blocking | warning
    message: str
    field: str = ""
    evidence: str = ""
    fix: str = ""


def validate_against_spec(
    *,
    platform: str,
    content_type: str,
    title: str = "",
    hook: str = "",
    body: str = "",
    call_to_action: str = "",
    hashtags: Iterable[str] | None = None,
    outline: Iterable[str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> list[SpecIssue]:
    """Check one draft against its platform spec. Deterministic and side-effect free."""
    spec = get_spec(platform, overrides=overrides)
    issues: list[SpecIssue] = []
    hashtags = list(hashtags or [])

    if not spec.supports(content_type):
        issues.append(
            SpecIssue(
                rule="content_type_unsupported",
                severity="blocking",
                field="content_type",
                message=(
                    f"{spec.label} does not support content type '{content_type}' "
                    f"in Phase 3A."
                ),
                evidence=content_type,
                fix=f"Choose one of: {', '.join(spec.supported_types)}.",
            )
        )

    if not body.strip():
        issues.append(
            SpecIssue(
                rule="empty_copy",
                severity="blocking",
                field="body",
                message="Body copy is empty.",
                fix="Generate or write body copy.",
            )
        )

    if content_type == ContentType.THREAD.value:
        # A thread is many posts, so the platform limit applies per post rather
        # than to the concatenated body.
        posts = [line for line in body.split("\n") if line.strip()]
        over = [p for p in posts if len(p) > spec.body_max_chars]
        if over:
            issues.append(
                SpecIssue(
                    rule="thread_post_within_limit",
                    severity="blocking",
                    field="body",
                    message=(
                        f"{len(over)} thread post(s) exceed {spec.body_max_chars} characters."
                    ),
                    evidence=f"longest {max(len(p) for p in over)} > {spec.body_max_chars}",
                    fix="Split or shorten the over-long posts.",
                )
            )
    elif len(body) > spec.body_max_chars:
        issues.append(
            SpecIssue(
                rule="platform_length",
                severity="blocking",
                field="body",
                message=(
                    f"Body is {len(body)} characters; {spec.label} allows "
                    f"{spec.body_max_chars}."
                ),
                evidence=f"{len(body)} > {spec.body_max_chars}",
                fix=f"Trim at least {len(body) - spec.body_max_chars} characters.",
            )
        )

    if title and len(title) > spec.title_max_chars:
        issues.append(
            SpecIssue(
                rule="title_within_limit",
                severity="blocking",
                field="title",
                message=(
                    f"Title is {len(title)} characters; {spec.label} allows "
                    f"{spec.title_max_chars}."
                ),
                evidence=f"{len(title)} > {spec.title_max_chars}",
                fix="Shorten the title.",
            )
        )

    if hook and len(hook) > spec.hook_max_chars:
        issues.append(
            SpecIssue(
                rule="hook_within_limit",
                severity="warning",
                field="hook",
                message=(
                    f"Hook is {len(hook)} characters; {spec.label} shows about "
                    f"{spec.hook_max_chars} before truncation."
                ),
                evidence=f"{len(hook)} > {spec.hook_max_chars}",
                fix="Lead with the point inside the first line.",
            )
        )

    if call_to_action and len(call_to_action) > spec.cta_max_chars:
        issues.append(
            SpecIssue(
                rule="cta_within_limit",
                severity="warning",
                field="call_to_action",
                message=f"CTA is longer than the {spec.cta_max_chars}-character guidance.",
                evidence=f"{len(call_to_action)} > {spec.cta_max_chars}",
                fix="Make the CTA one short action.",
            )
        )

    # hashtags
    if spec.hashtag_max == 0 and hashtags:
        issues.append(
            SpecIssue(
                rule="no_hashtags",
                severity="blocking",
                field="hashtags",
                message=f"{spec.label} content must not carry hashtags.",
                evidence=", ".join(hashtags[:5]),
                fix="Remove the hashtags.",
            )
        )
    elif len(hashtags) > spec.hashtag_max:
        issues.append(
            SpecIssue(
                rule="hashtag_count",
                severity="warning",
                field="hashtags",
                message=(
                    f"{len(hashtags)} hashtags; {spec.label} guidance is at most "
                    f"{spec.hashtag_max}."
                ),
                evidence=f"{len(hashtags)} > {spec.hashtag_max}",
                fix=f"Keep at most {spec.hashtag_max}.",
            )
        )
    elif hashtags and len(hashtags) < spec.hashtag_min:
        issues.append(
            SpecIssue(
                rule="hashtag_count",
                severity="warning",
                field="hashtags",
                message=(
                    f"{len(hashtags)} hashtags; {spec.label} expects at least "
                    f"{spec.hashtag_min}."
                ),
                fix=f"Add up to {spec.hashtag_max} relevant tags.",
            )
        )

    # content-type specific structure
    if content_type == ContentType.CAROUSEL_OUTLINE.value:
        slides = list(outline or [])
        if len(slides) < 3:
            issues.append(
                SpecIssue(
                    rule="carousel_needs_slides",
                    severity="blocking",
                    field="outline",
                    message="A carousel outline needs at least three slides.",
                    evidence=f"{len(slides)} slide(s)",
                    fix="Add slide-by-slide beats (text only in Phase 3A).",
                )
            )

    if content_type == ContentType.THREAD.value:
        posts = [line for line in body.split("\n") if line.strip()]
        if len(posts) < 2:
            issues.append(
                SpecIssue(
                    rule="thread_numbering",
                    severity="blocking",
                    field="body",
                    message="A thread needs at least two posts, one per line.",
                    evidence=f"{len(posts)} line(s)",
                    fix="Split the thread into numbered posts separated by newlines.",
                )
            )
        unnumbered = [p for p in posts if not p.strip()[:3].rstrip(".").rstrip(")").isdigit()]
        if posts and len(unnumbered) == len(posts):
            issues.append(
                SpecIssue(
                    rule="thread_numbering",
                    severity="warning",
                    field="body",
                    message="Thread posts are not numbered, which hurts readability.",
                    fix="Prefix each post with '1/', '2/', …",
                )
            )

    if content_type == ContentType.SHORT_FORM_VIDEO_SCRIPT.value:
        if platform == "youtube" and not list(outline or []):
            issues.append(
                SpecIssue(
                    rule="scene_outline_present",
                    severity="blocking",
                    field="outline",
                    message="A YouTube script needs a scene-by-scene outline.",
                    fix="Add scene beats with visual and voiceover notes.",
                )
            )

    return issues
