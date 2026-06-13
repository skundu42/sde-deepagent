"""Executes one task end-to-end: resolve repo -> clone -> run deep agent ->
test -> push -> PR, while streaming every agent step to the DB and event bus."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .agent_factory import _shell_env, build_agent
from .bus import EventBus
from .config import ConfigStore, RepoConfig
from .db import Database, Task
from .gitops import (
    GitError,
    commit_all,
    commits_ahead,
    create_pull_request,
    diff_stat,
    has_changes,
    prepare_workspace,
    prune_workspaces,
    push_branch,
)
from .llm import build_model
from .memory import memory_from_settings, repo_tag
from .pricing import BudgetExceeded, CostTracker, DailyBudget, DailyBudgetExceeded
from .prompts import REPO_RESOLVER_PROMPT, REVISION_TASK_TEMPLATE
from .secrets import Redactor, SecretStore, resolve_repo_secrets
from .settings import Settings

logger = logging.getLogger(__name__)

TRUNCATE_ARGS = 1500
TRUNCATE_RESULT = 2500


class TaskRunner:
    def __init__(self, db: Database, bus: EventBus, cfg: ConfigStore, settings: Settings,
                 daily_budget: DailyBudget | None = None,
                 secret_store: SecretStore | None = None):
        self.mailboxes: dict[str, list[str]] = {}  # task_id -> pending operator messages
        self._redactors: dict[str, Redactor] = {}  # task_id -> secret-value redactor
        self.db = db
        self.bus = bus
        self.cfg = cfg
        self.settings = settings
        self.memory = memory_from_settings(settings)
        # decrypts UI-entered (`store`) secrets; None/unavailable => those fail closed
        self.secret_store = secret_store
        # shared with the worker so launch-gating and mid-stream enforcement see
        # the same (persisted + in-flight) daily spend
        self.daily_budget = daily_budget or DailyBudget(db, settings.daily_budget_usd)

    async def emit(self, task_id: str, kind: str, content: dict[str, Any],
                   agent: str = "orchestrator") -> None:
        redactor = self._redactors.get(task_id)
        if redactor is not None and redactor.active:
            content = redactor.redact_obj(content)
        event = await self.db.add_event(task_id, kind, content, agent)
        self.bus.publish(task_id, event)

    def _redact(self, task_id: str, text: str) -> str:
        """Mask this task's secret values out of a free-text string (e.g. an
        error or commit message) that doesn't flow through emit()."""
        redactor = self._redactors.get(task_id)
        return redactor.redact(text) if (redactor is not None and text) else text

    # ---- repo resolution ----

    async def resolve_repo(self, task: Task) -> RepoConfig:
        repos = self.cfg.repos()
        if not repos:
            raise GitError("no codebases registered — add one in the UI or config/repos.yaml")
        if task.repo:
            if task.repo not in repos:
                raise GitError(
                    f"unknown repo '{task.repo}'. Registered: {', '.join(sorted(repos))}"
                )
            return repos[task.repo]
        if len(repos) == 1:
            return next(iter(repos.values()))
        # several candidates: let a model route it
        agents_cfg = self.cfg.agents()
        model = build_model(task.model or agents_cfg.orchestrator_model, max_tokens=50)
        repo_list = "\n".join(
            f"- {name}: {r.description or r.url}" for name, r in repos.items()
        )
        prompt = REPO_RESOLVER_PROMPT.format(
            repo_list=repo_list, task=f"{task.title}\n{task.description}"
        )
        reply = await model.ainvoke(prompt)
        choice = str(reply.content).strip().strip("`'\"")
        if choice in repos:
            return repos[choice]
        raise GitError(
            f"could not determine target repo (model said: {choice!r}). "
            f"Specify one of: {', '.join(sorted(repos))}"
        )

    # ---- agent stream -> events ----

    @staticmethod
    def _text_of(message: Any) -> str:
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(p for p in parts if p)
        return str(content)

    def _agent_for_ns(self, ns: tuple, call_map: dict[str, str],
                      active: dict[str, str]) -> str:
        if not ns:
            return "orchestrator"
        for segment in ns:
            if isinstance(segment, str) and ":" in segment:
                suffix = segment.split(":", 1)[1]
                if suffix in call_map:
                    return call_map[suffix]
        # the namespace doesn't expose the task tool-call id (observed live with
        # deepagents 0.6.8) — if exactly one delegation is in flight, it's that one
        names = set(active.values())
        return names.pop() if len(names) == 1 else "subagent"

    async def _consume_stream(self, task: Task, built, tracker: CostTracker,
                              budget_usd: float) -> str:
        """Stream the agent run, persisting events. Returns the final text."""
        call_map: dict[str, str] = {}  # tool_call_id -> subagent name
        active: dict[str, str] = {}    # task calls currently in flight
        final_text = ""
        seen_message_ids: set[str] = set()

        stream = built.agent.astream(
            {"messages": [{"role": "user", "content": "Begin the task now."}]},
            stream_mode="updates",
            subgraphs=True,
            config={"recursion_limit": self.settings.recursion_limit},
        )
        async for chunk in stream:
            ns: tuple = ()
            update = chunk
            if isinstance(chunk, tuple) and len(chunk) == 2:
                ns, update = chunk
            if not isinstance(update, dict):
                continue
            agent_name = self._agent_for_ns(ns, call_map, active)
            for node_value in update.values():
                if not isinstance(node_value, dict):
                    continue
                if "todos" in node_value and node_value["todos"] is not None:
                    await self.emit(task.id, "todos", {"todos": node_value["todos"]}, agent_name)
                for msg in node_value.get("messages") or []:
                    await self._handle_message(task, msg, agent_name, call_map,
                                               active, tracker, budget_usd,
                                               seen_message_ids)
                    if (getattr(msg, "type", "") == "ai" and not ns
                            and not getattr(msg, "tool_calls", None)):
                        text = self._text_of(msg)
                        if text:
                            final_text = text
        return final_text

    async def _handle_message(self, task: Task, msg: Any, agent_name: str,
                              call_map: dict[str, str], active: dict[str, str],
                              tracker: CostTracker, budget_usd: float,
                              seen: set[str]) -> None:
        msg_id = getattr(msg, "id", None)
        dedup_key = f"{agent_name}:{msg_id}"
        mtype = getattr(msg, "type", "")
        if mtype == "ai":
            if msg_id and dedup_key in seen:
                return
            if msg_id:
                seen.add(dedup_key)
            meta = getattr(msg, "usage_metadata", None) or {}
            if meta:
                resp_meta = getattr(msg, "response_metadata", None) or {}
                tracker.add_usage(meta, resp_meta.get("model_name"))
                await self._enforce_budget(task, tracker, budget_usd)
                await self._enforce_daily_budget()
            text = self._text_of(msg)
            if text.strip():
                await self.emit(task.id, "message", {"text": text}, agent_name)
            for tc in getattr(msg, "tool_calls", None) or []:
                args = tc.get("args") or {}
                if tc.get("name") == "task" and tc.get("id"):
                    call_map[tc["id"]] = args.get("subagent_type", "subagent")
                    active[tc["id"]] = call_map[tc["id"]]
                shown = {k: (str(v)[:TRUNCATE_ARGS] if isinstance(v, str) else v)
                         for k, v in args.items()}
                await self.emit(task.id, "tool_call",
                                {"name": tc.get("name"), "args": shown}, agent_name)
        elif mtype == "tool":
            if getattr(msg, "name", None) == "task":
                active.pop(getattr(msg, "tool_call_id", None), None)  # delegation done
            text = self._text_of(msg)
            await self.emit(task.id, "tool_result",
                            {"name": getattr(msg, "name", None),
                             "output": text[:TRUNCATE_RESULT],
                             "truncated": len(text) > TRUNCATE_RESULT}, agent_name)

    async def _enforce_budget(self, task: Task, tracker: CostTracker,
                              budget_usd: float) -> None:
        if budget_usd <= 0:
            return
        if tracker.cost_usd >= budget_usd:
            raise BudgetExceeded(tracker.cost_usd, budget_usd)
        if not tracker.budget_warned and tracker.cost_usd >= 0.8 * budget_usd:
            tracker.budget_warned = True
            await self.emit(task.id, "log", {
                "text": f"budget warning: ${tracker.cost_usd:.2f} of "
                        f"${budget_usd:.2f} used (≥80%)"})

    async def _enforce_daily_budget(self) -> None:
        """Hard daily cap: abort the instant cumulative spend (persisted + every
        in-flight task) reaches the limit. Together with the dispatcher's launch
        gate this guarantees no task begins new LLM work past the ceiling — the
        only unavoidable slack is the single response already in flight when the
        line is crossed (its tokens are billed before any code can react)."""
        limit = self.daily_budget.limit_usd
        if limit <= 0:
            return
        spent = await self.daily_budget.spent_usd()
        if spent >= limit:
            raise DailyBudgetExceeded(spent, limit)

    async def _persist_usage(self, task: Task, tracker: CostTracker | None) -> None:
        if tracker is None:
            return
        try:
            await self.db.update_task(
                task.id, input_tokens=tracker.input_tokens,
                output_tokens=tracker.output_tokens,
                cost_usd=round(tracker.cost_usd, 6),
            )
            task.input_tokens = tracker.input_tokens
            task.output_tokens = tracker.output_tokens
            task.cost_usd = round(tracker.cost_usd, 6)
        except Exception:  # noqa: BLE001 — bookkeeping must not mask the real outcome
            logger.exception("failed to persist usage for task %s", task.id)

    # ---- finalize ----

    async def _finalize(self, task: Task, built) -> str | None:
        """Ensure committed work ends up in a PR. Returns the PR URL if any."""
        ws = built.workspace
        if built.result.get("pr_url"):
            return built.result["pr_url"]
        dirty = await has_changes(ws)
        ahead = await commits_ahead(ws)
        if not dirty and ahead == 0:
            return None  # agent made no changes (e.g. blocked task) — nothing to ship
        if not self.settings.auto_finalize:
            await self.emit(task.id, "log", {"text": "changes left unshipped (auto_finalize off)"})
            return None
        await self.emit(task.id, "log",
                        {"text": "agent left work without a PR — auto-finalizing"})
        if dirty:
            await commit_all(ws, self._redact(task.id, f"feat: {task.title[:70]}"))
        await push_branch(ws, self.settings)
        stat = await diff_stat(ws)
        title = self._redact(task.id, task.title[:80])
        body = self._redact(
            task.id,
            f"Automated change for task `{task.id}`.\n\n{task.description}"
            f"\n\n```\n{stat}\n```")
        try:
            return await create_pull_request(ws, self.settings, title, body)
        except GitError as e:
            # work is safe on the pushed branch even when a PR isn't possible
            # (local remote, missing token, non-GitHub host)
            await self.emit(task.id, "log",
                            {"text": f"branch {ws.branch} pushed, but PR not possible: {e}"})
            return None

    async def _record_outcome(self, task: Task, summary: str) -> None:
        """Passively store the task outcome so future tasks on this repo can
        recall what was done, even if the agent never called save_memory."""
        if not self.memory or not task.repo:
            return
        outcome = f"completed, PR: {task.pr_url}" if task.pr_url else "completed (no PR)"
        content = self._redact(task.id, (
            f"Task outcome — {task.title} (repo {task.repo}): {outcome}.\n"
            f"{summary[:1200]}"))
        mem_id = await self.memory.add(
            content, repo_tag(task.repo),
            metadata={"task_id": task.id, "repo": task.repo,
                      "pr_url": task.pr_url or "", "source": "auto"},
        )
        if mem_id:
            await self.emit(task.id, "log", {"text": "task outcome saved to long-term memory"})

    async def _request_approval(self, task: Task, built, final_text: str) -> bool:
        """Approval mode: commit work, record the proposal, hold for a human.
        Returns True if there is work awaiting approval (task stays open)."""
        ws = built.workspace
        if await has_changes(ws):
            await commit_all(ws, self._redact(task.id, f"feat: {task.title[:70]}"))
        if await commits_ahead(ws) == 0:
            return False  # nothing to ship — let the task complete normally
        stat = await diff_stat(ws)
        await self.emit(task.id, "approval_request", {
            "title": built.result.get("pr_title") or task.title[:80],
            "body": built.result.get("pr_body")
                    or f"Automated change for task `{task.id}`.\n\n{task.description}",
            "diff_stat": stat,
            "summary": final_text[:2000],
        })
        await self.db.update_task(task.id, status="awaiting_approval")
        task.status = "awaiting_approval"
        await self.emit(task.id, "status", {"status": "awaiting_approval"})
        return True

    async def _protected_workspaces(self) -> set[str]:
        """Workspaces holding unpushed approved-pending work must survive pruning."""
        waiting = await self.db.list_tasks(status="awaiting_approval", limit=500)
        return {t.id for t in waiting}

    def steer(self, task_id: str, message: str) -> bool:
        """Queue an operator message for a running task; delivered when the
        agent next calls check_messages. Returns False if the task isn't running."""
        if task_id not in self.mailboxes:
            return False
        self.mailboxes[task_id].append(message)
        return True

    def _drain_mailbox(self, task_id: str) -> list[str]:
        msgs = self.mailboxes.get(task_id, [])
        self.mailboxes[task_id] = []
        return msgs

    def _resolve_approval(self, repo) -> bool:
        """Per-repo approval policy overrides the server-wide default."""
        if repo.approval == "required":
            return True
        if repo.approval == "auto":
            return False
        return self.settings.require_approval

    def _resolve_sandbox(self, repo) -> bool:
        return repo.sandbox if repo.sandbox is not None else self.settings.sandbox_default

    # ---- entry point ----

    async def run(self, task: Task) -> Task:
        await self.db.update_task(task.id, status="running",
                                  started_at=time.time())
        await self.emit(task.id, "status", {"status": "running"})
        built = None
        tracker: CostTracker | None = None
        sandboxed = False
        self.mailboxes[task.id] = []
        try:
            parent: Task | None = None
            if task.parent_id:
                parent = await self.db.get_task(task.parent_id)
                if not parent or not parent.branch:
                    raise GitError(f"cannot revise: parent task {task.parent_id} "
                                   "not found or never produced a branch")
                if not task.repo:
                    task.repo = parent.repo
            repo = await self.resolve_repo(task)
            if task.repo != repo.name:
                await self.db.update_task(task.id, repo=repo.name)
                task.repo = repo.name
            require_approval = self._resolve_approval(repo)
            # resolve this repo's secrets (host-env refs + encrypted store) and
            # arm the redactor BEFORE any setup/agent output can carry a value
            secrets, missing_secrets = resolve_repo_secrets(
                repo, store=self.secret_store)
            self._redactors[task.id] = Redactor(secrets)
            wants_store = any(ref == "store" for ref in (repo.secrets or {}).values())
            if wants_store and (self.secret_store is None
                                or not self.secret_store.available):
                await self.emit(task.id, "log", {
                    "text": "this repo has stored secrets but SECRETS_KEY is not "
                            "configured — they are unavailable; set SECRETS_KEY to "
                            "enable them"})
            if missing_secrets:
                await self.emit(task.id, "log", {
                    "text": "repo secrets not available, skipped: "
                            + ", ".join(missing_secrets)})
            await self.emit(task.id, "log", {"text": f"cloning {repo.name} ({repo.url})"})
            ws = await prepare_workspace(task.id, task.title, repo, self.settings,
                                         existing_branch=parent.branch if parent else None)
            await self.db.update_task(task.id, branch=ws.branch)
            task.branch = ws.branch
            await self.emit(task.id, "log",
                            {"text": f"workspace ready on branch {ws.branch}"
                                     + (f" (revising task {parent.id})" if parent else "")})

            # per-repo container sandbox (isolates the agent's shell + egress);
            # reused across tasks so toolchains/dependency caches survive
            sandbox_container: str | None = None
            sandbox_workdir: str | None = None
            sandbox_network: str | None = None
            if self._resolve_sandbox(repo):
                from . import sandbox as sbx
                if not sbx.docker_available():
                    raise GitError(
                        "tasks run sandboxed but Docker is unavailable — refusing "
                        "to run untrusted commands on the host. Install/start Docker, "
                        "or opt out (SANDBOX_DEFAULT=false, or sandbox: false on "
                        "this repo) to run on the host.")
                image = repo.sandbox_image or self.settings.sandbox_image
                network = repo.sandbox_network or self.settings.sandbox_network
                if network not in ("none", "bridge"):
                    network = "none"
                sandbox_network = network
                mount_dir = ws.path.parent.parent  # workspaces/<repo_slug>
                # to_thread: creation may pull the image — don't block the loop
                sandbox_container, created = await asyncio.to_thread(
                    sbx.ensure_container, repo.name, str(mount_dir),
                    image=image, network=network,
                    memory=self.settings.sandbox_memory,
                    cpus=self.settings.sandbox_cpus,
                    probe_subdir=f"{task.id}/repo")
                sandbox_workdir = f"/workspaces/{task.id}/repo"
                sbx.mark_used(sandbox_container, self.settings.sandbox_state_path)
                sandboxed = True
                await self.emit(task.id, "log", {
                    "text": f"sandbox container "
                            f"{'started' if created else 'reused'} "
                            f"(image={image}, network={network})"})

            # secrets + open egress = a compromised task could phone them out
            egress_open = (not sandboxed) or (sandbox_network == "bridge")
            if secrets and egress_open:
                where = ("network egress is enabled (bridge)" if sandboxed
                         else "tasks run on the host with full network access")
                await self.emit(task.id, "log", {
                    "text": f"security warning: this repo has secrets and {where}; "
                            "a compromised task could exfiltrate them. Prefer "
                            "sandbox_network: none for repos with secrets."})
                logger.warning("repo %s: secrets present with open egress (%s)",
                               repo.name, "host" if not sandboxed else "bridge")

            if repo.setup:
                await self.emit(task.id, "log", {"text": f"running setup: {repo.setup}"})
                if sandbox_container:
                    from .sandbox import exec_in_container
                    res = await asyncio.to_thread(
                        exec_in_container, sandbox_container, repo.setup,
                        timeout=900, workdir=sandbox_workdir, secrets=secrets or None)
                    code, out = res.exit_code, res.output
                else:
                    from .gitops import run_cmd
                    # sanitized base env (no host API keys) + this repo's secrets
                    code, out = await run_cmd(["bash", "-lc", repo.setup], cwd=ws.path,
                                              timeout=900,
                                              env={**_shell_env(), **secrets})
                await self.emit(task.id, "tool_result",
                                {"name": "setup", "output": out[-2000:], "exit_code": code})
                if code != 0:
                    await self.emit(task.id, "log",
                                    {"text": "setup failed — continuing, agent may retry"})

            agents_cfg = self.cfg.agents()
            tracker = CostTracker(
                default_model=task.model or agents_cfg.orchestrator_model,
                overrides=agents_cfg.pricing,
            )
            # count this task's spend toward the daily cap while it runs, before
            # its cost is persisted at the end of the run
            self.daily_budget.track(task.id, tracker)
            budget = task.budget_usd or self.settings.task_budget_usd

            async def on_tool_event(kind: str, content: dict) -> None:
                await self.emit(task.id, kind, content)

            if parent:
                task_description = REVISION_TASK_TEMPLATE.format(
                    parent_id=parent.id, parent_title=parent.title,
                    parent_description=parent.description,
                    revision_description=task.description,
                )
            else:
                task_description = f"{task.title}\n\n{task.description}"
            built = await build_agent(
                ws, task_description, agents_cfg, self.settings,
                model_override=task.model, on_event=on_tool_event,
                sandbox_container=sandbox_container,
                sandbox_workdir=sandbox_workdir,
                sandbox_network=sandbox_network,
                require_approval=require_approval,
                drain_messages=lambda: self._drain_mailbox(task.id),
                secrets=secrets,
                redactor=self._redactors.get(task.id))
            await self.emit(task.id, "log", {
                "text": f"agent started (orchestrator={task.model or agents_cfg.orchestrator_model}, "
                        f"subagents={', '.join(s.name for s in agents_cfg.subagents)}"
                        + (f", budget=${budget:.2f}" if budget else "")
                        + (", sandboxed" if sandboxed else "") + ")"})

            final_text = await asyncio.wait_for(
                self._consume_stream(task, built, tracker, budget),
                timeout=self.settings.task_timeout_seconds,
            )

            if require_approval:
                await self._persist_usage(task, tracker)
                if await self._request_approval(task, built, final_text):
                    return task  # held for a human; approve/reject endpoint finishes it
            pr_url = None if require_approval else await self._finalize(task, built)
            if pr_url:
                await self.db.update_task(task.id, pr_url=pr_url)
                task.pr_url = pr_url
            await self._persist_usage(task, tracker)
            await self.db.update_task(task.id, status="completed",
                                      finished_at=time.time())
            task.status = "completed"
            await self.emit(task.id, "status", {
                "status": "completed", "pr_url": pr_url,
                "summary": final_text[:4000], "usage": tracker.summary(),
            })
            await self._record_outcome(task, final_text)
        except asyncio.CancelledError:
            await self._persist_usage(task, tracker)
            await self.db.update_task(task.id, status="cancelled",
                                      finished_at=time.time())
            task.status = "cancelled"
            await self.emit(task.id, "status", {"status": "cancelled"})
            raise
        except BudgetExceeded as e:
            await self._persist_usage(task, tracker)
            task.status, task.error = "failed", self._redact(task.id, str(e))
            await self.db.update_task(task.id, status="failed", error=task.error,
                                      finished_at=time.time())
            await self.emit(task.id, "status", {
                "status": "failed", "error": task.error,
                "usage": tracker.summary() if tracker else None,
            })
        except asyncio.TimeoutError:
            await self._persist_usage(task, tracker)
            task.status, task.error = "failed", "task timed out"
            await self.db.update_task(task.id, status="failed", error=task.error,
                                      finished_at=time.time())
            await self.emit(task.id, "status", {"status": "failed", "error": task.error})
        except Exception as e:  # noqa: BLE001 — agent runs must never kill the worker
            logger.exception("task %s failed", task.id)
            await self._persist_usage(task, tracker)
            task.status = "failed"
            task.error = self._redact(task.id, f"{type(e).__name__}: {e}")
            await self.db.update_task(task.id, status="failed", error=task.error[:1000],
                                      finished_at=time.time())
            await self.emit(task.id, "status", {"status": "failed", "error": task.error[:1000]})
        finally:
            self.mailboxes.pop(task.id, None)
            self._redactors.pop(task.id, None)
            self.daily_budget.untrack(task.id)  # cost is now persisted in the DB
            if sandboxed:
                # the container stays up for reuse; restamp its idle clock so
                # the reaper counts the 7-day TTL from this task's end
                try:
                    from . import sandbox as sbx
                    sbx.mark_used(sandbox_container,
                                  self.settings.sandbox_state_path)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to mark sandbox use for %s", task.id)
            try:
                deleted = prune_workspaces(self.settings,
                                           protect=await self._protected_workspaces())
                if deleted:
                    logger.info("pruned %d old workspace(s): %s",
                                len(deleted), ", ".join(deleted))
            except Exception:  # noqa: BLE001
                logger.exception("workspace pruning failed")
        return task
