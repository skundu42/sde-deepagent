"""Builds the per-task deep agent: orchestrator + configurable subagents, with
a shell/filesystem backend rooted at the task's repo clone."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.tools import tool

from .config import AgentsConfig
from .context import build_context_block
from .gitops import (
    GitError, Workspace, commit_all, create_pull_request, has_changes, push_branch,
)
from .context import build_repo_map
from .llm import EFFORT_LEVELS, build_model, normalize_model_id
from .mcp_tools import load_mcp_tools
from .memory import GLOBAL_TAG, Memory, memory_from_settings, repo_tag
from .prompts import (
    DEFAULT_SUBAGENT_PROMPTS, MEMORY_PROMPT, ORCHESTRATOR_PROMPT, SHIP_APPROVAL,
    SHIP_NORMAL,
)
from .settings import Settings

# Env passed to agent shell commands: enough to build/test, without leaking
# the host's API keys into the agent's shell.
SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "SHELL")


def _shell_env() -> dict[str, str]:
    return {k: v for k in SAFE_ENV_KEYS if (v := os.environ.get(k))}


@dataclass
class BuiltAgent:
    agent: Any
    workspace: Workspace
    # mutated by the open_pull_request tool so the runner can record the URL
    result: dict[str, str | None]


def make_pr_tool(
    ws: Workspace,
    settings: Settings,
    result: dict[str, str | None],
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
):
    @tool
    async def open_pull_request(title: str, body: str) -> str:
        """Push the current branch and open a pull request against the default
        branch. Call this exactly once, after all work is committed. `title` is
        the PR title; `body` is a markdown description of what changed, why,
        and how it was tested."""
        if await has_changes(ws):
            await commit_all(ws, f"chore: remaining work for {title[:60]}")
        if settings.require_approval:
            # nothing leaves the machine without a human decision
            result["pr_title"] = title
            result["pr_body"] = body
            return ("Approval mode: your work is committed locally and your PR "
                    "proposal is recorded. An operator will review the diff and "
                    "ship it — do not retry, just finish up and summarize.")
        await push_branch(ws, settings)
        try:
            url = await create_pull_request(ws, settings, title, body)
        except GitError as e:
            return (f"Branch '{ws.branch}' was pushed, but no PR could be opened: {e}. "
                    "Do not retry — finish up and report the branch name instead.")
        result["pr_url"] = url
        result["pr_title"] = title
        if on_event:
            await on_event("pr_opened", {"url": url, "title": title})
        return f"Pull request opened: {url}"

    return open_pull_request


def make_memory_tools(memory: Memory, repo_name: str, task_id: str):
    """search_memory (also given to subagents) and save_memory (orchestrator only)."""
    r_tag = repo_tag(repo_name)

    @tool
    async def search_memory(query: str, scope: str = "all") -> str:
        """Search long-term memory for learnings from previous tasks: codebase
        conventions, gotchas, past decisions, recurring issues. Use BEFORE
        exploring from scratch. `scope` is "repo" (this codebase), "global"
        (org-wide), or "all" (default)."""
        tags = {"repo": [r_tag], "global": [GLOBAL_TAG]}.get(scope, [r_tag, GLOBAL_TAG])
        results = await memory.search(query, tags, limit=6)
        if not results:
            return "No relevant memories found."
        lines = []
        for r in results:
            origin = "repo" if r["container"] == r_tag else "global"
            lines.append(f"- [{origin}] {r['memory']}")
        return "\n".join(lines)

    @tool
    async def save_memory(content: str, scope: str = "repo") -> str:
        """Save a durable, non-obvious learning to long-term memory so future
        tasks benefit: a convention you discovered, a tricky subsystem
        explained, a decision and its rationale. One concise fact per call.
        Never save trivia or anything obvious from a quick file read.
        `scope` is "repo" (this codebase, default) or "global" (org-wide)."""
        tag = GLOBAL_TAG if scope == "global" else r_tag
        mem_id = await memory.add(content, tag,
                                  metadata={"task_id": task_id, "repo": repo_name,
                                            "source": "agent"})
        return f"Saved to {scope} memory." if mem_id else \
            "Memory server unavailable — not saved, continue without it."

    return search_memory, save_memory


async def build_agent(
    ws: Workspace,
    task_description: str,
    agents_cfg: AgentsConfig,
    settings: Settings,
    model_override: str | None = None,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> BuiltAgent:
    backend = LocalShellBackend(
        root_dir=ws.path,
        virtual_mode=True,
        timeout=600,
        max_output_bytes=60000,
        env=_shell_env(),
    )

    result: dict[str, str | None] = {"pr_url": None, "pr_title": None,
                                     "pr_body": None}
    tools: list = [make_pr_tool(ws, settings, result, on_event)]
    tools.extend(await load_mcp_tools(agents_cfg.mcp_servers))

    memory = memory_from_settings(settings)
    subagent_tools: list = []
    memory_prompt = ""
    if memory:
        search_memory, save_memory = make_memory_tools(memory, ws.repo.name, ws.task_id)
        tools.extend([search_memory, save_memory])
        subagent_tools.append(search_memory)  # subagents read memory; only the
        memory_prompt = MEMORY_PROMPT          # orchestrator writes it

    orchestrator_model = build_model(model_override or agents_cfg.orchestrator_model,
                                     effort=agents_cfg.orchestrator_effort)

    subagents = []
    for sub in agents_cfg.subagents:
        prompt = sub.system_prompt or DEFAULT_SUBAGENT_PROMPTS.get(sub.name)
        if not prompt:
            prompt = f"You are the {sub.name} subagent. {sub.description}"
        spec: dict[str, Any] = {
            "name": sub.name,
            "description": sub.description or f"{sub.name} subagent",
            "system_prompt": prompt,
        }
        if subagent_tools:
            spec["tools"] = list(subagent_tools)
        if sub.model:
            spec["model"] = build_model(sub.model, effort=sub.effort)
        subagents.append(spec)

    repo = ws.repo
    system_prompt = (agents_cfg.orchestrator_prompt or ORCHESTRATOR_PROMPT).format(
        branch=ws.branch,
        repo_name=repo.name,
        repo_description=repo.description or "(no description registered)",
        default_branch=repo.default_branch,
        setup_cmd=repo.setup or "(none registered)",
        test_cmd=repo.test or "(none registered — discover it from the repo)",
        task_description=task_description,
        ship_instructions=SHIP_APPROVAL if settings.require_approval else SHIP_NORMAL,
        repo_map=build_repo_map(ws.path),
        context_block=build_context_block(ws.path, repo, settings),
    ) + memory_prompt

    agent = create_deep_agent(
        model=orchestrator_model,
        tools=tools,
        system_prompt=system_prompt,
        subagents=subagents,
        backend=backend,
        name="devagent-orchestrator",
    )
    return BuiltAgent(agent=agent, workspace=ws, result=result)


def validate_models(agents_cfg: AgentsConfig) -> list[str]:
    """Return a list of config errors (bad model ids / effort levels) without
    instantiating models."""
    errors = []
    for label, model in [
        ("orchestrator", agents_cfg.orchestrator_model),
        *[(f"subagents.{s.name}", s.model) for s in agents_cfg.subagents if s.model],
    ]:
        try:
            normalize_model_id(model)  # type: ignore[arg-type]
        except ValueError as e:
            errors.append(f"{label}: {e}")
    for label, effort in [
        ("orchestrator", agents_cfg.orchestrator_effort),
        *[(f"subagents.{s.name}", s.effort) for s in agents_cfg.subagents],
    ]:
        if effort and effort not in EFFORT_LEVELS:
            errors.append(f"{label}: effort must be one of {EFFORT_LEVELS}, "
                          f"got '{effort}'")
    return errors
