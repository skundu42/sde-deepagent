"""Context-block assembly: path-traversal containment and the total-size cap."""

from sde_deepagent.config import RepoConfig
from sde_deepagent.context import (
    CONTEXT_MOUNT,
    MAX_TOTAL_CHARS,
    build_context_block,
    mount_company_context,
)
from sde_deepagent.settings import Settings


def test_mount_company_context_skips_dotfiles(tmp_path):
    # the listing returned to the prompt and the files actually copied must MATCH —
    # a dotfile (e.g. a real .env) must never be silently materialized into the
    # agent's workspace while being hidden from the listing
    ctx = tmp_path / "company"
    ctx.mkdir()
    (ctx / "guide.md").write_text("hello")
    (ctx / ".env").write_text("SECRET=leak")
    (ctx / ".hidden").mkdir()
    (ctx / ".hidden" / "creds.txt").write_text("AWS=leak")
    repo = tmp_path / "repo"
    repo.mkdir()

    listed = mount_company_context(repo, Settings(context_dir=ctx))

    assert listed == ["guide.md"]
    dest = repo / CONTEXT_MOUNT
    assert (dest / "guide.md").exists()
    assert not (dest / ".env").exists()        # secret NOT materialized
    assert not (dest / ".hidden").exists()      # nor a hidden dir's contents


def _settings(tmp_path):
    # point context_dir at a non-existent dir so the company-docs mount is a no-op
    return Settings(context_dir=tmp_path / "no_company_ctx")


def _repo(**kw) -> RepoConfig:
    base = dict(name="backend", url="git@github.com:acme/backend.git")
    base.update(kw)
    return RepoConfig(**base)


def test_context_block_includes_safe_docs(tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "docs").mkdir(parents=True)
    (repo_dir / "docs" / "arch.md").write_text("ARCHITECTURE NOTES")
    block = build_context_block(repo_dir, _repo(context=["docs/*.md"]),
                                _settings(tmp_path))
    assert "ARCHITECTURE NOTES" in block


def test_context_block_refuses_traversal_pattern(tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "docs").mkdir(parents=True)
    (repo_dir / "docs" / "arch.md").write_text("ARCHITECTURE NOTES")
    (tmp_path / "secret.env").write_text("API_KEY=supersecret123")

    block = build_context_block(
        repo_dir, _repo(context=["docs/*.md", "../secret.env"]), _settings(tmp_path))
    assert "ARCHITECTURE NOTES" in block       # safe pattern still works
    assert "supersecret123" not in block       # traversal pattern is skipped


def test_context_block_refuses_symlink_escape(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (tmp_path / "secret.env").write_text("API_KEY=supersecret123")
    # a symlink inside the repo whose name matches the glob but resolves outside
    (repo_dir / "leak.md").symlink_to(tmp_path / "secret.env")

    block = build_context_block(repo_dir, _repo(context=["*.md"]), _settings(tmp_path))
    assert "supersecret123" not in block


def test_context_block_respects_total_char_cap(tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "docs").mkdir(parents=True)
    for i in range(12):  # ~72k of content, well over the cap
        (repo_dir / "docs" / f"big{i}.md").write_text("x" * 6000)
    block = build_context_block(repo_dir, _repo(context=["docs/*.md"]),
                                _settings(tmp_path))
    assert len(block) <= MAX_TOTAL_CHARS
