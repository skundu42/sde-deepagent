"""Executes one task end-to-end: resolve repo -> clone -> run deep agent ->
test -> push -> PR, while streaming every agent step to the DB and event bus."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .agent_factory import build_agent
from .bus import EventBus
from .config import ConfigStore, RepoConfig
from .db import Database, Task
from .gitops import (
    GitError, commit_all, commits_ahead, create_pull_request, diff_stat,
    has_changes, prepare_workspace, prune_workspaces, push_branch,
)
from .llm import build_model
from .memory import memory_from_settings, repo_tag
from .pricing import BudgetExceeded, CostTracker
from .prompts import REPO_RESOLVER_PROMPT, REVISION_TASK_TEMPLATE
from .settings import Settings

logger = logging.getLogger(__name__)

TRUNCATE_ARGS = 1500
TRUNCATE_RESULT = 2500


class TaskRunner:
    def __init__(self, db: Database, bus: EventBus, cfg: ConfigStore, settings: Settings):
        self.db = db
        self.bus = bus
        self.cfg = cfg
        self.settings = settings
        self.memory = memory_from_settings(settings)

    async def emit(self, task_id: str, kind: str, content: dict[str, Any],
                   agent: str = "orchestrator") -> None:
        event = await self.db.add_event(task_id, kind, content, agent)
        self.bus.publish(task_id, event)

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
            await commit_all(ws, f"feat: {task.title[:70]}")
        await push_branch(ws, self.settings)
        stat = await diff_stat(ws)
        try:
            return await create_pull_request(
                ws, self.settings, task.title[:80],
                f"Automated change for task `{task.id}`.\n\n{task.description}"
                f"\n\n```\n{stat}\n```",
            )
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
        content = (f"Task outcome — {task.title} (repo {task.repo}): {outcome}.\n"
                   f"{summary[:1200]}")
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
            await commit_all(ws, f"feat: {task.title[:70]}")
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

    # ---- entry point ----

    async def run(self, task: Task) -> Task:
        await self.db.update_task(task.id, status="running",
                                  started_at=time.time())
        await self.emit(task.id, "status", {"status": "running"})
        built = None
        tracker: CostTracker | None = None
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
            await self.emit(task.id, "log", {"text": f"cloning {repo.name} ({repo.url})"})
            ws = await prepare_workspace(task.id, task.title, repo, self.settings,
                                         existing_branch=parent.branch if parent else None)
            await self.db.update_task(task.id, branch=ws.branch)
            task.branch = ws.branch
            await self.emit(task.id, "log",
                            {"text": f"workspace ready on branch {ws.branch}"
                                     + (f" (revising task {parent.id})" if parent else "")})

            if repo.setup:
                await self.emit(task.id, "log", {"text": f"running setup: {repo.setup}"})
                from .gitops import run_cmd
                code, out = await run_cmd(["bash", "-lc", repo.setup], cwd=ws.path, timeout=900)
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
            built = await build_agent(ws, task_description,
                                      agents_cfg, self.settings,
                                      model_override=task.model, on_event=on_tool_event)
            await self.emit(task.id, "log", {
                "text": f"agent started (orchestrator={task.model or agents_cfg.orchestrator_model}, "
                        f"subagents={', '.join(s.name for s in agents_cfg.subagents)}"
                        + (f", budget=${budget:.2f}" if budget else "") + ")"})

            final_text = await asyncio.wait_for(
                self._consume_stream(task, built, tracker, budget),
                timeout=self.settings.task_timeout_seconds,
            )

            if self.settings.require_approval:
                await self._persist_usage(task, tracker)
                if await self._request_approval(task, built, final_text):
                    return task  # held for a human; approve/reject endpoint finishes it
            pr_url = None if self.settings.require_approval \
                else await self._finalize(task, built)
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
            task.status, task.error = "failed", str(e)
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
            task.status, task.error = "failed", f"{type(e).__name__}: {e}"
            await self.db.update_task(task.id, status="failed", error=task.error[:1000],
                                      finished_at=time.time())
            await self.emit(task.id, "status", {"status": "failed", "error": task.error[:1000]})
        finally:
            try:
                deleted = prune_workspaces(self.settings,
                                           protect=await self._protected_workspaces())
                if deleted:
                    logger.info("pruned %d old workspace(s): %s",
                                len(deleted), ", ".join(deleted))
            except Exception:  # noqa: BLE001
                logger.exception("workspace pruning failed")
        return task
