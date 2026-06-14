# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Crash resume.** Agent state is now persisted to a SQLite checkpoint DB
  (`data/checkpoints.sqlite`) keyed per task, so a task interrupted by a server
  restart resumes from its last checkpoint instead of being marked failed — the
  workspace clone is reused so the agent's on-disk edits stay consistent with the
  restored conversation. Disable with `CHECKPOINT_RESUME=false`. Requires
  `langgraph-checkpoint-sqlite`; degrades to in-memory (no cross-restart resume)
  if absent. Stale checkpoints with no surviving workspace are cleared and the
  task starts clean.

### Changed

- The budget now charges models absent from the price table at the priciest known
  rate (fail-safe) instead of `$0`, so a typo'd/unknown model id can no longer slip
  past the per-task and daily caps. The first unpriced response is now flagged in
  the task log, and the fail-safe ceiling can't be lowered by a cheap `pricing:`
  override. Malformed/negative overrides are ignored instead of crashing a task.

### Fixed

- **Intake no longer creates duplicate tasks.** Telegram (offset resets to 0 on
  restart), Linear (in-memory dedup lost on restart; old `_already_tracked` only
  scanned the last 500 tasks), and a Linear poll-vs-webhook race could each
  re-ingest the same source event. Task creation now dedups on a stable
  `dedup_key` enforced by a UNIQUE index, so a re-delivered event or a concurrent
  poll+webhook resolves to one task.
- **Live UI updates survive entering a token.** EventSource can't carry a bearer
  token added after it opens, so the streams created at boot stayed 401'd once
  `AUTH_TOKEN` was set; the SSE streams are now re-opened (and the current task
  view re-subscribed) when the token changes, and a concurrent 401 no longer
  double-prompts.
- **Approval tasks from Telegram/Slack/Linear are no longer reported back as
  "❌ failed".** A task parked for approval is non-terminal; it now posts an
  "⏸ awaiting your approval" notice and the channel gets the real result on
  approve/reject.
- **Revisions now finalize correctly.** The revision clone is single-branch, so it
  lacked `origin/<default_branch>` that `commits_ahead`/`diff_stat` compare
  against — the finalize and approval gates were silently seeing 0 commits and
  dropping the revision's work. The default-branch ref is now fetched into the
  revision clone.
- **GitHub review feedback is no longer lost** when it arrives while a prior
  auto-revision is still running (the seen-floor was advanced before the
  open-revision check; it now advances only when feedback is consumed).
- **Telegram `/task@botname` (group chats) and `/taskfoo`** are parsed correctly
  instead of leaking `@botname`/`foo` into the task text.
- **Chat sessions no longer break on long tool-heavy histories** — trimming kept a
  tool result orphaned from its tool call, which providers reject; trimming now
  starts on a turn boundary.
- **Company-context dotfiles** (e.g. a real `.env`) are no longer copied into the
  agent's workspace while being hidden from its listing.
- A revision with an explicit repo different from its parent now correctly targets
  the parent's repo.
- Cancelling a queued/awaiting-approval task now emits a status event (live UI
  update) and sets `finished_at`.
- A bad per-task model override is rejected at task creation (400) instead of
  failing deep inside the run.
- UI: tool-call args are sliced before HTML-escaping, so truncation can't split an
  HTML entity.
- Keep deepagents' summarization offload file (`conversation_history/`) out of the
  agent's commits/PR by adding it to the per-workspace git excludes.
- Bound `checkpoints.sqlite` growth: a task's checkpoint is deleted once the run
  ends (it is only needed to resume a run the process died inside).
- Re-queuing an interrupted task no longer nulls `started_at`, so its
  already-persisted cost stays counted in the daily-budget window.
- A resumed run carries forward the interrupted run's persisted spend instead of
  restarting its per-task budget at `$0`.
- `_finalize` honors a PR URL persisted by a prior run, avoiding a redundant
  push/PR round-trip on resume.

## [0.2.1] - 2026-06-13

A hardening and deployment-fix release on top of 0.2.0. Closes an approval-gate
bypass, makes the Telegram/Slack intakes fail closed, gates GitHub
auto-revisions to trusted authors, makes repo filesystem identifiers
collision-resistant, fixes the Docker Compose sandbox path, and extends the
per-repo sandbox reuse window to 7 days.

### Security

- Per-repo approval overrides now reach the agent prompt and PR tool before any
  push, so `approval: required` cannot be bypassed when the global default is
  automatic.
- Telegram and Slack task creation now fail closed unless explicit chat/user
  allowlists are configured. GitHub auto-revisions accept feedback only from
  repository owners, members, and collaborators.
- Normalized repository filesystem identifiers now include a stable hash when
  needed, preventing distinct names from sharing a workspace/sandbox boundary.

### Changed

