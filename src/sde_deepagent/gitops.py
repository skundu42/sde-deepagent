"""Git workspace management: clone the right codebase per task, branch, commit,
push, and open PRs. GitHub auth uses GITHUB_TOKEN; ssh remotes work if the host
has keys configured."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import RepoConfig
from .settings import Settings


class GitError(RuntimeError):
    pass


async def run_cmd(
    args: list[str], cwd: Path | None = None, timeout: int = 300,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a subprocess, return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise GitError(f"command timed out: {' '.join(args)}")
    return proc.returncode or 0, out.decode(errors="replace")


async def git(args: list[str], cwd: Path, timeout: int = 300, check: bool = True) -> str:
    code, out = await run_cmd(["git", *args], cwd=cwd, timeout=timeout)
    if check and code != 0:
        raise GitError(f"git {' '.join(args)} failed ({code}):\n{out[-2000:]}")
    return out


GITHUB_URL_RE = re.compile(
    r"(?:git@(?P<host1>[\w.-]+):|https?://(?:[^@/]+@)?(?P<host2>[\w.-]+)/)"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)


def parse_remote(url: str) -> tuple[str, str, str] | None:
    """Return (host, owner, repo) for github-style remotes, else None."""
    m = GITHUB_URL_RE.match(url.strip())
    if not m:
        return None
    host = m.group("host1") or m.group("host2")
    return host, m.group("owner"), m.group("repo")


def auth_env(url: str, token: str | None) -> dict[str, str] | None:
    """Ephemeral git auth for https remotes via GIT_CONFIG_* env vars.

    The Authorization header exists only for the lifetime of the git process —
    unlike a token-in-URL remote, nothing is persisted into the workspace's
    .git/config where the agent's shell could read it."""
    if not token or not url.startswith("http") or not parse_remote(url):
        return None
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64}",
    }


def _git_env(extra: dict[str, str] | None) -> dict[str, str] | None:
    """Merge auth vars into the full process env (env= replaces, not extends)."""
    if not extra:
        return None
    return {**os.environ, **extra}


@dataclass
class Workspace:
    task_id: str
    repo: RepoConfig
    path: Path  # the cloned repository root
    branch: str


