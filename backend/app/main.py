"""Application factory. Run with: uvicorn app.main:create_app --factory."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.health import metrics
from app.api.router import api_router
from app.chat.api import install_turn_event_schema
from app.core.config import AppConfig, ConfigError, Environment, load_config
from app.core.container import Container, build_container
from app.core.errors import install_error_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.observability import ObservabilityMiddleware, configure_tracing
from app.infra.db import schema_is_current
from app.security.api import throttled_response
from app.security.auth import LoginThrottledError
from app.security.middleware import BodyLimitMiddleware, SecurityHeadersMiddleware


def _lifespan(container: Container) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if container.config.database.check_schema_on_startup and not schema_is_current(
            container.engine
        ):
            raise ConfigError("Database schema is behind the latest migration; run migrations.")
        yield
        container.engine.dispose()

    return lifespan


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config()
    container = build_container(config)
    configure_logging(config.observability.log_level)
    configure_tracing(config.observability)

    expose_docs = config.env is not Environment.PROD
    app = FastAPI(
        title="AI-Driven Autonomous Business Analyzer",
        version=__version__,
        docs_url="/api/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if expose_docs else None,
        lifespan=_lifespan(container),
        # JSON bodies need `Content-Type: application/json` (login CSRF); pinned, not defaulted.
        strict_content_type=True,
    )
    app.state.container = container
    install_error_handlers(app, container.clock)
    app.add_exception_handler(LoginThrottledError, throttled_response)  # adds Retry-After
    app.include_router(api_router, prefix="/api/v1")
    install_turn_event_schema(app)  # the chat SSE payload under components/schemas
    if config.observability.metrics_enabled:
        # Outside /api: the reverse proxy forwards only /api/, so /metrics stays internal.
        app.add_route("/metrics", metrics, include_in_schema=False)
    # Last added = outermost: trace context wraps everything, then metrics/root span, then
    # headers, then body limits.
    app.add_middleware(
        BodyLimitMiddleware,
        max_body_bytes=config.security.max_body_bytes,
        max_upload_bytes=config.storage.max_upload_bytes,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=config.security.cookie_secure)
    app.add_middleware(ObservabilityMiddleware)
    app.add_middleware(RequestContextMiddleware)
    return app
