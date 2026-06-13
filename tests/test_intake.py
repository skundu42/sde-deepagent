from sde_deepagent.db import Task
from sde_deepagent.intake.base import parse_task_text, task_summary
from sde_deepagent.intake.slack import SlackIntake
from sde_deepagent.intake.telegram import TelegramIntake
from sde_deepagent.settings import get_settings


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


def test_external_chat_intakes_deny_by_default(temp_env):
    settings = get_settings()
    telegram = TelegramIntake(settings, None)
    slack = SlackIntake(settings, None)
    assert telegram._allowed_chat(12345) is False
    assert slack._allowed_user("U123") is False


def test_external_chat_intakes_require_explicit_allowlist(temp_env):
    settings = get_settings()
    settings.telegram_allowed_chats = "12345"
    settings.slack_allowed_users = "U123"
    telegram = TelegramIntake(settings, None)
    slack = SlackIntake(settings, None)
    assert telegram._allowed_chat(12345) is True
    assert telegram._allowed_chat(99999) is False
    assert slack._allowed_user("U123") is True
    assert slack._allowed_user("U999") is False


def test_external_chat_intakes_allow_all_only_with_wildcard(temp_env):
    settings = get_settings()
    settings.telegram_allowed_chats = "*"
    settings.slack_allowed_users = "*"
    assert TelegramIntake(settings, None)._allowed_chat(99999) is True
    assert SlackIntake(settings, None)._allowed_user("U999") is True
