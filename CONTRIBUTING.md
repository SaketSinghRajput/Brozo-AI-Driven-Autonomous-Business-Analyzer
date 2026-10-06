# Contributing

The binding rules are in [`CLAUDE.md`](CLAUDE.md). This file is the short operational version.

## Task workflow (`docs/task.md` §10)

1. Pick a `READY` task from `docs/task.md` (all dependencies DONE, research closed).
2. Read its linked PRD / TRD / design / UI-UX sections and inspect the current code.
3. Research first if the task says so (`docs/research/`); never pick an architecture-level technology inside an implementation task.
4. Implement with tests. Run lint, type checks and tests locally.
5. Open a PR to `develop`; the reviewer named in the task reviews it (🔒 tasks also need DEVOPS review).
6. Update `docs/task.md` status and add a record to `docs/taskrecord.md`.

## Branches

`main` (releases) ← `develop` (integration) ← `feature/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>`.
Never force-push or rewrite `main`/`develop`.

## Commits

Conventional Commits, referencing the task: `feat(data): add CSV parser [TASK-DATA-003]`.
Types: `feat`, `fix`, `test`, `docs`, `refactor`, `perf`, `chore`, `ci`, `build`.

## Definition of Done (`docs/task.md` §59)

Implementation + tests + CI green + review + documentation + observability + integration + task status and task record updated + commit. "Code written" is not done.

## Hard rules

- No hosted AI SDKs or endpoints (OpenAI, Anthropic/Claude API, Gemini, Groq, OpenRouter, …) in product code (`CLAUDE.md` §20). CI enforces this.
- The LLM never computes business numbers; tools do, and every number carries evidence.
- No secrets in the repository; `.env` is git-ignored, `.env.example` lists keys only.
