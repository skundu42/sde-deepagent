"""Builds the per-task deep agent: orchestrator + configurable subagents, with
a shell/filesystem backend rooted at the task's repo clone."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from deepagents import create_deep_agent
from langchain_core.tools import tool

from .config import AgentsConfig
from .context import build_context_block, build_repo_map
from .gitops import (
    GitError,
    Workspace,
    commit_all,
    create_pull_request,
    has_changes,
    push_branch,
    run_cmd,
)
from .llm import EFFORT_LEVELS, build_model, normalize_model_id
from .mcp_tools import load_mcp_tools
from .memory import GLOBAL_TAG, Memory, memory_from_settings, repo_tag
from .prompts import (
    DEFAULT_SUBAGENT_PROMPTS,
    HOST_ENV_PROMPT,
    MEMORY_PROMPT,
    ORCHESTRATOR_PROMPT,
    SANDBOX_ENV_PROMPT,
    SANDBOX_ENV_PROMPT_OFFLINE,
    SHIP_APPROVAL,
    SHIP_NORMAL,
)
from .secrets import Redactor
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
    require_approval: bool,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
    redact: Callable[[str], str] | None = None,
):
    @tool
    async def open_pull_request(title: str, body: str) -> str:
        """Push the current branch and open a pull request against the default
        branch. Call this exactly once, after all work is committed. `title` is
        the PR title; `body` is a markdown description of what changed, why,
        and how it was tested."""
        if redact:  # never let a secret value reach the remote / approval record
            title, body = redact(title), redact(body)
        if await has_changes(ws):
            await commit_all(ws, f"chore: remaining work for {title[:60]}")
        if require_approval:
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


_SAFE_SELECTOR_RE = re.compile(r"^[\w./:= -]*$")


def _safe_selector(selector: str) -> str:
    """Allow only a conservative subset (no shell metacharacters) so an agent's
    test selector cannot break out of the registered command's `bash -lc`."""
    s = (selector or "").strip()
    return s if _SAFE_SELECTOR_RE.match(s) else ""


def make_run_tests_tool(
    *,
    test_cmd: str | None,
    ws: Workspace,
    sandbox_container: str | None,
    sandbox_workdir: str | None,
    secrets: dict[str, str] | None,
    redact: Callable[[str], str] | None,
    on_event: Callable[[str, dict], Awaitable[None]] | None,
):
    """Controller-run test execution: runs the repo's REGISTERED test command
    with its configured secrets injected, then returns redacted output. The
    agent never gets a shell with the secrets — this tool is the only path that
    runs the registered tests with them."""

    @tool
    async def run_tests(selector: str = "") -> str:
        """Run the repository's REGISTERED test command to verify your changes.
        It runs with this repo's configured secrets injected — you cannot see
        their values, and they are redacted from the output. Optionally pass
        `selector` to narrow the run (e.g. a test path or `-k expr`); it is
        appended to the registered command. This is the ONLY way to run the
        project's tests with their secrets — do NOT run the test command
        directly in your shell. Returns pass/fail and the (redacted) output."""
        if not test_cmd:
            return ("No test command is registered for this repo, so there are no "
                    "secret-backed tests to run. If the project has tests, ask the "
                    "operator to register the test command for this codebase.")
        sel = _safe_selector(selector)
        note = "\n(note: selector ignored — unsafe characters)" if (
            selector and not sel) else ""
        cmd = f"{test_cmd} {sel}".strip() if sel else test_cmd
        if sandbox_container:
            from .sandbox import exec_in_container
            resp = await asyncio.to_thread(
                exec_in_container, sandbox_container, cmd,
                timeout=1200, workdir=sandbox_workdir or "/workspaces",
                secrets=secrets or None)
            code, out = resp.exit_code, resp.output
        else:
            code, out = await run_cmd(
                ["bash", "-lc", cmd], cwd=ws.path, timeout=1200,
                env={**_shell_env(), **(secrets or {})})
        if redact:
            out = redact(out)
        if len(out) > 8000:
            out = out[:8000] + "\n\n... output truncated."
        if on_event:
            await on_event("tool_result",
                           {"name": "run_tests", "output": out, "exit_code": code})
        status = "passed" if code == 0 else f"FAILED (exit {code})"
        return f"Tests {status}.\nCommand: {cmd}{note}\n\n{out}"

    return run_tests


def make_memory_tools(memory: Memory, repo_name: str, task_id: str,
                      redact: Callable[[str], str] | None = None):
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
        if redact:
            content = redact(content)
        mem_id = await memory.add(content, tag,
                                  metadata={"task_id": task_id, "repo": repo_name,
                                            "source": "agent"})
        return f"Saved to {scope} memory." if mem_id else \
            "Memory server unavailable — not saved, continue without it."

    return search_memory, save_memory


def make_check_messages_tool(drain: Callable[[], list[str]]):
    @tool
    async def check_messages() -> str:
        """Check for steering messages the operator has sent while you work.
        Call this periodically during long tasks and ALWAYS right before you
        open the pull request, so you can incorporate late guidance. Returns
        any pending operator messages, or a note that there are none."""
        msgs = drain()
        if not msgs:
            return "No new operator messages."
        return "Operator messages (incorporate these now):\n" + \
            "\n".join(f"- {m}" for m in msgs)

    return check_messages


