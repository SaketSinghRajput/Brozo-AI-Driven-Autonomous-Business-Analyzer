"""Prefixed, time-ordered identifiers (`ds_…`, `dsv_…`, `ev_…`).

Uses stdlib UUIDv7 (Python 3.14): time-ordered like the ULIDs named in design.md section 6,
without an extra dependency.
"""

import re
import uuid
from typing import Protocol

_PREFIX = re.compile(r"^[a-z]{1,8}$")


class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


def _check(prefix: str) -> None:
    if not _PREFIX.match(prefix):
        raise ValueError(f"invalid id prefix {prefix!r}")


class UuidV7Ids:
    def new(self, prefix: str) -> str:
        _check(prefix)
        return f"{prefix}_{uuid.uuid7().hex}"


class SequentialIds:
    """Deterministic ids for tests: ds_000001, ds_000002, …"""

    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        _check(prefix)
        self._n += 1
        return f"{prefix}_{self._n:06d}"
