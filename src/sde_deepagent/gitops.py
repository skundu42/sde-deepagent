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
from urllib.parse import urlparse

import httpx

from .config import RepoConfig, legacy_repo_slug, repo_slug
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


async def git(args: list[str], cwd: Path, timeout: int = 300, check: bool = True,
              env: dict[str, str] | None = None) -> str:
    code, out = await run_cmd(["git", *args], cwd=cwd, timeout=timeout, env=env)
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


def trusted_github_hosts(settings: Settings) -> set[str]:
    """Hosts that may receive GITHUB_TOKEN.

    github.com is always supported. A GitHub Enterprise host is trusted only
    when GITHUB_API_URL explicitly points at that same host.
    """
    hosts = {"github.com"}
    api_host = urlparse(settings.github_api_url).hostname
    if api_host and api_host != "api.github.com":
        hosts.add(api_host.lower())
    return hosts


def auth_env(url: str, token: str | None,
             allowed_hosts: set[str] | None = None) -> dict[str, str] | None:
    """Ephemeral git auth for https remotes via GIT_CONFIG_* env vars.

    The Authorization header exists only for the lifetime of the git process —
    unlike a token-in-URL remote, nothing is persisted into the workspace's
    .git/config where the agent's shell could read it."""
    parsed = parse_remote(url)
    if not token or not url.startswith("https://") or not parsed:
        return None
    host = parsed[0].lower()
    if host not in (allowed_hosts or {"github.com"}):
        return None
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "2",
        # Scope the credential to the exact registered remote. Redirects and
        # unrelated hosts must never receive the GitHub token.
        "GIT_CONFIG_KEY_0": f"http.{url.rstrip('/')}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64}",
        "GIT_CONFIG_KEY_1": "http.followRedirects",
        "GIT_CONFIG_VALUE_1": "false",
    }


