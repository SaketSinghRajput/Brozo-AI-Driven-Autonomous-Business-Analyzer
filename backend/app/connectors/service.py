"""Connector registration and snapshot import (TASK-DATA-013).

create: validate (SSRF guard + table/query rules), seal the password with `CredentialCipher`
(context `connector:<id>` as AAD), insert, audit. Secrets never leave this module: `Connector`
(the API model) has no credential field, and the password is decrypted only inside `run_import`.
import: the request enqueues an `ingest` job with payload `{connector_id}`; the worker calls
`run_import`, which re-checks the target (DNS may have changed), streams the snapshot to a
temporary CSV and hands it to `UploadService`: the same raw store, dedupe, version state
machine and job chain as a file upload, so ingestion -> profiling -> understanding follow.
"""

import io
import tempfile
from datetime import datetime
from typing import BinaryIO, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.network import resolve_target
from app.connectors.postgres import Limits, Target, select_query, snapshot_to_csv, table_parts
from app.core.clock import Clock
from app.core.config import AppConfig, ConfigError
from app.core.container import Container
from app.core.errors import ApplicationError, NotFoundError, SecurityError
from app.core.ids import IdGenerator
from app.core.logging import trace_id_var
from app.core.scope import WorkspaceScope
from app.data.upload import UploadRequest, UploadService
from app.infra.db import UnitOfWork
from app.infra.repository import Repository
from app.infra.storage import Storage
from app.jobs.models import Job, JobType
from app.jobs.repository import JobRepository
from app.security.audit import AuditKeys, AuditLog
from app.security.crypto import CredentialCipher

SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
_COLUMNS = (
    "id, name, kind, host, port, database, username, sslmode, source_table AS table_name, "
    "source_query AS query, dataset_id, created_at"
)


class ConnectorsDisabledError(ApplicationError):
    code, http_status = "CONNECTORS_DISABLED", 503


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    kind: Literal["postgres"] = "postgres"
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=63)
    username: str = Field(min_length=1, max_length=63)
    password: SecretStr = Field(min_length=1, max_length=1024)
    sslmode: SslMode = "require"
    table_name: str | None = Field(default=None, max_length=129)
    query: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if (self.table_name is None) == (self.query is None):
            raise ValueError("give exactly one of table_name or query")
        return self


