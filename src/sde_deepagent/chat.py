"""Conversational interface over devagent's history: ask about past/current
tasks, their traces, costs, and remembered learnings. Chat sessions live in
process memory (the task record itself stays the durable audit trail)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from .config import ConfigStore
from .db import Database
from .llm import normalize_model_id
from .memory import GLOBAL_TAG, Memory, memory_from_settings, repo_tag
from .pricing import CostTracker
from .settings import Settings

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 60


def _safe_trim(messages: list, limit: int) -> list:
    """Keep the most recent `limit` messages without orphaning a tool result.

    A naive `messages[-limit:]` can begin the window on a ToolMessage whose
    preceding AIMessage (with the matching tool_calls) was cut off; providers
    reject a tool_result with no preceding tool_use, which permanently poisons the
    session. Start the window at the first HumanMessage (a turn boundary) instead."""
    window = messages[-limit:]
    for i, m in enumerate(window):
        if getattr(m, "type", "") == "human":
            return window[i:]
    return window

CHAT_PROMPT = """\
You are the sde-deepagent assistant. sde-deepagent is a software developer agent system:
tasks come in from the UI/Telegram/Slack/Linear, an orchestrator agent (with
explorer/coder/tester/reviewer subagents) implements them on a cloned codebase,
runs tests, and opens a PR.

Answer the operator's questions grounded in everything the system knows, using
your tools:
- search_knowledge — answer content questions from ingested resources (websites,
  docs and pasted text added on the Resources page) AND the learnings the agents
  recorded about codebases. Use this whenever asked what a resource or doc says.
- list_resources — see what resources have been ingested (title, source URL,
  scope, and indexing status).
- list_repos / get_repo — the registered codebases (description, branch, setup
  and test commands, context docs).
- list_tasks / get_task / get_task_trace — task records and step-by-step traces.

Ground every claim in tool results — never invent details. Cite sources: a
resource's title or URL, or a task id like `69e1b9e5ca`. If search_knowledge
finds nothing and list_resources shows a matching resource still `queued`, tell
the user it is still being indexed rather than that it doesn't exist. Be concise;
lead with the answer.
"""


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = max(0, time.time() - ts)
    if s < 3600:
        return f"{int(s / 60)}m ago"
    if s < 86400:
        return f"{int(s / 3600)}h ago"
    return f"{int(s / 86400)}d ago"


def make_chat_tools(db: Database, settings: Settings, cfg: ConfigStore,
                    memory: Memory | None = None) -> list:
    from langchain_core.tools import tool

    @tool
    async def list_tasks(status: str = "", limit: int = 20) -> str:
        """List recent tasks, newest first. `status` filters by one of:
        queued, running, completed, failed, cancelled. Empty = all."""
        tasks = await db.list_tasks(status=status or None, limit=min(limit, 100))
        if not tasks:
            return "No tasks found."
        lines = []
        for t in tasks:
            cost = f" ${t.cost_usd:.4f}" if t.cost_usd else ""
            pr = f" PR={t.pr_url}" if t.pr_url else ""
            lines.append(f"{t.id} [{t.status}] ({t.source}, {_ago(t.created_at)},"
                         f" repo={t.repo or '?'}{cost}{pr}) {t.title}")
        return "\n".join(lines)

    @tool
    async def get_task(task_id: str) -> str:
        """Full record of one task: description, repo, branch, PR, cost, error."""
        t = await db.get_task(task_id.strip())
        if not t:
            return f"No task with id {task_id!r}."
        parts = [
            f"id: {t.id}", f"title: {t.title}", f"status: {t.status}",
            f"source: {t.source}", f"repo: {t.repo}", f"branch: {t.branch}",
            f"pr_url: {t.pr_url}", f"model: {t.model or 'default'}",
            f"cost_usd: {t.cost_usd}",
            f"tokens: {t.input_tokens} in / {t.output_tokens} out",
            f"created: {_ago(t.created_at)}",
            f"description: {t.description[:1500]}",
        ]
        if t.error:
            parts.append(f"error: {t.error}")
        return "\n".join(parts)

    @tool
    async def get_task_trace(task_id: str, limit: int = 150) -> str:
        """Step-by-step trace of what the agent did during a task: messages,
        tool calls (shell commands, file edits, subagent delegations), results,
        and status changes."""
        events = await db.list_events(task_id.strip(), limit=min(limit, 400))
        if not events:
            return f"No events recorded for task {task_id!r}."
        lines: list[str] = []
        budget = 9000
        for e in events:
            c = e["content"]
            if e["kind"] == "tool_call":
                summary = f"$ {c.get('name')}({str(c.get('args'))[:160]})"
            elif e["kind"] == "tool_result":
                summary = f"-> {c.get('name')}: {str(c.get('output'))[:160]}"
            elif e["kind"] in ("message", "log"):
                summary = str(c.get("text", ""))[:240]
            elif e["kind"] == "status":
                summary = f"STATUS {c.get('status')}" + (
                    f" error={c.get('error')}" if c.get("error") else "")
            elif e["kind"] == "todos":
                summary = "updated todo plan"
            else:
                summary = str(c)[:160]
            line = f"[{e['agent']}] {e['kind']}: {summary}"
            budget -= len(line)
            if budget < 0:
                lines.append(f"... trace truncated ({len(events)} events total)")
                break
            lines.append(line)
        return "\n".join(lines)

    @tool
    async def list_repos() -> str:
        """List the registered codebases the agents work on, with the
        description used for task routing and the default branch."""
        repos = cfg.repos()
        if not repos:
            return "No codebases registered."
        lines = []
        for name, r in repos.items():
            desc = (r.description or "").strip() or "(no description)"
            lines.append(f"{name} [{r.default_branch}] — {desc}")
        return "\n".join(lines)

    @tool
    async def get_repo(name: str) -> str:
        """Full config of one registered codebase: url, default branch, setup
        and test commands, and the context docs the agents are given."""
        r = cfg.repos().get(name.strip())
        if not r:
            return f"No repo named {name!r}. Use list_repos to see registered codebases."
        return "\n".join([
            f"name: {r.name}", f"url: {r.url}", f"default_branch: {r.default_branch}",
            f"description: {(r.description or '').strip() or '(none)'}",
            f"setup: {r.setup or '(none)'}", f"test: {r.test or '(none)'}",
            f"context: {', '.join(r.context) or '(none)'}",
        ])

    tools = [list_tasks, get_task, get_task_trace, list_repos, get_repo]

    if memory is None:
        memory = memory_from_settings(settings)
    if memory:
        @tool
        async def search_knowledge(query: str) -> str:
            """Search ingested knowledge to answer content questions: resources
            added on the Resources page (websites, docs, pasted text) AND the
            conventions, gotchas and learnings the agents recorded about the
            codebases. Use this whenever asked what a resource or doc says."""
            tags = [GLOBAL_TAG] + [repo_tag(name) for name in cfg.repos()]
            results = await memory.search(query, tags, limit=8)
            if not results:
                return ("No matching knowledge found. If a resource was just added it "
                        "may still be indexing — check list_resources for its status.")
            return "\n".join(f"- [{r['container']}] {r['memory']}" for r in results)

        @tool
        async def list_resources(scope: str = "") -> str:
            """List resources ingested on the Resources page (websites, docs,
            pasted text). Optional `scope`: a repo name or 'global'. Shows each
            resource's title, kind, scope, indexing status, and source URL."""
            tags = [GLOBAL_TAG] + [repo_tag(name) for name in cfg.repos()]
            docs = await memory.list_documents(tags, limit=200)
            rows = []
            for d in docs:
                meta = d.get("metadata") or {}
                if meta.get("source") != "resource":
                    continue  # agent learnings / task outcomes belong to search_knowledge
                sc = meta.get("scope", "global")
                if scope and sc != scope:
                    continue
                title = d.get("title") or meta.get("title") or meta.get("url") or d.get("id")
                url = meta.get("url")
                rows.append(f"- [{d.get('status') or '?'}] {title} "
                            f"({meta.get('kind', 'text')}, {sc})" + (f" {url}" if url else ""))
            return "\n".join(rows) if rows else "No resources ingested yet."

        tools += [search_knowledge, list_resources]
    return tools


