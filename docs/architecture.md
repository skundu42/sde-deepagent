# Architecture

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

Single process: FastAPI server, asyncio worker pool, SQLite persistence, and a
prebuilt React web UI served as static files. Supermemory runs as a sidecar
(compose service or local binary). Built on
[LangChain deepagents](https://docs.langchain.com/oss/python/deepagents/overview).

## How a task runs

1. **Resolve** the target codebase (explicit, or model-routed by repo
   descriptions).
2. **Clone** into an isolated workspace on branch `agent/<id>-<slug>`; start
   (or reuse) the repo's sandbox container and run its setup command,
   otherwise the agent installs what it needs on the fly.
3. **Recall**: search long-term memory for conventions and prior learnings
   about this codebase.
4. **Plan and implement**: todo-list planning, delegation to subagents, real
   shell + filesystem scoped to the workspace.
5. **Test** until green with the repo's test command.
6. **Review**: the reviewer subagent audits the final diff.
7. **Ship**: commit, push, open the PR. With `REQUIRE_APPROVAL=true` the task
   instead parks as *awaiting approval* with the diff in the UI for one-click
   ship/reject.
8. **Record and report**: durable learnings and the outcome are saved to
   memory; the result (with PR link) is posted back to the source channel.

## Reliability

- **Transient provider failures** (connection drops, 429s, 5xx) retry
  automatically with backoff and resume from the last checkpoint, so completed
  steps are never re-billed.
- **Restarts**: agent state is checkpointed per task; a task interrupted by a
  server restart re-queues and resumes from its last checkpoint with its
  workspace intact.
- **Budgets**: per-task caps abort runaway runs; the daily cap parks running
  tasks (checkpoint kept) and the queue resumes after the UTC midnight reset.
  Spend is persisted mid-run, so a crash cannot lose it.
- **Bounded growth**: runaway command output is capped in memory; old events,
  chat spend and unused reference clones are pruned on a daily retention
  sweep.

## Steering and revisions

Steer a running task at any time (UI steer bar or
`POST /api/tasks/{id}/steer`); the agent picks the message up at its next
check. **Revise** a completed task to continue on the same branch; the
existing PR updates in place. With `GITHUB_REVIEW_POLLING=true`, review
comments from repository owners, members and collaborators auto-queue
revisions.

## Sandboxing

Each repo's shell runs in its own Docker container (`sde-repo-<slug>`, from
`SANDBOX_IMAGE`), reused across tasks so toolchains and dependency caches
persist; idle containers are reaped after `SANDBOX_IDLE_HOURS`. The agent
bootstraps its own environment inside the container. Fails closed when Docker
is unavailable. Lock egress with `SANDBOX_NETWORK=none`, or opt out entirely
with `SANDBOX_DEFAULT=false`.

## Security model

- Untrusted content (repo files, web pages, memory) is framed as data, not
  instructions, to resist prompt injection.
- Git credentials travel via ephemeral `GIT_CONFIG_*` environment variables,
  scoped to trusted hosts; they are never written into the workspace.
- Secrets are injected only into controller-run setup/test commands and
  redacted from all output sinks.
- The control plane requires `AUTH_TOKEN` for any non-loopback access; the
  public health endpoint exposes only liveness and version.