def branch_name_for(task_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
    return f"agent/{task_id}-{slug}"


# Build artifacts the agent must never commit, regardless of the repo's own
# .gitignore (observed live: a run committed __pycache__ on a repo without one).
JUNK_EXCLUDES = [
    "__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", "*.egg-info/", ".coverage", "node_modules/", "dist/",
    "build/", ".DS_Store", "*.swp", ".venv/", "venv/",
]


def _write_junk_excludes(repo_dir: Path) -> None:
    exclude = repo_dir / ".git" / "info" / "exclude"
    if exclude.parent.exists():
        with exclude.open("a") as f:
            f.write("\n# devagent: never commit build junk\n")
            f.write("\n".join(JUNK_EXCLUDES) + "\n")


async def prepare_workspace(
    task_id: str, title: str, repo: RepoConfig, settings: Settings,
    existing_branch: str | None = None,
) -> Workspace:
    """Clone the repo into a fresh per-task workspace.

    Normal tasks get a new work branch; revision tasks pass `existing_branch`
    (a previously pushed agent branch) and continue on it, so pushes update
    the same PR."""
    ws_root = settings.workspaces_dir / task_id
    if ws_root.exists():
        shutil.rmtree(ws_root)
    ws_root.mkdir(parents=True)
    repo_dir = ws_root / "repo"

    # the remote URL stays token-free; auth rides in process-scoped env config
    env = _git_env(auth_env(repo.url, settings.github_token))
    clone_branch = existing_branch or repo.default_branch
    code, out = await run_cmd(
        ["git", "clone", "--depth", "50", "--branch", clone_branch, repo.url,
         str(repo_dir)],
        timeout=600, env=env,
    )
    if code != 0:
        # fall back to full clone without branch pin (e.g. local paths, odd remotes)
        code, out = await run_cmd(["git", "clone", repo.url, str(repo_dir)],
                                  timeout=600, env=env)
        if code != 0:
            raise GitError(f"clone of {repo.url} failed:\n{out[-2000:]}")
        if existing_branch:
            out = await git(["checkout", existing_branch], cwd=repo_dir, check=False)
            head = await git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
            if head.strip() != existing_branch:
                raise GitError(
                    f"branch '{existing_branch}' not found on {repo.url} — cannot "
                    f"revise; the original work may never have been pushed")

    if existing_branch:
        branch = existing_branch
    else:
        branch = branch_name_for(task_id, title)
        await git(["checkout", "-b", branch], cwd=repo_dir)
    await git(["config", "user.name", "sde-deepagent"], cwd=repo_dir)
    await git(["config", "user.email", "sde-deepagent@localhost"], cwd=repo_dir)
    _write_junk_excludes(repo_dir)
    return Workspace(task_id=task_id, repo=repo, path=repo_dir, branch=branch)


async def has_changes(ws: Workspace) -> bool:
    out = await git(["status", "--porcelain"], cwd=ws.path)
    return bool(out.strip())


async def commits_ahead(ws: Workspace) -> int:
    try:
        out = await git(
            ["rev-list", "--count", f"origin/{ws.repo.default_branch}..HEAD"], cwd=ws.path
        )
        return int(out.strip())
    except (GitError, ValueError):
        return 0


async def commit_all(ws: Workspace, message: str) -> str:
    await git(["add", "-A"], cwd=ws.path)
    out = await git(["commit", "-m", message], cwd=ws.path, check=False)
    return out


async def push_branch(ws: Workspace, settings: Settings) -> None:
    env = _git_env(auth_env(ws.repo.url, settings.github_token))
    proc = await asyncio.create_subprocess_exec(
        "git", "push", "-f", "origin", f"HEAD:refs/heads/{ws.branch}",
        cwd=str(ws.path), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise GitError("git push timed out")
    if proc.returncode != 0:
        raise GitError(f"git push failed ({proc.returncode}):\n"
                       f"{out.decode(errors='replace')[-2000:]}")


async def diff_stat(ws: Workspace) -> str:
    return await git(
        ["diff", "--stat", f"origin/{ws.repo.default_branch}...HEAD"], cwd=ws.path, check=False
    )


async def create_pull_request(
    ws: Workspace, settings: Settings, title: str, body: str
) -> str:
    """Open a PR on GitHub (or compatible API). Returns the PR URL."""
    parsed = parse_remote(ws.repo.url)
    if not parsed:
        raise GitError(
            f"cannot parse owner/repo from remote '{ws.repo.url}' — PR creation supports "
            "GitHub-style remotes"
        )
    if not settings.github_token:
        raise GitError("GITHUB_TOKEN is not set — cannot create a pull request")
    host, owner, repo = parsed
    api = settings.github_api_url
    if host != "github.com" and api == "https://api.github.com":
        api = f"https://{host}/api/v3"  # GitHub Enterprise convention

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api}/repos/{owner}/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "title": title,
                "body": body,
                "head": ws.branch,
                "base": ws.repo.default_branch,
            },
        )
        if resp.status_code == 422 and "already exist" in resp.text:
            # PR already open for this branch — fetch it
            existing = await client.get(
                f"{api}/repos/{owner}/{repo}/pulls",
                headers={"Authorization": f"Bearer {settings.github_token}"},
                params={"head": f"{owner}:{ws.branch}", "state": "open"},
            )
            prs = existing.json()
            if prs:
                return prs[0]["html_url"]
        if resp.status_code >= 300:
            raise GitError(f"PR creation failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()["html_url"]


def prune_workspaces(settings: Settings, keep: int | None = None,
                     protect: set[str] | None = None) -> list[str]:
    """Delete all but the `keep` most recently used task workspaces.
    Pushed branches preserve the work; workspaces are just scratch space.
    Workspaces in `protect` (e.g. tasks awaiting approval — their unpushed
    commits live only here) are never deleted. Returns deleted names."""
    if keep is None:
        keep = settings.workspace_retention
    root = settings.workspaces_dir
    if not root.exists():
        return []
    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    deleted = []
    for d in dirs[max(keep, 0):]:
        if protect and d.name in protect:
            continue
        shutil.rmtree(d, ignore_errors=True)
        deleted.append(d.name)
    return deleted
