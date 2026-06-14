from types import SimpleNamespace

from sde_deepagent.db import Task
from sde_deepagent.intake.base import parse_ask, parse_task_text, task_summary


def test_parse_ask_detects_command_and_extracts_question():
    assert parse_ask("/ask what did task abc do?") == "what did task abc do?"
    assert parse_ask("/ask@mybot how is the queue?") == "how is the queue?"  # group chat
    assert parse_ask("/ask") == ""               # command with no question yet
    assert parse_ask("/askfoo") is None          # not the /ask command
    assert parse_ask("fix the login bug") is None  # a normal task, not a question
    assert parse_ask("[repo] do x") is None
from sde_deepagent.intake.slack import SlackIntake
from sde_deepagent.intake.telegram import TelegramIntake, _strip_task_command
from sde_deepagent.settings import get_settings


def test_task_summary_awaiting_approval_is_not_reported_as_failed():
    # a task parked for human approval is not terminal; it must NOT be posted back
    # to the channel as "❌ failed: Error: unknown"
    t = SimpleNamespace(id="t1", title="add a flag", status="awaiting_approval",
                        pr_url=None, error=None)
    s = task_summary(t)
    assert "awaiting your approval" in s
    assert "failed" not in s.lower()


def test_strip_task_command_handles_botname_and_runons():
    assert _strip_task_command("/task fix the bug") == "fix the bug"
    assert _strip_task_command("/task@mybot fix the bug") == "fix the bug"  # group chat
    assert _strip_task_command("/task") == ""
    assert _strip_task_command("/taskfoo") == "/taskfoo"        # a different command
    assert _strip_task_command("[repo] do x") == "[repo] do x"  # no command at all


class _FakeChat:
    def __init__(self):
        self.calls = []

    async def ask(self, message, session_id=None):
        self.calls.append((message, session_id))
        return {"reply": "the answer", "session_id": session_id}


async def test_telegram_ask_routes_to_chat_not_task(temp_env):
    import httpx

    from sde_deepagent.db import Database
    from sde_deepagent.intake.telegram import TelegramIntake

    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    chat = _FakeChat()
    intake = TelegramIntake(settings, db, chat=chat)
    intake.allow_all = True  # permit the test chat

    sent: list[bytes] = []

    def handler(req):
        sent.append(req.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    update = {"update_id": 1, "message": {"message_id": 5, "chat": {"id": 42},
                                          "text": "/ask what did task abc do?"}}
    try:
        await intake._handle_update(client, update)
        assert chat.calls == [("what did task abc do?", "telegram:42")]  # routed to chat
        assert await db.list_tasks() == []                              # no task created
        assert any(b"the answer" in c for c in sent)                    # answer posted back
    finally:
        await client.aclose()
        await db.close()


async def test_slack_handle_ask_posts_reply_and_degrades(temp_env):
    from sde_deepagent.intake.slack import SlackIntake

    settings = get_settings()
    posted: list[dict] = []

    class FakeWeb:
        async def chat_postMessage(self, **kw):
            posted.append(kw)

    intake = SlackIntake(settings, None, chat=_FakeChat())
    intake._web = FakeWeb()
    await intake._handle_ask("C1", "T1", "how many tasks ran?")
    assert posted[-1]["text"] == "the answer"
    assert posted[-1]["channel"] == "C1" and posted[-1]["thread_ts"] == "T1"

    # no chat configured -> graceful message, never raises
    intake2 = SlackIntake(settings, None, chat=None)
    intake2._web = FakeWeb()
    await intake2._handle_ask("C1", "T1", "q")
    assert "isn't available" in posted[-1]["text"]


async def test_linear_ingest_dedups_same_issue(temp_env):
    # re-polling the same labelled issue (or a poll + webhook on the same issue)
    # must create exactly one task — survives restarts, no 500-row scan limit
    import httpx

    from sde_deepagent.db import Database
    from sde_deepagent.intake.linear import LinearIntake

    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    intake = LinearIntake(settings, db)
    issue = {"id": "iss_1", "identifier": "ABC-1", "title": "do it",
             "description": "x", "url": "https://linear.app/x/issue/ABC-1"}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    try:
        await intake.ingest_issue(client, issue)
        await intake.ingest_issue(client, issue)  # duplicate pickup
        linear_tasks = [t for t in await db.list_tasks() if t.source == "linear"]
        assert len(linear_tasks) == 1
    finally:
        await client.aclose()
        await db.close()


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
