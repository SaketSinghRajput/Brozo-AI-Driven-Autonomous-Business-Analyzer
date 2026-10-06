"""Liveness, readiness (TASK-INFRA-004, TASK-LLM-011) and the Prometheus endpoint.

Public, no auth: responses carry booleans only - never hosts, versions of dependencies,
error messages or model names. Failure details go to the log.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.deps import ContainerDep
from app.core.container import Container
from app.core.errors import ApplicationError, ErrorEnvelope

router = APIRouter(tags=["health"])
log = logging.getLogger("app.health")
LLM_PROBE_TIMEOUT_S = 2.0


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadinessResponse(BaseModel):
    # degraded = serving, but AI features are unavailable (LLM unreachable or model missing).
    status: Literal["ready", "degraded", "not_ready"]
    database: bool
    job_queue: bool
    llm: bool


@dataclass(frozen=True)
class ReadinessProbe:
    database: Callable[[], bool]
    job_queue: Callable[[], bool]
    llm: Callable[[], bool]


def _sql_ok(container: Container, sql: str, name: str) -> Callable[[], bool]:
    def check() -> bool:
        try:
            with container.engine.connect() as conn:
                conn.execute(text(sql))
        except SQLAlchemyError as exc:
            fields = {"check": name, "error": type(exc).__name__}
            log.warning("readiness.check.failed", extra={"fields": fields})
            return False
        return True

    return check


def _llm_ok(container: Container) -> Callable[[], bool]:
    """Ollama reachable and the configured model pulled (`/api/tags`), not merely 'up'."""
    cfg = container.config.llm

    def check() -> bool:
        try:
            with httpx.Client(
                base_url=str(cfg.endpoint),
                timeout=LLM_PROBE_TIMEOUT_S,
                trust_env=False,  # no proxy env: the endpoint is local by policy
                follow_redirects=False,
            ) as client:
                resp = client.get("/api/tags")
                resp.raise_for_status()
                names = {m.get("name") for m in resp.json().get("models", [])}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.warning("readiness.llm.failed", extra={"fields": {"error": type(exc).__name__}})
            return False
        return cfg.model in names

    return check


def get_readiness_probe(container: ContainerDep) -> ReadinessProbe:
    return ReadinessProbe(
        database=_sql_ok(container, "SELECT 1", "database"),
        job_queue=_sql_ok(container, "SELECT 1 FROM jobs LIMIT 1", "job_queue"),
        llm=_llm_ok(container),
    )


@router.get("/health")
def health() -> HealthResponse:
    """Liveness only: no auth, no dependency checks."""
    return HealthResponse(status="ok", version=__version__)


class NotReadyError(ApplicationError):
    """503 for a failed readiness probe; ``details`` carries the ReadinessResponse booleans."""

    code, http_status, default_retryable = "NOT_READY", 503, True


@router.get(
    "/health/ready",
    responses={503: {"model": ErrorEnvelope, "description": "Not ready (details: the checks)"}},
)
def ready(probe: Annotated[ReadinessProbe, Depends(get_readiness_probe)]) -> ReadinessResponse:
    """Readiness: 503 (error envelope, B8) when the database or job queue is down; LLM down is
    only `degraded`."""
    database = probe.database()
    job_queue = database and probe.job_queue()
    llm = probe.llm()
    if not (database and job_queue):
        flags = ReadinessResponse(
            status="not_ready", database=database, job_queue=job_queue, llm=llm
        )
        raise NotReadyError("the service is not ready", details=flags.model_dump())
    status: Literal["ready", "degraded"] = "ready" if llm else "degraded"
    return ReadinessResponse(status=status, database=database, job_queue=job_queue, llm=llm)


def metrics(_: Request) -> Response:
    """Prometheus text format. Mounted at /metrics (outside /api: never behind the proxy)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
