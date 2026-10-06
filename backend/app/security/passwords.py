"""Argon2id password hashing (ADR-0013; argon2-cffi 25.1.0, MIT).

`verify` always runs one Argon2 verification — against a dummy hash when the user is unknown or
has no password — so "unknown user" and "wrong password" take the same time.
"""

import secrets
from functools import cached_property

from argon2 import PasswordHasher as _Argon2
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import SecurityConfig

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # bounds hashing work per request


def check_new_password(password: str) -> None:
    """Policy for passwords being set (OWASP: length over composition rules)."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")


class PasswordHasher:
    def __init__(self, cfg: SecurityConfig) -> None:
        self._argon2 = _Argon2(
            time_cost=cfg.argon2_time_cost,
            memory_cost=cfg.argon2_memory_kib,
            parallelism=cfg.argon2_parallelism,
            type=Type.ID,
        )

    @cached_property
    def _dummy_hash(self) -> str:
        return self._argon2.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        return self._argon2.hash(password)

    def verify(self, stored_hash: str | None, password: str) -> bool:
        try:
            ok = self._argon2.verify(stored_hash or self._dummy_hash, password)
        except VerificationError, InvalidHashError:
            return False
        return ok and stored_hash is not None

    def needs_rehash(self, stored_hash: str) -> bool:
        return self._argon2.check_needs_rehash(stored_hash)
