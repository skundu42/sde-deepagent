"""FastAPI application: REST API + SSE streams for the management UI,
webhook endpoints, and the static UI itself."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .agent_factory import validate_models
from .bus import EventBus
from .chat import ChatService
from .config import ConfigStore, RepoConfig
from .db import Database
from .intake.linear import LinearIntake
from .intake.slack import SlackIntake
from .intake.telegram import TelegramIntake
from .memory import GLOBAL_TAG, memory_from_settings, repo_tag
from .runner import TaskRunner
from .settings import get_settings
from .worker import Worker

logger = logging.getLogger(__name__)


# ---- request models ----

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    repo: str | None = None
    model: str | None = None
    budget_usd: float | None = Field(default=None, ge=0)
    parent_id: str | None = Field(default=None, max_length=32)  # revise this task


class ChatMessage(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=64)


class ResourceCreate(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)  # URL or raw text
    scope: str = "global"  # "global" or a registered repo name


class RepoCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[\w./-]+$")
    url: str = Field(min_length=1)
    default_branch: str = "main"
    description: str = ""
    setup: str | None = None
    test: str | None = None
    context: list[str] = []


# ---- app ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    bus = EventBus()
    cfg = ConfigStore(settings.config_dir)
    runner = TaskRunner(db, bus, cfg, settings)
    worker = Worker(db, runner, max_concurrent=settings.max_concurrent_tasks,
                    settings=settings)

    intakes: list[Any] = []
    if settings.telegram_bot_token:
        intakes.append(TelegramIntake(settings, db))
    if settings.slack_bot_token and settings.slack_app_token:
        intakes.append(SlackIntake(settings, db))
    linear: LinearIntake | None = None
    if settings.linear_api_key:
        linear = LinearIntake(settings, db)
        intakes.append(linear)

    for intake in intakes:
        intake.start()
        worker.add_notifier(intake.notify)
    worker.start()

    app.state.settings = settings
    app.state.db = db
    app.state.bus = bus
    app.state.cfg = cfg
    app.state.worker = worker
    app.state.chat = ChatService(db, cfg, settings)
    app.state.memory = memory_from_settings(settings)
    app.state.linear = linear
    app.state.intakes = intakes
    logger.info("sde-deepagent up: %d intake channel(s), max %d concurrent tasks",
                len(intakes), settings.max_concurrent_tasks)
    try:
        yield
    finally:
        await worker.stop()
        for intake in intakes:
            await intake.stop()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="sde-deepagent", version=__version__, lifespan=lifespan)

    # ---- health & stats ----

    @app.get("/api/health")
    async def health(request: Request):
        s = request.app.state.settings
        return {
            "ok": True,
            "version": __version__,
            "providers": {
                "anthropic": bool(s.anthropic_api_key),
                "google": bool(s.google_api_key),
                "openai": bool(s.openai_api_key),
            },
            "github": bool(s.github_token),
            "memory": bool(s.supermemory_base_url and s.supermemory_api_key),
            "firecrawl": bool(s.firecrawl_url),
            "require_approval": s.require_approval,
            "intakes": [type(i).__name__.replace("Intake", "").lower()
                        for i in request.app.state.intakes],
            "running": len(request.app.state.worker.running),
        }

    @app.get("/api/stats")
    async def stats(request: Request):
        from .worker import _utc_midnight_ts

        s = request.app.state.settings
        out = await request.app.state.db.stats()
        out["spend_today_usd"] = round(
            await request.app.state.db.spend_since(_utc_midnight_ts()), 4)
        out["spend_today_chat_usd"] = round(
            await request.app.state.db.chat_spend_since(_utc_midnight_ts()), 4)
        out["daily_budget_usd"] = s.daily_budget_usd
        out["task_budget_usd"] = s.task_budget_usd
        out["budget_paused"] = request.app.state.worker.budget_paused
        return out

    # ---- tasks ----

    @app.get("/api/tasks")
    async def list_tasks(request: Request, status: str | None = None, limit: int = 200):
        tasks = await request.app.state.db.list_tasks(status=status, limit=min(limit, 500))
        return [t.to_dict() for t in tasks]

    @app.post("/api/tasks", status_code=201)
    async def create_task(request: Request, body: TaskCreate):
        repos = request.app.state.cfg.repos()
        if body.repo and body.repo not in repos:
            raise HTTPException(400, f"unknown repo '{body.repo}'")
        repo = body.repo
        if body.parent_id:
            parent = await request.app.state.db.get_task(body.parent_id)
            if not parent:
                raise HTTPException(404, f"parent task '{body.parent_id}' not found")
            if not parent.branch:
                raise HTTPException(400, "parent task never produced a branch — "
                                         "nothing to revise")
            repo = repo or parent.repo
        task = await request.app.state.db.create_task(
            title=body.title, description=body.description or body.title,
            repo=repo, source="ui", model=body.model,
            budget_usd=body.budget_usd or None, parent_id=body.parent_id,
        )
        return task.to_dict()

    @app.get("/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str):
        task = await request.app.state.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        return task.to_dict()

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: str):
        db = request.app.state.db
        task = await db.get_task(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        if task.status in ("queued", "awaiting_approval"):
            await db.update_task(task_id, status="cancelled")
            return {"status": "cancelled"}
        if task.status == "running":
            if request.app.state.worker.cancel_task(task_id):
                return {"status": "cancelling"}
        raise HTTPException(409, f"task is {task.status}, cannot cancel")

    async def _ship_approved(request: Request, task) -> str | None:
        """Push the held branch and open the PR (degrading to branch-only like
        the normal finalize path). Returns the PR URL if one was opened."""
        from .gitops import GitError, Workspace, create_pull_request, push_branch

        s = request.app.state.settings
        repo = request.app.state.cfg.repos().get(task.repo)
        if not repo:
            raise HTTPException(409, f"repo '{task.repo}' is no longer registered")
        path = s.workspaces_dir / task.id / "repo"
        if not path.exists():
            raise HTTPException(409, "workspace no longer on disk — re-run the task")
        ws = Workspace(task_id=task.id, repo=repo, path=path, branch=task.branch)
        await push_branch(ws, s)
        events = await request.app.state.db.list_events(task.id)
        proposal = next((e["content"] for e in reversed(events)
                         if e["kind"] == "approval_request"), {})
        try:
            return await create_pull_request(
                ws, s, proposal.get("title") or task.title[:80],
                proposal.get("body") or task.description)
        except GitError:
            return None  # branch pushed; PR not possible (local remote / no token)

    @app.post("/api/tasks/{task_id}/approve")
    async def approve_task(request: Request, task_id: str):
        db = request.app.state.db
        task = await db.get_task(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        if task.status != "awaiting_approval":
            raise HTTPException(409, f"task is {task.status}, not awaiting approval")
        pr_url = await _ship_approved(request, task)
        import time as _time

        await db.update_task(task_id, status="completed", pr_url=pr_url,
                             finished_at=_time.time())
        task.status, task.pr_url = "completed", pr_url
        event = await db.add_event(task_id, "status",
                                   {"status": "completed", "pr_url": pr_url,
                                    "approved_by": "operator"})
        request.app.state.bus.publish(task_id, event)
        worker = request.app.state.worker
        for notify in worker.notifiers:
            try:
                await notify(task)
            except Exception:  # noqa: BLE001
                logger.exception("notifier failed after approval")
        await worker.runner._record_outcome(task, "approved and shipped by operator")
        return {"status": "completed", "pr_url": pr_url}

    @app.post("/api/tasks/{task_id}/reject")
    async def reject_task(request: Request, task_id: str):
        db = request.app.state.db
        task = await db.get_task(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        if task.status != "awaiting_approval":
            raise HTTPException(409, f"task is {task.status}, not awaiting approval")
        import time as _time

        await db.update_task(task_id, status="cancelled",
                             error="rejected by operator", finished_at=_time.time())
        event = await db.add_event(task_id, "status",
                                   {"status": "cancelled",
                                    "error": "rejected by operator"})
        request.app.state.bus.publish(task_id, event)
        return {"status": "cancelled"}

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(request: Request, task_id: str, after: int = 0):
        return await request.app.state.db.list_events(task_id, after_id=after)

    # ---- SSE streams ----

    def _sse(event: dict) -> str:
        return f"data: {json.dumps(event, default=str)}\n\n"

    async def _stream(request: Request, task_id: str | None, after: int):
        db, bus = request.app.state.db, request.app.state.bus
        q = bus.attach(task_id)  # attach BEFORE replay so no event slips through
        last_id = after
        try:
            if task_id:
                for ev in await db.list_events(task_id, after_id=after):
                    last_id = ev["id"]
                    yield _sse(ev)
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if ev["id"] > last_id:
                    last_id = ev["id"]
                    yield _sse(ev)
        finally:
            bus.detach(task_id, q)

    @app.get("/api/tasks/{task_id}/stream")
    async def stream_task(request: Request, task_id: str, after: int = 0):
        if not await request.app.state.db.get_task(task_id):
            raise HTTPException(404, "task not found")
        return StreamingResponse(_stream(request, task_id, after),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/stream")
    async def stream_all(request: Request):
        return StreamingResponse(_stream(request, None, 0),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---- chat ----

    @app.post("/api/chat")
    async def chat(request: Request, body: ChatMessage):
        from .worker import _utc_midnight_ts

        s = request.app.state.settings
        if s.daily_budget_usd > 0:
            spend = await request.app.state.db.spend_since(_utc_midnight_ts())
            if spend >= s.daily_budget_usd:
                raise HTTPException(
                    429, f"daily budget reached (${spend:.2f} of "
                         f"${s.daily_budget_usd:.2f}) — chat resumes at UTC midnight")
        try:
            return await request.app.state.chat.ask(body.message, body.session_id)
        except Exception as e:  # noqa: BLE001 — surface model/tool errors to the UI
            logger.exception("chat failed")
            raise HTTPException(502, f"chat failed: {type(e).__name__}: {e}")

    @app.delete("/api/chat/{session_id}")
    async def reset_chat(request: Request, session_id: str):
        request.app.state.chat.reset(session_id)
        return {"reset": session_id}

    # ---- resources (links/docs ingested into long-term memory) ----

    def _memory_or_503(request: Request):
        memory = request.app.state.memory
        if not memory:
            raise HTTPException(
                503, "long-term memory is not configured — set SUPERMEMORY_BASE_URL "
                     "and SUPERMEMORY_API_KEY")
        return memory

    def _resource_tag(request: Request, scope: str) -> str:
        if scope in ("", "global"):
            return GLOBAL_TAG
        if scope not in request.app.state.cfg.repos():
            raise HTTPException(400, f"unknown scope '{scope}' — use 'global' or a "
                                     "registered repo name")
        return repo_tag(scope)

    @app.post("/api/resources", status_code=201)
    async def add_resource(request: Request, body: ResourceCreate):
        from .webfetch import FetchError, fetch_page_text

        memory = _memory_or_503(request)
        tag = _resource_tag(request, body.scope)
        content = body.content.strip()
        is_url = content.startswith(("http://", "https://")) and "\n" not in content
        title = ""
        if is_url:
            # fetch ourselves: self-hosted supermemory's web extractor needs
            # third-party API keys, and we promise self-hosting
            s = request.app.state.settings
            try:
                title, text = await fetch_page_text(
                    content, firecrawl_url=s.firecrawl_url,
                    firecrawl_key=s.firecrawl_api_key)
            except FetchError as e:
                raise HTTPException(422, str(e))
            stored = f"Source: {content}\nTitle: {title}\n\n{text}"
        else:
            stored = content
        doc_id = await memory.add(stored, tag, metadata={
            "source": "resource",
            "kind": "url" if is_url else "text",
            "scope": body.scope or "global",
            **({"url": content, "title": title} if is_url else {}),
        })
        if not doc_id:
            raise HTTPException(502, "memory server rejected the resource")
        return {"id": doc_id, "scope": body.scope or "global",
                "kind": "url" if is_url else "text", "title": title}

    @app.get("/api/resources")
    async def list_resources(request: Request):
        memory = _memory_or_503(request)
        tags = [GLOBAL_TAG] + [repo_tag(n) for n in request.app.state.cfg.repos()]
        docs = await memory.list_documents(tags, limit=200)
        out = []
        for d in docs:
            meta = d.get("metadata") or {}
            if meta.get("source") != "resource":
                continue  # agent learnings / task outcomes aren't shown here
            out.append({
                "id": d.get("id"),
                "title": d.get("title") or meta.get("title") or meta.get("url"),
                "summary": (d.get("summary") or "")[:300],
                "status": d.get("status"),
                "kind": meta.get("kind", "text"),
                "scope": meta.get("scope", "global"),
                "url": meta.get("url"),
                "created_at": d.get("createdAt"),
            })
        return out

    @app.delete("/api/resources/{doc_id}")
    async def delete_resource(request: Request, doc_id: str):
        memory = _memory_or_503(request)
        if not await memory.delete_document(doc_id):
            raise HTTPException(502, "delete failed on memory server")
        return {"deleted": doc_id}

    # ---- models ----

    @app.get("/api/models")
    async def list_models(request: Request):
        from .llm import KNOWN_MODELS

        s = request.app.state.settings
        configured = {
            "anthropic": bool(s.anthropic_api_key),
            "google_genai": bool(s.google_api_key),
            "openai": bool(s.openai_api_key),
        }
        return {
            provider: {
                "configured": configured[provider],
                "models": [f"{provider}:{m}" for m in models],
            }
            for provider, models in KNOWN_MODELS.items()
        }

    # ---- repos ----

    @app.get("/api/repos")
    async def list_repos(request: Request):
        return {name: r.to_dict() for name, r in request.app.state.cfg.repos().items()}

    @app.post("/api/repos", status_code=201)
    async def upsert_repo(request: Request, body: RepoCreate):
        repo = RepoConfig(name=body.name, url=body.url, default_branch=body.default_branch,
                          description=body.description, setup=body.setup, test=body.test,
                          context=body.context)
        request.app.state.cfg.upsert_repo(repo)
        return repo.to_dict() | {"name": repo.name}

    @app.delete("/api/repos/{name}")
    async def delete_repo(request: Request, name: str):
        if not request.app.state.cfg.delete_repo(name):
            raise HTTPException(404, "repo not found")
        return {"deleted": name}

    # ---- agent config ----

    @app.get("/api/config/agents")
    async def get_agents_config(request: Request):
        return request.app.state.cfg.agents_raw()

    @app.put("/api/config/agents")
    async def put_agents_config(request: Request, body: dict):
        cfg: ConfigStore = request.app.state.cfg
        old = cfg.agents_raw()
        cfg.update_agents(body)
        errors = validate_models(cfg.agents())
        if errors:
            cfg.update_agents(old)  # roll back
            raise HTTPException(400, "; ".join(errors))
        return cfg.agents_raw()

    # ---- webhooks ----

    @app.post("/webhooks/linear")
    async def linear_webhook(request: Request):
        linear: LinearIntake | None = request.app.state.linear
        if not linear:
            raise HTTPException(404, "linear intake not configured")
        raw = await request.body()
        secret = request.app.state.settings.linear_webhook_secret
        if secret:
            sig = request.headers.get("linear-signature", "")
            expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise HTTPException(401, "bad signature")
        await linear.handle_webhook(json.loads(raw))
        return {"ok": True}

    # ---- static UI (mounted last so /api wins) ----

    ui_dir = get_settings().ui_dir
    if ui_dir.exists():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")

    return app
