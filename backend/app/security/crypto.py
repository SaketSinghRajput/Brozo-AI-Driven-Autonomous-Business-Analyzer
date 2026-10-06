"""Connector credential encryption at rest (TASK-SEC-009).

AES-256-GCM from `cryptography` (PyCA; Apache-2.0 OR BSD-3-Clause): authenticated encryption,
so a wrong key or any modified byte fails instead of returning garbage. Random 96-bit nonce per
message. `context` (e.g. "connector:<id>") is bound as associated data, so a ciphertext copied
onto another connector row does not decrypt. The key comes from `APP_SECURITY__CREDENTIAL_KEY`
(base64url, 32 bytes) and never from the database.

Stored form: `v1.<base64url(nonce || ciphertext_and_tag)>`. The version prefix leaves room for
key rotation (add `v2` with a key id) without guessing formats.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import ConfigError, SecurityConfig
from app.core.errors import SecurityError

_VERSION = "v1."
_NONCE_BYTES = 12


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ConfigError("credential key must be 32 bytes (AES-256)")
        self._aead = AESGCM(key)

    @classmethod
    def from_config(cls, cfg: SecurityConfig) -> CredentialCipher:
        if cfg.credential_key is None:
            raise ConfigError("APP_SECURITY__CREDENTIAL_KEY is not configured")
        try:
            key = base64.urlsafe_b64decode(cfg.credential_key.get_secret_value())
        except binascii.Error, ValueError:
            raise ConfigError("credential key is not valid base64url") from None
        return cls(key)

    @staticmethod
    def generate_key() -> str:
        return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()

    def encrypt(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return _VERSION + base64.urlsafe_b64encode(nonce + sealed).decode()

    def decrypt(self, stored: str, *, context: str) -> str:
        if not stored.startswith(_VERSION):
            raise SecurityError("unsupported credential format")
        try:
            raw = base64.urlsafe_b64decode(stored.removeprefix(_VERSION))
            plain = self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode())
        except InvalidTag, binascii.Error, ValueError:
            raise SecurityError("credential could not be decrypted") from None
        return plain.decode()
