"""Per-platform transport: publish, verify, measure.

THREE RULES THIS MODULE EXISTS TO ENFORCE
-----------------------------------------
1. **A publish is not done until it is verified.** Every ``publish()`` follows the
   create call with a ``verify()`` read of the object it claims to have created.
   ``PublishedPost`` rows are only written from a verified result, so a 200 that
   did not actually create anything cannot fabricate a publication.
2. **A refused capability is refused before any network call.** Instagram cannot
   publish text-only; YouTube cannot publish without a video asset; X text posts
   are limited to 280 characters. Those are checked locally.
3. **Metrics are measured or absent.** :meth:`fetch_metrics` returns only values a
   platform actually reported, plus an explicit list of what it did not. It never
   zero-fills a series.

Every capability is declared in :data:`CAPABILITIES` with its documented support,
whether this build implements it, and — separately — whether it has ever been
verified against a live account. On a fresh checkout that last field is
``unverified`` for all six platforms, because it has not been.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from app.integrations import oauth
from app.integrations.http import (
    PlatformError,
    PlatformRejected,
    RetryPolicy,
    request_json,
)
from app.integrations.vault import TokenRecord
from app.publishing.adapters import PublishPayload, PublishResult

CapabilityState = Literal["supported", "unsupported", "not_implemented"]
VerificationState = Literal["unverified", "sandbox", "production"]


@dataclass(frozen=True)
class Capability:
    documented: CapabilityState
    implemented: bool
    verification: VerificationState = "unverified"
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "documented": self.documented,
            "implemented": self.implemented,
            "usable": self.documented == "supported" and self.implemented,
            "verification": self.verification,
            "note": self.note,
        }


def _cap(
    documented: CapabilityState,
    implemented: bool,
    note: str = "",
    verification: VerificationState = "unverified",
) -> Capability:
    return Capability(documented, implemented, verification, note)


#: Documented support is what the provider's API reference says; ``implemented``
#: is what this build actually does; ``verification`` is what has been proven
#: against a real account. They are three different questions.
CAPABILITIES: dict[str, dict[str, Capability]] = {
    "facebook": {
        "text_post": _cap("supported", True, "Page feed posts"),
        "image_post": _cap("supported", True, "Photos endpoint"),
        "video_post": _cap("supported", False, "Video upload not implemented"),
        "scheduling": _cap("supported", False, "native_scheduled_publish_time not implemented"),
        "analytics": _cap("supported", True, "post insights + reaction summaries"),
        "paid_ads": _cap("supported", False, "see the ads module; requires a founder authorisation"),
    },
    "instagram": {
        "text_post": _cap("unsupported", False, "Instagram requires at least one media asset"),
        "image_post": _cap("supported", True, "container + media_publish"),
        "video_post": _cap("supported", True, "Reels/IGTV container with video_url"),
        "scheduling": _cap("supported", False, "not implemented"),
        "analytics": _cap("supported", True, "media insights"),
        "paid_ads": _cap("supported", False, "Meta Marketing API not implemented"),
    },
    "linkedin": {
        "text_post": _cap("supported", True, "UGC Posts, shareMediaCategory NONE"),
        "image_post": _cap("supported", True, "UGC Posts with IMAGE asset"),
        "video_post": _cap("supported", False, "videos API not implemented"),
        "scheduling": _cap("supported", False, "not implemented"),
        "analytics": _cap("supported", True, "socialActions + share statistics"),
        "paid_ads": _cap("supported", False, "LinkedIn Marketing API not implemented"),
    },
    "x": {
        "text_post": _cap("supported", True, "POST /2/tweets, 280 characters"),
        "image_post": _cap("supported", False, "media upload uses the v1.1 endpoint; not implemented"),
        "video_post": _cap("supported", False, "not implemented"),
        "scheduling": _cap("unsupported", False, "no native scheduling in the v2 API"),
        "analytics": _cap("supported", True, "public_metrics only; impressions are not public"),
        "paid_ads": _cap("supported", False, "X Ads API not implemented"),
    },
    "youtube": {
        "text_post": _cap("unsupported", False, "YouTube has no text post surface"),
        "image_post": _cap("unsupported", False, "thumbnails are not posts"),
        "video_post": _cap("supported", True, "resumable upload"),
        "scheduling": _cap("supported", True, "status.publishAt on upload"),
        "analytics": _cap("supported", True, "statistics part; deeper analytics need the Reporting API"),
        "paid_ads": _cap("supported", False, "Google Ads API not implemented"),
    },
    "reddit": {
        "text_post": _cap("supported", True, "self post"),
        "image_post": _cap("unsupported", False, "media upload requires the media_asset flow"),
        "video_post": _cap("unsupported", False, "not supported via the submit API"),
        "scheduling": _cap("unsupported", False, "no scheduling API"),
        "analytics": _cap("supported", True, "score and comment count only"),
        "paid_ads": _cap("supported", False, "Reddit Ads API not implemented"),
    },
}


def capabilities_for(platform: str) -> dict[str, dict[str, Any]]:
    return {name: cap.as_dict() for name, cap in CAPABILITIES.get(platform, {}).items()}


def capability(platform: str, name: str) -> Capability | None:
    return CAPABILITIES.get(platform, {}).get(name)


@dataclass(frozen=True)
class AccountRef:
    """The remote identity a post is made as."""

    platform: str
    external_account_id: str
    handle: str = ""
    #: Platform-specific extras (page id vs ig user id, organisation urn, subreddit).
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PostMetrics:
    """Measured values only. ``unavailable`` lists what the platform did not give."""

    platform: str
    external_post_id: str
    measured: dict[str, int]
    unavailable: list[str]
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "external_post_id": self.external_post_id,
            "measured": dict(self.measured),
            "unavailable": list(self.unavailable),
            "source": self.source,
        }


USER_AGENT = "crusor-marketing-director/1.0 (+https://crusor.ai)"
MAX_X_CHARS = 280


class PlatformClient:
    """Base client. Subclasses implement ``publish``, ``verify`` and ``metrics``."""

    platform: str = ""

    def __init__(
        self,
        settings: Any,
        http: httpx.AsyncClient,
        *,
        policy: RetryPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.http = http
        self.policy = policy or RetryPolicy()

    # --------------------------------------------------------------- helpers
    @property
    def provider(self) -> oauth.OAuthProvider:
        return oauth.get_provider(self.platform)

    def _headers(self, token: TokenRecord) -> dict[str, str]:
        return {
            "Authorization": f"{token.token_type or 'Bearer'} {token.access_token}",
            "User-Agent": USER_AGENT,
        }

    def _require_capability(self, name: str, *, reason: str = "") -> None:
        cap = capability(self.platform, name)
        if cap is None or not (cap.documented == "supported" and cap.implemented):
            raise PlatformRejected(
                f"{self.platform} cannot {name}: "
                + (reason or (cap.note if cap else "no such capability")),
                status=422,
            )

    async def publish(
        self,
        payload: PublishPayload,
        *,
        account: AccountRef,
        token: TokenRecord,
    ) -> PublishResult:
        raise NotImplementedError

    async def verify(
        self, external_post_id: str, *, account: AccountRef, token: TokenRecord
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def fetch_metrics(
        self, external_post_id: str, *, account: AccountRef, token: TokenRecord
    ) -> PostMetrics:
        raise NotImplementedError

    async def request(
        self, method: str, url: str, *, idempotent: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        return await request_json(
            self.http, method, url, policy=self.policy, idempotent=idempotent, **kwargs
        )


def _compose_caption(payload: PublishPayload) -> str:
    parts = [payload.body.strip()]
    if payload.call_to_action.strip():
        parts.append(payload.call_to_action.strip())
    if payload.hashtags:
        parts.append(" ".join(f"#{h.lstrip('#')}" for h in payload.hashtags if h.strip()))
    return "\n\n".join(p for p in parts if p)


def _first_media(payload: PublishPayload, *, kind: str) -> str | None:
    for url in payload.media_urls:
        suffix = url.lower().split("?")[0]
        if kind == "video" and suffix.endswith((".mp4", ".mov", ".webm", ".m4v")):
            return url
        if kind == "image" and suffix.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return url
    if kind == "image" and payload.media_urls:
        return payload.media_urls[0]
    return None


# ------------------------------------------------------------------- Facebook
class FacebookClient(PlatformClient):
    platform = "facebook"

    @property
    def base(self) -> str:
        return self.provider.api_base

    async def publish(self, payload, *, account, token) -> PublishResult:
        self._require_capability("text_post")
        page_id = account.external_account_id or account.settings.get("page_id", "")
        if not page_id:
            raise PlatformRejected("Facebook publish needs a page id", status=422)
        data: dict[str, Any] = {"message": _compose_caption(payload), "access_token": token.access_token}
        link = account.settings.get("link_url")
        if link:
            data["link"] = link
        created = await self.request(
            "POST", f"{self.base}/{page_id}/feed", data=data, idempotent=False
        )
        post_id = created.get("id")
        if not post_id:
            raise PlatformRejected("Facebook created no post id", status=502, payload=created)
        evidence = await self.verify(str(post_id), account=account, token=token)
        return PublishResult(
            external_post_id=str(post_id),
            permalink=evidence.get("permalink_url"),
            metadata={"verified": True, "evidence": evidence},
        )

    async def verify(self, external_post_id, *, account, token) -> dict[str, Any]:
        evidence = await self.request(
            "GET",
            f"{self.base}/{external_post_id}",
            params={"fields": "id,permalink_url,created_time", "access_token": token.access_token},
        )
        if str(evidence.get("id", "")) not in {external_post_id, external_post_id.split("_")[-1]}:
            raise PlatformRejected(
                "Facebook verification returned a different object", status=502, payload=evidence
            )
        return evidence

    async def fetch_metrics(self, external_post_id, *, account, token) -> PostMetrics:
        measured: dict[str, int] = {}
        unavailable: list[str] = []
        data = await self.request(
            "GET",
            f"{self.base}/{external_post_id}",
            params={
                "fields": "shares,comments.summary(true).limit(0),reactions.summary(true).limit(0)",
                "access_token": token.access_token,
            },
        )
        if isinstance(data.get("shares"), dict) and "count" in data["shares"]:
            measured["shares"] = int(data["shares"]["count"])
        else:
            unavailable.append("shares")
        comments = (data.get("comments") or {}).get("summary", {}).get("total_count")
        reactions = (data.get("reactions") or {}).get("summary", {}).get("total_count")
        if comments is None:
            unavailable.append("comments")
        else:
            measured["comments"] = int(comments)
        if reactions is None:
            unavailable.append("reactions")
        else:
            measured["reactions"] = int(reactions)
        unavailable.extend(["impressions", "reach"])
        return PostMetrics(
            platform=self.platform,
            external_post_id=external_post_id,
            measured=measured,
            unavailable=sorted(set(unavailable)),
            source="facebook_graph_api",
        )


# ------------------------------------------------------------------ Instagram
class InstagramClient(PlatformClient):
    platform = "instagram"

    @property
    def base(self) -> str:
        return self.provider.api_base

    async def publish(self, payload, *, account, token) -> PublishResult:
        ig_user = account.external_account_id or account.settings.get("ig_user_id", "")
        if not ig_user:
            raise PlatformRejected("Instagram publish needs an IG user id", status=422)
        video = _first_media(payload, kind="video")
        image = _first_media(payload, kind="image")
        if not video and not image:
            self._require_capability("text_post")  # raises: Instagram has no text surface
            raise PlatformRejected("Instagram requires at least one media asset", status=422)

        self._require_capability("video_post" if video else "image_post")
        container_data: dict[str, Any] = {
            "caption": _compose_caption(payload),
            "access_token": token.access_token,
        }
        if video:
            container_data["media_type"] = "REELS"
            container_data["video_url"] = video
        else:
            container_data["image_url"] = image

        container = await self.request(
            "POST", f"{self.base}/{ig_user}/media", data=container_data, idempotent=False
        )
        creation_id = container.get("id")
        if not creation_id:
            raise PlatformRejected("Instagram created no container", status=502, payload=container)

        published = await self.request(
            "POST",
            f"{self.base}/{ig_user}/media_publish",
            data={"creation_id": creation_id, "access_token": token.access_token},
            idempotent=False,
        )
        media_id = published.get("id")
        if not media_id:
            raise PlatformRejected(
                "Instagram published no media id", status=502, payload=published
            )
        evidence = await self.verify(str(media_id), account=account, token=token)
        return PublishResult(
            external_post_id=str(media_id),
            permalink=evidence.get("permalink"),
            metadata={"verified": True, "container_id": creation_id, "evidence": evidence},
        )

    async def verify(self, external_post_id, *, account, token) -> dict[str, Any]:
        evidence = await self.request(
            "GET",
            f"{self.base}/{external_post_id}",
            params={
                "fields": "id,permalink,media_type,timestamp",
                "access_token": token.access_token,
            },
        )
        if str(evidence.get("id", "")) != external_post_id:
            raise PlatformRejected(
                "Instagram verification returned a different object",
                status=502,
                payload=evidence,
            )
        return evidence

    async def fetch_metrics(self, external_post_id, *, account, token) -> PostMetrics:
        measured: dict[str, int] = {}
        unavailable: list[str] = []
        data = await self.request(
            "GET",
            f"{self.base}/{external_post_id}/insights",
            params={"metric": "reach,likes,comments,saved", "access_token": token.access_token},
        )
        for entry in data.get("data", []):
            name = entry.get("name")
            values = entry.get("values") or [{}]
            value = values[0].get("value")
            if isinstance(value, int):
                measured[str(name)] = value
            else:
                unavailable.append(str(name))
        unavailable.extend(["impressions", "clicks"])
        return PostMetrics(
            platform=self.platform,
            external_post_id=external_post_id,
            measured=measured,
            unavailable=sorted(set(unavailable)),
            source="instagram_graph_api",
        )


# ------------------------------------------------------------------- LinkedIn
class LinkedInClient(PlatformClient):
    platform = "linkedin"

    @property
    def base(self) -> str:
        return self.provider.api_base

    def _headers(self, token: TokenRecord) -> dict[str, str]:
        headers = super()._headers(token)
        headers["X-Restli-Protocol-Version"] = "2.0.0"
        headers["Content-Type"] = "application/json"
        headers["LinkedIn-Version"] = "202405"
        return headers

    async def publish(self, payload, *, account, token) -> PublishResult:
        author = account.external_account_id or account.settings.get("author_urn", "")
        if not author.startswith("urn:li:"):
            raise PlatformRejected(
                "LinkedIn publish needs an author urn (urn:li:person:… or urn:li:organization:…)",
                status=422,
            )
        image = _first_media(payload, kind="image")
        media_category = "IMAGE" if image else "NONE"
        if image:
            self._require_capability("image_post")
        else:
            self._require_capability("text_post")

        body: dict[str, Any] = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": _compose_caption(payload)},
                    "shareMediaCategory": media_category,
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": account.settings.get(
                    "visibility", "PUBLIC"
                )
            },
        }
        if image:
            body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = [
                {"status": "READY", "originalUrl": image}
            ]

        created = await self.request(
            "POST",
            f"{self.base}/v2/ugcPosts",
            json=body,
            headers=self._headers(token),
            idempotent=False,
        )
        post_urn = created.get("id") or created.get("$URN") or ""
        if not post_urn:
            raise PlatformRejected("LinkedIn created no post urn", status=502, payload=created)
        evidence = await self.verify(str(post_urn), account=account, token=token)
        return PublishResult(
            external_post_id=str(post_urn),
            permalink=evidence.get("permalink"),
            metadata={
                "verified": True,
                "evidence": evidence,
                "permalink": f"https://www.linkedin.com/feed/update/{post_urn}",
            },
        )

    async def verify(self, external_post_id, *, account, token) -> dict[str, Any]:
        evidence = await self.request(
            "GET",
            f"{self.base}/v2/ugcPosts/{external_post_id}",
            headers=self._headers(token),
        )
        if str(evidence.get("id", "")) != external_post_id:
            raise PlatformRejected(
                "LinkedIn verification returned a different object", status=502, payload=evidence
            )
        evidence.setdefault("permalink", f"https://www.linkedin.com/feed/update/{external_post_id}")
        return evidence

    async def fetch_metrics(self, external_post_id, *, account, token) -> PostMetrics:
        measured: dict[str, int] = {}
        unavailable: list[str] = []
        try:
            social = await self.request(
                "GET",
                f"{self.base}/v2/socialActions/{external_post_id}",
                headers=self._headers(token),
            )
        except PlatformError:
            social = {}
        summary = social.get("likesSummary", {}).get("totalLikes")
        if summary is None:
            unavailable.append("likes")
        else:
            measured["likes"] = int(summary)
        comments = social.get("commentsSummary", {}).get("totalFirstLevelComments")
        if comments is None:
            unavailable.append("comments")
        else:
            measured["comments"] = int(comments)
        unavailable.extend(["impressions", "reach", "shares", "clicks"])
        return PostMetrics(
            platform=self.platform,
            external_post_id=external_post_id,
            measured=measured,
            unavailable=sorted(set(unavailable)),
            source="linkedin_v2_api",
        )


# -------------------------------------------------------------------------- X
class XClient(PlatformClient):
    platform = "x"

    @property
    def base(self) -> str:
        return self.provider.api_base

    def _headers(self, token: TokenRecord) -> dict[str, str]:
        headers = super()._headers(token)
        headers["Content-Type"] = "application/json"
        return headers

    async def publish(self, payload, *, account, token) -> PublishResult:
        text = _compose_caption(payload)
        if _first_media(payload, kind="image") or _first_media(payload, kind="video"):
            self._require_capability("image_post")
        self._require_capability("text_post")
        if len(text) > MAX_X_CHARS:
            raise PlatformRejected(
                f"X rejects posts longer than {MAX_X_CHARS} characters (got {len(text)})",
                status=422,
            )
        created = await self.request(
            "POST",
            f"{self.base}/tweets",
            json={"text": text},
            headers=self._headers(token),
            idempotent=False,
        )
        tweet_id = (created.get("data") or {}).get("id")
        if not tweet_id:
            raise PlatformRejected("X created no tweet id", status=502, payload=created)
        evidence = await self.verify(str(tweet_id), account=account, token=token)
        return PublishResult(
            external_post_id=str(tweet_id),
            permalink=f"https://x.com/i/web/status/{tweet_id}",
            metadata={"verified": True, "evidence": evidence},
        )

    async def verify(self, external_post_id, *, account, token) -> dict[str, Any]:
        evidence = await self.request(
            "GET",
            f"{self.base}/tweets/{external_post_id}",
            params={"tweet.fields": "created_at,text"},
            headers=self._headers(token),
        )
        if str((evidence.get("data") or {}).get("id", "")) != external_post_id:
            raise PlatformRejected(
                "X verification returned a different tweet", status=502, payload=evidence
            )
        return evidence

    async def fetch_metrics(self, external_post_id, *, account, token) -> PostMetrics:
        data = await self.request(
            "GET",
            f"{self.base}/tweets/{external_post_id}",
            params={"tweet.fields": "public_metrics"},
            headers=self._headers(token),
        )
        metrics = (data.get("data") or {}).get("public_metrics") or {}
        mapping = {
            "like_count": "likes",
            "reply_count": "comments",
            "retweet_count": "reposts",
            "quote_count": "quotes",
            "bookmark_count": "bookmarks",
        }
        measured = {out: int(metrics[key]) for key, out in mapping.items() if isinstance(metrics.get(key), int)}
        unavailable = ["impressions", "reach", "clicks"]
        return PostMetrics(
            platform=self.platform,
            external_post_id=external_post_id,
            measured=measured,
            unavailable=sorted(unavailable),
            source="x_api_v2",
        )


# --------------------------------------------------------------------- YouTube
class YouTubeClient(PlatformClient):
    platform = "youtube"

    @property
    def base(self) -> str:
        return self.provider.api_base

    async def publish(self, payload, *, account, token) -> PublishResult:
        video_url = _first_media(payload, kind="video")
        if not video_url:
            raise PlatformRejected(
                "YouTube requires a video asset (media_urls must contain an .mp4/.mov/.webm)",
                status=422,
            )
        self._require_capability("video_post")
        if not video_url.startswith("https://"):
            raise PlatformRejected(
                "YouTube uploads require an https media URL", status=422
            )

        metadata = {
            "snippet": {
                "title": (payload.headline or payload.body or "Untitled")[:100],
                "description": _compose_caption(payload)[:5000],
            },
            "status": {"privacyStatus": account.settings.get("privacy_status", "private")},
        }
        publish_at = account.settings.get("publish_at")
        if publish_at:
            metadata["status"]["publishAt"] = publish_at
            metadata["status"]["privacyStatus"] = "private"

        # 1) open a resumable session
        init = await self.http.request(
            "POST",
            f"{self.base}/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=metadata,
            headers={**self._headers(token), "X-Upload-Content-Type": "video/*"},
            timeout=self.policy.timeout_seconds,
        )
        if init.status_code >= 400:
            raise PlatformRejected(
                f"YouTube refused the upload session ({init.status_code})", status=init.status_code
            )
        location = init.headers.get("location")
        if not location:
            raise PlatformRejected("YouTube returned no upload location", status=502)

        # 2) stream the bytes
        media = await self.http.get(video_url, timeout=self.policy.timeout_seconds)
        if media.status_code >= 400:
            raise PlatformRejected(
                f"could not read the video asset ({media.status_code})", status=502
            )
        uploaded = await self.http.put(
            location,
            content=media.content,
            headers={"Content-Type": "video/*", "Content-Length": str(len(media.content))},
            timeout=self.policy.timeout_seconds,
        )
        if uploaded.status_code >= 400:
            raise PlatformRejected(
                f"YouTube rejected the upload ({uploaded.status_code})", status=uploaded.status_code
            )
        try:
            body = uploaded.json()
        except ValueError:
            body = {}
        video_id = body.get("id")
        if not video_id:
            # Upload accepted but no id: cannot verify, therefore not published.
            raise PlatformRejected("YouTube returned no video id", status=502, payload=body)
        evidence = await self.verify(str(video_id), account=account, token=token)
        return PublishResult(
            external_post_id=str(video_id),
            permalink=f"https://www.youtube.com/watch?v={video_id}",
            metadata={"verified": True, "evidence": evidence},
        )

    async def verify(self, external_post_id, *, account, token) -> dict[str, Any]:
        evidence = await self.request(
            "GET",
            f"{self.base}/youtube/v3/videos",
            params={"part": "snippet,status", "id": external_post_id},
            headers=self._headers(token),
        )
        items = evidence.get("items") or []
        if not items or str(items[0].get("id", "")) != external_post_id:
            raise PlatformRejected(
                "YouTube verification found no such video", status=502, payload=evidence
            )
        return evidence

    async def fetch_metrics(self, external_post_id, *, account, token) -> PostMetrics:
        data = await self.request(
            "GET",
            f"{self.base}/youtube/v3/videos",
            params={"part": "statistics", "id": external_post_id},
            headers=self._headers(token),
        )
        items = data.get("items") or []
        stats = (items[0].get("statistics") if items else {}) or {}
        mapping = {
            "viewCount": "views",
            "likeCount": "likes",
            "commentCount": "comments",
            "favoriteCount": "favorites",
        }
        measured: dict[str, int] = {}
        for key, out in mapping.items():
            raw = stats.get(key)
            if raw is None:
                continue
            try:
                measured[out] = int(raw)
            except (TypeError, ValueError):
                continue
        unavailable = [k for k in mapping.values() if k not in measured]
        unavailable.extend(["impressions", "reach", "clicks", "shares"])
        return PostMetrics(
            platform=self.platform,
            external_post_id=external_post_id,
            measured=measured,
            unavailable=sorted(set(unavailable)),
            source="youtube_data_api_v3",
        )


# --------------------------------------------------------------------- Reddit
class RedditClient(PlatformClient):
    platform = "reddit"

    @property
    def base(self) -> str:
        return self.provider.api_base

    async def publish(self, payload, *, account, token) -> PublishResult:
        subreddit = account.settings.get("subreddit") or account.handle.lstrip("r/")
        if not subreddit:
            raise PlatformRejected("Reddit publish needs a subreddit", status=422)
        link = account.settings.get("link_url")
        if link:
            kind = "link"
            data: dict[str, Any] = {
                "sr": subreddit,
                "title": (payload.headline or payload.body)[:300],
                "url": link,
                "kind": kind,
            }
            self._require_capability("image_post")
        else:
            data = {
                "sr": subreddit,
                "title": (payload.headline or payload.body)[:300],
                "text": _compose_caption(payload),
                "kind": "self",
            }
            self._require_capability("text_post")
        data.update({"api_type": "json", "resubmit": "false"})

        created = await self.request(
            "POST",
            f"{self.base}/api/submit",
            data=data,
            headers=self._headers(token),
            idempotent=False,
        )
        body = (created.get("json") or {}).get("data") or created.get("data") or {}
        fullname = body.get("id") or body.get("name")
        if not fullname:
            errors = (created.get("json") or {}).get("errors")
            raise PlatformRejected(
                f"Reddit created no post ({errors or 'no id returned'})",
                status=502,
                payload=created,
            )
        post_id = str(fullname)
        evidence = await self.verify(post_id, account=account, token=token)
        return PublishResult(
            external_post_id=post_id,
            permalink=evidence.get("permalink") or body.get("url"),
            metadata={"verified": True, "evidence": evidence},
        )

    async def verify(self, external_post_id, *, account, token) -> dict[str, Any]:
        fullname = external_post_id if external_post_id.startswith("t3_") else f"t3_{external_post_id}"
        evidence = await self.request(
            "GET",
            f"{self.base}/api/info",
            params={"id": fullname},
            headers=self._headers(token),
        )
        children = ((evidence.get("data") or {}).get("children")) or []
        if not children:
            raise PlatformRejected(
                "Reddit verification found no such post", status=502, payload=evidence
            )
        child = children[0].get("data") or {}
        return {
            "id": child.get("name") or fullname,
            "permalink": f"https://www.reddit.com{child.get('permalink', '')}",
            "score": child.get("score"),
        }

    async def fetch_metrics(self, external_post_id, *, account, token) -> PostMetrics:
        fullname = external_post_id if external_post_id.startswith("t3_") else f"t3_{external_post_id}"
        data = await self.request(
            "GET",
            f"{self.base}/api/info",
            params={"id": fullname},
            headers=self._headers(token),
        )
        children = ((data.get("data") or {}).get("children")) or []
        child = (children[0].get("data") if children else {}) or {}
        measured: dict[str, int] = {}
        for src, out in (("score", "score"), ("num_comments", "comments"), ("upvote_ratio", "upvote_ratio")):
            value = child.get(src)
            if isinstance(value, (int, float)):
                measured[out] = int(value * 100) if out == "upvote_ratio" else int(value)
        unavailable = [k for k in ("score", "comments") if k not in measured]
        unavailable.extend(["impressions", "reach", "clicks", "shares"])
        return PostMetrics(
            platform=self.platform,
            external_post_id=external_post_id,
            measured=measured,
            unavailable=sorted(set(unavailable)),
            source="reddit_api",
        )


CLIENTS: dict[str, type[PlatformClient]] = {
    "facebook": FacebookClient,
    "instagram": InstagramClient,
    "linkedin": LinkedInClient,
    "x": XClient,
    "youtube": YouTubeClient,
    "reddit": RedditClient,
}


def build_client(
    platform: str,
    settings: Any,
    http: httpx.AsyncClient,
    *,
    policy: RetryPolicy | None = None,
) -> PlatformClient:
    try:
        cls = CLIENTS[platform]
    except KeyError as exc:
        raise PlatformRejected(f"no transport for platform {platform!r}", status=422) from exc
    return cls(settings, http, policy=policy)
