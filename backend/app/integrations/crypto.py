"""Envelope encryption for OAuth credentials.

Platform tokens are the highest-value secret this application holds: a Meta or
LinkedIn access token can post as the brand. They are therefore never stored in
plaintext and never placed in a column the API serialises.

Design:

* **AES-256-GCM** with a 96-bit random nonce per record and associated data that
  binds the ciphertext to its owner (brand id, platform, account key). Swapping
  one brand's ciphertext into another brand's row therefore fails to decrypt —
  the associated data will not match.
* The key is derived with **HKDF-SHA256** from ``TOKEN_VAULT_KEY``. A caller can
  supply a 32-byte hex key, a 32-byte base64 key, or any long passphrase; all are
  normalised to 32 bytes. Keys shorter than :data:`MIN_KEY_MATERIAL_CHARS` are
  rejected instead of being stretched, because a short key cannot be rescued into
  a trustworthy one.
* Without ``TOKEN_VAULT_KEY`` the vault is **unavailable**, not "encrypted with a
  default". A development default would be indistinguishable from a real key and
  would silently produce credentials that anyone with the repository can read.

Ciphertext format: ``v1:<base64 nonce>:<base64 ciphertext+tag>``. The version
prefix makes a future key rotation an explicit, detectable change.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "v1"
NONCE_BYTES = 12
KEY_BYTES = 32
#: Shortest accepted passphrase. Anything shorter is refused rather than stretched.
MIN_KEY_MATERIAL_CHARS = 32
_HKDF_INFO = b"crusor.token-vault.v1"


class TokenCipherError(Exception):
    """Base class for vault cryptography failures."""


class TokenCipherUnavailable(TokenCipherError):
    """No usable key material is configured."""


class TokenDecryptionError(TokenCipherError):
    """Ciphertext is corrupt, was tampered with, or belongs to another owner."""


def _hkdf(material: bytes, salt: bytes | None = None) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        info=_HKDF_INFO,
    ).derive(material)


def _decode_key_material(raw: str) -> bytes:
    """Accept hex, base64 or passphrase key material."""
    candidate = raw.strip()
    if not candidate:
        raise TokenCipherUnavailable("TOKEN_VAULT_KEY is empty.")
    # 64 hex chars -> 32 bytes
    if len(candidate) == KEY_BYTES * 2:
        try:
            return bytes.fromhex(candidate)
        except ValueError:
            pass
    # base64 that decodes to 32 bytes
    try:
        decoded = base64.b64decode(candidate, validate=True)
        if len(decoded) == KEY_BYTES:
            return decoded
    except (binascii.Error, ValueError):
        pass
    if len(candidate) < MIN_KEY_MATERIAL_CHARS:
        raise TokenCipherUnavailable(
            f"TOKEN_VAULT_KEY must be at least {MIN_KEY_MATERIAL_CHARS} characters "
            "(or 32 bytes hex/base64)."
        )
    return candidate.encode("utf-8")


@dataclass(frozen=True)
class AssociatedData:
    """Binds a ciphertext to the identity of its owner.

    Decryption with different associated data fails, so a copied ciphertext
    cannot be replayed into another account's row.
    """

    brand_profile_id: str
    platform: str
    account_key: str

    def to_bytes(self) -> bytes:
        return f"{self.brand_profile_id}|{self.platform}|{self.account_key}".encode("utf-8")


class TokenCipher:
    """AES-256-GCM envelope encryption with a derived key."""

    def __init__(self, key_material: str) -> None:
        self._key = _hkdf(_decode_key_material(key_material))
        self._aead = AESGCM(self._key)

    @classmethod
    def from_settings(cls, key_material: str | None) -> TokenCipher:
        if not key_material or not key_material.strip():
            raise TokenCipherUnavailable(
                "TOKEN_VAULT_KEY is not configured; OAuth token storage is unavailable."
            )
        return cls(key_material)

    def encrypt(self, plaintext: str, aad: AssociatedData) -> str:
        if not plaintext:
            raise TokenCipherError("Refusing to encrypt an empty secret.")
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad.to_bytes())
        return ":".join(
            (
                VERSION,
                base64.b64encode(nonce).decode("ascii"),
                base64.b64encode(ciphertext).decode("ascii"),
            )
        )

    def decrypt(self, sealed: str, aad: AssociatedData) -> str:
        try:
            version, b64_nonce, b64_ciphertext = sealed.split(":", 2)
        except ValueError as exc:
            raise TokenDecryptionError("malformed ciphertext") from exc
        if version != VERSION:
            raise TokenDecryptionError(f"unsupported ciphertext version {version!r}")
        try:
            nonce = base64.b64decode(b64_nonce, validate=True)
            ciphertext = base64.b64decode(b64_ciphertext, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TokenDecryptionError("malformed ciphertext") from exc
        try:
            return self._aead.decrypt(nonce, ciphertext, aad.to_bytes()).decode("utf-8")
        except InvalidTag as exc:
            raise TokenDecryptionError(
                "ciphertext failed authentication (wrong key, tampered, or foreign owner)"
            ) from exc

    def rotate_key(self, key_material: str) -> TokenCipher:
        """Derive a cipher bound to a new key without mutating this one."""
        return TokenCipher(key_material)
