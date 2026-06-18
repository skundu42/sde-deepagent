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


async def test_linear_assignee_mode_resolves_viewer_and_filters(temp_env, monkeypatch):
    # in assignee mode the poll resolves the key's own user id once (cached) and
    # queries for issues assigned to it, not by label
    import json

    import httpx

    from sde_deepagent.intake.linear import ASSIGNED_QUERY, LinearIntake

    monkeypatch.setenv("LINEAR_TRIGGER", "assignee")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_test")
    settings = get_settings()
    intake = LinearIntake(settings, db=None)
    assert intake._by_assignee is True

    viewer_calls = []

    def handler(req):
        q = json.loads(req.content)["query"]
        if "viewer" in q:
            viewer_calls.append(q)
            return httpx.Response(200, json={"data": {"viewer": {"id": "usr_agent",
                                                                 "name": "Agent"}}})
        return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        query, variables = await intake._poll_query(client)
        assert query == ASSIGNED_QUERY
        assert variables == {"uid": "usr_agent"}
        await intake._poll_query(client)              # second poll reuses the id
        assert len(viewer_calls) == 1                 # viewer resolved only once
    finally:
        await client.aclose()


def test_linear_webhook_assignee_matches_only_agents_issues(temp_env, monkeypatch):
    from sde_deepagent.intake.linear import LinearIntake

    monkeypatch.setenv("LINEAR_TRIGGER", "assignee")
    settings = get_settings()
    intake = LinearIntake(settings, db=None)
    intake._my_user_id = "usr_agent"  # pretend the viewer id is already resolved

    mine = {"type": "Issue", "data": {"id": "iss_9", "identifier": "ABC-9",
            "title": "do it", "url": "u", "assigneeId": "usr_agent"}}
    theirs = {"type": "Issue", "data": {"id": "iss_10", "identifier": "ABC-10",
              "title": "not mine", "url": "u", "assigneeId": "usr_other"}}
    assert intake._webhook_issue(mine)["identifier"] == "ABC-9"
    assert intake._webhook_issue(theirs) is None      # assigned to someone else


def test_linear_webhook_label_mode_unchanged(temp_env, monkeypatch):
    # default (label) mode keeps its original behavior
    from sde_deepagent.intake.linear import LinearIntake

    monkeypatch.setenv("LINEAR_LABEL", "agent")
    settings = get_settings()
    intake = LinearIntake(settings, db=None)
    assert intake._by_assignee is False

    labelled = {"type": "Issue", "data": {"id": "iss_1", "identifier": "ABC-1",
                "title": "t", "url": "u", "labels": [{"name": "agent"}]}}
    other = {"type": "Issue", "data": {"id": "iss_2", "identifier": "ABC-2",
             "title": "t", "url": "u", "labels": [{"name": "bug"}]}}
    assert intake._webhook_issue(labelled)["identifier"] == "ABC-1"
    assert intake._webhook_issue(other) is None       # missing the agent label


def test_linear_write_headers_prefers_oauth_token(temp_env):
    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    intake = LinearIntake(Settings(linear_oauth_token="oauth_tok",
                                   linear_api_key="lin_key"), db=None)
    assert intake._write_headers()["Authorization"] == "Bearer oauth_tok"  # app token wins


def test_linear_write_headers_falls_back_to_api_key(temp_env):
    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    intake = LinearIntake(Settings(linear_api_key="lin_key"), db=None)
    assert intake._write_headers()["Authorization"] == "lin_key"  # personal key, no Bearer


def test_linear_write_headers_none_without_credential(temp_env):
    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    intake = LinearIntake(Settings(linear_webhook_secret="whsec"), db=None)
    assert intake._write_headers() is None  # webhook-only, no write-back credential


def test_linear_polling_disabled_without_api_key(temp_env):
    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    webhook_only = LinearIntake(Settings(linear_webhook_secret="whsec",
                                         linear_oauth_token="oauth_tok"), db=None)
    assert webhook_only._polling is False
    with_key = LinearIntake(Settings(linear_api_key="lin_key"), db=None)
    assert with_key._polling is True


async def test_linear_comment_posts_with_app_token(temp_env):
    import json

    import httpx

    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    intake = LinearIntake(Settings(linear_oauth_token="oauth_tok"), db=None)
    captured: dict = {}

    def handler(req):
        captured["auth"] = req.headers.get("authorization")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": {"commentCreate": {"success": True}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await intake._comment(client, "iss_1", "hello")
    finally:
        await client.aclose()
    assert captured["auth"] == "Bearer oauth_tok"          # posted as the app
    assert captured["body"]["variables"]["issueId"] == "iss_1"


