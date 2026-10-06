"""File storage port and local filesystem adapter (TASK-STORE-001).

Keys are built from generated IDs, never from user filenames. Writes go to a temp file, are
fsynced, then moved into place; a crash mid-write never leaves a visible final file.
Stored objects are immutable: writing an existing key is an error.
"""

import os
import re
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.errors import ApplicationError, SecurityError

_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class StorageError(ApplicationError):
    code, http_status = "STORAGE_ERROR", 500


class Storage(Protocol):
    def writer(self, key: str) -> AbstractContextManager[BinaryIO]: ...
    def open_read(self, key: str) -> BinaryIO: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._tmp = self._root / ".tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        parts = key.split("/")
        if any(not _SEGMENT.match(p) for p in parts):
            raise SecurityError("Invalid storage key.")
        path = (self._root / Path(*parts)).resolve()
        if not path.is_relative_to(self._root) or path.is_relative_to(self._tmp):
            raise SecurityError("Invalid storage key.")
        return path

    @contextmanager
    def writer(self, key: str) -> Iterator[BinaryIO]:
        dest = self._path(key)
        if dest.exists():
            raise StorageError("Stored objects are immutable; key already exists.")
        tmp = self._tmp / uuid.uuid4().hex
        f = tmp.open("xb")
        try:
            yield f
            f.flush()
            os.fsync(f.fileno())
            f.close()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise StorageError("Stored objects are immutable; key already exists.")
            os.replace(tmp, dest)
            os.chmod(dest, 0o440)
        finally:
            if not f.closed:
                f.close()
            tmp.unlink(missing_ok=True)

    def open_read(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            os.chmod(path, 0o640)
            path.unlink()
