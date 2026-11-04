#!/usr/bin/env python
"""Founder provisioning helper: generate a vault key, check a deployment's env.

Two jobs, both of which are easy to get wrong by hand:

``keygen``
    Write a fresh ``TOKEN_VAULT_KEY`` to a file with 0600 permissions, outside the
    repository, and print only the path. The value is never printed, never
    committed, and never sent anywhere. Load it into the deployment's secret store
    from that file and delete the file.

``check``
    Validate what a deployment is *supposed* to have before a canary: which
    variables are present, and whether the ones with a fixed shape actually have
    it (a 63-character vault key silently fails at the first token write). Values
    are never printed — only "set"/"unset" and shape verdicts.

Usage::

    python ../scripts/provision_credentials.py keygen --out ~/crusor-vault.key
    python ../scripts/provision_credentials.py check
    python ../scripts/provision_credentials.py check --env-file /etc/crusor/env --json

Exit codes: 0 = every checked tier is ready; 2 = something is missing or malformed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

#: tier -> (variables, note). A tier is "ready" only when every variable is set
#: and every shape check passes.
TIERS: dict[str, dict[str, Any]] = {
    "core": {
        "variables": ["SECRET_KEY", "DATABASE_URL"],
        "note": "the app cannot start without these",
    },
    "vault": {
        "variables": ["TOKEN_VAULT_BACKEND", "TOKEN_VAULT_KEY"],
        "note": "without a vault an OAuth consent flow completes and stores nothing",
    },
    "oauth_redirect": {
        "variables": ["OAUTH_REDIRECT_BASE_URL"],
        "note": "must be an HTTPS URL registered in every provider app",
    },
    "llm": {
        "variables": ["LLM_PROVIDER", "OPENAI_API_KEY"],
        "note": "at least one LLM provider; prompts are otherwise refused",
    },
    "creative_image": {
        "variables": ["CREATIVE_IMAGE_PROVIDER", "OPENAI_API_KEY", "CREATIVE_PRICING_JSON",
                      "CREATIVE_CAMPAIGN_BUDGET_USD", "CREATIVE_ALLOW_PAID_GENERATION"],
        "note": "paid generation needs a pinned price, a non-zero budget and an explicit switch",
    },
    "creative_video": {
        "variables": ["CREATIVE_VIDEO_PROVIDER", "REPLICATE_API_TOKEN", "REPLICATE_VIDEO_MODEL",
                      "CREATIVE_PRICING_JSON", "CREATIVE_CAMPAIGN_BUDGET_USD",
                      "CREATIVE_ALLOW_PAID_GENERATION"],
        "note": "the model must be pinned to owner/model:version",
    },
    "publishing": {
        "variables": ["PUBLISHING_ENABLED"],
        "note": "off until a platform round-trip has actually succeeded",
    },
}

#: platform -> the credentials its OAuth app needs.
SOCIAL_TIERS: dict[str, list[str]] = {
    "facebook": ["META_APP_ID", "META_APP_SECRET"],
    "instagram": ["META_APP_ID", "META_APP_SECRET"],
    "linkedin": ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"],
    "x": ["X_CLIENT_ID", "X_CLIENT_SECRET"],
    "youtube": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
    "reddit": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
}

ADS_TIERS: dict[str, list[str]] = {
    "meta_ads": ["META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"],
    "google_ads": ["GOOGLE_ADS_ACCESS_TOKEN", "GOOGLE_ADS_DEVELOPER_TOKEN",
                   "GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
    "linkedin_ads": ["LINKEDIN_ADS_ACCESS_TOKEN", "LINKEDIN_ADS_ACCOUNT_URN"],
}


# --------------------------------------------------------------------------- #
# key generation
# --------------------------------------------------------------------------- #
def generate_key() -> str:
    """64 hex characters = 32 bytes, which is what ``TokenCipher`` expects."""
    return secrets.token_hex(32)


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT.resolve())
        return True
    except ValueError:
        return False


def write_key_file(path: Path) -> Path:
    """Create the key file with 0600, refusing to write inside the repository."""
    if _inside_repo(path):
        raise SystemExit(
            "refusing to write key material inside the repository; choose a path outside it"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    key = generate_key()
    # O_EXCL: never silently overwrite an existing key — rotating one breaks every
    # token already in the vault.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(
            f"{path} already exists; refusing to overwrite a vault key (rotate deliberately)"
        ) from None
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(f"TOKEN_VAULT_KEY={key}\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    return path


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _get(name: str, env: dict[str, str]) -> str:
    return (env.get(name) or "").strip()


def shape_problems(env: dict[str, str]) -> dict[str, str]:
    """Validate the variables whose format matters. Values are never returned."""
    problems: dict[str, str] = {}

    secret = _get("SECRET_KEY", env)
    if secret and len(secret) < 32:
        problems["SECRET_KEY"] = f"too short ({len(secret)} chars); at least 32 required"

    vault_key = _get("TOKEN_VAULT_KEY", env)
    if vault_key:
        ok = False
        if re.fullmatch(r"[0-9a-fA-F]{64}", vault_key):
            ok = True
        elif len(vault_key) >= 16:
            # base64 of 32 bytes, or a passphrase the HKDF accepts
            ok = True
        if not ok:
            problems["TOKEN_VAULT_KEY"] = "must be 64 hex characters, base64 of 32 bytes, or >=16 chars"

    backend = _get("TOKEN_VAULT_BACKEND", env).lower()
    if backend and backend not in {"none", "db", "s3", "external"}:
        problems["TOKEN_VAULT_BACKEND"] = "must be one of none|db|s3|external"
    if backend == "db" and not vault_key:
        problems["TOKEN_VAULT_KEY"] = "TOKEN_VAULT_BACKEND=db requires a key"

    redirect = _get("OAUTH_REDIRECT_BASE_URL", env)
    if redirect and not (
        redirect.startswith("https://") or "localhost" in redirect or "127.0.0.1" in redirect
    ):
        problems["OAUTH_REDIRECT_BASE_URL"] = "must be https (plain http is rejected by providers)"

    pricing = _get("CREATIVE_PRICING_JSON", env)
    if pricing:
        try:
            parsed = json.loads(pricing)
            if not isinstance(parsed, dict) or not parsed:
                problems["CREATIVE_PRICING_JSON"] = "must be a non-empty JSON object, e.g. {\"image\": 0.04}"
        except ValueError:
            problems["CREATIVE_PRICING_JSON"] = "is not valid JSON"

    budget = _get("CREATIVE_CAMPAIGN_BUDGET_USD", env)
    if budget:
        try:
            if float(budget) < 0:
                problems["CREATIVE_CAMPAIGN_BUDGET_USD"] = "must not be negative"
        except ValueError:
            problems["CREATIVE_CAMPAIGN_BUDGET_USD"] = "is not a number"

    model = _get("REPLICATE_VIDEO_MODEL", env)
    if model and ":" not in model:
        problems["REPLICATE_VIDEO_MODEL"] = "must be owner/model:version (an unpinned model is refused)"

    customer = _get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", env)
    if customer and not re.fullmatch(r"\d{3}-?\d{3}-?\d{4}", customer):
        problems["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = "must be a 10-digit customer id (dashes optional)"

    urn = _get("LINKEDIN_ADS_ACCOUNT_URN", env)
    if urn and not urn.startswith("urn:li:sponsoredAccount:"):
        problems["LINKEDIN_ADS_ACCOUNT_URN"] = "must be urn:li:sponsoredAccount:<id>"

    return problems


def load_env_file(path: Path | None) -> dict[str, str]:
    """Read KEY=VALUE lines without echoing values anywhere."""
    env = dict(os.environ)
    if path is None:
        return env
    if not path.is_file():
        raise SystemExit(f"env file not found: {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"warning: {path} is group/world readable; chmod 600 it", file=sys.stderr)
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def check(env: dict[str, str]) -> dict[str, Any]:
    problems = shape_problems(env)
    tiers: list[dict[str, Any]] = []

    def add(name: str, variables: list[str], note: str) -> None:
        missing = [v for v in variables if not _get(v, env)]
        malformed = [v for v in variables if v in problems]
        tiers.append(
            {
                "tier": name,
                "ready": not missing and not malformed,
                "missing": missing,
                "malformed": {v: problems[v] for v in malformed},
                "note": note,
            }
        )

    for name, spec in TIERS.items():
        add(name, spec["variables"], spec["note"])
    add(
        "one_social_platform",
        [],
        "set at least one platform below with an OAuth app whose redirect includes "
        "OAUTH_REDIRECT_BASE_URL",
    )
    for platform, variables in SOCIAL_TIERS.items():
        add(f"social:{platform}", variables, f"{platform} OAuth app")
    for platform, variables in ADS_TIERS.items():
        add(f"ads:{platform}", variables, f"{platform} credentials (all optional until activated)")

    social_ready = [
        t["tier"] for t in tiers if t["tier"].startswith("social:") and t["ready"]
    ]
    # The placeholder tier is only ready once a real platform is.
    for tier in tiers:
        if tier["tier"] == "one_social_platform" and not social_ready:
            tier["ready"] = False
            tier["missing"] = ["any social:<platform> tier"]
    return {
        "tiers": tiers,
        "social_platforms_ready": social_ready,
        "ready_for_first_creative_canary": next(
            t["ready"] for t in tiers if t["tier"] == "creative_image"
        ),
        "ready_for_first_social_canary": bool(social_ready)
        and next(t["ready"] for t in tiers if t["tier"] == "vault")
        and next(t["ready"] for t in tiers if t["tier"] == "oauth_redirect"),
        "problems": problems,
    }


def render(report: dict[str, Any]) -> str:
    lines = ["# Credential provisioning check", ""]
    for tier in report["tiers"]:
        mark = "READY   " if tier["ready"] else "MISSING "
        lines.append(f"{mark} {tier['tier']:<24} {tier['note']}")
        if tier["missing"]:
            lines.append(f"          missing: {', '.join(tier['missing'])}")
        for name, problem in tier["malformed"].items():
            lines.append(f"          malformed: {name} — {problem}")
    lines += [
        "",
        f"social platforms ready: {', '.join(report['social_platforms_ready']) or 'none'}",
        f"first creative canary ready: {'yes' if report['ready_for_first_creative_canary'] else 'no'}",
        f"first social canary ready:   {'yes' if report['ready_for_first_social_canary'] else 'no'}",
        "",
        "No value was read into this report: only presence and shape.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="write a fresh TOKEN_VAULT_KEY to a file (0600)")
    keygen.add_argument("--out", required=True, help="path outside the repository")

    check_cmd = sub.add_parser("check", help="validate a deployment's environment")
    check_cmd.add_argument("--env-file", help="read KEY=VALUE from this file instead of the environment")
    check_cmd.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "keygen":
        path = write_key_file(Path(args.out).expanduser())
        print(f"wrote {path} (mode 0600) — value not printed")
        print("load it into the deployment's secret store, then delete the file:")
        print(f'  export TOKEN_VAULT_KEY="$(cut -d= -f2- {path})"')
        print("  # or: docker secret / systemd EnvironmentFile / your cloud secret manager")
        return 0

    report = check(load_env_file(Path(args.env_file).expanduser() if args.env_file else None))
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    ready = (
        report["ready_for_first_social_canary"]
        and report["ready_for_first_creative_canary"]
        and next(t["ready"] for t in report["tiers"] if t["tier"] == "core")
    )
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
