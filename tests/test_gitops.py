import base64
from pathlib import Path

import pytest

from sde_deepagent.config import RepoConfig
from sde_deepagent.gitops import (
    GitError,
    Workspace,
    _git_env,
    auth_env,
    branch_name_for,
    commit_all,
    commits_ahead,
    create_pull_request,
    has_changes,
    parse_remote,
    prepare_workspace,
    prune_workspaces,
    push_branch,
    run_cmd,
)
from sde_deepagent.settings import get_settings


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:acme/backend.git", ("github.com", "acme", "backend")),
    ("https://github.com/acme/backend.git", ("github.com", "acme", "backend")),
    ("https://github.com/acme/backend", ("github.com", "acme", "backend")),
    ("https://ghe.corp.io/team/repo.git", ("ghe.corp.io", "team", "repo")),
    ("git@gitlab.com:grp/proj.git", ("gitlab.com", "grp", "proj")),
    ("/local/path/repo", None),
])
def test_parse_remote(url, expected):
    assert parse_remote(url) == expected


def test_auth_env_https_token():
    env = auth_env("https://github.com/a/b.git", "tok123")
    assert env is not None
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/a/b.git.extraHeader"
    expected = base64.b64encode(b"x-access-token:tok123").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert env["GIT_CONFIG_VALUE_1"] == "false"  # redirects cannot carry credentials
    # crucially: the token never appears in any URL or persisted config
    assert "tok123" not in env["GIT_CONFIG_VALUE_0"]


def test_auth_env_skipped_when_not_applicable():
    assert auth_env("git@github.com:a/b.git", "tok") is None  # ssh has its own auth
    assert auth_env("/local/repo", "tok") is None
    assert auth_env("https://github.com/a/b.git", None) is None
    assert auth_env("https://attacker.example/a/b.git", "tok") is None
    assert auth_env("http://github.com/a/b.git", "tok") is None


def test_auth_env_allows_explicit_enterprise_host():
    assert auth_env(
        "https://ghe.corp.example/a/b.git", "tok", {"github.com", "ghe.corp.example"}
    ) is not None


