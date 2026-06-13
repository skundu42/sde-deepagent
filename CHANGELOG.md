# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/skundu42/sde-deepagent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/skundu42/sde-deepagent/releases/tag/v0.1.0
