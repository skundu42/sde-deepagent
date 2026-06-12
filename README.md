# sde-deepagent

**A self-hostable software developer agent system.** Assign it a task — from the web UI, Telegram, Slack, or Linear — and it clones the right codebase, plans, implements the change, runs the tests, and opens a pull request.

Built on [LangChain deepagents](https://docs.langchain.com/oss/python/deepagents/overview). The only required external dependency is a model API key (Anthropic, Google Gemini, and/or OpenAI); everything else — queue, storage, UI, event streaming, long-term memory — runs on your own infrastructure.

![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue) ![self--hosted](https://img.shields.io/badge/deployment-self--hosted-green) ![tests](https://img.shields.io/badge/tests-105%20passing-brightgreen) ![license](https://img.shields.io/badge/license-MIT-blue)

---

## Features

- **Orchestrator + specialist subagents** (explorer / coder / tester / reviewer) — model *and* reasoning effort configurable per role, providers freely mixed
- **Full task pipeline**: isolated git workspace → plan → implement → test until green → diff review → commit → push → GitHub PR
- **Multi-channel intake**: web UI, Telegram (long-polling), Slack (Socket Mode), Linear (label polling + webhook) — no public URL required; results are reported back to the originating channel
- **Long-term memory** (self-hosted [Supermemory](https://supermemory.ai/docs/self-hosting/overview)): agents recall conventions and gotchas from past tasks and record new learnings; every task outcome is stored per codebase
- **Company context**: per-repo doc globs, repo convention files (`AGENTS.md`, `CLAUDE.md`, …), a global `context/` folder, and a **Resources** page that ingests any URL or text into memory (optional Firecrawl for JS-rendered pages)
- **Cost control**: per-message cost tracking, per-task budgets that abort runaway runs, a daily budget that pauses the queue, live spend in the UI
- **Human oversight**: optional approval gate (nothing is pushed until an operator reviews the diff), one-click PR revisions that reuse the branch, full live trace of every agent step
- **Chat**: ask an assistant about any past or running task — grounded in the actual task records, traces, and memory
- **Observability**: every message, tool call, shell command, and token persisted in SQLite and streamed live over SSE

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

## Quick start (local)

```bash
git clone https://github.com/skundu42/sde-deepagent.git && cd sde-deepagent
cp .env.example .env        # add ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY
uv sync
uv run sde-deepagent             # → http://localhost:8321
```

Then in the UI: **Codebases** → register a repo (git URL or local path, test command) → **New task** → watch the live agent trace; the PR link appears when it ships.

## Deploying on a server

The recommended production setup is Docker Compose behind a TLS reverse proxy. A 2 vCPU / 4 GB VM is sufficient (no GPU — local embeddings run quantized on CPU; all LLM inference is API-side).

### 1. Provision

```bash
# Ubuntu/Debian: install Docker Engine + compose plugin
curl -fsSL https://get.docker.com | sh

git clone https://github.com/skundu42/sde-deepagent.git /opt/sde-deepagent
cd /opt/sde-deepagent
cp .env.example .env
```

Edit `.env` with at minimum one model key, and a `GITHUB_TOKEN` so the agent can push branches and open PRs (classic PAT with `repo` scope, or a fine-grained PAT with *Contents* and *Pull requests* read/write):

```bash
OPENAI_API_KEY=sk-...        # and/or ANTHROPIC_API_KEY / GOOGLE_API_KEY
GITHUB_TOKEN=ghp_...
DAILY_BUDGET_USD=50          # strongly recommended on a server
REQUIRE_APPROVAL=true        # recommended until you trust it unattended
```

### 2. Lock the ports to localhost

The UI and API have **no built-in authentication** — they must only be reachable through your reverse proxy. Create a `docker-compose.override.yml`:

```yaml
services:
  sde-deepagent:
    ports: !override
      - "127.0.0.1:8321:8321"
  supermemory:
    ports: !override
      - "127.0.0.1:6767:6767"
```

### 3. Start and wire up memory

```bash
docker compose up -d --build

# first boot only: copy supermemory's generated API key into .env
docker compose logs supermemory | grep "api key"
echo 'SUPERMEMORY_API_KEY=sm_...' >> .env       # paste the printed key
docker compose up -d                            # recreate sde-deepagent with the key
```

`SUPERMEMORY_BASE_URL` is already pointed at the sidecar by the compose file. Verify: `curl -s localhost:8321/api/health` should show `"ok": true` and `"memory": true`.

### 4. Reverse proxy with TLS + auth

Any proxy works; [Caddy](https://caddyserver.com) is the shortest path to TLS + basic auth (generate the hash with `caddy hash-password`):

```caddyfile
sde-deepagent.yourdomain.com {
    basic_auth {
        admin $2a$14$...hashed-password...
    }
    reverse_proxy 127.0.0.1:8321
}
```

SSE streaming works through Caddy/nginx out of the box (the API sets `X-Accel-Buffering: no`). If you use the Linear webhook, set `LINEAR_WEBHOOK_SECRET` and expose only `/webhooks/linear` unauthenticated. Telegram and Slack intakes poll outbound — they need no inbound route at all.

### 5. Operations

| Concern | How |
|---|---|
| Update | `git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f devagent` |
| Backup | `data` volumes (`devagent-data`: task DB + workspaces, `supermemory-data`: memories) plus `.env` and `config/` on the host |
| Budget guardrail | `DAILY_BUDGET_USD` pauses the queue at the cap (UTC day); per-task caps via `TASK_BUDGET_USD` or the new-task form |
| Crash recovery | tasks interrupted by a restart are marked failed (never silently lost); `awaiting_approval` work survives restarts |

<details>
<summary><b>Alternative: bare-metal with systemd (no Docker)</b></summary>

```bash
# as a dedicated user, in /opt/sde-deepagent
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --no-dev
curl -fsSL https://supermemory.ai/install | bash   # installs supermemory-server
```

`/etc/systemd/system/supermemory.service`:

```ini
[Unit]
Description=Supermemory local server
After=network-online.target

[Service]
User=sde-deepagent
WorkingDirectory=/opt/sde-deepagent
EnvironmentFile=/opt/sde-deepagent/.env
Environment=SUPERMEMORY_DATA_DIR=/opt/sde-deepagent/data/supermemory
ExecStart=/home/sde-deepagent/.local/bin/supermemory-server
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/sde-deepagent.service`:

```ini
[Unit]
Description=sde-deepagent
After=network-online.target supermemory.service

[Service]
User=sde-deepagent
WorkingDirectory=/opt/sde-deepagent
ExecStart=/home/sde-deepagent/.local/bin/uv run --no-sync sde-deepagent
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now supermemory sde-deepagent
```

sde-deepagent reads `.env` from its working directory; `git` must be on the PATH.
</details>

## Configuration

All runtime settings come from `.env` (see [`.env.example`](.env.example) for the full annotated list):

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` | model providers (≥ 1 required) |
| `GITHUB_TOKEN`, `GITHUB_API_URL` | push branches + open PRs (GitHub / GHE) |
| `SUPERMEMORY_BASE_URL`, `SUPERMEMORY_API_KEY` | long-term memory (optional) |
| `FIRECRAWL_API_URL` / `FIRECRAWL_API_KEY` | JS-rendering scraper for the Resources page (optional) |
| `TELEGRAM_BOT_TOKEN`, `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`, `LINEAR_API_KEY` | intake channels (each optional) |
| `TASK_BUDGET_USD`, `DAILY_BUDGET_USD` | cost guardrails (0 = unlimited) |
| `REQUIRE_APPROVAL` | hold every task for human review before push/PR |
| `MAX_CONCURRENT_TASKS`, `TASK_TIMEOUT_SECONDS`, `WORKSPACE_RETENTION` | runtime tuning |

### Agents & models — `config/agents.yaml` (or UI → Agents)

```yaml
orchestrator:
  model: openai:gpt-5.4
  effort: medium            # low | medium | high — reasoning depth per role
subagents:
  explorer:
    model: anthropic:claude-haiku-4-5-20251001
  coder:
    model: google_genai:gemini-2.5-pro     # mix providers freely
  reviewer:
    model: openai:o4-mini
```

Providers: `anthropic:*`, `google_genai:*`, `openai:*`. Effort maps to each provider's native knob (OpenAI `reasoning_effort`, Anthropic `effort`, Gemini `thinking_level`). Subagent descriptions and system prompts are also configurable, as are extra **MCP servers** (`mcp_servers:`) whose tools are handed to the orchestrator, and **pricing overrides** (`pricing:`) when provider prices drift.

### Codebases — `config/repos.yaml` (or UI → Codebases)

```yaml
repos:
  backend:
    url: git@github.com:acme/backend.git
    default_branch: main
    description: "Python FastAPI monolith serving the public API"
    setup: "uv sync"
    test: "uv run pytest -x -q"
    context:
      - docs/architecture.md
```

The `description` powers automatic task→repo routing; `test` is how the agent verifies its work.

## Giving the agent your company's context

| Layer | How |
|---|---|
| Repo conventions | `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` in the repo are always read |
| Repo docs | globs under `context:` per repo |
| Company-wide docs | files in the `context/` directory — mounted into every workspace |
| **Resources page** (UI → 06) | paste any URL or text → fetched, extracted, and indexed into long-term memory, scoped globally or per codebase |

Each agent also receives an auto-generated **repository map** (files, line counts, top-level symbols) so it navigates instead of exploring blindly. URL ingestion uses Firecrawl when configured (JS rendering) and falls back to a built-in fetcher for static pages.

## How a task runs

1. **Resolve** the target codebase (explicit, or model-routed by repo descriptions)
2. **Clone** into an isolated workspace on branch `agent/<id>-<slug>`; run the repo's setup command; build junk (`__pycache__`, `node_modules`, …) is excluded from git at the workspace level
3. **Recall** — search long-term memory for conventions and prior learnings about this codebase
4. **Plan & implement** — todo-list planning, delegation to subagents, real shell + filesystem scoped to the workspace
5. **Test** until green with the repo's test command
6. **Review** — the reviewer subagent audits the final diff
7. **Ship** — commit, push, open the PR. With `REQUIRE_APPROVAL=true` the task instead parks as *awaiting approval* with the diff in the UI for one-click ship/reject
8. **Record & report** — durable learnings and the task outcome are saved to memory; the result (with PR link) is posted back to the source channel

Need changes after human review? Hit **revise** on a completed task (or `POST /api/tasks` with `parent_id`): the agent continues on the same branch with your feedback, and the existing PR updates in place. Tasks that are impossible or underspecified finish *without* a PR and explain what's blocking.

## Cost budgets & spend tracking

Every agent and chat message is priced against a built-in per-model table (override under `pricing:`), including cache-read/write multipliers. Per-task budgets abort runs at the cap (with an 80% warning event); the daily budget pauses the queue and blocks chat until midnight UTC. The UI shows per-task cost live, per-reply chat cost, and today's total in the sidebar. Unknown models are tracked by tokens, priced $0, and flagged — never silently guessed.

## HTTP API

The UI is a thin client over a plain REST API:

```
GET  /api/health             GET  /api/stats              GET  /api/models
GET  /api/tasks              POST /api/tasks              {title, description, repo?, model?, budget_usd?, parent_id?}
GET  /api/tasks/{id}         POST /api/tasks/{id}/cancel
POST /api/tasks/{id}/approve POST /api/tasks/{id}/reject
GET  /api/tasks/{id}/events  GET  /api/tasks/{id}/stream  (SSE)
GET  /api/stream (SSE)       GET/POST/DELETE /api/repos
GET/PUT /api/config/agents   POST /api/chat               DELETE /api/chat/{id}
GET/POST/DELETE /api/resources                            POST /webhooks/linear
```

## Security notes

- Agent shell commands run inside the task workspace with a **sanitized environment** — your API keys are never visible to the agent's shell, and git credentials are passed via process-scoped ephemeral config, never written to disk
- The web UI/API have no built-in auth: bind to localhost and front with an authenticated reverse proxy (see Deployment)
- Restrict Telegram with `TELEGRAM_ALLOWED_CHATS`; set `LINEAR_WEBHOOK_SECRET` if you expose the webhook
- `REQUIRE_APPROVAL=true` guarantees nothing reaches your remotes without human sign-off
- Run the Docker deployment for filesystem isolation of agent shell commands

## Development

```bash
uv sync
uv run pytest        # 105 tests
uv run sde-deepagent
```

Layout: `src/sde-deepagent/` — `agent_factory.py` (deepagents wiring) · `runner.py` (task pipeline) · `worker.py` (queue + budgets) · `gitops.py` (git/PR) · `memory.py` (Supermemory client) · `chat.py` (task-history assistant) · `pricing.py` (cost tracking) · `intake/` (telegram/slack/linear) · `server.py` (API + SSE) · `ui/` (static SPA, no build step).