class ChatService:
    def __init__(self, db: Database, cfg: ConfigStore, settings: Settings):
        self.db = db
        self.cfg = cfg
        self.settings = settings
        self.sessions: dict[str, list[Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    async def ask(self, message: str, session_id: str | None = None) -> dict[str, str]:
        session_id = session_id or uuid.uuid4().hex[:12]
        # Serialize turns within a session: ask() appends the user message, then
        # overwrites the whole session with the agent result after an await. Two
        # concurrent requests on the same session_id would otherwise clobber each
        # other and drop messages.
        async with self._lock_for(session_id):
            return await self._ask_locked(message, session_id)

    async def _ask_locked(self, message: str, session_id: str) -> dict[str, str]:
        history = self.sessions.setdefault(session_id, [])
        history.append(HumanMessage(content=message))

        agents_cfg = self.cfg.agents()
        model_id = normalize_model_id(agents_cfg.orchestrator_model)
        agent = create_agent(
            model=model_id,
            tools=make_chat_tools(self.db, self.settings, self.cfg),
            system_prompt=CHAT_PROMPT,
        )
        sent = len(history)
        result = await agent.ainvoke({"messages": list(history)},
                                     config={"recursion_limit": 40})
        messages = result.get("messages", [])
        self.sessions[session_id] = _safe_trim(messages, MAX_HISTORY_MESSAGES)

        # chat spend counts against the daily budget like everything else
        tracker = CostTracker(default_model=model_id, overrides=agents_cfg.pricing)
        for msg in messages[sent:]:
            meta = getattr(msg, "usage_metadata", None)
            if meta:
                resp_meta = getattr(msg, "response_metadata", None) or {}
                tracker.add_usage(meta, resp_meta.get("model_name"))
        if tracker.input_tokens or tracker.output_tokens:
            try:
                await self.db.add_chat_spend(
                    session_id, model_id, tracker.input_tokens,
                    tracker.output_tokens, round(tracker.cost_usd, 6))
            except Exception:  # noqa: BLE001 — bookkeeping must not break the reply
                logger.exception("failed to record chat spend")

        reply = ""
        for msg in reversed(messages):
            if getattr(msg, "type", "") == "ai" and not getattr(msg, "tool_calls", None):
                content = msg.content
                if isinstance(content, list):
                    content = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
                reply = str(content)
                break
        return {"session_id": session_id, "reply": reply or "(no reply)",
                "cost_usd": round(tracker.cost_usd, 6)}

    def reset(self, session_id: str) -> bool:
        self._locks.pop(session_id, None)
        return self.sessions.pop(session_id, None) is not None