async def test_linear_comment_skipped_without_credential(temp_env):
    import httpx

    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    intake = LinearIntake(Settings(linear_webhook_secret="whsec"), db=None)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await intake._comment(client, "iss_1", "hello")  # no credential → no HTTP call
    finally:
        await client.aclose()
    assert calls["n"] == 0


def test_linear_agent_session_parse(temp_env):
    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    intake = LinearIntake(Settings(linear_oauth_token="tok"), db=None)
    created = {"type": "AgentSessionEvent", "action": "created",
              "agentSession": {"id": "sess_1", "issue": {
                  "id": "iss_1", "identifier": "ABC-1", "title": "do it",
                  "description": "d", "url": "https://linear.app/x/ABC-1"}}}
    sid, issue = intake._agent_session_issue(created)
    assert sid == "sess_1" and issue["identifier"] == "ABC-1" and issue["id"] == "iss_1"
    # only the 'created' action starts a task; follow-up prompts are ignored here
    assert intake._agent_session_issue({**created, "action": "prompted"}) is None
    # a session without an issue (e.g. a bare mention) is skipped
    assert intake._agent_session_issue(
        {"type": "AgentSessionEvent", "action": "created", "agentSession": {"id": "s"}}) is None


async def test_linear_agent_activity_posts_with_app_token(temp_env):
    import json

    import httpx

    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    intake = LinearIntake(Settings(linear_oauth_token="tok"), db=None)
    captured: dict = {}

    def handler(req):
        captured["auth"] = req.headers.get("authorization")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await intake._agent_activity(client, "sess_1", "response", "PR is ready")
    finally:
        await client.aclose()
    assert captured["auth"] == "Bearer tok"
    inp = captured["body"]["variables"]["input"]
    assert inp["agentSessionId"] == "sess_1"
    assert inp["content"] == {"type": "response", "body": "PR is ready"}


async def test_linear_agent_session_ingest_acks_and_dedups(temp_env):
    import json

    import httpx

    from sde_deepagent.db import Database
    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    settings = Settings(linear_oauth_token="tok")
    db = Database(settings.db_path)
    await db.connect()
    intake = LinearIntake(settings, db)
    acts: list[str] = []

    def handler(req):
        acts.append(json.loads(req.content)["variables"]["input"]["content"]["type"])
        return httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    payload = {"type": "AgentSessionEvent", "action": "created",
               "agentSession": {"id": "sess_1", "issue": {
                   "id": "iss_1", "identifier": "ABC-1", "title": "t", "url": "u"}}}
    try:
        await intake._handle_agent_session(client, payload)
        await intake._handle_agent_session(client, payload)  # same session → one task
        tasks = [t for t in await db.list_tasks() if t.source == "linear"]
    finally:
        await client.aclose()
        await db.close()
    assert len(tasks) == 1
    assert tasks[0].source_ref.get("agent_session_id") == "sess_1"
    assert acts == ["thought"]  # acked once; the dedup'd second event posts nothing


async def test_linear_notify_posts_response_activity_for_agent_session(temp_env):
    import json

    import httpx

    from sde_deepagent.db import Database
    from sde_deepagent.intake.linear import LinearIntake
    from sde_deepagent.settings import Settings
    settings = Settings(linear_oauth_token="tok")
    db = Database(settings.db_path)
    await db.connect()
    intake = LinearIntake(settings, db)
    t = await db.create_task_if_new(
        title="ABC-1: t", description="d", source="linear",
        source_ref={"issue_id": "iss_1", "agent_session_id": "sess_1"},
        dedup_key="linear:agent:sess_1")
    await db.update_task(t.id, status="completed", pr_url="https://github.com/x/y/pull/1")
    t = await db.get_task(t.id)
    captured: dict = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await intake.notify(t, client=client)
    finally:
        await client.aclose()
        await db.close()
    inp = captured["body"]["variables"]["input"]
    assert inp["agentSessionId"] == "sess_1"
    assert inp["content"]["type"] == "response"   # completed → response (error if failed)


async def test_linear_enabled_by_webhook_secret_without_api_key(temp_env, monkeypatch):
    import httpx

    from sde_deepagent.server import create_app
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("LINEAR_OAUTH_TOKEN", "oauth_tok")
    app = create_app()
    async with httpx.ASGITransport(app=app):
        async with app.router.lifespan_context(app):
            assert app.state.linear is not None  # Linear enabled with no personal key


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
