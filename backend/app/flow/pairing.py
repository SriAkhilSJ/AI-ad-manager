"""Pairing credential generator and standalone bridge entry point (Phase 3C.1).

Usage::

    python -m app.flow.pairing --print          # secrets for .env (never logged)
    python -m app.flow.bridge                    # run the loopback bridge

The secrets are printed **once** to stdout for the operator to paste into their
local ``.env``. They are never written to a file, never logged by the bridge, and
never returned by an API. Anyone holding them can drive the paired browser, so
they belong in the user's local environment only.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from app.flow.security import (
    PAIRING_SECRET_BYTES,
    entropy_bits,
    generate_pairing_id,
    generate_pairing_secret,
    secret_fingerprint,
)


@dataclass(frozen=True)
class PairingCredential:
    pairing_id: str
    pairing_secret: str

    @property
    def secret_bits(self) -> float:
        return entropy_bits(self.pairing_secret)

    @property
    def fingerprint(self) -> str:
        return secret_fingerprint(self.pairing_secret)

    def env_block(self) -> str:
        return "\n".join(
            [
                "# --- Crusor Flow bridge (local only; do not commit) ---",
                "FLOW_BRIDGE_ENABLED=true",
                "FLOW_BRIDGE_HOST=127.0.0.1",
                f"FLOW_BRIDGE_PORT=8793",
                "FLOW_BRIDGE_WS_PORT=8794",
                f"FLOW_PAIRING_ID={self.pairing_id}",
                f"FLOW_PAIRING_SECRET={self.pairing_secret}",
            ]
        )


def generate_pairing() -> PairingCredential:
    return PairingCredential(
        pairing_id=generate_pairing_id(),
        pairing_secret=generate_pairing_secret(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crusor Flow bridge utilities")
    parser.add_argument("--print", action="store_true", help="print a fresh pairing block")
    parser.add_argument(
        "--fingerprint",
        default="",
        help="print the non-reversible fingerprint of an existing secret",
    )
    args = parser.parse_args(argv)

    if args.fingerprint:
        print(secret_fingerprint(args.fingerprint))
        return 0
    if args.print:
        credential = generate_pairing()
        print(credential.env_block())
        print(
            f"\n# pairing_id and secret: {PAIRING_SECRET_BYTES * 8}-bit secret, "
            f"fingerprint {credential.fingerprint}",
            file=sys.stderr,
        )
        print(
            "# The extension must send this secret in its hello frame; the bridge "
            "refuses every unpaired connection.",
            file=sys.stderr,
        )
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