SAFE_GIT_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for trusted Git; provider/service secrets stay out."""
    env = {k: v for k in SAFE_GIT_ENV_KEYS if (v := os.environ.get(k))}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    if extra:
        env.update(extra)
    return env


@dataclass
class Workspace:
    task_id: str
    repo: RepoConfig
    path: Path  # the cloned repository root
    branch: str
    control_git_dir: Path


def control_git_dir_for(settings: Settings, repo_name: str, task_id: str) -> Path:
    return settings.control_git_dir / repo_slug(repo_name) / f"{task_id}.git"


def legacy_control_git_dir_for(settings: Settings, repo_name: str, task_id: str) -> Path:
    return settings.control_git_dir / legacy_repo_slug(repo_name) / f"{task_id}.git"


def github_api_for_host(settings: Settings, remote_host: str) -> str:
    """Return the configured API URL only when it matches the remote host."""
    host = remote_host.lower()
    api = settings.github_api_url.rstrip("/")
    parsed_api = urlparse(api)
    api_host = (parsed_api.hostname or "").lower()
    expected_api_host = "api.github.com" if host == "github.com" else host
    if parsed_api.scheme != "https" or api_host != expected_api_host:
        raise GitError(
            f"refusing to send GITHUB_TOKEN to API URL '{api}'; expected HTTPS "
            f"on host '{expected_api_host}' for remote host '{host}'"
        )
    return api


async def control_git(
    ws: Workspace, args: list[str], timeout: int = 300, check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    """Run Git against protected metadata, never the workspace-controlled .git."""
    cmd = [
        "git",
        f"--git-dir={ws.control_git_dir}",
        f"--work-tree={ws.path}",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", f"core.attributesFile={os.devnull}",
        *args,
    ]
    code, out = await run_cmd(cmd, cwd=ws.path, timeout=timeout, env=env or _git_env())
    if check and code != 0:
        raise GitError(f"trusted git {' '.join(args)} failed ({code}):\n{out[-2000:]}")
    return out


def branch_name_for(task_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
    return f"agent/{task_id}-{slug}"


# Build artifacts the agent must never commit, regardless of the repo's own
# .gitignore (observed live: a run committed __pycache__ on a repo without one).
JUNK_EXCLUDES = [
    "__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", "*.egg-info/", ".coverage", "node_modules/", "dist/",
    "build/", ".DS_Store", "*.swp", ".venv/", "venv/",
    # deepagents' summarization middleware offloads evicted history here (inside
    # the backend root = the repo clone); never let it leak into a commit/PR
    "conversation_history/",
]


def _write_junk_excludes(repo_dir: Path) -> None:
    exclude = repo_dir / ".git" / "info" / "exclude"
    if exclude.parent.exists():
        with exclude.open("a") as f:
            f.write("\n# devagent: never commit build junk\n")
            f.write("\n".join(JUNK_EXCLUDES) + "\n")


def workspace_root_for(settings: Settings, repo_name: str, task_id: str) -> Path:
    return settings.workspaces_dir / repo_slug(repo_name) / task_id


def legacy_workspace_root_for(settings: Settings, repo_name: str, task_id: str) -> Path:
    return settings.workspaces_dir / legacy_repo_slug(repo_name) / task_id


async def prepare_workspace(
    task_id: str, title: str, repo: RepoConfig, settings: Settings,
    existing_branch: str | None = None, *, reuse_existing: bool = False,
) -> Workspace:
    """Clone the repo into a fresh per-task workspace.

    Normal tasks get a new work branch; revision tasks pass `existing_branch`
    (a previously pushed agent branch) and continue on it, so pushes update
    the same PR.

    Layout is workspaces/<repo_slug>/<task_id>/repo: grouping by repo lets the
    repo's (reusable) sandbox container bind-mount one stable parent directory
    that covers every task workspace for that repo — and only that repo. The
    repo slug is collision-resistant, so distinct names get distinct parents."""
    ws_root = workspace_root_for(settings, repo.name, task_id)
    repo_dir = ws_root / "repo"
    control_dir = control_git_dir_for(settings, repo.name, task_id)
    if reuse_existing and (repo_dir / ".git").is_dir() and control_dir.exists():
        # resume after a restart: reuse the existing clone (preserving the agent's
        # uncommitted edits) and the trusted control-git copy made on the first run
        branch = existing_branch or branch_name_for(task_id, title)
        return Workspace(task_id=task_id, repo=repo, path=repo_dir, branch=branch,
                         control_git_dir=control_dir)
    if ws_root.exists():
        shutil.rmtree(ws_root)
    ws_root.mkdir(parents=True)

    # the remote URL stays token-free; auth rides in process-scoped env config
    env = _git_env(auth_env(
        repo.url, settings.github_token, trusted_github_hosts(settings)))
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
            safe_env = _git_env()
            out = await git(["checkout", existing_branch], cwd=repo_dir, check=False,
                            env=safe_env)
            head = await git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir,
                             env=safe_env)
            if head.strip() != existing_branch:
                raise GitError(
                    f"branch '{existing_branch}' not found on {repo.url} — cannot "
                    f"revise; the original work may never have been pushed")

    if existing_branch:
        branch = existing_branch
        # the revision clone is single-branch (--depth implies --single-branch), so
        # it lacks origin/<default_branch> — which commits_ahead()/diff_stat()
        # compare against. Fetch that ref so finalize and the approval gate see the
        # revision's commits instead of silently reading 0 ahead and dropping work.
        if repo.default_branch != existing_branch:
            await git(["fetch", "--depth", "50", "origin",
                       f"{repo.default_branch}:refs/remotes/origin/{repo.default_branch}"],
                      cwd=repo_dir, check=False, env=env)
    else:
        branch = branch_name_for(task_id, title)
        await git(["checkout", "-b", branch], cwd=repo_dir, env=_git_env())
    await git(["config", "user.name", "sde-deepagent"], cwd=repo_dir, env=_git_env())
    await git(["config", "user.email", "sde-deepagent@localhost"], cwd=repo_dir,
              env=_git_env())
    _write_junk_excludes(repo_dir)

    # Controller Git operations use a protected copy of the freshly-created
    # metadata. The sandbox can mutate repo/.git freely, but the host never
    # executes or trusts it after this point.
    if control_dir.exists():
        shutil.rmtree(control_dir)
    control_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo_dir / ".git", control_dir)
    ws = Workspace(task_id=task_id, repo=repo, path=repo_dir, branch=branch,
                   control_git_dir=control_dir)
    await control_git(ws, ["config", "user.name", "sde-deepagent"])
    await control_git(ws, ["config", "user.email", "sde-deepagent@localhost"])
    await control_git(ws, ["remote", "set-url", "origin", repo.url])
    return ws


async def has_changes(ws: Workspace) -> bool:
    out = await control_git(ws, ["status", "--porcelain"])
    return bool(out.strip())


async def commits_ahead(ws: Workspace) -> int:
    try:
        out = await control_git(
            ws, ["rev-list", "--count", f"origin/{ws.repo.default_branch}..HEAD"])
        return int(out.strip())
    except (GitError, ValueError):
        return 0


async def commit_all(ws: Workspace, message: str) -> str:
    await control_git(ws, ["add", "-A"])
    out = await control_git(ws, ["commit", "-m", message], check=False)
    return out


async def push_branch(ws: Workspace, settings: Settings) -> None:
    env = _git_env(auth_env(
        ws.repo.url, settings.github_token, trusted_github_hosts(settings)))
    expected = (await control_git(
        ws, ["for-each-ref", "--format=%(objectname)",
             f"refs/remotes/origin/{ws.branch}"],
    )).strip()
    lease = f"--force-with-lease=refs/heads/{ws.branch}:{expected}"
    proc = await asyncio.create_subprocess_exec(
        "git", f"--git-dir={ws.control_git_dir}", f"--work-tree={ws.path}",
        "-c", f"core.hooksPath={os.devnull}",
        "push", lease, ws.repo.url, f"HEAD:refs/heads/{ws.branch}",
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
    # A URL push does not reliably update origin/* tracking refs. Record the
    # successfully pushed commit so retries retain force-with-lease protection.
    await control_git(
        ws, ["update-ref", f"refs/remotes/origin/{ws.branch}", "HEAD"])


async def diff_stat(ws: Workspace) -> str:
    return await control_git(
        ws, ["diff", "--stat", f"origin/{ws.repo.default_branch}...HEAD"], check=False)


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
    if host.lower() not in trusted_github_hosts(settings):
        raise GitError(
            f"refusing to send GITHUB_TOKEN to untrusted host '{host}'; "
            "configure GITHUB_API_URL for the GitHub Enterprise host"
        )
    api = github_api_for_host(settings, host)

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
    # layout: <root>/<repo_slug>/<task_id>. Task dirs are pruned; the repo-level
    # dirs stay even when empty — a repo's sandbox container bind-mounts its
    # dir, and deleting/recreating a mount source leaves the container watching
    # a dead inode. Top-level dirs from the old flat layout (they contain
    # repo/.git directly) are legacy task workspaces and removed wholesale.
    task_dirs: list[Path] = []
    for top in (d for d in root.iterdir() if d.is_dir()):
        if (top / "repo" / ".git").exists():  # pre-per-repo-layout workspace
            task_dirs.append(top)
        else:
            task_dirs.extend(d for d in top.iterdir() if d.is_dir())
    dirs = sorted(task_dirs, key=lambda d: d.stat().st_mtime, reverse=True)
    deleted = []
    for d in dirs[max(keep, 0):]:
        if protect and d.name in protect:
            continue
        shutil.rmtree(d, ignore_errors=True)
        for control_dir in settings.control_git_dir.glob(f"*/{d.name}.git"):
            shutil.rmtree(control_dir, ignore_errors=True)
        deleted.append(d.name)
    return deleted
