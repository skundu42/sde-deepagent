from devagent.db import Task
from devagent.intake.base import parse_task_text, task_summary


def test_parse_plain():
    repo, title, desc = parse_task_text("Fix the login bug")
    assert repo is None
    assert title == "Fix the login bug"
    assert desc == "Fix the login bug"


def test_parse_repo_bracket():
    repo, title, desc = parse_task_text("[backend] Fix the login bug\nIt 500s on empty email")
    assert repo == "backend"
    assert title == "Fix the login bug"
    assert "500s" in desc


def test_parse_repo_kv():
    repo, title, _ = parse_task_text("repo=web-app: add dark mode toggle")
    assert repo == "web-app"
    assert title == "add dark mode toggle"


def _task(**kw) -> Task:
    base = dict(id="abc123", title="Fix bug", description="d", repo="r",
                source="telegram", source_ref={}, status="completed")
    base.update(kw)
    return Task(**base)


def test_summaries():
    assert "PR: https://x/pr/1" in task_summary(_task(pr_url="https://x/pr/1"))
    assert "without a PR" in task_summary(_task(pr_url=None))
    assert "failed" in task_summary(_task(status="failed", error="boom"))
    assert "cancelled" in task_summary(_task(status="cancelled"))
