"""Conversational interface over devagent's history: ask about past/current
tasks, their traces, costs, and remembered learnings. Chat sessions live in
process memory (the task record itself stays the durable audit trail)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from .config import ConfigStore
from .db import Database
from .gitops import GitError, parse_remote
from .llm import normalize_model_id
from .memory import GLOBAL_TAG, Memory, memory_from_settings, repo_tag
from .pricing import CostTracker
from .repo_reader import RepoReader
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
- search_knowledge: answer content questions from ingested resources (websites,
  docs and pasted text added on the Resources page) AND the learnings the agents
  recorded about codebases. Use this whenever asked what a resource or doc says.
- list_resources: see what resources have been ingested (title, source URL,
  scope, and indexing status).
- list_repos / get_repo: the registered codebases (description, branch, setup
  and test commands, context docs).
- list_repo_files / grep_repo / read_repo_file: read the ACTUAL source of a
  registered codebase OR any GitHub repo added on the Resources page. Use these to
  answer code-level questions ("what does X do", "where is Y defined", "how is Z
  implemented") from the real files instead of guessing or relying on the README.
  Typical flow: grep_repo or list_repo_files to locate the file, then read_repo_file.
- list_tasks / get_task / get_task_trace: task records and step-by-step traces.

When a question is about how code works, READ THE SOURCE with the repo tools -
search_knowledge only has summaries, not the code. The `repo` argument is a
registered codebase name or a GitHub URL/owner-repo shown by list_resources.

Ground every claim in tool results: never invent details. Cite sources: a file
path you read, a resource's title or URL, or a task id like `69e1b9e5ca`. If
search_knowledge finds nothing and list_resources shows a matching resource still
`queued`, tell the user it is still being indexed rather than that it doesn't
exist. Be concise; lead with the answer.
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

    # ---- code reading: clone + read the actual source of repos the operator
    # registered (Codebases) or ingested (a GitHub URL on the Resources page) ----
    reader = RepoReader(settings)

    def _canon_key(url: str | None) -> str | None:
        p = parse_remote(url or "")
        return "/".join(p).lower() if p else None

    async def _resolve_repo_url(identifier: str) -> str:
        """Map a repo name / GitHub URL / owner-repo to a clone URL — but only for
        repos the operator registered or ingested, so chat can't clone arbitrary
        URLs. Raises ValueError with a user-facing message otherwise."""
        ident = identifier.strip()
        repos = cfg.repos()
        if ident in repos:
            return repos[ident].url
        if re.fullmatch(r"[\w.-]+/[\w.-]+", ident):  # owner/repo shorthand
            ident = f"https://github.com/{ident}"
        key = _canon_key(ident)
        if not key:
            raise ValueError(
                f"'{identifier}' is not a registered codebase or a GitHub repo URL")
        allowed: dict[str, str] = {}
        for r in repos.values():
            if (k := _canon_key(r.url)):
                allowed.setdefault(k, r.url)
        if memory:
            tags = [GLOBAL_TAG] + [repo_tag(n) for n in repos]
            for d in await memory.list_documents(tags, limit=200):
                url = (d.get("metadata") or {}).get("url")
                if (k := _canon_key(url)):
                    allowed.setdefault(k, url)
        if key not in allowed:
            raise ValueError(
                f"repo '{identifier}' isn't available to read — register it under "
                "Codebases or add its GitHub URL on the Resources page first")
        return allowed[key]

    @tool
    async def list_repo_files(repo: str, subdir: str = "", limit: int = 300) -> str:
        """List the source files in a codebase so you can choose which to read.
        `repo` is a registered codebase name OR a GitHub repo URL/owner-repo that
        appears in list_resources. Optional `subdir` narrows to a path prefix."""
        try:
            url = await _resolve_repo_url(repo)
            files = await reader.list_files(url, subdir.strip(), min(limit, 1000))
        except (ValueError, GitError) as e:
            return f"Error: {e}"
        if not files:
            return "No files found" + (f" under {subdir!r}." if subdir else ".")
        head = f"{len(files)} file(s)" + (f" under {subdir!r}" if subdir else "") + ":\n"
        return head + "\n".join(files)

    @tool
    async def grep_repo(repo: str, pattern: str, limit: int = 80) -> str:
        """Search a codebase's source for a string/regex (git grep) to locate where
        something is defined or used before reading a file. `repo` is a registered
        codebase name OR a GitHub repo URL/owner-repo from list_resources."""
        try:
            url = await _resolve_repo_url(repo)
            lines = await reader.grep(url, pattern, min(limit, 300))
        except (ValueError, GitError) as e:
            return f"Error: {e}"
        return "\n".join(lines) if lines else f"No matches for {pattern!r}."

    @tool
    async def read_repo_file(repo: str, path: str, max_bytes: int = 40000) -> str:
        """Read one source file from a codebase to answer code-level questions from
        the ACTUAL source. `repo` is a registered codebase name OR a GitHub repo
        URL/owner-repo from list_resources; `path` is repo-relative."""
        try:
            url = await _resolve_repo_url(repo)
            text = await reader.read_file(url, path.strip(),
                                          min(max(max_bytes, 1000), 200_000))
        except (ValueError, GitError) as e:
            return f"Error: {e}"
        return f"=== {path} ===\n{text}"

    tools += [list_repo_files, grep_repo, read_repo_file]
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
                        "may still be indexing: check list_resources for its status.")
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
