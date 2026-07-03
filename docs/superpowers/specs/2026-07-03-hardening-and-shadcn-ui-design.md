# Hardening fixes + shadcn UI rewrite

Date: 2026-07-03. Approved by operator in session; implementation authorized end to end.

## Scope

Two independent workstreams:

- **A. Backend hardening**: the five prioritized fixes from the 2026-07-03 review.
- **B. UI rewrite**: replace the vanilla-JS static UI with React + Vite + Tailwind + shadcn/ui, full feature parity, committed build output so runtime deployments stay Node-free.

Plus: no em/en dashes in README or user-facing UI strings.

## A. Backend hardening

### A1. Ref-clone freshness (repo_reader.py)

- `ensure_clone()` refreshes an existing clone when its last-fetch stamp is older than `REF_CLONE_TTL_MINUTES` (settings, default 15): `git fetch --depth 1` then hard reset to the fetched head.
- Fetch failure: log a warning and serve the stale clone (chat keeps working offline).
- Every successful `ensure_clone` touches a last-use stamp file (input to A5 pruning).

### A2. Transient-error retry + mid-run spend persistence (runner.py)

- Retry wrapper around the agent stream: up to 3 retries with 2s/8s/30s backoff on transient failures only. Transient = connection/transport errors, or provider errors whose `status_code` is 429 or >= 500 (duck-typed; no SDK-specific imports). Retries re-enter the stream with the same checkpointer thread id, so the run resumes from its last checkpoint.
- Non-transient errors fail immediately as today.
- Cost tracker usage is flushed to the DB every ~30 seconds during the stream (today: only at task end). A hard crash no longer loses in-flight spend from the daily total; per-task budget carry-forward on resume becomes real.
- `DailyBudgetExceeded` mid-run: task returns to `queued` and its checkpoint is kept (worker queue is paused until the UTC midnight reset anyway). Previously: marked failed, checkpoint deleted.

### A3. Streamed, capped subprocess output (sandbox.py, gitops.py)

- `exec_in_container`: replace `subprocess.run` with a Popen drain loop that stores at most `max_output_bytes` (plus truncation marker) while still draining pipes to EOF/timeout. Memory bounded regardless of child output.
- `gitops.run_cmd`: incremental asyncio reads with a cap (default 5 MB; callers parse git output programmatically so the cap is generous).
- Out of scope (tracked separately): in-container process kill on timeout, container death detection.

### A4. Fault-injection tests for runner teardown

- Drive the real `run()` with fake agent streams raising: `asyncio.CancelledError`, `BudgetExceeded`, `DailyBudgetExceeded`, `asyncio.TimeoutError`, generic `Exception`.
- Assert: final task status, recorded error, checkpoint deleted (or kept, per A2), `mark_used` still fires, terminal events/notifications emitted.
- Builds on the `test_runner_e2e.py` fake-stream harness.

### A5. Retention sweeps + slim /api/health

- Daily sweep in the worker loop: delete `events` and `chat_spend` rows older than `RETENTION_DAYS` (settings, default 90; 0 disables), remove ref-clones unused for `REF_CLONE_RETENTION_DAYS` (default 14).
- `/api/health` (public) shrinks to `{ok, version}`. The config fingerprint (providers, github, memory, firecrawl, require_approval, auth, sandbox_default, review_polling, intakes, running) moves into the authenticated `/api/status` payload.

## B. UI rewrite (web/)

- New `web/` Vite + React + TypeScript project: Tailwind, shadcn/ui (Radix), lucide-react icons, react-markdown for chat rendering (escapes raw HTML by default, preserving the current XSS-safety property), react-router for deep-linkable views.
- Feature parity with ui/: tasks list + new-task form + filters; task detail with live SSE trace, steer bar, approve/reject/revise, cost, PR link; chat with sessions and markdown; resources; codebases config; agents config with prompt editors, server-side validation errors, prompt-defaults reset; MCP config; status page (enriched /api/status); token prompt on 401; SSE auth via ?token=.
- Design: shadcn neutral theme, sidebar navigation, light/dark toggle. No em/en dashes in visible strings.
- Build/deploy: `web/dist/` is committed; FastAPI serves it (static mount moves from ui/ to web/dist). Old `ui/` directory deleted. Dockerfile copies the committed dist (no Node stage). CI gains a job running `npm ci && npm run build` in web/ to catch a broken/stale dist.
- Tests: CI build; vitest for pure logic (API client auth/401 flow, formatters); manual end-to-end drive of every view against the running server.

## Sequencing

A1 through A5 first (TDD, one commit per fix), then B. Independent workstreams; UI issues cannot block the reliability fixes.

## Out of scope

Container lifecycle fixes (timeout kill semantics, death detection), schema-version table, GitHub review prompt-injection hardening.
