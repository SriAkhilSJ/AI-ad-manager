"""Social account metadata.

Token handling contract for Phase 2 connectors:

* The connector writes the access/refresh token to an external vault (S3 with
  SSE-KMS, or a managed secret store) and stores only an opaque ``token_ref``
  here.
* ``token_ref`` is *never* serialised to the frontend: the response schema is an
  allow-list and the field is excluded from ``SocialAccountOut`` entirely.
* Revocation clears ``token_ref`` and sets status ``revoked``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.social import Platform, SocialAccount, SocialAccountStatus


class SocialAccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, *, brand_profile_id: uuid.UUID | None = None
    ) -> list[SocialAccount]:
        stmt = select(SocialAccount)
        if brand_profile_id:
            stmt = stmt.where(SocialAccount.brand_profile_id == brand_profile_id)
        stmt = stmt.order_by(SocialAccount.platform.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, account_id: uuid.UUID) -> SocialAccount:
        account = await self.session.get(SocialAccount, account_id)
        if account is None:
            raise NotFoundError(f"Social account {account_id} not found.")
        return account

    async def ensure_platform_placeholders(
        self, brand_profile_id: uuid.UUID
    ) -> list[SocialAccount]:
        """Create ``not_connected`` rows so the UI can show every supported
        platform with an honest status instead of a blank screen."""
        existing = await self.list(brand_profile_id=brand_profile_id)
        have = {a.platform for a in existing if not a.handle}
        created: list[SocialAccount] = []
        for platform in Platform:
            if platform.value in have:
                continue
            matches = [a for a in existing if a.platform == platform.value]
            if matches:
                continue
            row = SocialAccount(
                brand_profile_id=brand_profile_id,
                platform=platform.value,
                handle="",
                status=SocialAccountStatus.NOT_CONNECTED.value,
            )
            self.session.add(row)
            created.append(row)
        await self.session.flush()
        return created

    async def upsert_metadata(
        self,
        *,
        brand_profile_id: uuid.UUID,
        platform: str,
        handle: str,
        display_name: str = "",
        status: str = SocialAccountStatus.PENDING.value,
        token_ref: str | None = None,
        token_expires_at: datetime | None = None,
        scopes: list[str] | None = None,
        external_account_id: str | None = None,
    ) -> SocialAccount:
        stmt = select(SocialAccount).where(
            SocialAccount.brand_profile_id == brand_profile_id,
            SocialAccount.platform == platform,
            SocialAccount.handle == handle,
        )
        account = (await self.session.execute(stmt)).scalar_one_or_none()
        if account is None:
            account = SocialAccount(
                brand_profile_id=brand_profile_id, platform=platform, handle=handle
            )
        account.display_name = display_name or account.display_name
        account.status = status
        account.token_ref = token_ref
        account.token_expires_at = token_expires_at
        account.scopes = scopes or account.scopes or []
        account.external_account_id = external_account_id or account.external_account_id
        self.session.add(account)
        await self.session.flush()
        return account

    async def revoke(self, account_id: uuid.UUID) -> SocialAccount:
        account = await self.get(account_id)
        account.status = SocialAccountStatus.REVOKED.value
        account.token_ref = None
        account.token_expires_at = None
        self.session.add(account)
        await self.session.flush()
        return account
