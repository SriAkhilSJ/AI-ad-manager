"""Phase 4.2 — the paid-media transports and their dry-run surface.

Everything here is served by ``httpx.MockTransport``. These tests prove the
*implementation*: request shape, bearer auth, no token in a URL, no retry of a
non-idempotent create, a read-back before anything is called live, and the
refusal to ever claim a verification that did not happen.

They are **not** evidence that Meta, Google or LinkedIn accepted a request. No
live call has been made from this repository, and every transport still reports
``verification="unverified"`` — see the last test in this module.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import Settings
from app.integrations.ads import (
    AdsAccountInvalid,
    AdsNotConfigured,
    AdsRequestRejected,
    GoogleAdsTransport,
    LinkedInAdsTransport,
    MetaAdsTransport,
    ads_transport_report,
    get_ads_transport,
    preview_fingerprint,
)
from app.models.ads import AdBudgetAuthorization, AdCampaignStatus, AdOperation
from app.services.ads import AdsService

META_TOKEN = "meta-secret-token-value"
GOOGLE_TOKEN = "google-secret-token-value"
LINKEDIN_TOKEN = "linkedin-secret-token-value"
TOKENS = (META_TOKEN, GOOGLE_TOKEN, LINKEDIN_TOKEN)

CREDS = dict(
    meta_ads_access_token=META_TOKEN,
    meta_ads_account_id="1234567890",
    google_ads_access_token=GOOGLE_TOKEN,
    google_ads_developer_token="dev-token-abcdef",
    google_ads_login_customer_id="123-456-7890",
    linkedin_ads_access_token=LINKEDIN_TOKEN,
    linkedin_ads_account_urn="urn:li:sponsoredAccount:987654",
)


def settings(**overrides) -> Settings:
    base = dict(environment="test", **CREDS)
    base.update(overrides)
    return Settings(**base)


def bare_settings() -> Settings:
    return Settings(environment="test")


def campaign(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        name="Launch push",
        platform="meta_ads",
        objective="traffic",
        plan={"phases": ["tease", "launch"]},
        targeting={"countries": ["IN"], "age": "18-34"},
        creatives=[{"kind": "image", "ref": "asset-1"}],
        daily_budget_cents=5_000,
        total_budget_cents=100_000,
        currency="USD",
        external_campaign_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def meta_campaign(**overrides) -> SimpleNamespace:
    return campaign(platform="meta_ads", **overrides)


def google_campaign(**overrides) -> SimpleNamespace:
    return campaign(platform="google_ads", **overrides)


def linkedin_campaign(**overrides) -> SimpleNamespace:
    return campaign(platform="linkedin_ads", **overrides)


class Recorder:
    """An httpx handler that records every request and replays canned replies."""

    def __init__(self, handler=None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler or (lambda request: httpx.Response(200, json={}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def bodies(self) -> list[dict]:
        out = []
        for request in self.requests:
            raw = request.content
            if not raw:
                out.append({})
                continue
            try:
                out.append(json.loads(raw))
                continue
            except ValueError:
                pass
            # Meta's Graph API takes a form body rather than JSON.
            form = parse_qs(raw.decode("utf-8", "replace"))
            if form:
                out.append({k: v[0] for k, v in form.items()})
            else:
                out.append({"raw": raw.decode("utf-8", "replace")})
        return out

    def assert_no_token_in_url(self) -> None:
        for request in self.requests:
            url = str(request.url)
            for token in TOKENS:
                assert token not in url, f"token leaked into a URL: {url}"

    def assert_bearer(self, token: str) -> None:
        for request in self.requests:
            assert request.headers.get("authorization") == f"Bearer {token}"


def client(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


# ------------------------------------------------------------------- reporting
def test_transport_report_is_honest_and_complete():
    rows = {r["platform"]: r for r in ads_transport_report(bare_settings())}
    assert set(rows) == {"meta_ads", "google_ads", "linkedin_ads"}
    for row in rows.values():
        assert row["implemented"] is True
        assert row["credentials_configured"] is False
        assert row["available"] is False
        assert row["verification"] == "unverified"
        assert row["api_version"]
        assert row["reason"] and row["docs_url"]


def test_configured_transports_are_available_but_still_unverified():
    for row in ads_transport_report(settings()):
        assert row["available"] is True
        assert row["verification"] == "unverified"
        assert "unverified" in row["reason"]


def test_each_platform_reads_only_its_own_credentials():
    only_meta = settings(
        google_ads_access_token=None,
        google_ads_developer_token=None,
        google_ads_login_customer_id=None,
        linkedin_ads_access_token=None,
        linkedin_ads_account_urn=None,
    )
    rows = {r["platform"]: r for r in ads_transport_report(only_meta)}
    assert rows["meta_ads"]["available"] is True
    assert rows["google_ads"]["available"] is False
    assert rows["linkedin_ads"]["available"] is False


# --------------------------------------------------------------------- previews
@pytest.mark.parametrize("platform", ["meta_ads", "google_ads", "linkedin_ads"])
async def test_preview_sends_nothing_and_hides_credentials(platform):
    recorder = Recorder(lambda request: pytest.fail("a preview must not call the network"))
    transport = get_ads_transport(platform, settings(), client=client(recorder))
    row = campaign(platform=platform)

    preview = transport.preview(campaign=row, kind="create", params={})
    assert transport.preview(campaign=row, kind="create", params={}) == preview
    assert preview["platform"] == platform
    assert preview["created_paused"] is True
    assert preview["currency"] == "USD"
    assert preview["estimated_daily_spend"] == "50.00"
    assert preview["requests"], "a preview must show what would be sent"

    serialized = json.dumps(preview)
    for token in TOKENS:
        assert token not in serialized
    assert preview_fingerprint(preview) == preview_fingerprint(preview)


async def test_preview_rejects_a_campaign_without_a_budget():
    transport = get_ads_transport("meta_ads", settings())
    with pytest.raises(AdsRequestRejected):
        transport.preview(
            campaign=campaign(platform="meta_ads", daily_budget_cents=0, total_budget_cents=0),
            kind="create",
            params={},
        )


# ------------------------------------------------------------------------- Meta
def meta_handler(verify_status: str = "PAUSED", *, fail_create: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/campaigns"):
            if fail_create:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return httpx.Response(200, json={"id": "cmp_1"})
        if request.method == "POST" and path.endswith("/adsets"):
            return httpx.Response(200, json={"id": "adset_1"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "cmp_1",
                    "status": verify_status,
                    "effective_status": verify_status,
                    "daily_budget": "5000",
                },
            )
        return httpx.Response(200, json={"id": "ok"})

    return handler


async def test_meta_create_builds_paused_and_verifies_by_read_back():
    recorder = Recorder(meta_handler())
    transport = MetaAdsTransport(settings(), get_ads_transport("meta_ads", settings()).credentials, client(recorder))
    assert isinstance(transport, MetaAdsTransport)

    result = await transport.execute(campaign=meta_campaign(), kind="create", params={})

    assert result.verified is True
    assert result.external_campaign_id == "cmp_1"
    assert result.status == "PAUSED"
    assert result.details["created_paused"] is True

    # campaign -> adset -> read back; no creative/ad without a page/link.
    assert recorder.paths == [
        "POST /v21.0/act_1234567890/campaigns",
        "POST /v21.0/act_1234567890/adsets",
        "GET /v21.0/cmp_1",
    ]
    assert recorder.bodies()[0]["status"] == "PAUSED"
    recorder.assert_bearer(META_TOKEN)
    recorder.assert_no_token_in_url()
    for body in recorder.bodies():
        assert "access_token" not in body


async def test_meta_create_error_is_not_retried():
    recorder = Recorder(meta_handler(fail_create=True))
    transport = MetaAdsTransport(settings(), get_ads_transport("meta_ads", settings()).credentials, client(recorder))

    with pytest.raises(AdsRequestRejected) as excinfo:
        await transport.execute(campaign=meta_campaign(), kind="create", params={})

    assert len(recorder.requests) == 1, "a create must never be retried blindly"
    for token in TOKENS:
        assert token not in str(excinfo.value)


async def test_meta_unconfirmed_read_back_is_not_a_success():
    recorder = Recorder(meta_handler(verify_status="ACTIVE"))
    transport = MetaAdsTransport(settings(), get_ads_transport("meta_ads", settings()).credentials, client(recorder))

    result = await transport.execute(campaign=meta_campaign(), kind="create", params={})
    assert result.verified is False
    assert "read back" in result.reason


async def test_meta_launch_activates_only_after_the_paused_build_verified():
    """The platform's own state drives the sequence: PAUSED until we enable it."""
    state = {"status": "PAUSED"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/campaigns"):
            return httpx.Response(200, json={"id": "cmp_1"})
        if request.method == "POST" and path.endswith("/adsets"):
            return httpx.Response(200, json={"id": "adset_1"})
        if request.method == "POST" and path == "/v21.0/cmp_1":
            state["status"] = "ACTIVE"
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"id": "cmp_1", "status": state["status"]})

    recorder = Recorder(handler)
    transport = MetaAdsTransport(settings(), get_ads_transport("meta_ads", settings()).credentials, client(recorder))

    result = await transport.execute(campaign=meta_campaign(), kind="launch", params={})
    paths = recorder.paths
    assert paths == [
        "POST /v21.0/act_1234567890/campaigns",
        "POST /v21.0/act_1234567890/adsets",
        "GET /v21.0/cmp_1",
        "POST /v21.0/cmp_1",
        "GET /v21.0/cmp_1",
    ]
    assert result.verified is True and result.status == "ACTIVE"


