"""Pluggable research providers and the anti-fabrication guarantees.

No test here touches the network: the HTTP provider is exercised through an
injected fake transport, and the deterministic provider never leaves the process.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import ResearchUnavailableError
from app.models.research import RESEARCH_CATEGORIES, VerificationStatus
from app.research.base import (
    DeterministicProvider,
    DisabledProvider,
    HttpSearchProvider,
    RawFinding,
    RawSource,
    ResearchQuery,
    build_provider,
    normalise_dedupe_key,
    research_status,
    resolve_provider_name,
)


def settings(**overrides) -> Settings:
    base = {
        "environment": "test",
        "research_provider": "auto",
        "research_http_endpoint": None,
        "research_http_api_key": None,
        "research_max_sources": 20,
    }
    base.update(overrides)
    return Settings(**base)


class FakeBrand:
    id = "11111111-1111-1111-1111-111111111111"
    company = "Crusor AI"
    product = "AI-powered IDE"


class FakeCompetitor:
    def __init__(self, name: str, positioning: str = "", website: str | None = None) -> None:
        self.name = name
        self.positioning = positioning
        self.website = website


class FakeFact:
    def __init__(self, statement: str, status: str, source_url: str = "") -> None:
        self.statement = statement
        self.verification_status = status
        self.source_url = source_url
        self.detail = ""


class FakeSegment:
    def __init__(self, name: str, motivations: list[str]) -> None:
        self.name = name
        self.motivations = motivations


def query(**overrides) -> ResearchQuery:
    base = {
        "objective": "Grow developer signups",
        "categories": list(RESEARCH_CATEGORIES),
        "brand_company": "Crusor AI",
        "brand_product": "AI-powered IDE",
        "audiences": ["software developers"],
    }
    base.update(overrides)
    return ResearchQuery(**base)


# --------------------------------------------------------------------------- #
# RawFinding invariants
# --------------------------------------------------------------------------- #
def test_unknown_category_is_rejected():
    with pytest.raises(ValueError, match="Unknown research category"):
        RawFinding(category="made_up_category", finding="anything")


def test_verified_without_source_is_downgraded():
    """A provider cannot assert 'verified' with no citation."""
    finding = RawFinding(
        category="market_trends",
        finding="AI tools are growing",
        verification_status=VerificationStatus.VERIFIED.value,
    )
    assert finding.has_source is False
    assert finding.resolved_status() == VerificationStatus.UNVERIFIED.value


def test_verified_with_source_is_kept():
    finding = RawFinding(
        category="market_trends",
        finding="AI tools are growing",
        source=RawSource(url="https://example.com/report", title="Report"),
        verification_status=VerificationStatus.VERIFIED.value,
    )
    assert finding.resolved_status() == VerificationStatus.VERIFIED.value


def test_confidence_is_clamped():
    assert RawFinding(category="market_trends", finding="x", confidence=5.0).confidence == 1.0
    assert RawFinding(category="market_trends", finding="x", confidence=-2).confidence == 0.0


def test_dedupe_key_is_stable_and_whitespace_insensitive():
    a = RawFinding(category="market_trends", finding="Same   finding  text")
    b = RawFinding(category="market_trends", finding="same finding text")
    assert a.dedupe_key() == b.dedupe_key()
    c = RawFinding(category="developer_pain_points", finding="same finding text")
    assert a.dedupe_key() != c.dedupe_key()


def test_normalise_dedupe_key_is_deterministic():
    assert normalise_dedupe_key("a", "b") == normalise_dedupe_key("a", "b")
    assert normalise_dedupe_key("a", "b") != normalise_dedupe_key("a", "c")


# --------------------------------------------------------------------------- #
# deterministic provider
# --------------------------------------------------------------------------- #
async def test_deterministic_provider_uses_stored_competitors():
    provider = DeterministicProvider(
        brand_profile=FakeBrand(),
        competitors=[
            FakeCompetitor("Cursor", "AI-first code editor with a website", "https://cursor.com"),
            FakeCompetitor("NoSite", "unknown positioning", None),
        ],
    )
    findings = await provider.search(query())
    competitors = [f for f in findings if f.category == "competitor_identification"]
    assert len(competitors) == 2

    with_site = next(f for f in competitors if f.competitor_name == "Cursor")
    without_site = next(f for f in competitors if f.competitor_name == "NoSite")
    assert with_site.resolved_status() == VerificationStatus.VERIFIED.value
    # No website means no citable source: must not be verified.
    assert without_site.resolved_status() == VerificationStatus.UNVERIFIED.value


async def test_deterministic_provider_only_cites_verified_product_facts():
    provider = DeterministicProvider(
        brand_profile=FakeBrand(),
        product_facts=[
            FakeFact("Runs in the browser", VerificationStatus.VERIFIED.value, "https://crusor.in"),
            FakeFact("10x faster than competitors", VerificationStatus.NEEDS_CONFIRMATION.value),
        ],
    )
    findings = await provider.search(query())
    facts = [f for f in findings if f.category == "feature_comparison"]
    statements = " ".join(f.finding for f in facts)
    assert "Runs in the browser" in statements
    assert "10x faster" not in statements, "an unverified claim was cited as evidence"


async def test_deterministic_provider_labels_gaps_instead_of_inventing_data():
    provider = DeterministicProvider(brand_profile=FakeBrand())
    findings = await provider.search(query())
    by_category = {}
    for finding in findings:
        by_category.setdefault(finding.category, []).append(finding)

    for category in ("market_trends", "competitor_content_strategy", "developer_communities"):
        assert category in by_category
        for finding in by_category[category]:
            assert finding.has_source is False
            assert finding.resolved_status() == VerificationStatus.NEEDS_CONFIRMATION.value
            assert finding.confidence <= 0.3


async def test_deterministic_provider_respects_requested_categories():
    provider = DeterministicProvider(
        brand_profile=FakeBrand(), competitors=[FakeCompetitor("Cursor")]
    )
    findings = await provider.search(query(categories=["competitor_identification"]))
    assert findings
    assert {f.category for f in findings} == {"competitor_identification"}


async def test_deterministic_provider_skips_already_known_findings():
    provider = DeterministicProvider(
        brand_profile=FakeBrand(), competitors=[FakeCompetitor("Cursor")]
    )
    first = await provider.search(query())
    existing = {f.dedupe_key() for f in first}
    second = await provider.search(query(existing_keys=existing))
    assert second == []


def test_deterministic_provider_reports_no_network():
    status = DeterministicProvider().status()
    assert status["network"] is False
    assert status["cost_usd"] == 0.0
    assert "will not invent" in status["note"]


# --------------------------------------------------------------------------- #
# http provider (no real network)
# --------------------------------------------------------------------------- #
async def test_http_provider_requires_configuration():
    provider = HttpSearchProvider(settings())
    assert provider.status()["available"] is False
    with pytest.raises(ResearchUnavailableError):
        await provider.search(query())


async def test_http_provider_maps_response_and_refuses_unverified_claims(monkeypatch):
    provider = HttpSearchProvider(settings(research_http_endpoint="https://fake.test/search"))

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "results": [
                    {
                        "finding": "Cursor publishes changelog-driven content",
                        "evidence": "Weekly changelog posts",
                        "confidence": 0.8,
                        "verification_status": "verified",
                        "source": {
                            "url": "https://cursor.com/changelog",
                            "title": "Changelog",
                            "publisher": "Cursor",
                            "credibility": "primary",
                        },
                    },
                    {
                        # claims verified but cites nothing -> must be downgraded
                        "finding": "Developers hate slow IDEs",
                        "confidence": 0.9,
                        "verification_status": "verified",
                    },
                    {"evidence": "no finding text"},  # ignored
                    "not-a-dict",  # ignored
                ]
            }

        class _Client:
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    findings = await provider.search(query(categories=["competitor_content_strategy"]))
    assert len(findings) == 2

    cited = next(f for f in findings if "changelog-driven" in f.finding)
    assert cited.resolved_status() == VerificationStatus.VERIFIED.value
    assert cited.source.url == "https://cursor.com/changelog"

    uncited = next(f for f in findings if "hate slow IDEs" in f.finding)
    assert uncited.resolved_status() == VerificationStatus.UNVERIFIED.value


async def test_http_provider_surfaces_transport_errors(monkeypatch):
    provider = HttpSearchProvider(settings(research_http_endpoint="https://fake.test/search"))

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise __import__("httpx").ConnectError("boom")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(ResearchUnavailableError) as exc:
        await provider.search(query())
    assert "failed" in str(exc.value)


# --------------------------------------------------------------------------- #
# disabled provider + registry
# --------------------------------------------------------------------------- #
async def test_disabled_provider_refuses():
    with pytest.raises(ResearchUnavailableError):
        await DisabledProvider().search(query())


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"research_provider": "disabled"}, "none"),
        ({"research_provider": "deterministic"}, "deterministic"),
        ({"research_provider": "http"}, "http"),
        ({"research_provider": "auto", "research_http_endpoint": None}, "deterministic"),
        ({"research_provider": "auto", "research_http_endpoint": "https://x.test"}, "http"),
    ],
)
def test_provider_resolution_precedence(overrides, expected):
    assert resolve_provider_name(settings(**overrides)) == expected


def test_build_provider_returns_matching_implementation():
    assert isinstance(
        build_provider(settings(research_provider="deterministic")), DeterministicProvider
    )
    assert isinstance(
        build_provider(settings(research_provider="http", research_http_endpoint="https://x")),
        HttpSearchProvider,
    )
    assert isinstance(build_provider(settings(research_provider="disabled")), DisabledProvider)


def test_research_status_is_honest_about_degraded_mode():
    degraded = research_status(settings(research_provider="deterministic"))
    assert degraded["status"] == "degraded"
    assert degraded["network"] is False
    assert "RESEARCH_HTTP_ENDPOINT" in degraded["note"]

    unavailable = research_status(settings(research_provider="disabled"))
    assert unavailable["status"] == "unavailable"
    assert unavailable["available"] is False
