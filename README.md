# sde-deepagent

**A self-hostable software developer agent system.** Assign it a task from the
web UI, Telegram, Slack, or Linear, and it clones the right codebase, plans,
implements the change, runs the tests, and opens a pull request.

[![CI](https://github.com/skundu42/sde-deepagent/actions/workflows/ci.yml/badge.svg)](https://github.com/skundu42/sde-deepagent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/skundu42/sde-deepagent)](https://github.com/skundu42/sde-deepagent/releases)
[![Container](https://img.shields.io/badge/ghcr.io-skundu42%2Fsde--deepagent-blue)](https://github.com/skundu42/sde-deepagent/pkgs/container/sde-deepagent)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Built on [LangChain deepagents](https://docs.langchain.com/oss/python/deepagents/overview).
Bring a model API key (Anthropic, Google Gemini, and/or OpenAI) and Docker;
everything else (queue, storage, UI, event streaming, long-term memory) runs
on your own infrastructure.

## Highlights

- **Full task pipeline**: isolated git workspace, plan, implement, test until
  green, diff review, commit, push, GitHub PR
- **Orchestrator + subagents** (explorer / coder / tester / reviewer), model
  and reasoning effort configurable per role, providers freely mixed
- **Multi-channel intake**: web UI, allowlisted Telegram/Slack, Linear
  (seatless agent sessions or polling), results posted back to the source
- **Sandboxed execution**: each repo's shell runs in its own reused Docker
  container
- **Human oversight**: approval gate, mid-task steering, one-click PR
  revisions on the same branch
- **Cost control and resilience**: per-task and daily budgets, automatic retry
  and resume on provider hiccups and restarts
- **Long-term memory and chat**: agents recall past learnings per codebase;
  ask the assistant about any task, repo, or ingested doc

## Quick start

Runs the prebuilt multi-arch image from
[GHCR](https://github.com/skundu42/sde-deepagent/pkgs/container/sde-deepagent);
nothing compiles on your box:

```bash
git clone https://github.com/skundu42/sde-deepagent.git && cd sde-deepagent
cp .env.example .env        # set AUTH_TOKEN, SDE_DATA_DIR (absolute path), a model key, GITHUB_TOKEN
docker compose up -d        # pulls ghcr.io/skundu42/sde-deepagent:latest

# first boot only: copy supermemory's generated key into .env, then recreate
docker compose logs supermemory | grep "api key"
echo 'SUPERMEMORY_API_KEY=sm_...' >> .env && docker compose up -d
```

Then open http://localhost:8321, register a codebase, and queue your first
task. Pin a version with `SDE_IMAGE_TAG=0.3.0` in `.env`; run from source with
`docker compose up -d --build`, or without Docker at all via
`uv sync && uv run sde-deepagent` (see the [deployment guide](docs/deployment.md)).

## Documentation

| Guide | Contents |
|---|---|
| [Deployment](docs/deployment.md) | local and production setup, security posture, sidecars |
| [Configuration](docs/configuration.md) | environment variables, models, agents, codebases, secrets, company context |
| [Architecture](docs/architecture.md) | how a task runs, reliability, sandboxing, security model |
| [HTTP API](docs/api.md) | REST + SSE endpoints behind the UI |
| [Development](docs/development.md) | working on the backend and web UI, CI, cutting releases |

## How it works

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

A single process (FastAPI + asyncio worker pool + SQLite) with a React web UI
and an optional Supermemory sidecar. Every message, tool call, shell command
and token is persisted and streamed live over SSE.

## Contributing

Issues and pull requests are welcome. Start with the
[development guide](docs/development.md); `uv run pytest` and
`uv run ruff check src tests` must pass, and UI changes should ship a
rebuilt `web/dist`.

## License

[MIT](LICENSE)
