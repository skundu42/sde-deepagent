"""RepoReader: shallow read-only clones backing the chat's code-reading tools."""

from pathlib import Path

import pytest

from sde_deepagent.gitops import GitError, run_cmd
from sde_deepagent.repo_reader import RepoReader
from sde_deepagent.settings import get_settings


async def _make_origin(path: Path) -> None:
    path.mkdir(parents=True)
    for args in (["git", "init", "-b", "main"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        code, out = await run_cmd(args, cwd=path)
        assert code == 0, out
    (path / "README.md").write_text("# demo\n")
    (path / "src").mkdir()
    (path / "src" / "hub.sol").write_text(
        "// SPDX-License-Identifier: MIT\ncontract Hub {\n  function register() public {}\n}\n")
    await run_cmd(["git", "add", "-A"], cwd=path)
    code, out = await run_cmd(["git", "commit", "-m", "init"], cwd=path)
    assert code == 0, out


async def test_list_read_grep(temp_env, tmp_path):
    origin = tmp_path / "origin"
    await _make_origin(origin)
    reader = RepoReader(get_settings())
    url = str(origin)

    files = await reader.list_files(url)
    assert "README.md" in files and "src/hub.sol" in files

    assert await reader.list_files(url, subdir="src") == ["src/hub.sol"]

    text = await reader.read_file(url, "src/hub.sol")
    assert "contract Hub" in text and "register()" in text

    hits = await reader.grep(url, "function register")
    assert any("src/hub.sol" in h for h in hits)
    assert await reader.grep(url, "nonexistent_symbol_xyz") == []


async def test_clone_is_cached(temp_env, tmp_path):
    origin = tmp_path / "origin"
    await _make_origin(origin)
    reader = RepoReader(get_settings())
    first = await reader.ensure_clone(str(origin))
    again = await reader.ensure_clone(str(origin))  # reuses on-disk clone, no re-clone
    assert first == again and (first / ".git").is_dir()
    assert get_settings().ref_clones_dir in first.parents


async def test_read_rejects_path_traversal(temp_env, tmp_path):
    origin = tmp_path / "origin"
    await _make_origin(origin)
    reader = RepoReader(get_settings())
    with pytest.raises(GitError, match="escapes repo"):
        await reader.read_file(str(origin), "../../etc/passwd")


async def test_read_truncates_large_file(temp_env, tmp_path):
    origin = tmp_path / "origin"
    await _make_origin(origin)
    big = origin / "big.txt"
    big.write_text("x" * 5000)
    await run_cmd(["git", "add", "-A"], cwd=origin)
    await run_cmd(["git", "commit", "-m", "big"], cwd=origin)
    reader = RepoReader(get_settings())
    text = await reader.read_file(str(origin), "big.txt", max_bytes=1000)
    assert "truncated at 1000 bytes" in text and len(text) < 5000


async def test_clone_failure_raises(temp_env, tmp_path):
    reader = RepoReader(get_settings())
    with pytest.raises(GitError, match="clone of .* failed"):
        await reader.ensure_clone(str(tmp_path / "does-not-exist"))


# ---- TTL refresh: reads must not serve permanently stale code ----

def _age_fetch_stamp(reader: RepoReader, clone: Path, seconds: float) -> None:
    """Backdate the clone's last-fetch stamp so the TTL appears expired."""
    import os
    import time
    stamp = clone / ".git" / "sde-fetch-stamp"
    old = time.time() - seconds
    os.utime(stamp, (old, old))


async def _commit_change(origin: Path, path: str, content: str) -> None:
    (origin / path).write_text(content)
    await run_cmd(["git", "add", "-A"], cwd=origin)
    code, out = await run_cmd(["git", "commit", "-m", "update"], cwd=origin)
    assert code == 0, out


async def test_stale_clone_refreshes_after_ttl(temp_env, tmp_path):
    origin = tmp_path / "origin"
    await _make_origin(origin)
    reader = RepoReader(get_settings())
    clone = await reader.ensure_clone(str(origin))
    await _commit_change(origin, "README.md", "# demo v2\n")

    _age_fetch_stamp(reader, clone, seconds=3600)  # past the 15-min default TTL
    text = await reader.read_file(str(origin), "README.md")
    assert "demo v2" in text  # served the CURRENT source, not the first-clone snapshot


async def test_fresh_clone_not_refetched_within_ttl(temp_env, tmp_path):
    origin = tmp_path / "origin"
    await _make_origin(origin)
    reader = RepoReader(get_settings())
    await reader.ensure_clone(str(origin))
    await _commit_change(origin, "README.md", "# demo v2\n")

    # stamp is seconds old — well inside the TTL, so no fetch happens
    text = await reader.read_file(str(origin), "README.md")
    assert "demo v2" not in text


async def test_refresh_failure_serves_stale_clone(temp_env, tmp_path):
    import shutil

    origin = tmp_path / "origin"
    await _make_origin(origin)
    reader = RepoReader(get_settings())
    clone = await reader.ensure_clone(str(origin))
    shutil.rmtree(origin)  # remote gone: fetch will fail

    _age_fetch_stamp(reader, clone, seconds=3600)
    text = await reader.read_file(str(origin), "README.md")  # must not raise
    assert "# demo" in text  # stale content beats no content


async def test_ensure_clone_touches_use_stamp(temp_env, tmp_path):
    import os
    import time

    origin = tmp_path / "origin"
    await _make_origin(origin)
    reader = RepoReader(get_settings())
    clone = await reader.ensure_clone(str(origin))
    stamp = clone / ".git" / "sde-use-stamp"
    assert stamp.exists()

    old = time.time() - 86400
    os.utime(stamp, (old, old))
    await reader.ensure_clone(str(origin))
    assert time.time() - stamp.stat().st_mtime < 60  # re-stamped on every use
