# AI-Driven Autonomous Business Analyzer

Privacy-first, self-hosted AI business-intelligence and decision-support platform.

> **LLM orchestrates. Tools calculate. ML predicts. Memory contextualizes. Evidence grounds. Validator verifies. Humans decide.**

## Status

Pre-MVP foundation. What actually exists is recorded in [`docs/taskrecord.md`](docs/taskrecord.md). Do not infer functionality from the design documents.

## Documentation

| Document | Purpose |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Engineering rules (how the team and Claude Code work) |
| [`docs/prd.md`](docs/prd.md) | Product requirements |
| [`docs/trd.md`](docs/trd.md) | Technical architecture |
| [`docs/design.md`](docs/design.md) | Detailed component design |
| [`docs/UI-UX.md`](docs/UI-UX.md) | User experience specification |
| [`docs/plan.md`](docs/plan.md) | Roadmap, milestones, research gates |
| [`docs/task.md`](docs/task.md) | Executable backlog |
| [`docs/taskrecord.md`](docs/taskrecord.md) | Execution history |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Architecture decision records |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module map and ownership (what exists now) |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model and security controls |
| [`docs/MLOPS.md`](docs/MLOPS.md) | Observability, deployment profiles, resource limits, backup/restore |
| [`docs/research/`](docs/research/) | Research records (RES-*) |
| [`docs/benchmarks/`](docs/benchmarks/) | Benchmark records (BENCH-*) |

## Local setup

Tested on Windows 11 + Git Bash + Docker Desktop (WSL2). The same commands work on Linux; see the Ollama note in [`docs/MLOPS.md`](docs/MLOPS.md) §3.1.

### Prerequisites

| Tool | Version used | Why |
|---|---|---|
| Docker Desktop / Docker Engine + Compose | Compose v2.24+ (tested v5.1) | the stack |
| [uv](https://docs.astral.sh/uv/) | 0.11 | backend dependencies, tests, scripts (installs Python 3.14 itself) |
| Node.js | 24 | frontend development only (the stack builds the frontend in Docker) |
| [Ollama](https://ollama.com) | ≥ 0.34, bound to `127.0.0.1` | local LLM; without it the stack runs in `degraded` mode (no AI features) |
| Git Bash (Windows) | any | runs `scripts/dev` |

### 1. Configure

```sh
cp .env.example .env
```

Edit `.env` and replace every `CHANGE_ME` (`POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `APP_SECURITY__SESSION_SECRET`; the database URL is only used by tools run outside Docker). Generate secrets with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

For plain `http://` local use set `APP_SECURITY__COOKIE_SECURE=false`, otherwise the browser drops the session cookie. Optionally pull the model: `ollama pull qwen2.5:7b` (ADR-0008).

### 2. Run

```sh
scripts/dev up        # builds images, migrates, waits until healthy
curl http://127.0.0.1:8000/api/v1/health/ready
# {"status":"ready","database":true,"job_queue":true,"llm":true}   ("degraded" + llm:false without Ollama)
```

Create the first admin (the password is read from the environment, never from argv):

```sh
docker compose -f infra/compose/compose.yml --env-file .env -p analyzer exec \
  -e APP_BOOTSTRAP_PASSWORD='<a strong password>' api \
  python -m app.security.bootstrap --email you@example.com --workspace "My workspace"
```

Open <http://127.0.0.1:8080> and sign in. `scripts/dev help` lists the other commands (`down`, `logs`, `migrate`, `test`, `lint`, `backup`, `restore`, `prod-up`, …). Ports are set in `.env`: `API_HOST_PORT`, `WEB_HOST_PORT`, `POSTGRES_HOST_PORT`; all bind to `127.0.0.1` only.

### 3. Develop and test

```sh
cd backend && uv sync          # once
scripts/dev lint               # ruff, format check, mypy --strict, import contracts, provider check
scripts/dev test               # unit + API tests
```

Integration tests need a disposable PostgreSQL superuser URL (the compose postgres works; tests create and drop their own database):

```sh
export APP_TEST_ADMIN_DATABASE_URL="postgresql+psycopg://analyzer:<POSTGRES_PASSWORD>@127.0.0.1:5432/postgres"
scripts/dev test tests/integration
```

Optional pre-commit hooks mirroring CI: `uv tool install pre-commit && pre-commit install && pre-commit install --hook-type pre-push`.

Production-like run (egress blocked, only the proxy published, `APP_ENV=prod`; needs `APP_SECURITY__AUDIT_KEY` in `.env` and HTTPS for browser logins): `scripts/dev prod-up`. See [`docs/MLOPS.md`](docs/MLOPS.md).
