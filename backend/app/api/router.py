"""Versioned API router (/api/v1). Feature routers are included here as modules land."""

from fastapi import APIRouter

from app.analytics.api import router as analytics_router
from app.api.health import router as health_router
from app.chat.api import router as chat_router
from app.connectors.api import router as connectors_router
from app.dashboard.api import router as dashboard_router
from app.data.api import router as datasets_router
from app.data.understanding_api import router as understanding_router
from app.evidence.api import router as evidence_router
from app.jobs.api import router as jobs_router
from app.security.api import audit_router
from app.security.api import router as auth_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(audit_router)
api_router.include_router(jobs_router)
api_router.include_router(evidence_router)
api_router.include_router(datasets_router)
api_router.include_router(understanding_router)
api_router.include_router(connectors_router)
api_router.include_router(analytics_router)
api_router.include_router(chat_router)
api_router.include_router(dashboard_router)
api_router.include_router(health_router)  # /health liveness, /health/ready (TASK-INFRA-004)
