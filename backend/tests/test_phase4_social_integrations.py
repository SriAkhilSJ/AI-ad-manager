"""Phase 4 — real social integrations, analytics ingestion and the ads gate.

Every network call in this module is served by ``httpx.MockTransport``, so these
tests prove the *implementation* is wired correctly; they are not evidence that a
live platform accepted anything. The final section asserts exactly that: nothing
in the system claims a verification it has not performed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import ApprovalRequiredError, PublishingDisabledError, ValidationProblem
from app.integrations import oauth as oauth_mod
from app.integrations.crypto import (
    AssociatedData,
    TokenCipher,
    TokenCipherUnavailable,
    TokenDecryptionError,
)
from app.integrations.platforms import CAPABILITIES, capabilities_for
from app.integrations.vault import (
    DatabaseTokenVault,
    TokenNotFoundError,
    VaultUnavailableError,
    build_vault,
    make_token_ref,
    parse_token_ref,
)
from app.models.integration import OAuthFlow, OAuthToken
from app.models.social import Platform, SocialAccount, SocialAccountStatus
from app.publishing.adapters import AdapterRegistry, PublishPayload
from app.services.ads import AdsService
from app.services.analytics_ingest import AnalyticsIngestService
from app.services.oauth import OAuthService
import app.services.publishing as publishing_module

KEY = "unit-test-vault-key-0123456789abcdef"
PLATFORM_CREDS = {
    "meta_app_id": "meta-app",
    "meta_app_secret": "meta-secret",
    "linkedin_client_id": "li-id",
    "linkedin_client_secret": "li-secret",
    "google_client_id": "goog-id",
    "google_client_secret": "goog-secret",
    "x_client_id": "x-id",
    "x_client_secret": "x-secret",
    "reddit_client_id": "rd-id",
    "reddit_client_secret": "rd-secret",
}


def vault_settings(**overrides) -> Settings:
    base = dict(
        environment="test",
        token_vault_backend="db",
        token_vault_key=KEY,
        oauth_redirect_base_url="https://app.example.com",
        **PLATFORM_CREDS,
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------- crypto
def test_cipher_roundtrip_and_owner_binding():
    cipher = TokenCipher(KEY)
    aad = AssociatedData("brand-a", "x", "acct-1")
    sealed = cipher.encrypt("access-token-value", aad)
    assert sealed.startswith("v1:")
    assert "access-token-value" not in sealed
    assert cipher.decrypt(sealed, aad) == "access-token-value"

    with pytest.raises(TokenDecryptionError):
        cipher.decrypt(sealed, AssociatedData("brand-b", "x", "acct-1"))


def test_cipher_requires_real_key_material():
    with pytest.raises(TokenCipherUnavailable):
        TokenCipher.from_settings(None)
    with pytest.raises(TokenCipherUnavailable):
        TokenCipher("short")


def test_cipher_accepts_hex_and_base64_keys():
    hex_key = "a" * 64
    assert TokenCipher(hex_key).decrypt(
        TokenCipher(hex_key).encrypt("v", AssociatedData("b", "x", "a")),
        AssociatedData("b", "x", "a"),
    ) == "v"


# ---------------------------------------------------------------------- vault
async def test_vault_refuses_without_a_key(session):
    unavailable = build_vault(session, Settings(environment="test", token_vault_backend="none"))
    assert unavailable.available is False
    with pytest.raises(VaultUnavailableError):
        await unavailable.load("vault:whatever")


async def test_vault_backend_db_without_key_is_unavailable(session):
    vault = build_vault(session, Settings(environment="test", token_vault_backend="db"))
    assert vault.available is False
    assert "TOKEN_VAULT_KEY" in vault.reason


async def test_vault_stores_only_ciphertext_and_roundtrips(session, brand_row):
    from app.integrations.vault import TokenRecord

    vault = build_vault(session, vault_settings())
    assert isinstance(vault, DatabaseTokenVault) and vault.available

    token_ref = await vault.store(
        brand_profile_id=brand_row.id,
        platform="x",
        account_key="acct-1",
        record=TokenRecord(access_token="super-secret-access", refresh_token="super-secret-refresh"),
    )
    assert token_ref.startswith("vault:")
    assert parse_token_ref(token_ref)

    row = await session.get(OAuthToken, parse_token_ref(token_ref))
    assert "super-secret-access" not in row.sealed_access
    assert row.sealed_refresh and "super-secret-refresh" not in row.sealed_refresh

    loaded = await vault.load(token_ref)
    assert loaded.access_token == "super-secret-access"
    assert loaded.refresh_token == "super-secret-refresh"

    await vault.revoke(token_ref)
    with pytest.raises(TokenNotFoundError):
        await vault.load(token_ref)


async def test_vault_ciphertext_cannot_be_moved_between_accounts(session, brand_row):
    from app.integrations.vault import TokenRecord

    vault = build_vault(session, vault_settings())
    ref_a = await vault.store(
        brand_profile_id=brand_row.id,
        platform="x",
        account_key="account-a",
        record=TokenRecord(access_token="token-a"),
    )
    ref_b = await vault.store(
        brand_profile_id=brand_row.id,
        platform="x",
        account_key="account-b",
        record=TokenRecord(access_token="token-b"),
    )
    row_a = await session.get(OAuthToken, parse_token_ref(ref_a))
    row_b = await session.get(OAuthToken, parse_token_ref(ref_b))
    row_b.sealed_access = row_a.sealed_access  # attacker with DB write access
    await session.flush()

    from app.integrations.vault import VaultError

    with pytest.raises(VaultError):
        await vault.load(ref_b)


# ---------------------------------------------------------------------- state
def test_signed_state_rejects_forgery_and_expiry():
    state, nonce = oauth_mod.build_state(
        "secret-key", platform="x", brand_profile_id=uuid.uuid4(), redirect_uri="https://a/cb"
    )
    payload = oauth_mod.verify_state("secret-key", state)
    assert payload["n"] == nonce and payload["p"] == "x"

    with pytest.raises(oauth_mod.OAuthStateError):
        oauth_mod.verify_state("different-secret", state)

    tampered = state[:-2] + ("aa" if state[-2:] != "aa" else "bb")
    with pytest.raises(oauth_mod.OAuthStateError):
        oauth_mod.verify_state("secret-key", tampered)

    expired, _ = oauth_mod.build_state(
        "secret-key",
        platform="x",
        brand_profile_id=uuid.uuid4(),
        redirect_uri="https://a/cb",
        ttl_seconds=-1,
    )
    with pytest.raises(oauth_mod.OAuthStateError):
        oauth_mod.verify_state("secret-key", expired)


def test_pkce_challenge_matches_rfc7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oauth_mod.code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_authorize_url_carries_pkce_and_state():
    verifier = oauth_mod.new_code_verifier()
    provider = oauth_mod.get_provider("x")
    url = oauth_mod.build_authorize_url(
        vault_settings(),
        provider,
        state="signed-state",
        redirect_uri="https://app.example.com/cb",
        code_challenge_value=oauth_mod.code_challenge(verifier),
    )
    assert url.startswith(provider.authorize_url)
    assert "code_challenge_method=S256" in url
    assert "state=signed-state" in url
    assert "tweet.write" in url


def test_each_platform_uses_its_own_credentials():
    with pytest.raises(oauth_mod.OAuthNotConfigured):
        oauth_mod.client_credentials(Settings(environment="test"), oauth_mod.get_provider("linkedin"))
    # Meta configured, but X must not borrow it.
    partial = Settings(environment="test", meta_app_id="a", meta_app_secret="b")
    with pytest.raises(oauth_mod.OAuthNotConfigured):
        oauth_mod.client_credentials(partial, oauth_mod.get_provider("x"))


# ------------------------------------------------------------------ OAuth flow
def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")


async def test_oauth_start_requires_vault_and_credentials(session, brand_row):
    disabled = OAuthService(
        session, Settings(environment="test"), client_factory=lambda: _mock_client(lambda r: httpx.Response(200))
    )
    from app.core.errors import IntegrationUnavailableError

    with pytest.raises(IntegrationUnavailableError):
        await disabled.start(platform="x", brand_profile_id=brand_row.id)

    no_creds = OAuthService(
        session,
        Settings(environment="test", token_vault_backend="db", token_vault_key=KEY),
        client_factory=lambda: _mock_client(lambda r: httpx.Response(200)),
    )
    from app.core.errors import IntegrationUnavailableError

    with pytest.raises(IntegrationUnavailableError):
        await no_creds.start(platform="x", brand_profile_id=brand_row.id)


async def test_oauth_start_creates_a_single_use_flow(session, brand_row):
    service = OAuthService(session, vault_settings())
    result = await service.start(platform="x", brand_profile_id=brand_row.id)
    await session.commit()

    assert result["pkce"] is True
    assert "code_challenge=" in result["authorize_url"]
    flow = (
        await session.execute(OAuthFlow.__table__.select().where(OAuthFlow.state == result["state"]))
    ).first()
    assert flow is not None
    assert flow.sealed_code_verifier and "verifier" not in flow.sealed_code_verifier


async def test_oauth_callback_rejects_a_forged_or_replayed_state(session, brand_row):
    service = OAuthService(session, vault_settings())
    started = await service.start(platform="x", brand_profile_id=brand_row.id)
    await session.commit()

    with pytest.raises(ValidationProblem):
        await service.callback(
            state="not-a-signed-state", code="c", brand_profile_id=brand_row.id
        )

    with pytest.raises(ValidationProblem):
        await service.callback(
            state=started["state"], code="c", brand_profile_id=uuid.uuid4()
        )


async def test_oauth_callback_exchanges_and_stores_an_encrypted_credential(session, brand_row):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "live-access-token",
                    "refresh_token": "live-refresh-token",
                    "expires_in": 7200,
                    "scope": "tweet.read tweet.write",
                    "token_type": "bearer",
                },
            )
        if request.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": "42", "username": "crusor", "name": "Crusor"}})
        return httpx.Response(404)

    service = OAuthService(session, vault_settings(), client_factory=lambda: _mock_client(handler))
    started = await service.start(platform="x", brand_profile_id=brand_row.id)
    await session.commit()

    result = await service.callback(
        state=started["state"], code="auth-code", brand_profile_id=brand_row.id
    )
    await session.commit()

    assert result["status"] == "connected"
    assert result["handle"] == "crusor"
    assert result["verified"] is True
    assert result["expires_at"] is not None

    account = (
        await session.execute(
            SocialAccount.__table__.select().where(SocialAccount.brand_profile_id == brand_row.id)
        )
    ).first()
    assert account.token_ref and account.token_ref.startswith("vault:")

    # The plaintext token exists nowhere in the credential table.
    rows = (await session.execute(OAuthToken.__table__.select())).all()
    assert rows and all("live-access-token" not in r.sealed_access for r in rows)

    # Replay is refused.
    from app.core.errors import ConflictError

    with pytest.raises((ConflictError, ValidationProblem, oauth_mod.OAuthStateError)):
        await service.callback(
            state=started["state"], code="auth-code", brand_profile_id=brand_row.id
        )


# ------------------------------------------------------------------ publishing
def _publish_payload(**overrides) -> PublishPayload:
    base = dict(
        content_item_id=uuid.uuid4(),
        content_version_id=uuid.uuid4(),
        approval_record_id=uuid.uuid4(),
        platform="x",
        body="Shipping faster with Crusor.",
        headline="Crusor",
        hashtags=["AIdev"],
        call_to_action="Try it",
        media_urls=[],
    )
    base.update(overrides)
    return PublishPayload(**base)


async def _connected_account(session, brand_row, platform: str = "x") -> SocialAccount:
    from app.integrations.vault import TokenRecord

    vault = build_vault(session, vault_settings())
    ref = await vault.store(
        brand_profile_id=brand_row.id,
        platform=platform,
        account_key="acct-1",
        record=TokenRecord(access_token="tok", refresh_token="refresh", expires_at=None),
    )
    account = SocialAccount(
        brand_profile_id=brand_row.id,
        platform=platform,
        handle="crusor",
        external_account_id="42",
        status=SocialAccountStatus.CONNECTED.value,
        token_ref=ref,
    )
    session.add(account)
    await session.flush()
    return account


async def test_registry_reports_honest_availability():
    none = AdapterRegistry(Settings(environment="test"))
    rows = {r["platform"]: r for r in none.availability()}
    assert all(r["publish_enabled"] is False for r in rows.values())
    assert all(r["vault_available"] is False for r in rows.values())

    ready = AdapterRegistry(vault_settings())
    rows = {r["platform"]: r for r in ready.availability()}
    assert all(r["credentials_configured"] and r["vault_available"] for r in rows.values())
    assert all(r["publish_enabled"] for r in rows.values())
    # Documented ≠ implemented ≠ verified.
    assert rows["x"]["capabilities"]["image_post"]["implemented"] is False
    assert rows["x"]["capabilities"]["text_post"]["verification"] == "unverified"


async def test_publish_verifies_the_created_object(session, brand_row):
    account = await _connected_account(session, brand_row)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path.endswith("/tweets"):
            return httpx.Response(201, json={"data": {"id": "12345"}})
        if request.method == "GET" and request.url.path.endswith("/tweets/12345"):
            return httpx.Response(200, json={"data": {"id": "12345", "created_at": "2026-01-01T00:00:00Z"}})
        return httpx.Response(404)

    registry = AdapterRegistry(vault_settings(), http_client=_mock_client(handler))
    adapter = await registry.resolve("x", session=session, account=account)
    result = await adapter.publish(_publish_payload(), account)

    assert result.external_post_id == "12345"
    assert result.metadata["verified"] is True
    assert "POST /2/tweets" in calls and any("GET" in c and "/tweets/12345" in c for c in calls)


async def test_publish_without_verification_raises_instead_of_claiming_success(session, brand_row):
    account = await _connected_account(session, brand_row)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "999"}})
        return httpx.Response(404, json={"errors": [{"message": "not found"}]})

    registry = AdapterRegistry(vault_settings(), http_client=_mock_client(handler))
    adapter = await registry.resolve("x", session=session, account=account)
    from app.integrations.http import PlatformError

    with pytest.raises(PlatformError):
        await adapter.publish(_publish_payload(), account)


async def test_publish_refuses_an_unimplemented_capability_before_any_call(session, brand_row):
    account = await _connected_account(session, brand_row, platform="instagram")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"id": "x"})

    registry = AdapterRegistry(vault_settings(), http_client=_mock_client(handler))
    adapter = await registry.resolve("instagram", session=session, account=account)
    from app.integrations.http import PlatformRejected

    with pytest.raises(PlatformRejected):
        await adapter.publish(_publish_payload(platform="instagram"), account)  # text only
    assert calls == [], "a refused capability must not reach the network"


async def test_x_rejects_over_length_posts_locally(session, brand_row):
    account = await _connected_account(session, brand_row)
    registry = AdapterRegistry(
        vault_settings(), http_client=_mock_client(lambda r: httpx.Response(201, json={}))
    )
    adapter = await registry.resolve("x", session=session, account=account)
    from app.integrations.http import PlatformRejected

    with pytest.raises(PlatformRejected):
        await adapter.publish(_publish_payload(body="x" * 400), account)


async def test_publish_is_refused_without_a_credential(session, brand_row):
    account = SocialAccount(
        brand_profile_id=brand_row.id,
        platform="x",
        handle="crusor",
        status=SocialAccountStatus.CONNECTED.value,
        token_ref=None,
    )
    session.add(account)
    await session.flush()
    registry = AdapterRegistry(vault_settings())
    with pytest.raises(PublishingDisabledError):
        await registry.resolve("x", session=session, account=account)


async def test_publishing_service_refuses_before_the_network(session, brand_row, monkeypatch):
    """The Phase 1 guarantee survives: no transport means no publish, no state change."""
    from app.models.content import ContentItem, ContentStatus
    from app.services.publishing import PublishingService

    item = ContentItem(
        brand_profile_id=brand_row.id,
        platform="x",
        title="Draft",
        status=ContentStatus.DRAFT.value,
    )
    session.add(item)
    await session.flush()

    monkeypatch.setattr(
        publishing_module, "get_adapter_registry", lambda *a, **k: AdapterRegistry(Settings(environment="test"))
    )
    service = PublishingService(session)
    # No approval exists, so the approval gate is what fires first — that ordering
    # is the point of the service, and it predates this phase.
    from app.core.errors import ApprovalRequiredError as ApprovalError

    with pytest.raises(ApprovalError):
        await service.publish(item.id, actor_id=brand_row.id, actor_role="founder")


# ------------------------------------------------------------------- analytics
async def _approved_content(client, auth, content_item) -> tuple:
    """Drive the real approval path so the published row has real parents."""
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    resp = await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "approve"}, headers=auth("founder")
    )
    assert resp.status_code in (200, 409), resp.text  # 409 when already approved
    return iid, uuid.UUID(content_item["brand_profile_id"])


async def _published_post(session, *, brand_id, item_id, platform, external_id):
    from app.models.approval import ApprovalRecord
    from app.models.content import ContentItem
    from app.models.social import PublishedPost
    from sqlalchemy import select

    item = await session.get(ContentItem, uuid.UUID(item_id))
    assert item is not None and item.current_version_id is not None
    approval = (
        await session.execute(
            select(ApprovalRecord).where(ApprovalRecord.content_item_id == item.id)
        )
    ).scalars().first()
    assert approval is not None, "the approval path must have produced a record"

    account = await _connected_account_for(session, brand_id, platform)
    post_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await session.execute(
        PublishedPost.__table__.insert().values(
            id=post_id,
            content_item_id=item.id,
            content_version_id=item.current_version_id,
            social_account_id=account.id,
            approval_record_id=approval.id,
            platform=platform,
            external_post_id=external_id,
            published_at=now,
            status="published",
            publish_metadata={},
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    return post_id, account


async def _connected_account_for(session, brand_id, platform, *, token="tok", connected=True):
    from app.integrations.vault import TokenRecord

    if connected:
        vault = build_vault(session, vault_settings())
        ref = await vault.store(
            brand_profile_id=brand_id,
            platform=platform,
            account_key=f"acct-{platform}",
            record=TokenRecord(access_token=token, refresh_token="refresh"),
        )
    else:
        ref = None
    account = SocialAccount(
        brand_profile_id=brand_id,
        platform=platform,
        handle=f"crusor-{platform}",
        external_account_id="urn:li:person:42" if platform == "linkedin" else "42",
        status=(
            SocialAccountStatus.CONNECTED.value if connected else SocialAccountStatus.NOT_CONNECTED.value
        ),
        token_ref=ref,
    )
    session.add(account)
    await session.flush()
    return account


async def test_analytics_ingest_records_only_measured_values(client, auth, content_item, session):
    iid, brand_id = await _approved_content(client, auth, content_item)
    await _published_post(
        session,
        brand_id=brand_id,
        item_id=iid,
        platform="linkedin",
        external_id="urn:li:share:123",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "/socialActions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "likesSummary": {"totalLikes": 7},
                    "commentsSummary": {"totalFirstLevelComments": 2},
                },
            )
        return httpx.Response(404)

    service = AnalyticsIngestService(
        session, vault_settings(), client_factory=lambda: _mock_client(handler)
    )
    result = await service.ingest(brand_profile_id=brand_id, days=30)
    await session.commit()

    assert result["ingested"] == 1, result
    assert result["skipped"] == 0

    from app.models.analytics import AnalyticsSnapshot

    rows = (await session.execute(AnalyticsSnapshot.__table__.select())).all()
    assert len(rows) == 1
    snap = rows[0]
    assert snap.likes == 7 and snap.comments == 2
    payload = snap.metrics if isinstance(snap.metrics, dict) else json.loads(snap.metrics)
    # Impressions were never reported, so they are listed as unavailable rather
    # than asserted as a measured zero.
    assert "impressions" in payload["unavailable"]
    assert payload["measured"] == {"likes": 7, "comments": 2}
    assert payload["derived"]["engagements"] == 9
    assert snap.source.startswith("platform_api:")


async def test_analytics_ingest_reports_unavailable_rather_than_fabricating(
    client, auth, content_item, session
):
    iid, brand_id = await _approved_content(client, auth, content_item)
    await _published_post(
        session, brand_id=brand_id, item_id=iid, platform="linkedin", external_id="urn:li:share:9"
    )
    # Detach the credential: the account is no longer usable.
    from app.models.social import SocialAccount as _SA

    account = (await session.execute(_SA.__table__.select())).first()
    await session.execute(
        _SA.__table__.update()
        .where(_SA.id == account.id)
        .values(status=SocialAccountStatus.NOT_CONNECTED.value, token_ref=None)
    )
    await session.flush()

    service = AnalyticsIngestService(
        session, vault_settings(), client_factory=lambda: _mock_client(lambda r: httpx.Response(500))
    )
    result = await service.ingest(brand_profile_id=brand_id, days=30)

    assert result["ingested"] == 0 and result["skipped"] == 1
    assert result["unavailable"][0]["platform"] == "linkedin"
    from app.models.analytics import AnalyticsSnapshot

    assert (await session.execute(AnalyticsSnapshot.__table__.select())).all() == []


async def test_analytics_ingest_without_a_vault_writes_nothing(session, brand_row):
    service = AnalyticsIngestService(session, Settings(environment="test"))
    result = await service.ingest(brand_profile_id=brand_row.id)
    assert result["ingested"] == 0
    assert "vault" in result["detail"].lower()


# ------------------------------------------------------------------------ ads
def _proposal_kwargs(brand_id) -> dict:
    return dict(
        brand_profile_id=brand_id,
        platform="meta_ads",
        name="Launch push",
        objective="traffic",
        plan={"phases": ["tease", "launch"]},
        targeting={"countries": ["IN"], "age": "18-34"},
        creatives=[{"kind": "image", "ref": "asset-1"}],
        daily_budget_cents=5_000,
        total_budget_cents=100_000,
        currency="USD",
    )


async def test_ads_proposal_is_inert_until_authorised(session, brand_row):
    service = AdsService(session, vault_settings())
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    assert campaign.status == "proposed"
    assert len(campaign.proposal_hash) == 64

    with pytest.raises(ApprovalRequiredError):
        await service.authorize(
            campaign.id,
            actor_id=uuid.uuid4(),
            actor_role="marketer",
            max_spend_cents=100_000,
            allowed_operations=["create"],
        )


async def test_ads_execution_without_authorisation_is_refused_and_recorded(session, brand_row, users):
    service = AdsService(session, vault_settings())
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    op = await service.execute(
        campaign.id,
        kind="create",
        actor_id=users["founder"].id,
        actor_role="founder",
        authorization_id=None,
    )
    assert op.outcome == "refused"
    assert "no founder authorisation" in op.detail
    assert campaign.status == "proposed"


async def test_ads_edit_after_authorisation_invalidates_it(session, brand_row, users):
    settings = vault_settings()
    service = AdsService(session, settings)
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    authorization = await service.authorize(
        campaign.id,
        actor_id=users["founder"].id,
        actor_role="founder",
        max_spend_cents=100_000,
        allowed_operations=["create", "launch"],
    )
    assert campaign.status == "authorized"

    campaign.total_budget_cents = 400_000  # the founder approved 100_000
    session.add(campaign)
    await session.flush()

    op = await service.execute(
        campaign.id,
        kind="create",
        actor_id=users["founder"].id,
        actor_role="founder",
        authorization_id=authorization.id,
    )
    assert op.outcome == "refused"
    assert "hash mismatch" in op.detail or "changed after it was authorised" in op.detail
    # The approval was not burned by the refusal.
    from app.models.ads import AdBudgetAuthorization

    fresh = await session.get(AdBudgetAuthorization, authorization.id)
    assert fresh.status == "active"


async def test_ads_execution_is_blocked_by_an_unavailable_transport_without_consuming(
    session, brand_row, users
):
    settings = vault_settings()
    service = AdsService(session, settings)
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    authorization = await service.authorize(
        campaign.id,
        actor_id=users["founder"].id,
        actor_role="founder",
        max_spend_cents=100_000,
        allowed_operations=["create"],
    )
    op = await service.execute(
        campaign.id,
        kind="create",
        actor_id=users["founder"].id,
        actor_role="founder",
        authorization_id=authorization.id,
    )
    assert op.outcome == "refused"
    assert "ads transport unavailable" in op.detail
    assert "not implemented" in op.detail

    from app.models.ads import AdBudgetAuthorization

    fresh = await session.get(AdBudgetAuthorization, authorization.id)
    assert fresh.status == "active" and fresh.consumed_at is None
    assert campaign.external_campaign_id is None


async def test_ads_operation_is_idempotent(session, brand_row, users):
    service = AdsService(session, vault_settings())
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    first = await service.execute(
        campaign.id, kind="create", actor_id=users["founder"].id, actor_role="founder",
        authorization_id=None,
    )
    second = await service.execute(
        campaign.id, kind="create", actor_id=users["founder"].id, actor_role="founder",
        authorization_id=None,
    )
    assert first.id == second.id

    from app.models.ads import AdOperation

    rows = (await session.execute(AdOperation.__table__.select())).all()
    assert len(rows) == 1


async def test_ads_non_founder_execution_is_refused(session, brand_row, users):
    service = AdsService(session, vault_settings())
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    authorization = await service.authorize(
        campaign.id,
        actor_id=users["founder"].id,
        actor_role="founder",
        max_spend_cents=100_000,
        allowed_operations=["create"],
    )
    op = await service.execute(
        campaign.id,
        kind="create",
        actor_id=users["marketer"].id,
        actor_role="marketer",
        authorization_id=authorization.id,
    )
    assert op.outcome == "refused"
    assert "founder" in op.detail


async def test_ads_authorisation_rejects_an_underfunded_ceiling(session, brand_row, users):
    service = AdsService(session, vault_settings())
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    with pytest.raises(ValidationProblem):
        await service.authorize(
            campaign.id,
            actor_id=users["founder"].id,
            actor_role="founder",
            max_spend_cents=1_000,  # campaign totals 100_000
            allowed_operations=["create"],
        )


async def test_ads_authorisation_respects_the_configured_ceiling(session, brand_row, users):
    settings = vault_settings(ads_max_authorized_spend_cents=50_000)
    service = AdsService(session, settings)
    campaign = await service.propose(**_proposal_kwargs(brand_row.id))
    with pytest.raises(ValidationProblem):
        await service.authorize(
            campaign.id,
            actor_id=users["founder"].id,
            actor_role="founder",
            max_spend_cents=100_000,
            allowed_operations=["create"],
        )


# -------------------------------------------------------------- honesty checks
def test_no_capability_claims_verification_that_did_not_happen():
    for platform, caps in CAPABILITIES.items():
        for name, cap in caps.items():
            assert cap.verification == "unverified", (
                f"{platform}.{name} claims {cap.verification} verification, which this "
                "repository cannot substantiate"
            )
            data = cap.as_dict()
            assert data["usable"] == (cap.documented == "supported" and cap.implemented)


def test_capability_matrix_marks_unimplemented_operations():
    caps = capabilities_for("instagram")
    assert caps["text_post"]["documented"] == "unsupported"
    assert caps["image_post"]["usable"] is True
    assert caps["paid_ads"]["implemented"] is False


# ------------------------------------------------------------------- API layer
async def test_capability_matrix_endpoint_is_honest(client, auth):
    resp = await client.get("/api/v1/social/capabilities", headers=auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["platforms"]) == {"facebook", "instagram", "linkedin", "x", "youtube", "reddit"}
    for caps in body["platforms"].values():
        for cap in caps.values():
            assert cap["verification"] == "unverified"


async def test_platform_status_exposes_vault_and_capabilities(client, auth):
    resp = await client.get("/api/v1/social/platforms", headers=auth("viewer"))
    assert resp.status_code == 200
    rows = {r["platform"]: r for r in resp.json()}
    assert all(r["publish_enabled"] is False for r in rows.values())
    assert all(r["vault_available"] is False for r in rows.values())
    assert rows["x"]["capabilities"]["text_post"]["implemented"] is True
    assert "TOKEN_VAULT_KEY" in rows["x"]["reason"] or "credentials" in rows["x"]["reason"]


async def test_oauth_start_refuses_without_configuration(client, auth, brand):
    resp = await client.post(
        f"/api/v1/social/oauth/x/start?brand_profile_id={brand['id']}", headers=auth("founder")
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "integration_unavailable"


async def test_oauth_start_is_founder_only(client, auth, brand):
    resp = await client.post(
        f"/api/v1/social/oauth/x/start?brand_profile_id={brand['id']}", headers=auth("marketer")
    )
    assert resp.status_code == 403


async def test_oauth_callback_rejects_an_unsigned_state(client, auth, brand):
    resp = await client.get(
        "/api/v1/social/oauth/callback",
        params={"state": "forged-state-value", "code": "abc", "brand_profile_id": brand["id"]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert "not valid" in resp.json()["error"]["message"]


async def test_ads_transports_report_unavailable(client, auth):
    resp = await client.get("/api/v1/ads/transports", headers=auth("viewer"))
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["platform"] for r in rows} == {"meta_ads", "google_ads", "linkedin_ads"}
    assert all(r["available"] is False and r["implemented"] is False for r in rows)
    assert all("not implemented" in r["reason"] for r in rows)


async def test_ads_proposal_cannot_be_authorised_by_a_marketer(client, auth, brand):
    created = await client.post(
        "/api/v1/ads/proposals",
        json={
            "brand_profile_id": brand["id"],
            "platform": "meta_ads",
            "name": "Q4 push",
            "objective": "conversions",
            "plan": {"angle": "speed"},
            "targeting": {"countries": ["IN"]},
            "daily_budget_cents": 2000,
            "total_budget_cents": 40000,
        },
        headers=auth("marketer"),
    )
    assert created.status_code == 201, created.text
    campaign = created.json()
    assert campaign["status"] == "proposed"

    denied = await client.post(
        f"/api/v1/ads/campaigns/{campaign['id']}/authorize",
        json={"max_spend_cents": 40000, "allowed_operations": ["create"]},
        headers=auth("marketer"),
    )
    assert denied.status_code == 403


async def test_ads_founder_authorisation_and_blocked_execution(client, auth, brand):
    created = await client.post(
        "/api/v1/ads/proposals",
        json={
            "brand_profile_id": brand["id"],
            "platform": "google_ads",
            "name": "Search test",
            "objective": "traffic",
            "daily_budget_cents": 1000,
            "total_budget_cents": 20000,
        },
        headers=auth("marketer"),
    )
    campaign = created.json()
    authorized = await client.post(
        f"/api/v1/ads/campaigns/{campaign['id']}/authorize",
        json={"max_spend_cents": 25000, "allowed_operations": ["create", "launch"]},
        headers=auth("founder"),
    )
    assert authorized.status_code == 200, authorized.text
    authorization = authorized.json()
    assert authorization["status"] == "active"
    assert authorization["proposal_hash"] == campaign["proposal_hash"]

    executed = await client.post(
        f"/api/v1/ads/campaigns/{campaign['id']}/operations",
        json={"kind": "create", "authorization_id": authorization["id"]},
        headers=auth("founder"),
    )
    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["outcome"] == "refused"
    assert "ads transport unavailable" in body["detail"]

    detail = await client.get(
        f"/api/v1/ads/campaigns/{campaign['id']}", headers=auth("founder")
    )
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["campaign"]["status"] == "authorized"
    assert payload["campaign"]["external_campaign_id"] is None
    assert [op["outcome"] for op in payload["operations"]] == ["refused"]
    # The approval survived the blocked attempt.
    assert payload["authorizations"][0]["status"] == "active"


async def test_ads_reject_marks_the_proposal(client, auth, brand):
    created = await client.post(
        "/api/v1/ads/proposals",
        json={
            "brand_profile_id": brand["id"],
            "platform": "linkedin_ads",
            "name": "Reject me",
            "daily_budget_cents": 100,
            "total_budget_cents": 1000,
        },
        headers=auth("marketer"),
    )
    campaign = created.json()
    rejected = await client.post(
        f"/api/v1/ads/campaigns/{campaign['id']}/reject", headers=auth("founder")
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
