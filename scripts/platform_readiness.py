#!/usr/bin/env python
"""Generate the six-platform readiness matrix and the founder checklist.

The matrix has exactly five columns, and the last three are *evidence* columns:
they can only be filled by an actual sandbox or production call, which this
repository has never made. The script derives everything else from code and from
the current environment, so a founder can see precisely what is implemented, what
has only been proven against mocks, and what they must supply.

Usage::

    python ../scripts/platform_readiness.py                 # print
    python ../scripts/platform_readiness.py --write         # also write docs/25-...

Exit code is 0 always: this is a report, not a gate. Gates live in tests.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

#: platform -> the OAuth app credentials and the review/approval a human must
#: obtain. Kept here so the checklist cannot drift from the adapter list.
PLATFORM_REQUIREMENTS: dict[str, dict[str, object]] = {
    "facebook": {
        "label": "Facebook Page",
        "credentials": ["META_APP_ID", "META_APP_SECRET"],
        "scopes": ["pages_manage_posts", "pages_read_engagement", "read_insights"],
        "approval": "Meta app review for pages_manage_posts + a Page the app is allowed to post to",
        "publish_env": "PUBLISHING_ENABLED=true",
    },
    "instagram": {
        "label": "Instagram Business",
        "credentials": ["META_APP_ID", "META_APP_SECRET"],
        "scopes": ["instagram_basic", "instagram_content_publish", "instagram_manage_insights"],
        "approval": "Meta app review for instagram_content_publish + a Business account linked to a Page",
        "publish_env": "PUBLISHING_ENABLED=true",
    },
    "linkedin": {
        "label": "LinkedIn Page",
        "credentials": ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"],
        "scopes": ["w_organization_social", "r_organization_social", "rw_organization_admin"],
        "approval": "LinkedIn Marketing Developer Platform access for the app",
        "publish_env": "PUBLISHING_ENABLED=true",
    },
    "x": {
        "label": "X (Twitter)",
        "credentials": ["X_CLIENT_ID", "X_CLIENT_SECRET"],
        "scopes": ["tweet.write", "tweet.read", "users.read", "offline.access"],
        "approval": "X developer app with write access (a paid API tier as of 2025)",
        "publish_env": "PUBLISHING_ENABLED=true",
    },
    "youtube": {
        "label": "YouTube",
        "credentials": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
        "approval": "Google OAuth consent screen verification for the YouTube upload scope",
        "publish_env": "PUBLISHING_ENABLED=true",
    },
    "reddit": {
        "label": "Reddit",
        "credentials": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
        "scopes": ["submit", "read", "identity"],
        "approval": "Reddit app registration with a script/web app type and a subreddit you moderate",
        "publish_env": "PUBLISHING_ENABLED=true",
    },
}

ADS_REQUIREMENTS = {
    "meta_ads": {
        "label": "Meta Marketing API",
        "credentials": ["META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"],
        "approval": "ads_management permission on the ad account + a funded payment method",
        "api_version": "Graph v21.0",
    },
    "google_ads": {
        "label": "Google Ads API",
        "credentials": [
            "GOOGLE_ADS_ACCESS_TOKEN",
            "GOOGLE_ADS_DEVELOPER_TOKEN",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        ],
        "approval": "A developer token approved for the account + a customer id with billing",
        "api_version": "REST v17",
    },
    "linkedin_ads": {
        "label": "LinkedIn Ads API",
        "credentials": ["LINKEDIN_ADS_ACCESS_TOKEN", "LINKEDIN_ADS_ACCOUNT_URN"],
        "approval": "Advertising API access on the LinkedIn app + a sponsored account",
        "api_version": "202405",
    },
}


def _env_var(name: str) -> str:
    return os.environ.get(name, "")


def _present(name: str) -> bool:
    return bool(_env_var(name).strip())


def build_report() -> dict:
    from app.core.config import Settings
    from app.integrations.ads import ads_transport_report
    from app.integrations.platforms import CAPABILITIES

    settings = Settings()
    publish_enabled = bool(getattr(settings, "publishing_enabled", False))
    vault = (getattr(settings, "token_vault_backend", "none") or "none").lower()
    vault_key = bool(getattr(settings, "token_vault_key", None))

    rows = []
    for platform, caps in CAPABILITIES.items():
        req = PLATFORM_REQUIREMENTS[platform]
        missing = [name for name in req["credentials"] if not _present(name)]
        implemented = [name for name, cap in caps.items() if cap.implemented]
        unusable = [name for name, cap in caps.items() if not cap.implemented]
        rows.append(
            {
                "platform": platform,
                "label": req["label"],
                "implemented": "yes" if implemented else "no",
                "implemented_capabilities": implemented,
                "not_implemented": unusable,
                "mock_tested": "yes (httpx.MockTransport suites)",
                "sandbox_verified": "no",
                "production_verified": "no",
                "blockers": [
                    *([] if not missing else [f"missing credentials: {', '.join(missing)}"]),
                    *([] if vault == "db" and vault_key else ["token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)"]),
                    *([] if publish_enabled else ["PUBLISHING_ENABLED is false"]),
                    *([] if not missing and publish_enabled else [str(req["approval"])]),
                ],
                "scopes": req["scopes"],
            }
        )

    ads = []
    for row in ads_transport_report(settings):
        req = ADS_REQUIREMENTS[row["platform"]]
        missing = [name for name in req["credentials"] if not _present(name)]
        ads.append(
            {
                "platform": row["platform"],
                "label": req["label"],
                "api_version": req["api_version"],
                "implemented": "yes" if row["implemented"] else "no",
                "configured": "yes" if row["available"] else "no",
                "mock_tested": "yes (httpx.MockTransport suites)",
                "sandbox_verified": "no",
                "production_verified": "no",
                "missing": missing,
                "approval": req["approval"],
            }
        )

    from app.creative.providers import creative_provider_status

    creative = creative_provider_status(settings)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": getattr(settings, "environment", "unknown"),
        "publishing_enabled": publish_enabled,
        "token_vault": {"backend": vault, "key_present": vault_key},
        "platforms": rows,
        "ads": ads,
        "creative": creative,
        "flow": "disabled — source, signed release and installed artifact remain unverifiable",
    }


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add("# 25 — Platform readiness and founder checklist")
    add("")
    add(f"Generated {report['generated_at']} from code + the current environment "
        f"(`ENVIRONMENT={report['environment']}`).")
    add("")
    add("**How to read the columns.** `implemented` is what the code does. "
        "`mock-tested` means an automated suite exercises it against "
        "`httpx.MockTransport`. `sandbox-verified` and `production-verified` can "
        "only be filled by a real call against a real developer account — this "
        "repository has not made one, so both are `no` for every row. A feature is "
        "not production-ready because its code exists.")
    add("")
    add("## Six platforms")
    add("")
    add("| Platform | Implemented | Mock-tested | Sandbox-verified | Production-verified |")
    add("| --- | --- | --- | --- | --- |")
    for row in report["platforms"]:
        add(
            f"| {row['label']} | {row['implemented']} | {row['mock_tested']} | "
            f"{row['sandbox_verified']} | {row['production_verified']} |"
        )
    add("")
    add("## Paid media")
    add("")
    add("| Platform | API | Implemented | Credentials present | Mock-tested | Sandbox-verified | Production-verified |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for row in report["ads"]:
        add(
            f"| {row['label']} | {row['api_version']} | {row['implemented']} | "
            f"{row['configured']} | {row['mock_tested']} | {row['sandbox_verified']} | "
            f"{row['production_verified']} |"
        )
    add("")
    add("## Creative generation")
    add("")
    creative = report["creative"]
    add(f"- Provider set: `{creative.get('provider')}`")
    add(f"- State: `{creative.get('status')}` — {creative.get('note', '')}")
    if creative.get("blocked_by"):
        add("- Blocked by:")
        for item in creative["blocked_by"]:
            add(f"  - {item}")
    add(f"- Flow: {report['flow']}")
    add("")
    add("## Founder checklist")
    add("")
    add("Everything below is a *human* action. Nothing in this repository can do it, "
        "and nothing in this repository pretends it happened.")
    add("")
    add("### A. Core platform")
    add("")
    add("- [ ] Provision PostgreSQL 16 with the `vector` extension and run "
        "`alembic upgrade head`.")
    add("- [ ] Provision Redis and run a Celery worker "
        "(`celery -A app.tasks.celery_app:celery_app worker`).")
    step = 1
    for row in report["platforms"]:
        add("")
        add(f"### B{step}. {row['label']}")
        step += 1
        add("")
        add(f"- Scopes needed: {', '.join('`' + s + '`' for s in row['scopes'])}")
        add(f"- App approval: {PLATFORM_REQUIREMENTS[row['platform']]['approval']}")
        if row["blockers"]:
            add("- Missing now:")
            for blocker in row["blockers"]:
                add(f"  - [ ] {blocker}")
        add(f"- [ ] Set the credentials as deployment secrets, connect the account via "
            f"`POST /api/v1/social/oauth/{row['platform']}/start`, then run "
            f"`POST /api/v1/social/accounts/{{id}}/verify` and record the result.")
    add("")
    add("### C. Paid media")
    add("")
    for row in report["ads"]:
        add(f"- [ ] {row['label']} ({row['api_version']}): supply "
            f"{', '.join('`' + c + '`' for c in row['missing']) or 'credentials'}; "
            f"approval: {row['approval']}.")
    add("- [ ] Run `POST /api/v1/ads/campaigns/{id}/validate-account` and paste the "
        "result into the release record.")
    add("- [ ] Set `ADS_MAX_AUTHORIZED_SPEND_CENTS` to the real ceiling per "
        "authorisation.")
    add("")
    add("### D. Creative")
    add("")
    add("- [ ] Choose an image provider (`CREATIVE_IMAGE_PROVIDER=openai`) and/or a "
        "video provider (`CREATIVE_VIDEO_PROVIDER=replicate`).")
    add("- [ ] Supply the provider key(s) and pin `REPLICATE_VIDEO_MODEL` to an "
        "`owner/model:version` you have approved.")
    add("- [ ] Enter the contracted price per generation in `CREATIVE_PRICING_JSON` "
        "(`{\"image\": 0.0, \"video\": 0.0}`). Unknown pricing is refused, never "
        "treated as zero.")
    add("- [ ] Set `CREATIVE_CAMPAIGN_BUDGET_USD` to a non-zero budget and only then "
        "`CREATIVE_ALLOW_PAID_GENERATION=true`.")
    add("- [ ] Confirm Google Flow stays disabled: its source, signed release and "
        "installed artifact are still unverifiable.")
    add("")
    add("### E. Publishing and spending")
    add("")
    add("- [ ] Set `PUBLISHING_ENABLED=true` only after a platform's sandbox post "
        "round-trip succeeds.")
    add("- [ ] Keep `FLOW_LIVE_GENERATION_ENABLED=false`.")
    add("- [ ] Decide the founder authorisation policy for ad spend "
        "(`ADS_MAX_AUTHORIZED_SPEND_CENTS`, per-campaign ceilings).")
    add("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write docs/25-platform-readiness.md")
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = parser.parse_args()

    report = build_report()
    markdown = render_markdown(report)
    if args.json:
        import json

        print(json.dumps(report, indent=2, default=str))
    else:
        print(markdown)
    if args.write:
        target = ROOT / "docs" / "25-platform-readiness.md"
        target.write_text(markdown, "utf-8")
        print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
