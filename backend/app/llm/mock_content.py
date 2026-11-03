"""Deterministic mock content provider for Phase 3A.

Why this exists
---------------
Phase 3A must be testable and demonstrable **without spending money and without
provider credentials**, and it must never pretend that mock output is real model
output. This provider therefore:

* implements the same ``LLMProvider`` protocol as the real vendors,
* is **deterministic** — the same request always yields the same text, so tests can
  assert on exact strings and the UI can be exercised reproducibly,
* is **free** (``cost_usd = 0.0``) and declares ``is_billable = False``, so the
  Phase 2.6 budget guard treats it as a non-paid request. A zero budget blocks
  *paid* requests; it does not block a provider that cannot charge anything,
* **labels itself**: every response carries ``provider="mock"`` and
  ``metadata={"mock": True, ...}``, which propagates into content generation
  metadata and is shown in the UI.

What it is not
--------------
It is not a language model. It composes copy from a small template bank keyed by
platform and content type, always inside the platform's character budget and
always drawing only on the facts it is given. It cannot invent a statistic,
because it has no numeric generator at all.

Swapping in a real provider is a settings change (``content_provider``), not a
code change: the planning service depends on the protocol, never on this class.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm.base import LLMRequest, LLMResponse

MOCK_PROVIDER_NAME = "mock"

#: Template bank. Each entry is (hook, body, cta) with ``{placeholders}`` filled
#: from the request context. No numbers appear in any template: a mock provider
#: must not be able to fabricate a metric.
_TEMPLATES: dict[str, dict[str, tuple[str, str, str]]] = {
    "instagram": {
        "social_post": (
            "{angle}",
            "{angle}.\n\nWe are building {product} for {audience}.\n\nThe workflow we "
            "care about: fewer context switches between thinking and shipping.",
            "Follow along as we build in the open.",
        ),
        "caption": (
            "Your editor should get out of the way.",
            "Fewer tabs, fewer context switches.\n\n{product} is being built for "
            "{audience} who would rather ship than configure.",
            "Save this if it matches how you work.",
        ),
        "hook": (
            "The part of a dev tool nobody shows you.",
            "The set-up is the product.\n\n{angle}",
            "",
        ),
        "short_form_video_script": (
            "Three things I would fix in my editor.",
            "SCENE 1 — the problem, stated plainly.\nSCENE 2 — what a better default "
            "looks like.\nSCENE 3 — the workflow we are building for.\n\nOn-screen "
            "text: {angle}.",
            "Try the early build when it opens.",
        ),
        "carousel_outline": (
            "A short carousel about how we build.",
            "Slide 1: the problem.\nSlide 2: why existing tools do not solve it.\n"
            "Slide 3: the approach we chose.\nSlide 4: what we are not claiming yet.\n"
            "Slide 5: how to follow along.",
            "Save the thread for later.",
        ),
        "educational_post": (
            "A small thing that makes reviewing patches easier.",
            "Read the diff before the description.\n\nIt sounds obvious and it is still "
            "the fastest way to notice a change you did not intend.\n\n{angle}",
            "Try it on your next review.",
        ),
        "product_announcement": (
            "We are building in public.",
            "{product} is an environment for {audience}.\n\nThis week we are sharing "
            "what the workflow looks like, not what the numbers are.",
            "Link in bio for the build log.",
        ),
    },
    "linkedin": {
        "social_post": (
            "Most developer tools optimise the wrong thing.",
            "Most developer tools optimise for the first run.\n\nThe run that actually "
            "matters is the tenth, when you are mid-change and out of patience.\n\n"
            "{angle}\n\nThat is the workflow we are building {product} around.",
            "Curious how other teams handle this — reply if you have a strong opinion.",
        ),
        "founder_story": (
            "A decision we rewrote twice.",
            "When we started {product}, we assumed {audience} wanted more automation.\n\n"
            "The more useful question turned out to be simpler: what should never "
            "require a decision at all?\n\n{angle}\n\nWe are still finding out.",
            "If you are building something adjacent, I would like to compare notes.",
        ),
        "educational_post": (
            "A review habit that costs nothing.",
            "Read the diff before the description.\n\nDescriptions describe intent. Diffs "
            "describe behaviour, and the gap between the two sentence is where most "
            "avoidable mistakes live.\n\n{angle}",
            "What is the one review habit you refuse to give up?",
        ),
        "product_announcement": (
            "We are building in public, deliberately.",
            "We are building {product} for {audience}.\n\nWe are not announcing "
            "availability, pricing or numbers today — those need to be right before "
            "they are said out loud.\n\nWhat we can share is the workflow we are "
            "designing and the trade-offs behind it.",
            "Follow the build log if the trade-offs are interesting to you.",
        ),
        "short_form_video_script": (
            "Sixty seconds on why we started.",
            "0:00 — the problem: {angle}.\n0:15 — what existing tools optimise for.\n"
            "0:35 — the approach we chose.\n0:50 — what we are deliberately not "
            "claiming yet.",
            "The written version is linked below.",
        ),
    },
    "youtube": {
        "short_form_video_script": (
            "The editor problem nobody names.",
            "0:00 — HOOK (voiceover): the moment a small change turns into an afternoon.\n"
            "0:08 — SCENE 1: screen recording of the current workflow; no claims, just "
            "the steps.\n0:20 — SCENE 2: the same workflow in {product}.\n"
            "0:32 — SCENE 3: what is still missing.\n0:40 — CLOSE: what we are building "
            "next.\n\nOn-screen text: {angle}",
            "Subscribe for the build log.",
        ),
        "hook": (
            "If your editor needs a diagram, the defaults are wrong.",
            "HOOK (0–8s voiceover): {angle}",
            "",
        ),
        "educational_post": (
            "A short walkthrough, no editing tricks.",
            "Outline:\n1. The naive approach and where it breaks.\n2. The trade-off we "
            "picked instead.\n3. What it costs you in practice.\n\n{angle}",
            "Description link to the written notes.",
        ),
        "product_announcement": (
            "Building something new in the open.",
            "Video outline:\n1. What we are building ({product}).\n2. Who it is for "
            "({audience}).\n3. What we will not claim yet.\n\n{angle}",
            "Subscribe to follow the build.",
        ),
    },
    "x": {
        "social_post": (
            "The set-up is the product.",
            "The set-up is the product. {angle}",
            "Reply with your worst set-up step.",
        ),
        "thread": (
            "A thread on building a developer tool in public.",
            "1/ We are building {product} for {audience}.\n"
            "2/ The workflow we care about is the tenth run, not the first.\n"
            "3/ So we optimise for fewer decisions mid-change.\n"
            "4/ {angle}\n"
            "5/ What we are not claiming yet: numbers, pricing, availability.\n"
            "6/ The build log is the honest version. Follow along.",
            "Follow for the build log.",
        ),
        "product_announcement": (
            "Small announcement, no theatre.",
            "We are building {product}. No numbers, no pricing, no launch date today. "
            "Just the workflow and the trade-offs. {angle}",
            "Build log linked below.",
        ),
        "educational_post": (
            "Read the diff before the description.",
            "Read the diff before the description. Descriptions describe intent; diffs "
            "describe behaviour. The gap is where the bugs live. {angle}",
            "What is your review habit?",
        ),
        "founder_story": (
            "We rewrote this decision twice.",
            "Assumption one: more automation. Assumption two: fewer decisions. The "
            "second one shipped. {angle}",
            "Building in public — follow along.",
        ),
        "hook": (
            "The set-up is the product.",
            "{angle}",
            "",
        ),
    },
    "facebook": {
        "social_post": (
            "A short update on what we are building.",
            "{product} is being built for {audience}.\n\nWe are sharing the workflow as "
            "we go, including the parts that are not finished.\n\n{angle}",
            "Follow the page if you want the build updates.",
        ),
        "product_announcement": (
            "We are building in the open.",
            "We are building {product} for {audience}.\n\nWe are not announcing "
            "pricing, availability or numbers today. What we can share is the workflow "
            "we are designing and why.\n\n{angle}",
            "Full build log on the site.",
        ),
        "educational_post": (
            "A small habit for cleaner reviews.",
            "Read the diff before the description.\n\nIt is unglamorous and it catches "
            "the mistakes that intent-based reading misses.\n\n{angle}",
            "Tell us your version of this.",
        ),
        "short_form_video_script": (
            "What a better default looks like.",
            "SCENE 1: the workflow today.\nSCENE 2: one assumption worth questioning.\n"
            "SCENE 3: what we changed.\n\nOn-screen text: {angle}",
            "Follow for the next update.",
        ),
        "call_to_action": (
            "One ask.",
            "If you are building something adjacent, we would like to hear what your "
            "set-up actually costs you. {angle}",
            "Reply in the comments.",
        ),
    },
    "reddit": {
        "community_discussion": (
            "How do you handle the context switch between planning and editing?",
            "Disclosure first: I work on a developer tool ({product}), so treat this as "
            "a biased question rather than a neutral one.\n\nThe problem I keep running "
            "into is {angle}.\n\nWhat I am curious about: when you hit that, do you change "
            "tools, change workflow, or just absorb the cost?\n\nNot asking anyone to "
            "try anything — I want to understand how other people route around it.",
            "Happy to share what we tried, if it is useful.",
        ),
        "educational_post": (
            "What reading the diff first actually catches.",
            "Disclosure: I build developer tooling.\n\nA habit that survived our own "
            "review process: read the diff before the description. Descriptions encode "
            "intent; diffs encode behaviour.\n\n{angle}\n\nNot a novel idea, but the "
            "asymmetry between what the author meant and what the code does is where "
            "most of our avoidable mistakes were.",
            "Curious whether this holds for other people's teams.",
        ),
        "social_post": (
            "Building a tool in public — what questions should we be asking?",
            "Disclosure: I work on {product}.\n\nWe are documenting the workflow publicly "
            "and would rather be corrected early than flattered. {angle}\n\nWhat would "
            "you want to see demonstrated before you formed an opinion?",
            "Genuine questions welcome.",
        ),
        "founder_story": (
            "Two assumptions we got wrong while building.",
            "Disclosure: I work on {product}.\n\nAssumption one was that {audience} "
            "wanted more automation. Assumption two was that fewer features meant less "
            "work.\n\n{angle}\n\nWriting it down because the second one is still costing "
            "us time, and I would rather say that than pretend otherwise.",
            "Interested in how others have handled the same trade-off.",
        ),
    },
}


class MockContentProvider:
    """Deterministic, free, self-identifying content provider."""

    #: Not a vendor: this name never appears in provider credential routing.
    name = MOCK_PROVIDER_NAME
    #: Paid spend is impossible here, so the budget guard's cost gate is skipped.
    is_billable = False
    #: Always "configured": it needs no credential and no network.
    calls: list[LLMRequest]

    def __init__(self) -> None:
        self.calls = []

    def is_configured(self) -> bool:
        return True

    async def structured(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        context = _context_from(request)
        platform = str(context.get("platform") or "linkedin").lower()
        content_type = str(context.get("content_type") or "social_post").lower()
        payload = _render(platform, content_type, context)
        data = {
            **payload,
            "platform": platform,
            "content_type": content_type,
            "mock": True,
            "mock_provider": MOCK_PROVIDER_NAME,
            "billable": False,
            "deterministic": True,
        }
        return LLMResponse(
            provider=MOCK_PROVIDER_NAME,
            model="mock-content-v1",
            data=data,
            prompt_tokens=_estimate_tokens(
                f"{request.system_prompt}\n{request.user_prompt}"
            ),
            completion_tokens=_estimate_tokens(json.dumps(data)),
            cost_usd=0.0,
            latency_ms=0,
            raw_text=(
                "Generated by the Phase 3A mock content provider. This is template "
                "composition, not model inference."
            ),
        )

    def attribution(self) -> dict[str, Any]:
        return {
            "provider": MOCK_PROVIDER_NAME,
            "model": "mock-content-v1",
            "mock": True,
            "billable": False,
        }


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _context_from(request: LLMRequest) -> dict[str, Any]:
    """Recover context from the request.

    Real providers only see prompts, so the mock does the same: the Content Agent
    writes a JSON context block into the user prompt and this parses it. That keeps
    the mock honest about the interface it implements.
    """
    match = re.search(r"\{.*\}", request.user_prompt or "", re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


#: Alternate angles of *entry* for extra variants. These change what the copy leads
#: with (problem / trade-off / change), not just its wording, because a variant that
#: only swaps synonyms is volume without value.
_VARIANT_FRAMES = (
    "The problem",
    "The trade-off we made",
    "What we changed",
)


def _render(platform: str, content_type: str, context: dict[str, Any]) -> dict[str, Any]:
    bank = _TEMPLATES.get(platform) or _TEMPLATES["linkedin"]
    template = bank.get(content_type) or bank.get("social_post")
    if template is None:  # pragma: no cover - every bank has a social_post
        template = ("", "", "")
    hook_t, body_t, cta_t = template

    angle = str(context.get("angle") or context.get("primary_message") or "the set-up is the product")
    product = str(context.get("product") or "our developer tool")
    audience = context.get("audience") or "developers"
    if isinstance(audience, (list, tuple)):
        audience = ", ".join(str(a) for a in audience[:3])
    audience = str(audience or "developers")

    values = {"angle": angle, "product": product, "audience": audience}
    hashtags = [str(t) for t in (context.get("hashtags") or [])]
    if context.get("hashtag_limit") is not None:
        hashtags = hashtags[: int(context["hashtag_limit"])]

    body = body_t.format(**values)
    hook_line = hook_t.format(**values)
    body_limit = int(context.get("body_max_chars") or 0)
    # Read the variant selector before any branch uses it: the avoid-hook path
    # below needs it, and reading it later raised UnboundLocalError whenever an
    # earlier hook had to be avoided.
    variant_index = int(context.get("variant_index") or 0)
    avoid = {str(h).strip().lower() for h in (context.get("avoid_hooks") or [])}
    if hook_line.strip().lower() in avoid:
        # Shift to the next frame rather than repeating an opening already used.
        frame = _VARIANT_FRAMES[(variant_index + 1) % len(_VARIANT_FRAMES)]
        hook_line = f"{frame}: {angle}."

    # Variants open from a different angle and add a substantive frame sentence.
    if variant_index > 0:
        frame = _VARIANT_FRAMES[(variant_index - 1) % len(_VARIANT_FRAMES)]
        hook_line = f"{frame}: {angle}."
        body = f"{frame} — {angle}.\n\n{body}"
    if content_type == "thread":
        # The platform limit is per post. Clamp each post, keep the whole thread.
        posts = [p for p in body.split("\n") if p.strip()]
        body = "\n".join(
            (p if len(p) <= body_limit else p[: body_limit - 1].rstrip() + "…")
            if body_limit
            else p
            for p in posts
        )
    elif body_limit and len(body) > body_limit:
        body = body[: body_limit - 1].rstrip() + "…"

    return {
        "headline": hook_line[: int(context.get("title_max_chars") or 300)],
        "hook": hook_line[: int(context.get("hook_max_chars") or 300)],
        "body": body,
        "call_to_action": cta_t.format(**values),
        "hashtags": hashtags,
        "summary": (
            f"Mock {platform} {content_type} composed from the Phase 3A template bank "
            f"({len(body)} chars). Deterministic and non-billable."
        ),
    }


def build_mock_provider() -> MockContentProvider:
    return MockContentProvider()
