"""YAML-backed configuration for agents (models, prompts, MCP servers) and the
codebase registry. Both files are hot-reloadable: the UI edits them through the
API and the next task picks up the change."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .secrets import validate_secret_spec

logger = logging.getLogger(__name__)
HASHED_SLUG_RE = re.compile(r"--[0-9a-f]{16}$")


def legacy_repo_slug(name: str) -> str:
    """Pre-hardening slug retained only for locating existing workspaces."""
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-.")
    return (s or "repo").lower()[:50]


def repo_slug(name: str) -> str:
    """Filesystem- and Docker-name-safe slug for a repo name (workspace
    directories and sandbox containers are keyed by it).

    Preserve simple lowercase names for backwards compatibility, except names
    using the reserved hashed-slug suffix. Any name that needs normalization
    gets a stable hash suffix so distinct names such as ``foo/bar`` and
    ``foo-bar`` cannot share a workspace or sandbox boundary.
    """
    raw = name.strip()
    base = legacy_repo_slug(raw)
    if raw == base and len(base) <= 50 and not HASHED_SLUG_RE.search(base):
        return base
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{base[:32]}--{suffix}"


def is_safe_context_pattern(pattern: str) -> bool:
    """Whether a repo `context` glob is safe to feed to Path.glob().

    Patterns must stay inside the repo checkout. Path.glob() happily resolves
    `../` and absolute paths, so an unvalidated pattern like `../../../etc/passwd`
    would read arbitrary host files into the agent's context block. Reject
    absolute paths, home expansion, and any parent-directory traversal."""
    if not pattern or pattern.startswith(("/", "~", "\\")):
        return False
    return ".." not in re.split(r"[\\/]+", pattern)


def _safe_context(name: str, patterns: list[str]) -> list[str]:
    """Drop (and log) any unsafe context patterns from a repo spec."""
    safe = []
    for p in patterns:
        if is_safe_context_pattern(p):
            safe.append(p)
        else:
            logger.warning("repo %s: ignoring unsafe context pattern %r", name, p)
    return safe


def _safe_secrets(name: str, secrets: Any) -> dict[str, str]:
    """Drop (and log) any invalid/reserved secret references from a repo spec."""
    if not isinstance(secrets, dict):
        return {}
    safe: dict[str, str] = {}
    for key, ref in secrets.items():
        try:
            validate_secret_spec({key: ref})
        except ValueError as e:
            logger.warning("repo %s: ignoring invalid secret %r (%s)", name, key, e)
        else:
            safe[str(key)] = str(ref)
    return safe

DEFAULT_AGENTS_YAML = """\
# Model for every agent role. Format: "<provider>:<model>" where provider is
# `anthropic`, `google_genai` or `openai`. A bare model name is auto-prefixed
# (claude-* -> anthropic, gemini-* -> google_genai, gpt-*/o* -> openai).
# Each role also accepts `effort: low|medium|high` to control reasoning depth
# (OpenAI reasoning_effort / Anthropic effort / Gemini thinking_level).
orchestrator:
  model: anthropic:claude-sonnet-4-6

subagents:
  explorer:
    model: anthropic:claude-haiku-4-5-20251001
    description: >
      Read-only codebase scout. Use it to find relevant files, understand how a
      subsystem works, or locate where a change must be made. Give it a focused
      question; it returns file paths and explanations, not patches.
  coder:
    model: anthropic:claude-sonnet-4-6
    description: >
      Implementation specialist. Delegate a well-scoped code change with the
      exact files to touch and the conventions to follow. It edits files and
      reports what it changed.
  tester:
    model: anthropic:claude-sonnet-4-6
    description: >
      Runs the test suite, writes missing tests for the change, and debugs
      failures. Tell it the test command and which behavior to cover.
  reviewer:
    model: anthropic:claude-haiku-4-5-20251001
    description: >
      Reviews the final diff for bugs, style violations and missed edge cases
      before the PR is opened. Returns a list of must-fix issues or approval.

# Extra MCP servers whose tools are given to the orchestrator.
# Example:
# mcp_servers:
#   docs:
#     transport: streamable_http
#     url: http://localhost:8931/mcp
#   internal-cli:
#     transport: stdio
#     command: python
#     args: ["/opt/tools/server.py"]
mcp_servers: {}

