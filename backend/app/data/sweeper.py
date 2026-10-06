"""Orphan/temp file sweeper for the local dataset store (TASK-STORE-002; design 9 "Rollback", 51).

Candidates, all older than the grace period (default 24 h, so in-flight writes are never touched):
- ``.tmp/*``: temp files of writes that crashed before the atomic rename;
- ``raw/<ws>/<version_id>`` and ``datasets/<ws>/<dataset_id>/<version_id>.parquet`` whose
  ``dataset_versions`` row does not exist (the upload/ingest transaction rolled back after the
  file was stored).
A file whose version row exists is committed data and is NEVER deleted, whatever its state; a file
in an unknown layout is kept too. Dry run is the default and only reports:

    uv run python -m app.data.sweeper            # dry run
    uv run python -m app.data.sweeper --apply    # delete

A system-wide command (cron / operator), not a queue job: jobs are workspace-scoped
(``jobs.workspace_id``), the store is not. It uses SystemScope to see every workspace's versions.
"""

import argparse
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text

from app.core.scope import SystemScope
from app.infra.db import UnitOfWork

SYSTEM = SystemScope("system:sweeper")
_RAW = re.compile(r"^raw/ws_\w+/(dsv_\w+)$")
_PARQUET = re.compile(r"^datasets/ws_\w+/ds_\w+/(dsv_\w+)\.parquet$")


@dataclass
class SweepReport:
    dry_run: bool
    orphans: list[str] = field(default_factory=list)  # storage keys (relative paths)
    kept: int = 0


def _version_id(key: str) -> str | None:
    match = _RAW.match(key) or _PARQUET.match(key)
    return match.group(1) if match else None


class StorageSweeper:
    def __init__(self, uow: UnitOfWork, root: Path, grace_s: float = 24 * 3600) -> None:
        self._uow = uow
        self._root = root.resolve()
        self._grace_s = grace_s

    def sweep(self, *, dry_run: bool = True, now: float | None = None) -> SweepReport:
        cutoff = (time.time() if now is None else now) - self._grace_s
        old = [
            p
            for p in self._root.rglob("*")
            if p.is_file() and not p.is_symlink() and p.stat().st_mtime < cutoff
        ]
        keys = {p: p.relative_to(self._root).as_posix() for p in old}
        existing = self._existing(sorted({v for v in map(_version_id, keys.values()) if v}))
        report = SweepReport(dry_run=dry_run)
        for path, key in sorted(keys.items(), key=lambda kv: kv[1]):
            version = _version_id(key)
            orphan = key.startswith(".tmp/") or (version is not None and version not in existing)
            if not orphan:
                report.kept += 1
                continue
            report.orphans.append(key)
            if not dry_run:
                os.chmod(path, 0o640)  # stored objects are read-only
                path.unlink(missing_ok=True)
        return report

    def _existing(self, version_ids: list[str]) -> set[str]:
        if not version_ids:
            return set()
        with self._uow(SYSTEM) as s:
            rows = s.execute(
                text("SELECT id FROM dataset_versions WHERE id = ANY(:ids)"), {"ids": version_ids}
            ).scalars()
            return set(rows)


def main() -> None:  # pragma: no cover - operator entry point
    from app.core.config import load_config
    from app.infra.db import make_engine

    parser = argparse.ArgumentParser(description="Remove orphaned dataset files.")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument("--grace-hours", type=float, default=24.0)
    args = parser.parse_args()
    config = load_config()
    sweeper = StorageSweeper(
        UnitOfWork(make_engine(config.database)),
        config.storage.datastore_path,
        grace_s=args.grace_hours * 3600,
    )
    report = sweeper.sweep(dry_run=not args.apply)
    verb = "would delete" if report.dry_run else "deleted"
    print(f"{verb} {len(report.orphans)} orphan file(s); kept {report.kept}")
    for key in report.orphans:
        print(f"  {key}")


if __name__ == "__main__":
    main()
