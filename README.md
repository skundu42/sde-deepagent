# devagent

A self-hostable software developer agent system built on
[LangChain deepagents](https://docs.langchain.com/oss/python/deepagents/overview).

Assign it a task — from the **web UI, Telegram, Slack, or Linear** — and it
clones the right codebase, plans, implements the change, runs the tests, and
opens a pull request.

```
 Telegram ─┐
 Slack ────┤                ┌─ orchestrator (deep agent)
 Linear ───┼─▶ task queue ─▶│    ├─ explorer   (read-only scout)
 Web UI ───┘       │        │    ├─ coder      (implementation)
                   │        │    ├─ tester     (run/fix/write tests)
                   ▼        │    └─ reviewer   (final diff review)
              SQLite + SSE  └─▶ git branch ─▶ tests ─▶ GitHub PR
```

The **only external dependency is your model API key(s)** (Anthropic, Google
Gemini, and/or OpenAI). Everything else — queue, storage, UI, event streaming — runs in
one self-hosted process. A `GITHUB_TOKEN` is needed only if you want it to push
branches and open PRs on GitHub; chat/issue-tracker keys only if you use those
intakes.

## Quick start

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY
uv sync
uv run devagent             # → http://localhost:8321
```

or with Docker:

```bash
cp .env.example .env
docker compose up --build   # → http://localhost:8321
```

Then, in the UI:

1. **Codebases** → register a repo (git URL or local path, test command,
   context docs).
2. **New task** → describe the work and queue it.
3. Watch the live agent trace; the PR link appears when it ships.

## Giving the agent your company's context

Three layers, all picked up automatically:

| Layer | How |
|---|---|
| Repo conventions | `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` in the repo are always read |
| Repo docs | list globs under `context:` for each repo in `config/repos.yaml` / the UI |
| Company-wide docs | drop files into the `context/` directory — mounted into every task workspace at `_context/` |

## Configuring agents & models

`config/agents.yaml` (also editable in the UI under **Agents**) sets the model
for the orchestrator and each subagent independently:

```yaml
orchestrator:
  model: anthropic:claude-sonnet-4-6
subagents:
  explorer:
    model: anthropic:claude-haiku-4-5-20251001
  coder:
    model: google_genai:gemini-2.5-pro      # mix providers freely
  reviewer:
    model: openai:gpt-5.4
```

Supported providers: `anthropic:*` (Claude), `google_genai:*` (Gemini) and
`openai:*` (GPT / o-series).
Subagent `description` (when the orchestrator delegates to it) and
`system_prompt` are also configurable. Per-task model overrides are available
in the new-task form.

Extra tools can be wired in from any **MCP server** under `mcp_servers:` in the
same file (stdio or HTTP transports) — they're handed to the orchestrator.

## Long-term memory (Supermemory)

Agents get persistent, cross-task memory backed by a
[self-hosted Supermemory](https://supermemory.ai/docs/self-hosting/overview)
instance — a single local binary with built-in embeddings, no extra database.

```bash
npx supermemory local        # starts on :6767 and prints an sm_ API key
# (with Docker, the compose file already runs it: docker compose logs supermemory)
```

Put both values in `.env`:

```bash
SUPERMEMORY_BASE_URL=http://localhost:6767
SUPERMEMORY_API_KEY=sm_...
```

What it does once enabled:

- **`search_memory`** — the orchestrator *and all subagents* can recall
  conventions, gotchas, architecture notes and decisions recorded by previous
  tasks before exploring from scratch.
- **`save_memory`** — the orchestrator records durable learnings before
  opening a PR (scoped per codebase, or `global` for org-wide facts).
- **Automatic outcomes** — every completed task's summary + PR link is stored
  automatically, so the agent always knows what was recently changed and why.

Memories are partitioned per codebase (`devagent_repo_<name>`) plus a shared
`devagent_global` container. If the memory server is down, agents degrade
gracefully to working without it. Without the `SUPERMEMORY_*` vars the feature
is simply off (sidebar shows `memory off`).

Notes: supermemory itself needs one LLM credential to boot (it reuses your
`ANTHROPIC_API_KEY`/`GOOGLE_API_KEY`/`OPENAI_*` from the environment, or
`~/.supermemory/env`). Embeddings run locally, and devagent searches in
`hybrid` mode — so recall works immediately and even fully offline; the LLM
only enriches memory extraction.

### Resources page — feed it your company's links & docs

The **Resources** page (UI → 06) ingests any URL or pasted text into long-term
memory: documentation sites, runbooks, conventions, onboarding notes — scoped
either globally or to one codebase. Agents and the chat assistant recall this
content automatically via `search_memory`.

URL fetching has two tiers: with **Firecrawl** configured (self-hosted from
[firecrawl/firecrawl](https://github.com/firecrawl/firecrawl), or their cloud
API via `FIRECRAWL_API_KEY`), pages are JS-rendered and converted to clean
markdown — SPAs and modern docs portals extract properly. Without it, devagent's
built-in fetcher handles static pages. Firecrawl failures fall back to the
built-in fetcher automatically.

```bash
FIRECRAWL_API_URL=http://localhost:3002   # self-hosted, no key needed
# or
FIRECRAWL_API_KEY=fc-...                  # cloud (api.firecrawl.dev)
```

## Task intake

| Channel | Setup | Usage |
|---|---|---|
| Web UI | none | **New task** form |
| Telegram | `TELEGRAM_BOT_TOKEN` (via @BotFather); long-polling, no public URL | message the bot: `[backend] fix the login bug` |
| Slack | `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` (Socket Mode, no public URL) | @mention the bot or DM it |
| Linear | `LINEAR_API_KEY`; label issues `agent` (configurable) | polled every 30s; optional instant webhook at `/webhooks/linear` |

Every channel gets the result back where the task came from (reply, thread, or
issue comment) with the PR link.

`[repo-name]` or `repo=repo-name` at the start of a message pins the codebase;
otherwise devagent routes the task to the best-matching registered repo.

## How a task runs

1. **Resolve** the target codebase (explicit, or model-routed by repo descriptions).
2. **Clone** into an isolated per-task workspace, create branch `agent/<id>-<slug>`, run the repo's setup command.
3. **Run the deep agent** — filesystem + shell tools are rooted to the workspace; it plans (`write_todos`), delegates to subagents, implements, and runs the repo's test command until green.
4. **Review** — the reviewer subagent checks the final diff.
5. **Ship** — commit, push, open the PR (the agent calls `open_pull_request`; if it forgets and left committed work, the runner auto-finalizes — disable with `AUTO_FINALIZE=false`). With `REQUIRE_APPROVAL=true`, nothing is pushed: the task parks as **awaiting approval** with the diff in the UI, and an operator ships or rejects it with one click.
6. **Report** back to the source channel and the UI.

Need changes after review? Hit **revise** on a completed task (or POST a task
with `parent_id`): the agent continues on the same branch with the original
context plus your feedback, and the existing PR updates in place.

Each role in `agents.yaml` also takes `effort: low|medium|high` to control
reasoning depth (mapped to OpenAI `reasoning_effort`, Anthropic `effort`,
Gemini `thinking_level`), and every agent starts with an auto-generated
repository map (files, line counts, top-level symbols) so it navigates instead
of exploring blindly.

Every step is streamed live (SSE) to the UI and persisted in SQLite, so you can
audit any past run's full trace: messages, tool calls, shell output, todos,
subagent activity, and token usage.

## API

The UI is a thin client over a plain REST API you can script against:

```
GET  /api/health             GET  /api/stats
GET  /api/tasks              POST /api/tasks {title, description, repo?, model?}
GET  /api/tasks/{id}         POST /api/tasks/{id}/cancel
GET  /api/tasks/{id}/events  GET  /api/tasks/{id}/stream   (SSE)
GET  /api/stream (SSE)       GET/POST/DELETE /api/repos
GET/PUT /api/config/agents   POST /webhooks/linear
```

## Cost budgets & spend tracking

Every agent step's token usage is priced (built-in table for current Claude,
Gemini and OpenAI models — override under `pricing:` in `config/agents.yaml` when prices
drift) and recorded per task. The UI shows per-task cost live and today's total
spend in the sidebar.

Two enforcement levers, both off by default:

```bash
TASK_BUDGET_USD=5     # any single run aborts (task fails) past this spend;
                      # a warning event fires at 80%. Per-task override in the UI.
DAILY_BUDGET_USD=50   # once today's recorded spend (UTC) reaches this, the
                      # queue pauses — queued tasks wait until midnight UTC.
```

Cache reads/writes are priced with provider multipliers (Anthropic ~0.1×/1.25×,
Gemini reads ~0.25×). Unknown models are tracked by tokens, priced at $0, and
flagged in the task's usage summary as `unpriced_models` — add them to
`pricing:` to bring them under budget control.

Chat usage counts too: every chat turn's cost is recorded (shown per reply and
as `spend_today_chat_usd` in `/api/stats`), included in the daily total, and
chat returns 429 once the daily budget is exhausted.

## Security notes

- Agent shell commands run **on the host**, scoped to the task workspace, with
  a sanitized environment (your API keys are not exposed to the agent's shell).
  Git credentials are passed to clone/push via process-scoped ephemeral config —
  the `GITHUB_TOKEN` is never written into the workspace's `.git/config`.
  Run the Docker image for stronger isolation.
- Restrict Telegram with `TELEGRAM_ALLOWED_CHATS`; set `LINEAR_WEBHOOK_SECRET`
  if you expose the webhook.
- The web UI has no auth — keep it on localhost, a VPN/tailnet, or behind a
  reverse proxy with auth.

## Development

```bash
uv sync
uv run pytest        # 38 tests
uv run devagent
```

Layout: `src/devagent/` — `agent_factory.py` (deepagents wiring),
`runner.py` (task pipeline), `worker.py` (queue), `gitops.py` (git/PR),
`intake/` (telegram/slack/linear), `server.py` (API + SSE), `ui/` (static SPA).
