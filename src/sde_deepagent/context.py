"""Builds the context the agent works with: repo convention files, registered
doc globs from repos.yaml, and the global company `context/` directory (copied
into the workspace so the agent can read everything on demand)."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from .config import RepoConfig, is_safe_context_pattern
from .settings import Settings

logger = logging.getLogger(__name__)

# Files that conventionally carry agent/contributor instructions.
CONVENTION_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    "CONTRIBUTING.md",
]

MAX_FILE_CHARS = 6000
MAX_TOTAL_CHARS = 28000
CONTEXT_MOUNT = "_context"  # company docs land here inside the workspace


def _read_truncated(path: Path, limit: int = MAX_FILE_CHARS) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated, read the full file at {path.name}]"
    return text


def mount_company_context(repo_path: Path, settings: Settings) -> list[str]:
    """Copy the global context/ dir into the workspace (git-excluded).
    Returns the list of mounted relative paths."""
    src = settings.context_dir
    if not src.exists() or not src.is_dir():
        return []
    # skip dotfiles/dotdirs entirely — both in the listing AND the copy. Copying
    # the whole tree while only filtering the *listing* would silently materialize
    # a `.env`/`.hidden/` into the agent's workspace (readable by its file tools)
    # while hiding it from the prompt.
    files = [p for p in sorted(src.rglob("*"))
             if p.is_file()
             and not any(part.startswith(".") for part in p.relative_to(src).parts)]
    if not files:
        return []
    dest = repo_path / CONTEXT_MOUNT
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".*"))
    exclude = repo_path / ".git" / "info" / "exclude"
    if exclude.parent.exists():
        with exclude.open("a") as f:
            f.write(f"\n{CONTEXT_MOUNT}/\n")
    return [str(p.relative_to(src)) for p in files]


MAP_MAX_FILES = 400
MAP_MAX_CHARS = 6000
MAP_SYMBOL_FILES = 40  # extract def/class names from this many source files
_PY_SYMBOL_RE = re.compile(r"^(?:class|def|async def)\s+(\w+)", re.MULTILINE)
_JS_SYMBOL_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)", re.MULTILINE)
_SKIP_DIRS = {".git", CONTEXT_MOUNT, "__pycache__", "node_modules", ".venv",
              "venv", "dist", "build", ".pytest_cache", ".mypy_cache"}


def build_repo_map(repo_path: Path) -> str:
    """Cheap, deterministic map of the repo (no LLM): file listing with line
    counts plus top-level symbols of source files. Gives the agent navigation
    without burning tokens on blind exploration of larger codebases."""
    files: list[Path] = []
    for p in sorted(repo_path.rglob("*")):
        if any(part in _SKIP_DIRS for part in p.relative_to(repo_path).parts):
            continue
        if p.is_file():
            files.append(p)
        if len(files) >= MAP_MAX_FILES:
            break

    lines: list[str] = []
    symbol_budget = MAP_SYMBOL_FILES
    for p in files:
        rel = p.relative_to(repo_path)
        try:
            size = p.stat().st_size
        except OSError:
            continue
        entry = str(rel)
        if p.suffix in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb"):
            try:
                text = p.read_text(errors="replace")
                entry += f" ({text.count(chr(10)) + 1} lines)"
                if symbol_budget > 0 and p.suffix == ".py":
                    syms = _PY_SYMBOL_RE.findall(text)[:12]
                elif symbol_budget > 0 and p.suffix in (".js", ".ts", ".tsx", ".jsx"):
                    syms = _JS_SYMBOL_RE.findall(text)[:12]
                else:
                    syms = []
                if syms:
                    symbol_budget -= 1
                    entry += f": {', '.join(syms)}"
            except OSError:
                pass
        elif size > 50_000:
            entry += f" ({size // 1024}KB)"
        lines.append(entry)

    out = "\n".join(lines)
    if len(out) > MAP_MAX_CHARS:
        out = out[:MAP_MAX_CHARS] + f"\n... [map truncated; {len(files)} files total]"
    elif len(files) >= MAP_MAX_FILES:
        out += f"\n... [only the first {MAP_MAX_FILES} files mapped]"
    return out or "(empty repository)"


def build_context_block(repo_path: Path, repo: RepoConfig, settings: Settings) -> str:
    """Assemble the markdown context block injected into the orchestrator prompt."""
    sections: list[str] = []
    budget = MAX_TOTAL_CHARS
    repo_root = repo_path.resolve()

    def add_section(header: str, path: Path) -> None:
        # Reserve the header and the "\n\n" join separator against the budget so
        # the assembled block actually respects MAX_TOTAL_CHARS (previously only
        # raw file content was counted, letting headers push it over the cap).
        nonlocal budget
        avail = budget - len(header) - 2
        if avail <= 0:
            return
        section = header + _read_truncated(path, min(MAX_FILE_CHARS, avail))
        sections.append(section)
        budget -= len(section) + 2

    # 1. repo convention/instruction files
    for rel in CONVENTION_FILES:
        p = repo_path / rel
        if p.is_file():
            add_section(f"### {rel} (repository instructions)\n\n", p)

    # 2. registered context docs from repos.yaml
    for pattern in repo.context:
        if not is_safe_context_pattern(pattern):
            logger.warning("repo %s: skipping unsafe context pattern %r",
                           repo.name, pattern)
            continue
        for p in sorted(repo_path.glob(pattern)):
            if not p.is_file():
                continue
            # Belt-and-suspenders against the path-traversal surface: even a safe
            # pattern can match a symlink that points outside the checkout. Refuse
            # anything whose real path escapes the repo root.
            try:
                rel = p.resolve().relative_to(repo_root)
            except (OSError, ValueError):
                logger.warning("repo %s: skipping context file outside repo: %s",
                               repo.name, p)
                continue
            add_section(f"### {rel} (registered repo doc)\n\n", p)

    # 3. company-wide context directory, mounted into the workspace
    mounted = mount_company_context(repo_path, settings)
    if mounted:
        listing = "\n".join(f"- {CONTEXT_MOUNT}/{m}" for m in mounted[:100])
        sections.append(
            "### Company context documents\n\n"
            f"Company-wide docs are mounted at `{CONTEXT_MOUNT}/` inside the repo "
            "(excluded from git). Read any of them with the filesystem tools when "
            f"relevant:\n{listing}"
        )

    if not sections:
        return "(no additional context documents registered)"
    block = "\n\n".join(sections)
    # Hard cap: the company-docs listing is appended outside the per-section
    # budget, so clamp the final block to guarantee the limit holds.
    return block if len(block) <= MAX_TOTAL_CHARS else block[:MAX_TOTAL_CHARS]