# Pricing overrides in USD per million tokens — built-in prices for current
# Claude/Gemini models are bundled, add or correct entries here when they drift.
# pricing:
#   claude-sonnet-4-6: {input: 3.0, output: 15.0}
pricing: {}
"""

DEFAULT_REPOS_YAML = """\
# Codebase registry. Each entry teaches the agent about one repository.
# repos:
#   backend:
#     url: git@github.com:acme/backend.git   # or https://, or a local path
#     default_branch: main
#     description: "Python FastAPI monolith serving the public API"
#     setup: "uv sync"                        # run after clone (optional)
#     test: "uv run pytest -x -q"             # how the agent verifies its work
#     context:                                # docs the agent should read first
#       - docs/architecture.md
#       - CONTRIBUTING.md
repos: {}
"""


@dataclass
class RepoConfig:
    name: str
    url: str
    default_branch: str = "main"
    description: str = ""
    setup: str | None = None
    test: str | None = None
    context: list[str] = field(default_factory=list)
    # per-repo overrides (None = inherit the server-wide default)
    sandbox: bool | None = None          # run this repo's tasks in a container
    sandbox_image: str | None = None     # image for the sandbox (else server default)
    sandbox_network: str | None = None   # none | bridge (egress policy in the sandbox)
    approval: str | None = None          # auto | required (else server require_approval)
    # secret NAME -> "env:HOST_VAR" reference; values live in the host env, never
    # here. Injected into controller-run setup/test only, never the agent shell.
    secrets: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "default_branch": self.default_branch,
            "description": self.description,
            "setup": self.setup,
            "test": self.test,
            "context": self.context,
            "sandbox": self.sandbox,
            "sandbox_image": self.sandbox_image,
            "sandbox_network": self.sandbox_network,
            "approval": self.approval,
            # references only (e.g. "env:BACKEND_DB_URL") — safe to persist/serve
            "secrets": self.secrets,
        }


@dataclass
class SubagentConfig:
    name: str
    model: str | None = None
    effort: str | None = None
    description: str = ""
    system_prompt: str | None = None


@dataclass
class AgentsConfig:
    orchestrator_model: str
    orchestrator_effort: str | None
    orchestrator_prompt: str | None
    subagents: list[SubagentConfig]
    mcp_servers: dict[str, dict[str, Any]]
    pricing: dict[str, dict[str, Any]] = field(default_factory=dict)


class ConfigStore:
    """Reads/writes config/agents.yaml and config/repos.yaml."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.agents_path = config_dir / "agents.yaml"
        self.repos_path = config_dir / "repos.yaml"
        self._lock = threading.Lock()
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not self.agents_path.exists():
            self.agents_path.write_text(DEFAULT_AGENTS_YAML)
        if not self.repos_path.exists():
            self.repos_path.write_text(DEFAULT_REPOS_YAML)

    # ---- agents ----

    def agents_raw(self) -> dict[str, Any]:
        with self._lock:
            return yaml.safe_load(self.agents_path.read_text()) or {}

    def agents(self) -> AgentsConfig:
        raw = self.agents_raw()
        orch = raw.get("orchestrator") or {}
        subs = []
        for name, spec in (raw.get("subagents") or {}).items():
            spec = spec or {}
            subs.append(
                SubagentConfig(
                    name=name,
                    model=spec.get("model"),
                    effort=spec.get("effort"),
                    description=(spec.get("description") or "").strip(),
                    system_prompt=spec.get("system_prompt"),
                )
            )
        return AgentsConfig(
            orchestrator_model=orch.get("model") or "anthropic:claude-sonnet-4-6",
            orchestrator_effort=orch.get("effort"),
            orchestrator_prompt=orch.get("system_prompt"),
            subagents=subs,
            mcp_servers=raw.get("mcp_servers") or {},
            pricing=raw.get("pricing") or {},
        )

    def update_agents(self, raw: dict[str, Any]) -> None:
        with self._lock:
            self.agents_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    # ---- repos ----

    def repos_raw(self) -> dict[str, Any]:
        with self._lock:
            return yaml.safe_load(self.repos_path.read_text()) or {}

    def repos(self) -> dict[str, RepoConfig]:
        raw = self.repos_raw().get("repos") or {}
        out: dict[str, RepoConfig] = {}
        for name, spec in raw.items():
            spec = spec or {}
            out[name] = RepoConfig(
                name=name,
                url=spec.get("url", ""),
                default_branch=spec.get("default_branch", "main"),
                description=spec.get("description", ""),
                setup=spec.get("setup"),
                test=spec.get("test"),
                context=_safe_context(name, spec.get("context") or []),
                sandbox=spec.get("sandbox"),
                sandbox_image=spec.get("sandbox_image"),
                sandbox_network=spec.get("sandbox_network"),
                approval=spec.get("approval"),
                secrets=_safe_secrets(name, spec.get("secrets") or {}),
            )
        return out

    def upsert_repo(self, repo: RepoConfig) -> None:
        with self._lock:
            raw = yaml.safe_load(self.repos_path.read_text()) or {}
            raw.setdefault("repos", {})
            if raw["repos"] is None:
                raw["repos"] = {}
            raw["repos"][repo.name] = repo.to_dict()
            self.repos_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    def delete_repo(self, name: str) -> bool:
        with self._lock:
            raw = yaml.safe_load(self.repos_path.read_text()) or {}
            repos = raw.get("repos") or {}
            if name not in repos:
                return False
            del repos[name]
            raw["repos"] = repos
            self.repos_path.write_text(yaml.safe_dump(raw, sort_keys=False))
            return True
