"""Read-only access to repository source for the chat assistant.

Resource ingestion only stores a repo's landing page (its README), so the chat
could never answer code-level questions ("what does the Hub contract do?"). This
maintains a small cache of shallow, read-only clones — one per repo — and exposes
list/read/grep over them so the chat can read the *actual* source.

The clones are never written to, never pushed, and live outside the task-workspace
tree (so the workspace reaper leaves them alone). Cloning reuses gitops' auth so
private GitHub repos work with GITHUB_TOKEN, while public repos need no token."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .config import repo_slug
from .gitops import (
    GitError,
    _git_env,
    auth_env,
    parse_remote,
    run_cmd,
    trusted_github_hosts,
)
from .settings import Settings

DEFAULT_MAX_FILE_BYTES = 40_000


class RepoReader:
    """Lazily clones repos (shallow, single-branch) and reads files from them.

    The on-disk clone is the cache: it survives across chat turns and process
    restarts, so only the first read of a repo pays the clone cost."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._locks: dict[str, asyncio.Lock] = {}

    def _clone_dir(self, url: str) -> Path:
        parsed = parse_remote(url)
        key = "-".join(parsed) if parsed else url  # host-owner-repo, else raw url
        return self.settings.ref_clones_dir / repo_slug(key)

    def _lock(self, path: Path) -> asyncio.Lock:
        return self._locks.setdefault(str(path), asyncio.Lock())

    async def ensure_clone(self, url: str) -> Path:
        """Return a path to a shallow clone of `url`, cloning on first use."""
        clone_dir = self._clone_dir(url)
        async with self._lock(clone_dir):
            if (clone_dir / ".git").is_dir():
                return clone_dir
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            env = _git_env(auth_env(url, self.settings.github_token,
                                    trusted_github_hosts(self.settings)))
            code, out = await run_cmd(
                ["git", "clone", "--depth", "1", "--single-branch", url, str(clone_dir)],
                timeout=300, env=env)
            if code != 0:
                shutil.rmtree(clone_dir, ignore_errors=True)  # no half-clone behind
                raise GitError(f"clone of {url} failed:\n{out[-600:]}")
            return clone_dir

    async def list_files(self, url: str, subdir: str = "", limit: int = 300) -> list[str]:
        """Tracked files in the repo (git ls-files — ignores .git and gitignored)."""
        root = await self.ensure_clone(url)
        args = ["ls-files"] + (["--", subdir] if subdir else [])
        code, out = await run_cmd(["git", *args], cwd=root, timeout=60, env=_git_env())
        if code != 0:
            raise GitError(out[-400:] or "git ls-files failed")
        return [ln for ln in out.splitlines() if ln.strip()][:limit]

    async def read_file(self, url: str, path: str,
                        max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> str:
        """Read one repo-relative text file. Guards against path traversal and
        truncates large files with a note."""
        root = await self.ensure_clone(url)
        root_resolved = root.resolve()
        target = (root / path).resolve()
        if target != root_resolved and not target.is_relative_to(root_resolved):
            raise GitError(f"path escapes repo: {path!r}")
        if not target.is_file():
            raise GitError(f"no such file: {path!r}")
        size = target.stat().st_size
        data = target.read_bytes()[:max_bytes]
        if b"\x00" in data[:8192]:
            raise GitError(f"{path!r} looks binary — not reading")
        text = data.decode("utf-8", errors="replace")
        if size > max_bytes:
            text += f"\n... [truncated at {max_bytes} bytes; file is {size} bytes]"
        return text

    async def grep(self, url: str, pattern: str, limit: int = 80) -> list[str]:
        """`git grep -nI` for a pattern across tracked text files."""
        root = await self.ensure_clone(url)
        code, out = await run_cmd(
            ["git", "grep", "-n", "-I", "--no-color", "-e", pattern],
            cwd=root, timeout=60, env=_git_env())
        if code not in (0, 1):  # git grep exits 1 on "no matches", which isn't an error
            raise GitError(out[-400:] or "git grep failed")
        return [ln for ln in out.splitlines() if ln.strip()][:limit]
