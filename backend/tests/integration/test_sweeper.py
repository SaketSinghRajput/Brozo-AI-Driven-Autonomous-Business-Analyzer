"""TASK-STORE-002: only old orphans are removed; committed files survive; dry run default."""

import os
import time
from pathlib import Path

from app.data.sweeper import StorageSweeper

from .dataset_env import FIXTURES, Env


def _old(path: Path, age_s: float = 48 * 3600) -> None:
    then = time.time() - age_s
    os.utime(path, (then, then))


def test_sweeper_keeps_committed_data_and_removes_old_orphans(env: Env) -> None:
    r = env.upload(FIXTURES / "insufficient" / "sales.csv")
    assert r.status_code == 201, r.text
    env.settle()
    root = env.container.config.storage.datastore_path
    ws = env.scope.workspace_id
    tmp_orphan = root / ".tmp" / "deadbeef"
    raw_orphan = root / "raw" / ws / "dsv_orphan0001"
    fresh_orphan = root / "raw" / ws / "dsv_orphan0002"
    for path in (tmp_orphan, raw_orphan, fresh_orphan):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    version_id = r.json()["version_id"]
    committed = [p for p in root.rglob("*") if p.is_file() and version_id in p.name]
    assert len(committed) == 2  # raw upload + Parquet
    for path in (tmp_orphan, raw_orphan, *committed):
        _old(path)

    sweeper = StorageSweeper(env.container.uow, root)
    report = sweeper.sweep()
    expected = [".tmp/deadbeef", f"raw/{ws}/dsv_orphan0001"]
    assert (report.dry_run, report.orphans) == (True, expected)
    assert tmp_orphan.exists()  # dry run deletes nothing

    applied = sweeper.sweep(dry_run=False)
    assert applied.orphans == expected
    assert not tmp_orphan.exists()
    assert not raw_orphan.exists()
    assert fresh_orphan.exists()  # inside the grace period
    assert all(p.exists() for p in committed)
