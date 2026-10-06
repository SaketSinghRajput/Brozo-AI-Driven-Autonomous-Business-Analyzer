"""Hash-chained audit log (TASK-DB-009, TASK-SEC-012, design §52 and §60).

One chain per workspace (`workspace_id IS NULL` is the system chain). Appends to a chain are
serialized with a transaction-scoped advisory lock, so two writers cannot fork it:
`hash = HMAC-SHA256(audit_key, prev_hash + canonical_json(event))`. The key never lives in the
database, so someone with DB write access cannot recompute a consistent chain after editing it
(L4). Each row stores the `key_id` it was written with; verification looks the key up in the
keyring, so keys can be rotated. The verifier recomputes every link; an edited, inserted or
deleted row breaks the chain at that point. Truncating the *tail* of a chain is not detectable
from the chain alone (anchor the latest hash externally if that matters).

Metadata holds IDs and hashes only (design §60): values are restricted to str/int/bool/None.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.config import SecurityConfig
from app.core.errors import SecurityError
from app.core.ids import IdGenerator, UuidV7Ids
from app.core.logging import trace_id_var
from app.core.scope import Scope, WorkspaceScope
from app.infra.db import UnitOfWork
from app.infra.repository import Repository
from app.jobs.models import Job
from app.jobs.worker import Handler, JobContext

log = logging.getLogger("app.audit")

GENESIS_HASH = "0" * 64
AuditResult = Literal["success", "denied", "failure"]
MetadataValue = str | int | bool | None


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    at: datetime
    actor: str
    workspace_id: str | None
    action: str
    resource: str
    result: AuditResult
    trace_id: str | None
    metadata: dict[str, MetadataValue]
    key_id: str
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class ChainReport:
    checked: int
    broken_at_seq: int | None  # first row whose link does not verify

    @property
    def ok(self) -> bool:
        return self.broken_at_seq is None


DERIVED_KEY_ID = "derived"


@dataclass(frozen=True)
class AuditKeys:
    """Keyring for the audit chain: the key new rows are written with, plus retired keys that
    only verify older rows."""

    current_id: str
    keys: Mapping[str, bytes]

    @property
    def current_key(self) -> bytes:
        return self.keys[self.current_id]

    @classmethod
    def from_config(cls, cfg: SecurityConfig) -> AuditKeys:
        keys = {kid: k.get_secret_value().encode() for kid, k in cfg.audit_retired_keys.items()}
        if cfg.audit_key is None:
            # Dev/test only (AppConfig refuses a missing audit_key in staging/prod): a key
            # derived from session_secret, so the chain is keyed without extra setup.
            secret = cfg.session_secret.get_secret_value().encode()
            keys[DERIVED_KEY_ID] = hmac.new(secret, b"audit-chain-key", hashlib.sha256).digest()
            return cls(current_id=DERIVED_KEY_ID, keys=keys)
        keys[cfg.audit_key_id] = cfg.audit_key.get_secret_value().encode()
        return cls(current_id=cfg.audit_key_id, keys=keys)


def chain_hash(key: bytes, prev_hash: str, fields: Mapping[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key, (prev_hash + canonical).encode(), hashlib.sha256).hexdigest()


def hashed_fields(row: Mapping[Any, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "at": row["at"].astimezone(UTC).isoformat(),
        "actor": row["actor"],
        "workspace_id": row["workspace_id"],
        "action": row["action"],
        "resource": row["resource"],
        "result": row["result"],
        "trace_id": row["trace_id"],
        "metadata": row["metadata"],
        "key_id": row["key_id"],
    }


def _workspace_of(scope: Scope) -> str | None:
    return scope.workspace_id if isinstance(scope, WorkspaceScope) else None


class AuditLog(Repository):
    """The `AuditLogger` port's PostgreSQL adapter: appends in the caller's transaction."""

    def __init__(
        self,
        session: Session,
        keys: AuditKeys,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        super().__init__(session)
        self._keys = keys
        self._clock = clock or SystemClock()
        self._ids = ids or UuidV7Ids()

    def append(
        self,
        scope: Scope,
        *,
        action: str,
        resource: str,
        result: AuditResult,
        actor: str | None = None,
        metadata: Mapping[str, MetadataValue] | None = None,
    ) -> AuditEvent:
        meta = dict(metadata or {})
        for key, value in meta.items():
            if not isinstance(value, str | int | bool | None):
                raise ValueError(f"audit metadata {key!r} must be an id/hash/flag, not data")
        workspace_id = _workspace_of(scope)
        if actor is None:
            actor = scope.principal_id if isinstance(scope, WorkspaceScope) else scope.component
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :chain))"),
            {"chain": workspace_id or ""},
        )
        prev = self._session.execute(
            text(
                "SELECT hash FROM audit_logs WHERE workspace_id IS NOT DISTINCT FROM :ws "
                "ORDER BY seq DESC LIMIT 1"
            ),
            {"ws": workspace_id},
        ).scalar_one_or_none()
        row: dict[str, Any] = {
            "id": self._ids.new("aud"),
            "at": self._clock.now().astimezone(UTC),
            "actor": actor,
            "workspace_id": workspace_id,
            "action": action,
            "resource": resource,
            "result": result,
            "trace_id": trace_id_var.get(),
            "metadata": meta,
            "key_id": self._keys.current_id,
        }
        prev_hash = prev or GENESIS_HASH
        digest = chain_hash(self._keys.current_key, prev_hash, hashed_fields(row))
        self._session.execute(
            text(
                "INSERT INTO audit_logs (id, at, actor, workspace_id, action, resource, result, "
                "trace_id, metadata, key_id, prev_hash, hash) VALUES (:id, :at, :actor, "
                ":workspace_id, :action, :resource, :result, :trace_id, "
                "CAST(:metadata AS jsonb), :key_id, :prev, :hash)"
            ),
            {**row, "metadata": json.dumps(meta), "prev": prev_hash, "hash": digest},
        )
        return AuditEvent(**row, prev_hash=prev_hash, hash=digest)

    def verify(self, scope: Scope) -> ChainReport:
        """Recompute the chain of the scope's workspace (SystemScope → the system chain)."""
        # ponytail: loads the whole chain; page by seq once chains reach millions of rows.
        rows = self._session.execute(
            text(
                "SELECT seq, id, at, actor, workspace_id, action, resource, result, trace_id, "
                "metadata, key_id, prev_hash, hash FROM audit_logs "
                "WHERE workspace_id IS NOT DISTINCT FROM :ws ORDER BY seq"
            ),
            {"ws": _workspace_of(scope)},
        ).mappings()
        expected_prev, checked = GENESIS_HASH, 0
        for row in rows:
            key = self._keys.keys.get(row["key_id"])
            if (
                key is None
                or row["prev_hash"] != expected_prev
                or not hmac.compare_digest(
                    row["hash"], chain_hash(key, expected_prev, hashed_fields(row))
                )
            ):
                return ChainReport(checked=checked, broken_at_seq=row["seq"])
            expected_prev, checked = row["hash"], checked + 1
        return ChainReport(checked=checked, broken_at_seq=None)


VERIFIER = "system:audit-verifier"


def make_verify_handler(uow: UnitOfWork, keys: AuditKeys) -> Handler:
    """Job handler for `audit_verify`: verifies the chain of the job's workspace. Read-only."""

    def handle(job: Job, ctx: JobContext) -> str:
        scope = WorkspaceScope(workspace_id=job.workspace_id, principal_id=VERIFIER)
        with uow(scope) as s:
            report = AuditLog(s, keys).verify(scope)
        if not report.ok:
            log.error(
                "audit.chain_broken",
                extra={"fields": {"workspace_id": job.workspace_id, "seq": report.broken_at_seq}},
            )
            raise SecurityError(
                f"audit chain broken at seq {report.broken_at_seq}", retryable=False
            )
        return f"audit_verified:{report.checked}"

    return handle
