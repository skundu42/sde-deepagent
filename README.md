# sde-deepagent

**A self-hostable software developer agent system.** Assign it a task — from the web UI, Telegram, Slack, or Linear — and it clones the right codebase, plans, implements the change, runs the tests, and opens a pull request.

Built on [LangChain deepagents](https://docs.langchain.com/oss/python/deepagents/overview). Bring a model API key (Anthropic, Google Gemini, and/or OpenAI) and Docker for the default sandboxed execution path; everything else — queue, storage, UI, event streaming, long-term memory — runs on your own infrastructure.

![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue) ![self--hosted](https://img.shields.io/badge/deployment-self--hosted-green) ![tests](https://img.shields.io/badge/tests-230%20passing-brightgreen) ![license](https://img.shields.io/badge/license-MIT-blue)

---

## Features

| Feature | What it does |
|---|---|
| **Orchestrator + subagents** | explorer / coder / tester / reviewer — model *and* reasoning effort configurable per role, providers freely mixed |
| **Full task pipeline** | isolated git workspace → plan → implement → test until green → diff review → commit → push → GitHub PR |
| **Multi-channel intake** | web UI, allowlisted Telegram/Slack, and Linear (label polling + webhook) — no public URL required; results posted back to the source channel |
| **Sandboxed execution** | each repo's shell runs in its own reused Docker container, isolated from the host and other repos' workspaces |
| **Long-term memory** | self-hosted [Supermemory](https://supermemory.ai/docs/self-hosting/overview) — recalls conventions and gotchas from past tasks, records new learnings per codebase |
| **Company context** | per-repo doc globs, convention files (`AGENTS.md`, `CLAUDE.md`, …), a global `context/` folder, and a Resources page that ingests any URL or text |
| **Cost control** | per-task budgets that abort runaway runs, a daily budget that pauses the queue, live spend in the UI |
| **Human oversight** | optional approval gate (nothing ships until you review the diff), mid-task steering, one-click PR revisions on the same branch |
| **Chat** | ask an assistant about any past or running task — grounded in the actual records, traces, and memory |
| **Observability** | every message, tool call, shell command, and token persisted in SQLite and streamed live over SSE |

## Supported models

Set models per role in `config/agents.yaml` (or UI → Agents) as `provider:model`. Any model from a known provider works — the picks below are just the UI dropdown shortcuts.

| Provider | Prefix | Curated picks |
|---|---|---|
| Anthropic | `anthropic:` | `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` |
| Google Gemini | `google_genai:` | `gemini-3.1-pro-preview`, `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite` |
| OpenAI | `openai:` | `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5-mini`, `gpt-4.1`, `o3`, `o4-mini` |

Each role also takes an `effort: low | medium | high` knob that maps to the provider's native control (OpenAI `reasoning_effort`, Anthropic `effort`, Gemini `thinking_level`).

## Architecture

```
 Telegram ─┐
 Slack ────┤                 ┌─ orchestrator (deep agent)
 Linear ───┼─▶ task queue ─▶ │    ├─ explorer   (read-only scout)
 Web UI ───┘       │         │    ├─ coder      (implementation)
                   │         │    ├─ tester     (run/fix/write tests)
                   ▼         │    └─ reviewer   (final diff review)
            SQLite + SSE     └─▶ git branch ─▶ tests ─▶ GitHub PR
                   ▲                  │
            Supermemory ◀── learnings ┘   (self-hosted long-term memory)
```

Single process: FastAPI server, asyncio worker pool, SQLite persistence, static UI. Supermemory runs as a sidecar (compose service or local binary).

## Setup

**Local:**

```bash
git clone https://github.com/skundu42/sde-deepagent.git && cd sde-deepagent
cp .env.example .env        # add ≥1 model key, plus GITHUB_TOKEN to open PRs
uv sync
uv run sde-deepagent        # → http://localhost:8321
```

Docker must be running (the default sandboxed execution path) unless you set `SANDBOX_DEFAULT=false`.

**Docker Compose (production):**

```bash
cp .env.example .env        # set AUTH_TOKEN, SDE_DATA_DIR (absolute path), ≥1 model key, GITHUB_TOKEN
docker compose up -d --build

# first boot only: copy supermemory's generated key into .env, then recreate
docker compose logs supermemory | grep "api key"
echo 'SUPERMEMORY_API_KEY=sm_...' >> .env && docker compose up -d
```

Compose publishes both ports on localhost only and refuses to start without `AUTH_TOKEN`; front it with a TLS reverse proxy. On a server, set `REQUIRE_APPROVAL=true` and `DAILY_BUDGET_USD`. It mounts the host Docker socket to launch sandbox containers — treat access to the control plane as host-root-equivalent.

Then in the UI: **Codebases** → register a repo (git URL or local path, test command) → **New task** → watch the live trace; the PR link appears when it ships.

## Configuration

All runtime settings come from `.env` (see [`.env.example`](.env.example) for the full annotated list):

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` | model providers (≥ 1 required) |
| `GITHUB_TOKEN`, `GITHUB_API_URL` | push branches + open PRs (GitHub / GHE) |
| `AUTH_TOKEN` | bearer token for the API/UI; required for any non-loopback bind |
| `SDE_DATA_DIR` | absolute host data path (task DB + workspaces); required by Compose |
| `SUPERMEMORY_BASE_URL`, `SUPERMEMORY_API_KEY` | long-term memory (optional) |
| `FIRECRAWL_API_URL` / `FIRECRAWL_API_KEY` | JS-rendering scraper for the Resources page (optional) |
| `TELEGRAM_BOT_TOKEN`, `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`, `LINEAR_API_KEY` | intake channels (each optional) |
| `TELEGRAM_ALLOWED_CHATS`, `SLACK_ALLOWED_USERS` | authorized chat/user IDs; empty denies task creation, `*` allows all |
| `TASK_BUDGET_USD`, `DAILY_BUDGET_USD` | cost guardrails (0 = unlimited) |
| `REQUIRE_APPROVAL` | hold every task for human review before push/PR |
| `SECRETS_KEY` | enables UI-entered per-repo secrets, encrypted at rest |
| `SANDBOX_DEFAULT`, `SANDBOX_IMAGE`, `SANDBOX_NETWORK`, `SANDBOX_IDLE_HOURS` | sandbox defaults (on; `buildpack-deps:bookworm`; `bridge`; reused for 7 idle days) |
| `MAX_CONCURRENT_TASKS`, `TASK_TIMEOUT_SECONDS`, `WORKSPACE_RETENTION` | runtime tuning |

**Agents — `config/agents.yaml`** (or UI → Agents): pick the orchestrator and per-subagent models/effort, plus optional `mcp_servers:` (extra tools for the orchestrator) and `pricing:` overrides.

```yaml
orchestrator:
  model: openai:gpt-5.4
  effort: medium
subagents:
  explorer:  { model: anthropic:claude-haiku-4-5-20251001 }
  coder:     { model: google_genai:gemini-2.5-pro }   # mix providers freely
  reviewer:  { model: openai:o4-mini }
```

**Codebases — `config/repos.yaml`** (or UI → Codebases): the `description` powers automatic task→repo routing; `test` is how the agent verifies its work.

```yaml
repos:
  backend:
    url: git@github.com:acme/backend.git
    default_branch: main
    description: "Python FastAPI monolith serving the public API"
    setup: "uv sync"
    test: "uv run pytest -x -q"
    context: [docs/architecture.md]
```

**Per-repo secrets** (setup/tests that need credentials): reference a host env var (`secrets: { DB_URL: env:BACKEND_DB_URL }`, no app-side storage) or store a value encrypted at rest (`DB_URL: store`, needs `SECRETS_KEY`). Values are injected *only* into the controller-run `setup`/`test` commands — never the agent's own shell — and redacted (raw/base64/url-encoded) from every output sink. Residual risk: tests run the repo's own code with the secret in the environment, so attach secrets only to repos you trust, prefer `sandbox_network: none`, and keep `REQUIRE_APPROVAL=true`.

## How a task runs

1. **Resolve** the target codebase (explicit, or model-routed by repo descriptions).
2. **Clone** into an isolated workspace on branch `agent/<id>-<slug>`; start (or reuse) the repo's sandbox container and run its setup command — otherwise the agent installs what it needs on the fly.
3. **Recall** — search long-term memory for conventions and prior learnings about this codebase.
4. **Plan & implement** — todo-list planning, delegation to subagents, real shell + filesystem scoped to the workspace.
5. **Test** until green with the repo's test command.
6. **Review** — the reviewer subagent audits the final diff.
7. **Ship** — commit, push, open the PR. With `REQUIRE_APPROVAL=true` the task instead parks as *awaiting approval* with the diff in the UI for one-click ship/reject.
8. **Record & report** — durable learnings and the outcome are saved to memory; the result (with PR link) is posted back to the source channel.

Steer a running task at any time (UI steer bar or `POST /api/tasks/{id}/steer`), and **revise** a completed task to continue on the same branch — the existing PR updates in place. `GITHUB_REVIEW_POLLING=true` auto-queues revisions for review comments from repository owners, members, and collaborators.

## HTTP API

The UI is a thin client over a plain REST API:

```
GET  /api/health             GET  /api/stats              GET  /api/models
GET  /api/tasks              POST /api/tasks              {title, description, repo?, model?, budget_usd?, parent_id?}
GET  /api/tasks/{id}         POST /api/tasks/{id}/cancel  POST /api/tasks/{id}/steer
POST /api/tasks/{id}/approve POST /api/tasks/{id}/reject
GET  /api/tasks/{id}/events  GET  /api/tasks/{id}/stream  (SSE)
GET  /api/stream (SSE)       GET/POST/DELETE /api/repos
GET/PUT /api/config/agents   POST /api/chat               DELETE /api/chat/{id}
GET/POST/DELETE /api/resources                            POST /webhooks/linear
```

## Development

```bash
uv sync
uv run pytest
uv run sde-deepagent
```

Layout: `src/sde_deepagent/` — `agent_factory.py` (deepagents wiring) · `runner.py` (task pipeline) · `worker.py` (queue + budgets) · `gitops.py` (git/PR) · `sandbox.py` (container sandbox) · `memory.py` (Supermemory client) · `chat.py` (task-history assistant) · `pricing.py` (cost tracking) · `intake/` (telegram/slack/linear) · `server.py` (API + SSE) · `ui/` (static SPA, no build step).
