# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-07-03

Follow-up fixes from a post-release review: graceful shutdowns no longer lose
running work, daily-budget accounting is exact, chat is capped mid-turn, and
the API token never appears in URLs.

### Security

- **The API token no longer rides in SSE URLs.** The web UI streams
  server-sent events via `fetch()` with the `Authorization` header, and the
  backend no longer accepts `?token=` query parameters at all: query strings
  land in server and reverse-proxy access logs, and this token is
  control-plane access (host-root-equivalent under Compose).

### Fixed

- **Graceful shutdowns park running tasks instead of cancelling them.**
  SIGTERM, `docker stop`, deploys and Ctrl+C used to mark every running task
  `cancelled` and delete its checkpoint; only hard crashes could resume. The
  runner now distinguishes a shutdown interruption from an operator cancel:
  checkpointed tasks return to `queued` with checkpoint and workspace intact
  and resume on the next boot. Operator cancels stay terminal.
- **Daily spend is no longer double-counted after a mid-run flush.** The
  accountant summed persisted task cost plus each live tracker's full cost,
  but the periodic flush writes that cost into the task row while the tracker
  is still live, so a run counted twice and could park work at roughly half
  the configured daily budget. Live trackers now contribute only their
  unpersisted delta.
- **Chat can no longer blow through the daily budget.** Chat turns were
  checked once up front and metered only after completion; a long multi-step
  turn (or several concurrent ones) could overshoot the cap unbounded. Chat
  now streams the agent, meters spend per model response, counts in-flight
  cost in the shared daily accountant, and aborts the turn (HTTP 429) the
  moment the cap is reached; partial spend is recorded even on abort.
- **An explicit per-task budget of 0 now means "no cap".** It was coerced to
  null on the way in and fell back to the server default, so a nonzero
  default could not be overridden to unlimited for one task.
- `/api/health` reports the real version again: `__version__` now derives
  from package metadata instead of a second hardcoded constant (0.3.0 shipped
  reporting 0.2.1).

## [0.3.0] - 2026-07-03

A reliability, bounded-growth and polish release: the web UI is rebuilt on
React + shadcn/ui, transient provider errors retry and resume instead of
failing the task, hitting the daily budget parks work instead of killing it,
controller memory and database growth are now bounded, and releases ship as
prebuilt multi-arch container images on GHCR.

### Added

- **New web UI (React + Vite + Tailwind + shadcn/ui)** replacing the vanilla-JS
  SPA: sidebar navigation, light/dark theme, responsive layout, live agent
  trace with per-agent color rails, status filter pills, and a token dialog
  that reconnects the SSE streams once auth is entered. The built UI
  (`web/dist`) is committed, so deployments still need no Node; a CI job
  rebuilds it to catch a broken or stale bundle.
- **Transient-error resilience.** Connection failures and provider 429/5xx
  responses during the agent stream now retry up to 3 times with backoff,
  resuming from the task's last checkpoint so completed steps are never
  re-billed (without a checkpointer the error still fails fast, since a
  from-scratch replay would double-bill).
- **Mid-run spend persistence.** Token usage is flushed to the DB every
  `USAGE_FLUSH_SECONDS` (default 30) during a run, so a hard crash cannot lose
  in-flight spend from the daily-budget total, and a resumed run's per-task
  budget carries forward from what was actually spent.
- **Daily-cap parking.** A task stopped by the daily budget is re-queued with
  its checkpoint intact and resumes automatically after the UTC midnight
  reset, instead of being marked failed with its checkpoint deleted. Intake
  channels are not notified for parked (non-finished) tasks.
- **Retention sweeps.** A daily sweep prunes finished-task events and
  chat-spend rows older than `RETENTION_DAYS` (default 90; open tasks keep
  their full traces) and removes chat reference clones unused for
  `REF_CLONE_RETENTION_DAYS` (default 14), keeping the database and disk
  bounded.
- **Release pipeline.** Tagging `vX.Y.Z` re-runs the checks, builds a
  multi-arch (amd64 + arm64) image, publishes it to
  `ghcr.io/skundu42/sde-deepagent` as `X.Y.Z`, `X.Y` and `latest`, and creates
  the GitHub release from this changelog. Pushes to `main` publish a rolling
  `edge` image. Docker Compose now pulls the published image by default
  (`SDE_IMAGE_TAG` pins a version; `--build` still builds from source).
- Fault-injection tests covering every runner teardown path (cancel, timeout,
  task and daily budget stops, crashes), plus real-subprocess tests for the
  new capped output readers.
- **`/ask` in Slack & Telegram.** Mention/DM (Slack) or send (Telegram)
  `/ask <question>` to query the chat assistant about any past or running task and
  the long-term memory/knowledge base, instead of creating a task. Each chat/thread
  is its own conversational session; the same allowlist that gates task creation
  gates `/ask`.
- **Crash resume.** Agent state is now persisted to a SQLite checkpoint DB
  (`data/checkpoints.sqlite`) keyed per task, so a task interrupted by a server
  restart resumes from its last checkpoint instead of being marked failed — the
  workspace clone is reused so the agent's on-disk edits stay consistent with the
  restored conversation. Disable with `CHECKPOINT_RESUME=false`. Requires
  `langgraph-checkpoint-sqlite`; degrades to in-memory (no cross-restart resume)
  if absent. Stale checkpoints with no surviving workspace are cleared and the
  task starts clean.

### Changed

- **`/api/health` is now just `{ok, version}`.** The endpoint is public
  (unauthenticated) and used to fingerprint the deployment: which providers
  are configured, whether auth and the sandbox are on, active intake channels.
  That configuration block moved into the authenticated `/api/status` response
  as `config`.
- Chat's repo-reading clones refresh on a TTL (`REF_CLONE_TTL_MINUTES`,
  default 15) instead of serving the first-clone snapshot forever; when the
  remote is unreachable the stale clone is served rather than erroring.
- Em and en dashes were removed from the README and every UI-visible string
  (including server-emitted status details and error messages).
- The budget now charges models absent from the price table at the priciest known
  rate (fail-safe) instead of `$0`, so a typo'd/unknown model id can no longer slip
  past the per-task and daily caps. The first unpriced response is now flagged in
  the task log, and the fail-safe ceiling can't be lowered by a cheap `pricing:`
  override. Malformed/negative overrides are ignored instead of crashing a task.

### Fixed

- **The controller can no longer be OOMed by runaway command output.** Sandbox
  execs and git plumbing buffered a child's entire output before truncating;
  both now drain pipes incrementally with hard caps (60 KB formatted for
  sandbox execs, 5 MB for git plumbing) while still reading to EOF, so capped
  children never deadlock on a full pipe.
- A re-run of a previously failed or parked task clears its stale error once
  it completes.
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

[0.3.1]: https://github.com/skundu42/sde-deepagent/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/skundu42/sde-deepagent/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/skundu42/sde-deepagent/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/skundu42/sde-deepagent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/skundu42/sde-deepagent/releases/tag/v0.1.0
