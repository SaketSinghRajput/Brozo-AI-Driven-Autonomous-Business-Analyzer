# infra

Container images (`infra/docker/`) and Docker Compose definitions (`infra/compose/`). Profiles: `core` now; `llm`, `rag`, `observability` and `sandbox` are added when their tasks start (TRD §52).

Owner: DEVOPS. No secrets in this directory. Configuration comes from `.env` (git-ignored); keys are documented in `.env.example`.