async def test_clone_persists_no_token(temp_env, tmp_path, monkeypatch):
    """Even with GITHUB_TOKEN set, nothing token-like lands in .git/config."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret123")
    import sde_deepagent.settings as settings_mod
    settings_mod._settings = None
    origin = tmp_path / "origin2"
    await _make_origin(origin)
    settings = get_settings()
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    ws = await prepare_workspace("sec01", "check", repo, settings)
    git_config = (ws.path / ".git" / "config").read_text()
    assert "ghp_supersecret123" not in git_config
    assert "x-access-token" not in git_config


def test_prune_workspaces(temp_env):
    settings = get_settings()
    root = settings.workspaces_dir
    import os
    import time
    for i in range(5):
        d = root / "demo" / f"task{i}"  # layout: <root>/<repo_slug>/<task_id>
        d.mkdir(parents=True)
        ts = time.time() - (5 - i) * 60  # task0 oldest ... task4 newest
        os.utime(d, (ts, ts))
    deleted = prune_workspaces(settings, keep=2)
    assert sorted(deleted) == ["task0", "task1", "task2"]
    assert {d.name for d in (root / "demo").iterdir()} == {"task3", "task4"}
    # the repo-level dir survives even if emptied: the repo's sandbox
    # container bind-mounts it
    assert (root / "demo").exists()
    assert prune_workspaces(settings, keep=2) == []  # idempotent


def test_prune_workspaces_protect_and_legacy(temp_env):
    settings = get_settings()
    root = settings.workspaces_dir
    import os
    import time
    now = time.time()
    # old flat-layout workspace (pre per-repo dirs): <root>/<task>/repo/.git
    legacy = root / "oldtask" / "repo" / ".git"
    legacy.mkdir(parents=True)
    os.utime(root / "oldtask", (now - 7200, now - 7200))
    for i, name in enumerate(["keepme", "newest"]):
        d = root / "demo" / name
        d.mkdir(parents=True)
        os.utime(d, (now - (2 - i) * 60, now - (2 - i) * 60))
    deleted = prune_workspaces(settings, keep=1, protect={"keepme"})
    assert sorted(deleted) == ["oldtask"]  # legacy removed wholesale
    assert (root / "demo" / "keepme").exists()  # protected despite being old
    assert (root / "demo" / "newest").exists()


def test_branch_name():
    b = branch_name_for("abc123", "Fix the Login Bug!! (urgent)")
    assert b.startswith("agent/abc123-fix-the-login-bug")
    assert " " not in b and "!" not in b


async def _make_origin(path: Path) -> None:
    path.mkdir(parents=True)
    for args in (["git", "init", "-b", "main"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        code, out = await run_cmd(args, cwd=path)
        assert code == 0, out
    (path / "README.md").write_text("# origin\n")
    await run_cmd(["git", "add", "-A"], cwd=path)
    code, out = await run_cmd(["git", "commit", "-m", "init"], cwd=path)
    assert code == 0, out


async def test_workspace_clone_and_commit(temp_env, tmp_path):
    origin = tmp_path / "origin"
    await _make_origin(origin)

    settings = get_settings()
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    ws = await prepare_workspace("task01", "Add feature", repo, settings)

    assert ws.path.exists() and (ws.path / "README.md").exists()
    assert ws.branch.startswith("agent/task01-add-feature")
    assert not await has_changes(ws)
    assert await commits_ahead(ws) == 0

    (ws.path / "new.txt").write_text("hello")
    assert await has_changes(ws)
    await commit_all(ws, "add new.txt")
    assert not await has_changes(ws)
    assert await commits_ahead(ws) == 1


async def test_controller_git_ignores_workspace_hooks_and_secrets(
    temp_env, tmp_path, monkeypatch
):
    origin = tmp_path / "origin-hook"
    await _make_origin(origin)
    ws = await prepare_workspace(
        "hook01", "Hook escape", RepoConfig("demo", str(origin)), get_settings())
    assert not ws.control_git_dir.is_relative_to(ws.path.parent.parent)

    marker = tmp_path / "escaped"
    hook = ws.path / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf \"$OPENAI_API_KEY\" > '{marker}'\n")
    hook.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-controller-git")
    (ws.path / "change.txt").write_text("safe\n")

    await commit_all(ws, "safe commit")
    assert not marker.exists()


async def test_push_uses_registered_url_not_workspace_origin(temp_env, tmp_path):
    origin = tmp_path / "trusted-origin"
    attacker = tmp_path / "attacker-origin"
    await _make_origin(origin)
    await _make_origin(attacker)
    settings = get_settings()
    ws = await prepare_workspace(
        "push01", "Safe push", RepoConfig("demo", str(origin)), settings)
    (ws.path / "change.txt").write_text("safe\n")
    await commit_all(ws, "safe commit")

    # Untrusted workspace metadata attempts to redirect the credentialed push.
    await run_cmd(["git", "remote", "set-url", "origin", str(attacker)], cwd=ws.path)
    await push_branch(ws, settings)
    await push_branch(ws, settings)  # retry remains safe after tracking-ref update

    trusted_branches = await run_cmd(["git", "branch", "-a"], cwd=origin)
    attacker_branches = await run_cmd(["git", "branch", "-a"], cwd=attacker)
    assert ws.branch in trusted_branches[1]
    assert ws.branch not in attacker_branches[1]


async def test_pr_api_rejects_untrusted_host(temp_env, tmp_path):
    settings = get_settings()
    settings.github_token = "ghp_secret"
    ws = Workspace(
        "pr01", RepoConfig("demo", "https://attacker.example/acme/repo.git"),
        tmp_path, "agent/pr01", tmp_path / "control.git",
    )
    with pytest.raises(GitError, match="untrusted host"):
        await create_pull_request(ws, settings, "title", "body")


async def test_pr_api_rejects_plain_http(temp_env, tmp_path):
    settings = get_settings()
    settings.github_token = "ghp_secret"
    settings.github_api_url = "http://api.github.com"
    ws = Workspace(
        "pr02", RepoConfig("demo", "https://github.com/acme/repo.git"),
        tmp_path, "agent/pr02", tmp_path / "control.git",
    )
    with pytest.raises(GitError, match="expected HTTPS"):
        await create_pull_request(ws, settings, "title", "body")


def test_controller_git_env_excludes_provider_secrets(temp_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    env = _git_env()
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