async def test_meta_pause_without_an_external_id_is_refused():
    transport = get_ads_transport("meta_ads", settings())
    with pytest.raises(AdsRequestRejected):
        await transport.execute(campaign=meta_campaign(), kind="pause", params={})


# ----------------------------------------------------------------------- Google
def google_handler(verify_status: str = "PAUSED", *, fail_budget: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path.endswith("/campaignBudgets:mutate"):
            if fail_budget:
                return httpx.Response(400, json={"error": {"message": "bad budget"}})
            return httpx.Response(
                200, json={"results": [{"resourceName": "customers/1234567890/campaignBudgets/9"}]}
            )
        if path.endswith("/campaigns:mutate"):
            if "operations" in body and "create" in body["operations"][0]:
                return httpx.Response(
                    200, json={"results": [{"resourceName": "customers/1234567890/campaigns/77"}]}
                )
            return httpx.Response(200, json={"results": [{}]})
        if path.endswith(":search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "campaign": {
                                "id": "77",
                                "name": "Launch push",
                                "status": verify_status,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    return handler


async def test_google_create_mutates_with_request_ids_and_verifies():
    recorder = Recorder(google_handler())
    creds = get_ads_transport("google_ads", settings()).credentials
    transport = GoogleAdsTransport(settings(), creds, client(recorder))

    result = await transport.execute(campaign=google_campaign(), kind="create", params={})

    assert result.verified is True
    assert result.external_campaign_id == "customers/1234567890/campaigns/77"
    assert result.details["budget_resource_name"].endswith("/9")

    mutate_ids = [
        r.headers.get("request-id")
        for r in recorder.requests
        if r.url.path.endswith(":mutate")
    ]
    assert all(mutate_ids), "every mutate must carry an idempotency request-id"
    assert len(set(mutate_ids)) == len(mutate_ids), "ids must be distinct per operation"
    for request in recorder.requests:
        assert request.headers.get("developer-token") == "dev-token-abcdef"
        assert request.headers.get("login-customer-id") == "1234567890"
        assert request.headers.get("authorization") == f"Bearer {GOOGLE_TOKEN}"
        assert GOOGLE_TOKEN not in str(request.url)
    assert recorder.bodies()[0]["operations"][0]["create"]["amountMicros"] == "50000000"


async def test_google_create_failure_is_not_retried():
    recorder = Recorder(google_handler(fail_budget=True))
    creds = get_ads_transport("google_ads", settings()).credentials
    transport = GoogleAdsTransport(settings(), creds, client(recorder))

    with pytest.raises(AdsRequestRejected):
        await transport.execute(campaign=google_campaign(), kind="create", params={})
    assert len(recorder.requests) == 1


async def test_google_mismatched_read_back_is_unverified():
    recorder = Recorder(google_handler(verify_status="ENABLED"))
    creds = get_ads_transport("google_ads", settings()).credentials
    transport = GoogleAdsTransport(settings(), creds, client(recorder))
    result = await transport.execute(campaign=google_campaign(), kind="create", params={})
    assert result.verified is False


# --------------------------------------------------------------------- LinkedIn
def linkedin_handler(verify_status: str = "DRAFT", *, header_only: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/adCampaignGroups"):
            return httpx.Response(
                201, headers={"x-restli-id": "88001"}, content=b"" if header_only else b"{}"
            )
        if request.method == "POST" and path.endswith("/adCampaigns"):
            return httpx.Response(201, headers={"x-restli-id": "99002"}, content=b"")
        if request.method == "GET" and "/adCampaigns/" in path:
            return httpx.Response(200, json={"id": "99002", "status": verify_status})
        return httpx.Response(200, json={"elements": []})

    return handler


async def test_linkedin_reads_the_created_id_from_the_restli_header():
    recorder = Recorder(linkedin_handler())
    creds = get_ads_transport("linkedin_ads", settings()).credentials
    transport = LinkedInAdsTransport(settings(), creds, client(recorder))

    result = await transport.execute(campaign=linkedin_campaign(), kind="create", params={})

    assert result.verified is True
    assert result.external_campaign_id == "urn:li:sponsoredCampaign:99002"
    assert result.details["campaign_group_id"] == "88001"
    assert result.details["created_paused"] is True
    first = recorder.bodies()[0]
    assert first["status"] == "DRAFT"
    recorder.assert_bearer(LINKEDIN_TOKEN)
    for request in recorder.requests:
        assert request.headers.get("linkedin-version") == "202405"
        assert LINKEDIN_TOKEN not in str(request.url)


async def test_linkedin_pause_uses_a_partial_update():
    recorder = Recorder(linkedin_handler(verify_status="PAUSED"))
    creds = get_ads_transport("linkedin_ads", settings()).credentials
    transport = LinkedInAdsTransport(settings(), creds, client(recorder))
    row = linkedin_campaign(external_campaign_id="urn:li:sponsoredCampaign:99002")

    result = await transport.execute(campaign=row, kind="pause", params={})
    assert result.verified is True
    update = recorder.requests[0]
    assert update.headers.get("x-restli-method") == "PARTIAL_UPDATE"
    assert json.loads(update.content)["patch"]["$set"]["status"] == "PAUSED"


# ------------------------------------------------------- account + credential checks
async def test_meta_account_validation_reports_status():
    ok = Recorder(lambda r: httpx.Response(200, json={"id": "act_1", "account_status": 1, "currency": "USD"}))
    transport = MetaAdsTransport(settings(), get_ads_transport("meta_ads", settings()).credentials, client(ok))
    assert (await transport.validate_account())["usable"] is True

    disabled = Recorder(lambda r: httpx.Response(200, json={"id": "act_1", "account_status": 2}))
    transport = MetaAdsTransport(settings(), get_ads_transport("meta_ads", settings()).credentials, client(disabled))
    report = await transport.validate_account()
    assert report["account_status"] == 2
    assert report["usable"] is False


async def test_validation_without_an_account_is_an_error():
    recorder = Recorder(lambda r: httpx.Response(200, json={"data": []}))
    transport = MetaAdsTransport(settings(), get_ads_transport("meta_ads", settings()).credentials, client(recorder))
    with pytest.raises(AdsAccountInvalid):
        await transport.validate_account()


@pytest.mark.parametrize("platform", ["meta_ads", "google_ads", "linkedin_ads"])
async def test_unconfigured_transport_refuses_instead_of_guessing(platform):
    transport = get_ads_transport(platform, bare_settings())
    with pytest.raises(AdsNotConfigured):
        await transport.validate_account()
    with pytest.raises(AdsNotConfigured):
        await transport.execute(campaign=campaign(platform=platform), kind="create", params={})
    with pytest.raises(AdsNotConfigured):
        await transport.report(external_campaign_id="x")


# ------------------------------------------------------------- service + ledger
def proposal_kwargs(brand_id, platform: str = "meta_ads") -> dict:
    return dict(
        brand_profile_id=brand_id,
        platform=platform,
        name="Launch push",
        objective="traffic",
        plan={"phases": ["tease", "launch"]},
        targeting={"countries": ["IN"], "age": "18-34"},
        creatives=[{"kind": "image", "ref": "asset-1"}],
        daily_budget_cents=5_000,
        total_budget_cents=100_000,
        currency="USD",
    )


async def test_service_marks_a_campaign_live_only_after_a_verified_read_back(
    session, brand_row, users
):
    recorder = Recorder(meta_handler())
    service = AdsService(session, settings(), http_client=client(recorder))
    row = await service.propose(**proposal_kwargs(brand_row.id))
    authorized_hash = row.proposal_hash
    authorization = await service.authorize(
        row.id,
        actor_id=users["founder"].id,
        actor_role="founder",
        max_spend_cents=100_000,
        allowed_operations=["create"],
    )

    op = await service.execute(
        row.id,
        kind="create",
        actor_id=users["founder"].id,
        actor_role="founder",
        authorization_id=authorization.id,
    )

    assert op.outcome == "succeeded"
    assert row.external_campaign_id == "cmp_1"
    # The platform created it paused, so the local row says paused too.
    assert row.status == AdCampaignStatus.PAUSED.value
    assert row.last_synced_at is not None
    # The proposal the founder approved is byte-for-byte unchanged.
    assert row.proposal_hash == authorized_hash
    fresh = await session.get(AdBudgetAuthorization, authorization.id)
    assert fresh.consumed_by_operation_id == op.id
    assert op.evidence["verified"] is True


async def test_service_refuses_to_go_live_when_the_platform_did_not_confirm(
    session, brand_row, users
):
    recorder = Recorder(meta_handler(verify_status="ACTIVE"))
    service = AdsService(session, settings(), http_client=client(recorder))
    row = await service.propose(**proposal_kwargs(brand_row.id))
    authorization = await service.authorize(
        row.id,
        actor_id=users["founder"].id,
        actor_role="founder",
        max_spend_cents=100_000,
        allowed_operations=["create"],
    )

    op = await service.execute(
        row.id,
        kind="create",
        actor_id=users["founder"].id,
        actor_role="founder",
        authorization_id=authorization.id,
    )

    assert op.outcome == "unverified"
    assert row.status == AdCampaignStatus.AUTHORIZED.value
    assert row.external_campaign_id is None
    fresh = await session.get(AdBudgetAuthorization, authorization.id)
    assert fresh.consumed_by_operation_id is None, "an unproven run must not burn the approval"


async def test_service_preview_is_a_dry_run_and_matches_the_approved_hash(session, brand_row):
    recorder = Recorder(lambda r: pytest.fail("preview must not call the network"))
    service = AdsService(session, settings(), http_client=client(recorder))
    row = await service.propose(**proposal_kwargs(brand_row.id))

    preview = await service.preview(row.id, kind="create")

    assert preview["spends_money"] is False
    assert preview["transport_available"] is True
    assert preview["hash_matches"] is True
    assert preview["preview_fingerprint"]
    assert recorder.requests == []


async def test_service_report_records_what_the_platform_measured(session, brand_row):
    payload = {"data": [{"impressions": "1200", "clicks": "37", "spend": "18.42"}]}
    recorder = Recorder(lambda r: httpx.Response(200, json=payload))
    service = AdsService(session, settings(), http_client=client(recorder))
    row = await service.propose(**proposal_kwargs(brand_row.id))
    row.external_campaign_id = "cmp_1"
    session.add(row)
    await session.flush()

    report = await service.report(row.id)
    assert report["available"] is True
    assert report["measured"]["clicks"] == "37"
    ops = (
        await session.execute(
            AdOperation.__table__.select().where(AdOperation.ad_campaign_id == row.id)
        )
    ).all()
    assert len(ops) == 1
    evidence = ops[0].evidence if isinstance(ops[0].evidence, dict) else json.loads(ops[0].evidence)
    assert evidence["source"] == "meta_marketing_api_insights"
    assert evidence["measured"]["impressions"] == "1200"


# --------------------------------------------------------------- API endpoints
async def test_preview_endpoint_returns_the_dry_run(client, auth, brand):
    proposal = await client.post(
        "/api/v1/ads/proposals",
        headers=auth("marketer"),
        json={
            "brand_profile_id": brand["id"],
            "platform": "meta_ads",
            "name": "Launch push",
            "objective": "traffic",
            "plan": {"phases": ["launch"]},
            "targeting": {"countries": ["IN"]},
            "creatives": [{"kind": "image", "ref": "asset-1"}],
            "daily_budget_cents": 5_000,
            "total_budget_cents": 100_000,
            "currency": "USD",
        },
    )
    assert proposal.status_code in (200, 201), proposal.text
    campaign_id = proposal.json()["id"]

    resp = await client.post(
        f"/api/v1/ads/campaigns/{campaign_id}/preview",
        headers=auth("marketer"),
        params={"kind": "create"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spends_money"] is False
    assert body["created_paused"] is True
    assert body["transport_available"] is False  # no credentials in the test env
    assert body["hash_matches"] is True


async def test_validate_account_endpoint_is_founder_only_and_says_why(client, auth, brand):
    proposal = await client.post(
        "/api/v1/ads/proposals",
        headers=auth("marketer"),
        json={
            "brand_profile_id": brand["id"],
            "platform": "google_ads",
            "name": "Search push",
            "objective": "traffic",
            "plan": {"phases": ["launch"]},
            "targeting": {"countries": ["IN"]},
            "creatives": [{"kind": "image", "ref": "asset-1"}],
            "daily_budget_cents": 5_000,
            "total_budget_cents": 100_000,
            "currency": "USD",
        },
    )
    assert proposal.status_code in (200, 201), proposal.text
    campaign_id = proposal.json()["id"]

    denied = await client.post(
        f"/api/v1/ads/campaigns/{campaign_id}/validate-account", headers=auth("marketer")
    )
    assert denied.status_code == 403

    resp = await client.post(
        f"/api/v1/ads/campaigns/{campaign_id}/validate-account", headers=auth("founder")
    )
    assert resp.status_code in (424, 409, 503), resp.text
    assert "GOOGLE_ADS" in resp.text


async def test_report_endpoint_requires_an_external_id(client, auth, brand):
    proposal = await client.post(
        "/api/v1/ads/proposals",
        headers=auth("marketer"),
        json={
            "brand_profile_id": brand["id"],
            "platform": "linkedin_ads",
            "name": "Brand push",
            "objective": "awareness",
            "plan": {"phases": ["launch"]},
            "targeting": {"countries": ["IN"]},
            "creatives": [{"kind": "image", "ref": "asset-1"}],
            "daily_budget_cents": 5_000,
            "total_budget_cents": 100_000,
            "currency": "USD",
        },
    )
    campaign_id = proposal.json()["id"]
    resp = await client.get(
        f"/api/v1/ads/campaigns/{campaign_id}/report", headers=auth("viewer")
    )
    # LinkedIn has no credentials in the test environment, so the honest answer is
    # "unavailable" rather than a fabricated zero-metric report.
    assert resp.status_code == 503, resp.text
    assert "LINKEDIN_ADS_ACCESS_TOKEN" in resp.text


def test_nothing_claims_a_live_verification():
    for row in ads_transport_report(settings()):
        assert row["verification"] == "unverified", (
            "no live call has been made from this repository, so no transport may "
            "claim verification"
        )
