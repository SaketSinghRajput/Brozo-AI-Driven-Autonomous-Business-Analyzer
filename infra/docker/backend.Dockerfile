# syntax=docker/dockerfile:1
# Backend image for the api, worker and migrate services (build context: backend/).
FROM python:3.14.4-slim AS runtime

# Pick up OS security fixes newer than the base image (SEC-014 Trivy triage, SECURITY.md §5.4).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --system --uid 10001 --no-create-home appuser \
 && mkdir -p /data/datastore \
 && chown appuser /data/datastore
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
