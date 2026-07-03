# Development

## Backend

```bash
uv sync
uv run pytest               # backend tests
uv run ruff check src tests
uv run sde-deepagent        # http://localhost:8321
```

## Web UI

The UI lives in `web/` (React + Vite + Tailwind +
[shadcn/ui](https://ui.shadcn.com)). The built output in `web/dist/` is
committed, so Node is only needed when changing the UI:

```bash
cd web
npm install
npm run dev                 # dev server proxying /api to localhost:8321
npx vitest run              # UI unit tests
npm run build               # refresh web/dist (commit it with your change)
```

## Source layout

`src/sde_deepagent/`:

| Module | Responsibility |
|---|---|
| `agent_factory.py` | deepagents wiring (models, tools, prompts, MCP) |
| `runner.py` | task pipeline: stream, budgets, retry, finalize |
| `worker.py` | queue, daily budget gate, retention sweeps |
| `gitops.py` | clone/branch/commit/push/PR, credential scoping |
| `sandbox.py` | per-repo Docker containers, capped exec |
| `repo_reader.py` | chat's read-only reference clones |
| `memory.py` | Supermemory client |
| `chat.py` | operator assistant (tasks, code, knowledge) |
| `pricing.py` | cost tracking, budgets |
| `intake/` | telegram, slack, linear, github reviews |
| `server.py` | FastAPI API + SSE + static UI |

`web/` is the React UI; `tests/` the backend suite; `config/` the hot-editable
agent/repo configuration.

## CI

Every push and pull request runs ruff + pytest, builds the web UI (to catch a
broken or stale committed bundle), validates the compose file and builds the
Docker image.

## Releases

Cutting a release is one tag push:

1. Bump `version` in `pyproject.toml` and move the `Unreleased` changelog
   section to the new version in `CHANGELOG.md`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`

The Release workflow then re-runs lint and the full test suite, verifies the
tag matches `pyproject.toml`, builds the `linux/amd64` + `linux/arm64` image,
pushes it to GHCR as `X.Y.Z`, `X.Y` and `latest`, and publishes a GitHub
release whose notes come from the changelog section plus generated commit
notes. Every push to `main` also refreshes a rolling `edge` image.

Note: the GHCR package must be public (GitHub package settings, one-time) for
anonymous `docker pull` to work.