async def build_agent(
    ws: Workspace,
    task_description: str,
    agents_cfg: AgentsConfig,
    settings: Settings,
    model_override: str | None = None,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
    sandbox_container: str | None = None,
    sandbox_workdir: str | None = None,
    sandbox_network: str | None = None,
    require_approval: bool | None = None,
    drain_messages: Callable[[], list[str]] | None = None,
    secrets: dict[str, str] | None = None,
    redactor: Redactor | None = None,
    checkpointer: Any = None,
) -> BuiltAgent:
    # masks any repo secret value that surfaces through the agent's own shell or
    # file reads (its env is secret-free, but test code could write one out)
    redact = redactor.redact if (redactor is not None and redactor.active) else None
    if sandbox_container:
        from .sandbox import DockerShellBackend
        backend = DockerShellBackend(ws.path, sandbox_container,
                                     workdir=sandbox_workdir or "/workspaces",
                                     timeout=600, max_output_bytes=60000,
                                     redact=redact)
    else:
        from .sandbox import RedactingLocalShellBackend
        backend = RedactingLocalShellBackend(
            root_dir=ws.path,
            virtual_mode=True,
            timeout=600,
            max_output_bytes=60000,
            env=_shell_env(),
            redact=redact,
        )

    result: dict[str, str | None] = {"pr_url": None, "pr_title": None,
                                     "pr_body": None}
    effective_approval = settings.require_approval if require_approval is None else require_approval
    # run_tests runs the REGISTERED test command with secrets injected by the
    # controller; the agent and its subagents trigger it but never see the values
    run_tests_tool = make_run_tests_tool(
        test_cmd=ws.repo.test, ws=ws, sandbox_container=sandbox_container,
        sandbox_workdir=sandbox_workdir, secrets=secrets, redact=redact,
        on_event=on_event)
    tools: list = [
        make_pr_tool(ws, settings, result, effective_approval, on_event, redact),
        run_tests_tool,
    ]
    if drain_messages is not None:
        tools.append(make_check_messages_tool(drain_messages))
    tools.extend(await load_mcp_tools(agents_cfg.mcp_servers))

    memory = memory_from_settings(settings)
    subagent_tools: list = [run_tests_tool]  # the tester runs the suite through it
    memory_prompt = ""
    if memory:
        search_memory, save_memory = make_memory_tools(
            memory, ws.repo.name, ws.task_id, redact)
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

    if sandbox_container:
        exec_environment = (SANDBOX_ENV_PROMPT if sandbox_network == "bridge"
                            else SANDBOX_ENV_PROMPT_OFFLINE)
    else:
        exec_environment = HOST_ENV_PROMPT

    repo = ws.repo
    system_prompt = (agents_cfg.orchestrator_prompt or ORCHESTRATOR_PROMPT).format(
        exec_environment=exec_environment,
        branch=ws.branch,
        repo_name=repo.name,
        repo_description=repo.description or "(no description registered)",
        default_branch=repo.default_branch,
        setup_cmd=repo.setup or "(none registered)",
        test_cmd=repo.test or "(none registered — discover it from the repo)",
        task_description=task_description,
        ship_instructions=SHIP_APPROVAL if effective_approval else SHIP_NORMAL,
        repo_map=build_repo_map(ws.path),
        context_block=build_context_block(ws.path, repo, settings),
    ) + memory_prompt

    # NB: history summarization near the context limit is already provided by
    # create_deep_agent's default middleware stack (deepagents'
    # _DeepAgentsSummarizationMiddleware — backend-offloaded, model-aware
    # trigger), so we don't add our own. The checkpointer persists agent state
    # for resume-after-restart when one is supplied.
    agent = create_deep_agent(
        model=orchestrator_model,
        tools=tools,
        system_prompt=system_prompt,
        subagents=subagents,
        backend=backend,
        checkpointer=checkpointer,
        name="sde-deepagent-orchestrator",
    )
    return BuiltAgent(agent=agent, workspace=ws, result=result)


# Placeholders the orchestrator system prompt is .format()-ed with at task time
# (see the .format(...) call in build_agent). A custom orchestrator override may
# only reference these; anything else — or an unescaped literal brace — raises
# at agent-build time, so we reject it up front. Keep in sync with that call.
ORCHESTRATOR_FORMAT_KEYS = frozenset({
    "exec_environment", "branch", "repo_name", "repo_description", "default_branch",
    "setup_cmd", "test_cmd", "task_description", "ship_instructions", "repo_map",
    "context_block",
})


def validate_orchestrator_prompt(text: str) -> str | None:
    """Return an error message if an orchestrator prompt override would fail
    str.format() at task time (unknown placeholder or unescaped brace), else None.
    Subagent prompts are used verbatim and need no such check."""
    try:
        text.format(**{k: "" for k in ORCHESTRATOR_FORMAT_KEYS})
    except KeyError as e:
        allowed = ", ".join("{%s}" % k for k in sorted(ORCHESTRATOR_FORMAT_KEYS))
        return f"orchestrator prompt uses unknown placeholder {e}; allowed: {allowed}"
    except (ValueError, IndexError) as e:
        return f"orchestrator prompt has invalid format ({e}); escape literal braces as {{{{ }}}}"
    return None


def validate_agents_payload(body: dict) -> list[str]:
    """Validate a raw agents-config payload (the dict the UI PUTs) before it is
    written: orchestrator prompt placeholders and mcp_servers shape."""
    errors = []
    orch = body.get("orchestrator") or {}
    if isinstance(orch, dict) and orch.get("system_prompt"):
        err = validate_orchestrator_prompt(orch["system_prompt"])
        if err:
            errors.append(err)
    mcp = body.get("mcp_servers")
    if mcp is not None:
        if not isinstance(mcp, dict):
            errors.append("mcp_servers must be a mapping of name -> config object")
        else:
            for name, spec in mcp.items():
                if not isinstance(spec, dict):
                    errors.append(f"mcp server '{name}' must be a config object")
    return errors


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