- Per-repo sandbox containers are now reaped after 7 idle days
  (`SANDBOX_IDLE_HOURS=168`) instead of 24 hours, so a repo's installed
  toolchain and caches survive up to a week of inactivity before cleanup.

### Fixed

- Docker Compose now includes the Docker CLI, mounts the host Docker socket,
  and uses an identical absolute `SDE_DATA_DIR` path on host and controller so
  the sandbox can actually start.
- Updated the lockfile and deployment/development documentation to match the
  current release and sandbox defaults.

## [0.2.0] - 2026-06-13

A security- and isolation-focused release. The agent's shell now runs inside a
per-repo container sandbox by default, the control plane can require an auth
token, secrets are kept away from the model, and untrusted content is framed as
data to resist prompt injection. Adds mid-task steering, an automatic PR review
loop, and CI. Test suite grew from 105 to 225 passing tests.

### Added

- **Container sandbox (on by default).** The agent's shell runs in a Docker
  container with the workspace mounted and a configurable egress policy. Each
  repo gets one persistent, language-agnostic container (`sde-repo-<slug>`,
  based on `buildpack-deps:bookworm`) that is reused across tasks so toolchains
  and dependency caches persist; a background reaper removes idle containers
  after 24 hours (`SANDBOX_IDLE_HOURS`). The agent bootstraps its own
  environment (`apt`/`pip`/`npm`/…) inside the container — register a repo,
  assign a task, no host setup required. Fails closed when Docker is
  unavailable. Lock egress down with `SANDBOX_NETWORK=none` or opt out with
  `SANDBOX_DEFAULT=false`.
- **Per-repo secrets, kept away from the LLM** (`secrets.py`). A repo's
  `secrets` map references host env vars by name (`DATABASE_URL: env:BACKEND_DB_URL`)
  or the encrypted `store`; `repos.yaml` and the API only ever carry the
  reference, never the value. Resolved values are injected into registered
  setup/test commands only — never into the agent's own shell — and a
  `Redactor` masks them from every output sink. Reserved names that could
  hijack the execution environment (`PATH`, `HOME`, `LD_PRELOAD`, …) are
  rejected.
- **Optional API authentication** (`auth.py`). Set `AUTH_TOKEN` to require a
  bearer token on API and SSE requests (header or `?token=` for SSE); health
  and webhook endpoints stay exempt, and the UI prompts for and remembers the
  token. Without a token the entire control plane is restricted to loopback
  clients regardless of the server's bind address.
- **Mid-task steering.** Send follow-up guidance to a running task via
  `POST /api/tasks/{id}/steer`; the agent picks it up through a new
  `check_messages` tool, surfaced with a steering bar in the UI.
- **Automatic review loop** (`intake/github_reviews.py`). Polls PRs the agent
  opened for new review comments and auto-queues a revision task
  (`GITHUB_REVIEW_POLLING`).
- **Per-repo approval policy** (`auto` | `required`) that overrides the global
  default for diff review before commit/push.
- **Continuous integration.** GitHub Actions runs ruff + pytest + a Docker
  build on every push and pull request.

### Changed

- Workspaces moved to `workspaces/<repo_slug>/<task_id>/repo` so each container
  bind-mounts one stable per-repo parent and sees only that repo's checkouts.
- The orchestrator prompt now instructs the agent to provision its own
  toolchain inside the sandbox instead of relying on a pre-baked image.
- Container creation and setup commands run off the event loop so image pulls
  no longer block the server.

### Security

- **Prompt-injection resistance.** Untrusted repo, web, and long-term-memory
  content is now framed as data rather than instructions.
- **Git hardening** (`gitops.py`). Tightened credential handling and git
  invocation so tokens are not exposed to the agent or leaked into output.
- **Linear webhook now fails closed.** The `/webhooks/linear` endpoint returns
  403 until `LINEAR_WEBHOOK_SECRET` is set, and verifies the HMAC signature
  when it is (previously fail-open).
- **SSRF and output hardening** across `webfetch.py` and the intake channels
  (Linear, Slack, Telegram), plus assorted fixes in `config`, `context`, `db`,
  and `chat`.

### Fixed

- **Daily budget hard cap** is now enforced correctly, stopping work once the
  configured spend limit is reached.
- Sandbox bind mount resolves the workspace to an absolute path (`docker -v`
  treated the relative path as a named volume, so the container failed to
  start).
- Docker build copies `LICENSE` so the hatchling build no longer fails; CI
  action versions bumped.
- Adding a repository from the web UI no longer fails.

## [0.1.0] - 2026-06-12

Initial release: orchestrator + specialist subagents, full clone → plan →
implement → test → review → PR pipeline, multi-channel intake (web UI,
Telegram, Slack, Linear), per-role model/effort configuration, daily budget
tracking, and long-term memory.

[0.2.1]: https://github.com/skundu42/sde-deepagent/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/skundu42/sde-deepagent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/skundu42/sde-deepagent/releases/tag/v0.1.0
