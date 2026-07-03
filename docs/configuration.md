# Configuration

All runtime settings come from `.env` (see [`.env.example`](../.env.example)
for the full annotated list). Agent and repository configuration live in
`config/` and are hot-editable from the UI.

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` | model providers (at least one required) |
| `GITHUB_TOKEN`, `GITHUB_API_URL` | push branches + open PRs (GitHub / GHE) |
| `AUTH_TOKEN` | bearer token for the API/UI; required for any non-loopback bind |
| `SDE_DATA_DIR` | absolute host data path (task DB + workspaces); required by Compose |
| `SDE_IMAGE_TAG` | compose image tag (default `latest`) |
| `SUPERMEMORY_BASE_URL`, `SUPERMEMORY_API_KEY` | long-term memory (optional) |
| `FIRECRAWL_API_URL` / `FIRECRAWL_API_KEY` | JS-rendering scraper for the Resources page (optional) |
| `TELEGRAM_BOT_TOKEN` | Telegram intake (optional) |
| `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` | Slack intake, Socket Mode (optional) |
| `LINEAR_WEBHOOK_SECRET` + `LINEAR_OAUTH_TOKEN` (seatless) or `LINEAR_API_KEY` (polling) | Linear intake (optional) |
| `TELEGRAM_ALLOWED_CHATS`, `SLACK_ALLOWED_USERS` | authorized chat/user IDs; empty denies task creation, `*` allows all |
| `TASK_BUDGET_USD`, `DAILY_BUDGET_USD` | cost guardrails (0 = unlimited); hitting the daily cap parks running tasks to resume after the UTC midnight reset |
| `REQUIRE_APPROVAL` | hold every task for human review before push/PR |
| `SECRETS_KEY` | enables UI-entered per-repo secrets, encrypted at rest |
| `SANDBOX_DEFAULT`, `SANDBOX_IMAGE`, `SANDBOX_NETWORK`, `SANDBOX_IDLE_HOURS` | sandbox defaults (on; `buildpack-deps:bookworm`; `bridge`; reused for 7 idle days) |
| `RETENTION_DAYS`, `REF_CLONE_TTL_MINUTES`, `REF_CLONE_RETENTION_DAYS` | bounded growth: prune old events/chat spend after 90 days, refresh chat's reference clones every 15 minutes, drop unused clones after 14 days |
| `MAX_CONCURRENT_TASKS`, `TASK_TIMEOUT_SECONDS`, `WORKSPACE_RETENTION`, `USAGE_FLUSH_SECONDS` | runtime tuning |
| `GITHUB_REVIEW_POLLING`, `GITHUB_REVIEW_POLL_SECONDS` | auto-queue revisions for PR review comments |

## Supported models

Set models per role in `config/agents.yaml` (or UI > Agents) as
`provider:model`. Any model from a known provider works; the picks below are
the UI dropdown shortcuts.

| Provider | Prefix | Curated picks |
|---|---|---|
| Anthropic | `anthropic:` | `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` |
| Google Gemini | `google_genai:` | `gemini-3.1-pro-preview`, `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite` |
| OpenAI | `openai:` | `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5-mini`, `gpt-4.1`, `o3`, `o4-mini` |

Each role also takes an `effort: low | medium | high` knob that maps to the
provider's native control (OpenAI `reasoning_effort`, Anthropic `effort`,
Gemini `thinking_level`).

## Agents (`config/agents.yaml`)

Pick the orchestrator and per-subagent models/effort, plus optional
`mcp_servers:` (extra tools for the orchestrator) and `pricing:` overrides.
System prompts are editable per role in the UI, with a reset to the built-in
default.

```yaml
orchestrator:
  model: openai:gpt-5.4
  effort: medium
subagents:
  explorer:  { model: anthropic:claude-haiku-4-5-20251001 }
  coder:     { model: google_genai:gemini-2.5-pro }   # mix providers freely
  reviewer:  { model: openai:o4-mini }
```

## Codebases (`config/repos.yaml`)

The `description` powers automatic task-to-repo routing; `test` is how the
agent verifies its work.

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

Per-repo knobs: `sandbox: true|false`, `sandbox_network: none|bridge`,
`approval: auto|required`, `sandbox_image`, `secrets` (below).

## Per-repo secrets

For setup/test commands that need credentials. Two ways to supply them, mixed
freely per repo:

1. Reference a host env var: `secrets: { DB_URL: env:BACKEND_DB_URL }`. No
   app-side storage.
2. Store a value encrypted at rest: enter it in the UI (Codebases > Secrets),
   which sets `DB_URL: store`. Requires `SECRETS_KEY`.

Values are injected *only* into the controller-run `setup`/`test` commands,
never the agent's own shell, and redacted (raw/base64/url-encoded) from every
output sink. Residual risk: tests run the repo's own code with the secret in
the environment, so attach secrets only to repos you trust, prefer
`sandbox_network: none`, and keep `REQUIRE_APPROVAL=true`.

## Company context

Give the agents standing knowledge:

- per-repo doc globs (`context:` in `repos.yaml`),
- convention files picked up automatically (`AGENTS.md`, `CLAUDE.md`, ...),
- a global `context/` folder mounted into every task,
- the **Resources** page, which ingests any URL or pasted text into long-term
  memory (GitHub URLs also let the chat assistant read that repo's source).