class Connector(BaseModel):
    """API view: never holds credentials."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    kind: str
    host: str
    port: int
    database: str
    username: str
    sslmode: str
    table_name: str | None
    query: str | None
    dataset_id: str | None
    created_at: datetime


class ConnectorRepository(Repository):
    def insert(
        self, scope: WorkspaceScope, connector_id: str, body: ConnectorCreate, sealed: str
    ) -> Connector:
        row = self._session.execute(
            text(
                "INSERT INTO connectors (id, workspace_id, name, kind, host, port, database, "  # noqa: S608
                "username, sslmode, source_table, source_query, password_enc, created_by) "
                "VALUES (:id, :ws, :name, :kind, :host, :port, :db, :user, :ssl, :tbl, :q, "
                f":enc, :by) RETURNING {_COLUMNS}"
            ),
            {
                "id": connector_id,
                "ws": scope.workspace_id,
                "name": body.name,
                "kind": body.kind,
                "host": body.host,
                "port": body.port,
                "db": body.database,
                "user": body.username,
                "ssl": body.sslmode,
                "tbl": body.table_name,
                "q": body.query,
                "enc": sealed,
                "by": scope.principal_id,
            },
        ).one()
        return Connector.model_validate(dict(row._mapping))

    def list_all(self, scope: WorkspaceScope) -> list[Connector]:
        rows = self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM connectors WHERE workspace_id = :ws "  # noqa: S608
                "ORDER BY created_at DESC, id DESC"
            ),
            {"ws": scope.workspace_id},
        ).all()
        return [Connector.model_validate(dict(r._mapping)) for r in rows]

    def get(self, scope: WorkspaceScope, connector_id: str) -> tuple[Connector, str] | None:
        """The connector and its sealed password (for `run_import` only)."""
        row = self._session.execute(
            text(
                f"SELECT {_COLUMNS}, password_enc FROM connectors "  # noqa: S608
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": connector_id, "ws": scope.workspace_id},
        ).one_or_none()
        if row is None:
            return None
        data = dict(row._mapping)
        sealed = str(data.pop("password_enc"))
        return Connector.model_validate(data), sealed

    def set_dataset(self, scope: WorkspaceScope, connector_id: str, dataset_id: str) -> None:
        self._session.execute(
            text(
                "UPDATE connectors SET dataset_id = :ds "
                "WHERE id = :id AND workspace_id = :ws AND dataset_id IS NULL"
            ),
            {"ds": dataset_id, "id": connector_id, "ws": scope.workspace_id},
        )


def _context(connector_id: str) -> str:
    return f"connector:{connector_id}"


class ConnectorService:
    def __init__(
        self,
        config: AppConfig,
        uow: UnitOfWork,
        storage: Storage,
        ids: IdGenerator,
        clock: Clock,
        audit_keys: AuditKeys,
    ) -> None:
        self._config = config
        self._cfg = config.connectors
        self._uow = uow
        self._storage = storage
        self._ids = ids
        self._clock = clock
        self._keys = audit_keys

    @classmethod
    def from_container(cls, c: Container) -> ConnectorService:
        return cls(c.config, c.uow, c.storage, c.ids, c.clock, c.auth.audit_keys)

    def _cipher(self) -> CredentialCipher:
        try:
            return CredentialCipher.from_config(self._config.security)
        except ConfigError:
            raise ConnectorsDisabledError(
                "Connectors are disabled: no credential key is configured."
            ) from None

    def _audit(
        self,
        session: Session,
        scope: WorkspaceScope,
        action: str,
        resource: str,
        result: Literal["success", "denied"],
        **meta: str,
    ) -> None:
        AuditLog(session, self._keys, self._clock, self._ids).append(
            scope, action=action, resource=resource, result=result, metadata=meta
        )

    def create(self, scope: WorkspaceScope, body: ConnectorCreate) -> Connector:
        cipher = self._cipher()
        connector_id = self._ids.new("conn")
        if body.table_name is not None:
            table_parts(body.table_name)
        if body.query is not None:
            select_query(body.query)
        try:
            resolve_target(body.host, body.port, self._cfg)
        except SecurityError as exc:
            reason = str((exc.details or {}).get("reason", exc.code))
            with self._uow(scope) as s:
                self._audit(s, scope, "connector.create", "connector:-", "denied", reason=reason)
            raise
        sealed = cipher.encrypt(body.password.get_secret_value(), context=_context(connector_id))
        with self._uow(scope) as s:
            connector = ConnectorRepository(s).insert(scope, connector_id, body, sealed)
            self._audit(
                s, scope, "connector.create", _context(connector_id), "success", kind=body.kind
            )
        return connector

    def list(self, scope: WorkspaceScope) -> list[Connector]:
        with self._uow(scope) as s:
            return ConnectorRepository(s).list_all(scope)

    def enqueue_import(self, scope: WorkspaceScope, connector_id: str) -> Job:
        with self._uow(scope) as s:
            if ConnectorRepository(s).get(scope, connector_id) is None:
                raise NotFoundError("connector not found")
            job = JobRepository(s).enqueue(
                scope,
                self._ids.new("job"),
                JobType.INGEST,
                {"connector_id": connector_id},
                trace_id=trace_id_var.get(),
            )
            self._audit(
                s, scope, "connector.import", _context(connector_id), "success", job_id=job.id
            )
        return job

    def run_import(self, scope: WorkspaceScope, connector_id: str, job_id: str) -> str:
        """Worker side: snapshot -> CSV -> upload path. Returns the dataset version id."""
        cipher = self._cipher()
        with self._uow(scope) as s:
            found = ConnectorRepository(s).get(scope, connector_id)
        if found is None:
            raise NotFoundError("connector not found")
        connector, sealed = found
        target = Target(
            host=connector.host,
            address=resolve_target(connector.host, connector.port, self._cfg),
            port=connector.port,
            database=connector.database,
            username=connector.username,
            sslmode=connector.sslmode,
        )
        cfg = self._cfg
        limits = Limits(cfg.connect_timeout_s, cfg.statement_timeout_ms, cfg.max_rows)
        with tempfile.TemporaryFile() as raw:
            out = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            snapshot_to_csv(
                target,
                cipher.decrypt(sealed, context=_context(connector_id)),
                limits,
                out,
                table=connector.table_name,
                query=connector.query,
            )
            out.flush()
            out.detach()
            raw.seek(0)
            upload = UploadService(
                self._uow, self._storage, self._ids, self._config.storage.max_upload_bytes
            )
            response, _ = upload.upload(
                scope,
                cast(BinaryIO, raw),
                UploadRequest(
                    filename=f"{connector.name}.csv",
                    name=connector.name,
                    dataset_id=connector.dataset_id,
                    idempotency_key=f"connector:{job_id}",
                ),
            )
        if connector.dataset_id is None:
            with self._uow(scope) as s:
                ConnectorRepository(s).set_dataset(scope, connector_id, response.dataset_id)
        return response.version_id
