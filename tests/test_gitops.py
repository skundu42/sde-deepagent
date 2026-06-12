import base64
from pathlib import Path

import pytest

from sde_deepagent.config import RepoConfig
from sde_deepagent.gitops import (
    auth_env, branch_name_for, commit_all, commits_ahead, has_changes,
    parse_remote, prepare_workspace, prune_workspaces, run_cmd,
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
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    expected = base64.b64encode(b"x-access-token:tok123").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    # crucially: the token never appears in any URL or persisted config
    assert "tok123" not in env["GIT_CONFIG_VALUE_0"]


def test_auth_env_skipped_when_not_applicable():
    assert auth_env("git@github.com:a/b.git", "tok") is None  # ssh has its own auth
    assert auth_env("/local/repo", "tok") is None
    assert auth_env("https://github.com/a/b.git", None) is None


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
    import os, time
    for i in range(5):
        d = root / f"task{i}"
        d.mkdir(parents=True)
        ts = time.time() - (5 - i) * 60  # task0 oldest ... task4 newest
        os.utime(d, (ts, ts))
    deleted = prune_workspaces(settings, keep=2)
    assert sorted(deleted) == ["task0", "task1", "task2"]
    assert {d.name for d in root.iterdir()} == {"task3", "task4"}
    assert prune_workspaces(settings, keep=2) == []  # idempotent


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
