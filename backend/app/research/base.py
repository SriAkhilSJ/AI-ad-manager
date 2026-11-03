"""Pluggable research providers.

The Research Agent must not depend on any single search vendor. Everything it
knows about the outside world arrives as a :class:`RawFinding` from a
:class:`ResearchProvider`, and every finding must carry provenance or be marked
unverified.

Three implementations ship:

* :class:`DeterministicProvider` — no network. It composes findings from the
  brand's own stored knowledge (competitor profiles, product facts, audience
  segments) and marks each one with its true provenance. It is a real provider,
  not a stub: it produces useful, correctly-attributed output with zero cost.
* :class:`HttpSearchProvider` — a generic, vendor-neutral HTTP endpoint. The
  request/response contract is documented below so any search API (or an
  internal proxy) can be adapted without touching agent code.
* :class:`DisabledProvider` — explicit opt-out, fails with a typed error.

A provider never returns a "verified" finding. Verification is a separate,
deliberate act by a human or by a provider that supplies a resolvable source;
the model layer only ever proposes.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol, Sequence

from app.core.config import Settings, get_settings
from app.core.errors import ResearchUnavailableError
from app.models.research import RESEARCH_CATEGORIES, VerificationStatus

# Default verification state for a provider finding that makes no claim.
DEFAULT_FINDING_STATUS = VerificationStatus.UNVERIFIED.value

RESEARCH_QUESTIONS: dict[str, str] = {
    "competitor_identification": "Who competes with this product, directly and indirectly?",
    "competitor_positioning": "How do those competitors position themselves?",
    "feature_comparison": "How do verified product capabilities compare?",
    "competitor_content_strategy": "What content are competitors publishing, and where?",
    "developer_pain_points": "What problems are developers actually complaining about?",
    "market_trends": "Which AI developer-tool trends are emerging?",
    "developer_communities": "Where do these audiences gather and converse?",
    "content_opportunities": "Which content gaps could this brand credibly fill?",
    "audience_questions_objections": "What do prospects ask and object to?",
}


def normalise_dedupe_key(*parts: str) -> str:
    """Stable key so the same finding is never stored twice for one project."""
    blob = "|".join(
        re.sub(r"\s+", " ", (p or "").strip().lower())[:400] for p in parts
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:40]


@dataclass
class RawSource:
    """A citable source as returned by a provider."""

    url: str
    title: str = ""
    publisher: str = ""
    excerpt: str = ""
    retrieved_at: datetime | None = None
    credibility: str = "unknown"
    content_hash: str = ""

    def normalised(self) -> "RawSource":
        retrieved = self.retrieved_at or datetime.now(timezone.utc)
        content_hash = self.content_hash or hashlib.sha256(
            f"{self.url}\x00{self.title}\x00{self.excerpt}".encode()
        ).hexdigest()
        return RawSource(
            url=(self.url or "").strip(),
            title=(self.title or "").strip()[:500],
            publisher=(self.publisher or "").strip()[:200],
            excerpt=(self.excerpt or "").strip()[:4000],
            retrieved_at=retrieved,
            credibility=self.credibility if self.credibility else "unknown",
            content_hash=content_hash,
        )


@dataclass
class RawFinding:
    """One candidate finding, before persistence."""

    category: str
    finding: str
    evidence_summary: str = ""
    confidence: float = 0.5
    source: RawSource | None = None
    competitor_name: str = ""
    tags: list[str] = field(default_factory=list)
    verification_status: str = VerificationStatus.UNVERIFIED.value
    provider: str = ""

    def __post_init__(self) -> None:
        if self.category not in RESEARCH_CATEGORIES:
            raise ValueError(
                f"Unknown research category '{self.category}'. "
                f"Expected one of: {', '.join(RESEARCH_CATEGORIES)}"
            )
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))

    @property
    def has_source(self) -> bool:
        return bool(self.source and self.source.url.strip())

    def resolved_status(self) -> str:
        """A finding can only be verified if it actually has a source.

        This mirrors the database CHECK constraint, so a provider that tries to
        assert ``verified`` without a citation is corrected here rather than
        failing at insert with a less useful error.
        """
        if self.verification_status == VerificationStatus.VERIFIED.value and not self.has_source:
            return VerificationStatus.UNVERIFIED.value
        if not self.has_source and self.verification_status == VerificationStatus.VERIFIED.value:
            return VerificationStatus.UNVERIFIED.value
        return self.verification_status

    def dedupe_key(self) -> str:
        return normalise_dedupe_key(
            self.category,
            self.competitor_name,
            self.finding,
            self.source.url if self.source else "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "finding": self.finding,
            "evidence_summary": self.evidence_summary,
            "confidence": self.confidence,
            "competitor_name": self.competitor_name,
            "tags": self.tags,
            "verification_status": self.resolved_status(),
            "provider": self.provider,
            "source": (
                {
                    "url": self.source.url,
                    "title": self.source.title,
                    "publisher": self.source.publisher,
                    "retrieved_at": (
                        self.source.retrieved_at.isoformat()
                        if self.source.retrieved_at
                        else None
                    ),
                    "credibility": self.source.credibility,
                }
                if self.source
                else None
            ),
        }


@dataclass
class ResearchQuery:
    """What the agent asks a provider for."""

    objective: str
    categories: list[str]
    brand_company: str = ""
    brand_product: str = ""
    brand_domain: str = ""
    competitors: list[str] = field(default_factory=list)
    audiences: list[str] = field(default_factory=list)
    max_sources: int = 25
    existing_keys: set[str] = field(default_factory=set)

    def prompt_for(self, category: str) -> str:
        parts = [
            f"Objective: {self.objective}",
            f"Product: {self.brand_company} — {self.brand_product}".strip(" —"),
            f"Category: {category}",
            f"Question: {RESEARCH_QUESTIONS.get(category, '')}",
        ]
        if self.competitors:
            parts.append(f"Known competitors: {', '.join(self.competitors[:12])}")
        if self.audiences:
            parts.append(f"Audiences: {', '.join(self.audiences[:8])}")
        return "\n".join(parts)


class ResearchProvider(Protocol):
    """The seam every provider satisfies."""

    name: str
    available: bool

    async def search(self, query: ResearchQuery) -> list[RawFinding]: ...

    def status(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# deterministic provider
# --------------------------------------------------------------------------- #
class DeterministicProvider:
    """Derives findings from stored brand knowledge — no network, no cost.

    Every finding is attributed to an internal record, so provenance is honest:
    the "source" is the brand database, and the verification status reflects
    that. This provider deliberately produces *fewer* findings than a live
    search would, and says so in its status, because inventing market research
    is worse than admitting there is none.
    """

    name = "deterministic"
    available = True

    def __init__(self, *, brand_profile: Any = None, product_facts: Sequence[Any] = (),
                 competitors: Sequence[Any] = (), segments: Sequence[Any] = ()) -> None:
        self.brand = brand_profile
        self.product_facts = list(product_facts)
        self.competitors = list(competitors)
        self.segments = list(segments)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": True,
            "network": False,
            "cost_usd": 0.0,
            "note": (
                "Offline provider: composes findings from stored brand knowledge and "
                "product facts. Produces no live market data and will not invent any."
            ),
        }

    def _internal_source(self, label: str) -> RawSource:
        brand_id = getattr(self.brand, "id", None) or "unseeded"
        return RawSource(
            url=f"brand://{brand_id}/{label}",
            title=f"Internal brand knowledge — {label}",
            publisher="brand_profile",
            excerpt="Stored brand record; not external market evidence.",
            credibility="internal",
        )

    async def search(self, query: ResearchQuery) -> list[RawFinding]:
        out: list[RawFinding] = []

        # 1. competitors are known because they are stored, not discovered
        for competitor in self.competitors:
            name = getattr(competitor, "name", "") or ""
            if not name:
                continue
            positioning = getattr(competitor, "positioning", "") or ""
            source_url = getattr(competitor, "website", None) or ""
            source = (
                RawSource(
                    url=source_url,
                    title=f"{name} — website",
                    publisher=name,
                    excerpt=positioning[:500],
                    credibility="primary",
                )
                if source_url
                else self._internal_source(f"competitor/{name}")
            )
            out.append(
                RawFinding(
                    category="competitor_identification",
                    finding=f"{name} is a known competitor in this category.",
                    evidence_summary=(
                        positioning or "Recorded in the brand's competitor profiles."
                    ),
                    confidence=0.7,
                    source=source,
                    competitor_name=name,
                    tags=["competitor"],
                    verification_status=(
                        VerificationStatus.VERIFIED.value
                        if source_url
                        else VerificationStatus.UNVERIFIED.value
                    ),
                    provider=self.name,
                )
            )
            if positioning:
                out.append(
                    RawFinding(
                        category="competitor_positioning",
                        finding=f"{name} positions as: {positioning[:300]}",
                        evidence_summary=f"Recorded positioning for {name}.",
                        confidence=0.65 if source_url else 0.45,
                        source=source,
                        competitor_name=name,
                        tags=["positioning"],
                        verification_status=(
                            VerificationStatus.VERIFIED.value
                            if source_url
                            else VerificationStatus.UNVERIFIED.value
                        ),
                        provider=self.name,
                    )
                )

        # 2. verified product facts are the only capability claims available
        for fact in self.product_facts:
            statement = getattr(fact, "statement", "") or ""
            if not statement:
                continue
            status = getattr(fact, "verification_status", VerificationStatus.UNVERIFIED.value)
            if status != VerificationStatus.VERIFIED.value:
                # Unverified facts are surfaced as gaps, never as evidence.
                continue
            out.append(
                RawFinding(
                    category="feature_comparison",
                    finding=statement,
                    evidence_summary=getattr(fact, "detail", "")
                    or "Confirmed product fact.",
                    confidence=0.8,
                    source=RawSource(
                        url=getattr(fact, "source_url", "") or "",
                        title="Verified product fact",
                        publisher="brand_profile",
                        credibility="primary",
                    ),
                    tags=["product_fact"],
                    verification_status=VerificationStatus.VERIFIED.value,
                    provider=self.name,
                )
            )

        # 3. audience pain points derived from stated segments
        for segment in self.segments:
            label = getattr(segment, "name", "") or ""
            if not label:
                continue
            motivations = getattr(segment, "motivations", None) or []
            out.append(
                RawFinding(
                    category="developer_pain_points",
                    finding=(
                        f"{label} are primarily motivated by "
                        f"{', '.join(motivations) if motivations else 'shipping software faster'}."
                    ),
                    evidence_summary=(
                        "Derived from the founder-maintained audience segment. This is a "
                        "stated assumption, not observed market evidence — confirm with "
                        "primary research before treating it as fact."
                    ),
                    confidence=0.4,
                    source=self._internal_source(f"audience/{label}"),
                    tags=["audience", "assumption"],
                    verification_status=VerificationStatus.NEEDS_CONFIRMATION.value,
                    provider=self.name,
                )
            )

        # 4. community + opportunity findings are explicitly hypotheses
        if query.brand_company:
            for category, statement in (
                (
                    "developer_communities",
                    "Developer communities for this audience are not yet verified. "
                    "Candidate venues must be confirmed before engagement.",
                ),
                (
                    "content_opportunities",
                    "No external content gap analysis is available offline. "
                    "Opportunity ranking requires a configured research provider.",
                ),
                (
                    "audience_questions_objections",
                    "Audience questions and objections have not been collected; "
                    "only founder-stated segments are known.",
                ),
                (
                    "market_trends",
                    "Market trends cannot be established without an external source.",
                ),
                (
                    "competitor_content_strategy",
                    "Competitor content strategy is not observable offline.",
                ),
            ):
                out.append(
                    RawFinding(
                        category=category,
                        finding=statement,
                        evidence_summary=(
                            "Recorded as an open research gap. No provider is configured to "
                            "close it, and no data has been invented to fill it."
                        ),
                        confidence=0.2,
                        source=None,
                        tags=["gap", "unverified"],
                        verification_status=VerificationStatus.NEEDS_CONFIRMATION.value,
                        provider=self.name,
                    )
                )

        # respect the requested categories only
        allowed = set(query.categories) if query.categories else set(RESEARCH_CATEGORIES)
        out = [f for f in out if f.category in allowed]

        # never return something the project already has
        return [f for f in out if f.dedupe_key() not in query.existing_keys]


# --------------------------------------------------------------------------- #
# http provider
# --------------------------------------------------------------------------- #
class HttpSearchProvider:
    """Vendor-neutral HTTP research provider.

    Contract (documented so any search API can be adapted without code changes
    in the agent):

    ``POST {endpoint}``

    .. code-block:: json

        {"query": "...", "category": "...", "max_results": 10,
         "objective": "...", "product": "..."}

    ``200`` response body::

        {"results": [
          {"finding": "...", "evidence": "...", "confidence": 0.7,
           "source": {"url": "...", "title": "...", "publisher": "...",
                      "excerpt": "...", "credibility": "primary"}}
        ]}

    Anything else is a provider failure and is reported as such. A result with no
    source URL is kept but can never be marked verified.
    """

    name = "http"
    available = True

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self.endpoint = settings.research_http_endpoint
        self.api_key = settings.research_http_api_key
        self.timeout = settings.llm_request_timeout_seconds
        self.max_sources = settings.research_max_sources

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": bool(self.endpoint),
            "network": True,
            "endpoint_configured": bool(self.endpoint),
            "note": (
                "Generic HTTP research endpoint configured."
                if self.endpoint
                else "RESEARCH_HTTP_ENDPOINT is not set."
            ),
        }

    async def search(self, query: ResearchQuery) -> list[RawFinding]:
        if not self.endpoint:
            raise ResearchUnavailableError(
                "RESEARCH_HTTP_ENDPOINT is not configured.",
                details={"provider": self.name},
            )

        import httpx

        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        out: list[RawFinding] = []
        per_category = max(2, min(10, self.max_sources // max(len(query.categories), 1)))

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for category in query.categories:
                payload = {
                    "query": query.prompt_for(category),
                    "category": category,
                    "max_results": per_category,
                    "objective": query.objective,
                    "product": f"{query.brand_company} {query.brand_product}".strip(),
                }
                try:
                    response = await client.post(self.endpoint, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise ResearchUnavailableError(
                        f"Research endpoint failed ({type(exc).__name__}).",
                        details={"provider": self.name},
                    ) from exc
                if response.status_code >= 400:
                    raise ResearchUnavailableError(
                        f"Research endpoint returned HTTP {response.status_code}.",
                        details={"provider": self.name},
                    )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise ResearchUnavailableError(
                        "Research endpoint returned non-JSON.",
                        details={"provider": self.name},
                    ) from exc
                for item in body.get("results", []) if isinstance(body, dict) else []:
                    finding = self._to_finding(item, category)
                    if finding is not None:
                        out.append(finding)
        return [f for f in out if f.dedupe_key() not in query.existing_keys]

    def _to_finding(self, item: Any, category: str) -> RawFinding | None:
        if not isinstance(item, dict):
            return None
        text = str(item.get("finding") or "").strip()
        if not text:
            return None
        source_block = item.get("source") or {}
        source = None
        if isinstance(source_block, dict) and str(source_block.get("url") or "").strip():
            source = RawSource(
                url=str(source_block.get("url")),
                title=str(source_block.get("title") or ""),
                publisher=str(source_block.get("publisher") or ""),
                excerpt=str(source_block.get("excerpt") or ""),
                credibility=str(source_block.get("credibility") or "unknown"),
            ).normalised()
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        # A provider may *claim* verified; we only accept that with a real source.
        claimed = str(item.get("verification_status") or DEFAULT_FINDING_STATUS)
        return RawFinding(
            category=category,
            finding=text[:4000],
            evidence_summary=str(item.get("evidence") or "")[:4000],
            confidence=confidence,
            source=source,
            competitor_name=str(item.get("competitor") or "")[:200],
            tags=[str(t)[:64] for t in (item.get("tags") or [])][:10],
            verification_status=(
                VerificationStatus.VERIFIED.value
                if claimed == VerificationStatus.VERIFIED.value and source
                else VerificationStatus.UNVERIFIED.value
            ),
            provider=self.name,
        )


class DisabledProvider:
    name = "none"
    available = False

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": False,
            "network": False,
            "note": "Research is disabled by configuration (RESEARCH_PROVIDER=disabled).",
        }

    async def search(self, query: ResearchQuery) -> list[RawFinding]:
        raise ResearchUnavailableError(
            "Research is disabled by configuration.",
            details={"provider": self.name},
        )


def resolve_provider_name(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    mode = settings.research_provider
    if mode == "disabled":
        return "none"
    if mode == "deterministic":
        return "deterministic"
    if mode == "http":
        return "http"
    # auto
    return "http" if settings.research_http_endpoint else "deterministic"


def build_provider(
    settings: Settings | None = None,
    *,
    brand_profile: Any = None,
    product_facts: Sequence[Any] = (),
    competitors: Sequence[Any] = (),
    segments: Sequence[Any] = (),
) -> ResearchProvider:
    settings = settings or get_settings()
    name = resolve_provider_name(settings)
    if name == "http":
        return HttpSearchProvider(settings)
    if name == "deterministic":
        return DeterministicProvider(
            brand_profile=brand_profile,
            product_facts=product_facts,
            competitors=competitors,
            segments=segments,
        )
    return DisabledProvider()


def research_status(settings: Settings | None = None) -> dict[str, Any]:
    """Capability report for ``/system/status`` — mirrors storage/llm handling."""
    settings = settings or get_settings()
    name = resolve_provider_name(settings)
    if name == "http":
        return {
            "provider": "http",
            "status": "available" if settings.research_http_endpoint else "unavailable",
            "available": bool(settings.research_http_endpoint),
            "network": True,
            "max_sources": settings.research_max_sources,
            "note": (
                "Generic HTTP research endpoint configured."
                if settings.research_http_endpoint
                else "RESEARCH_HTTP_ENDPOINT missing; no live research will run."
            ),
        }
    if name == "deterministic":
        return {
            "provider": "deterministic",
            "status": "degraded",
            "available": True,
            "network": False,
            "max_sources": settings.research_max_sources,
            "note": (
                "Offline provider: findings are derived from stored brand knowledge "
                "and open gaps are labelled rather than invented. Configure "
                "RESEARCH_HTTP_ENDPOINT for live market research."
            ),
        }
    return {
        "provider": "none",
        "status": "unavailable",
        "available": False,
        "network": False,
        "note": "RESEARCH_PROVIDER=disabled: research jobs are refused.",
    }


__all__ = [
    "DeterministicProvider",
    "DisabledProvider",
    "HttpSearchProvider",
    "RESEARCH_QUESTIONS",
    "RawFinding",
    "RawSource",
    "ResearchProvider",
    "ResearchQuery",
    "build_provider",
    "normalise_dedupe_key",
    "research_status",
    "resolve_provider_name",
]
