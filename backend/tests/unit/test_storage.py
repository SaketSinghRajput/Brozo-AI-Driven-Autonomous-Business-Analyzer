from pathlib import Path

import pytest

from app.core.errors import SecurityError
from app.infra.storage import LocalStorage, StorageError


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "store")


def test_write_then_read(storage: LocalStorage) -> None:
    with storage.writer("datasets/ds_1/dsv_1.parquet") as f:
        f.write(b"payload")
    assert storage.exists("datasets/ds_1/dsv_1.parquet")
    with storage.open_read("datasets/ds_1/dsv_1.parquet") as f:
        assert f.read() == b"payload"


def _write(storage: LocalStorage, key: str, data: bytes, crash: bool = False) -> None:
    with storage.writer(key) as f:
        f.write(data)
        if crash:
            raise RuntimeError("crash")


def test_objects_are_immutable(storage: LocalStorage) -> None:
    _write(storage, "a/b", b"1")
    with pytest.raises(StorageError, match="immutable"):
        _write(storage, "a/b", b"2")
    with storage.open_read("a/b") as f:
        assert f.read() == b"1"


def test_crash_mid_write_leaves_no_file(storage: LocalStorage, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        _write(storage, "x/y", b"partial", crash=True)
    assert not storage.exists("x/y")
    assert list((tmp_path / "store" / ".tmp").iterdir()) == []


@pytest.mark.parametrize(
    "key",
    ["../escape", "a/../../escape", "/abs", "C:/win", "a\\b", "", "a//b", ".tmp/x", "a/.", " a"],
)
def test_path_traversal_and_bad_keys_rejected(storage: LocalStorage, key: str) -> None:
    with pytest.raises(SecurityError):
        storage.exists(key)


def test_delete(storage: LocalStorage) -> None:
    with storage.writer("d/e") as f:
        f.write(b"z")
    storage.delete("d/e")
    assert not storage.exists("d/e")
