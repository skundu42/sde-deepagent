"""SQLite persistence for tasks and their event streams."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    repo TEXT,
    source TEXT NOT NULL DEFAULT 'ui',
    source_ref TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    branch TEXT,
    pr_url TEXT,
    error TEXT,
    model TEXT,
    parent_id TEXT,
    budget_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    agent TEXT NOT NULL DEFAULT 'orchestrator',
    kind TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS chat_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_spend_ts ON chat_spend(ts);
"""

VALID_STATUSES = {"queued", "running", "awaiting_approval", "completed",
                  "failed", "cancelled"}


@dataclass
class Task:
    id: str
    title: str
    description: str
    repo: str | None
    source: str
    source_ref: dict[str, Any]
    status: str
    branch: str | None = None
    pr_url: str | None = None
    error: str | None = None
    model: str | None = None
    parent_id: str | None = None
    budget_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_task_id() -> str:
    return uuid.uuid4().hex[:10]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        # Anything left "running" from a previous process crashed mid-flight.
        await self._db.execute(
            "UPDATE tasks SET status='failed', error='interrupted by server restart',"
            " finished_at=? WHERE status='running'",
            (time.time(),),
        )
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add columns introduced after the first release to existing DBs."""
        async with self._db.execute("PRAGMA table_info(tasks)") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        for name, decl in [("budget_usd", "REAL"), ("input_tokens", "INTEGER"),
                           ("output_tokens", "INTEGER"), ("cost_usd", "REAL"),
                           ("parent_id", "TEXT")]:
            if name not in existing:
                await self._db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    # ---- tasks ----

    async def create_task(
        self,
        title: str,
        description: str,
        repo: str | None = None,
        source: str = "ui",
        source_ref: dict[str, Any] | None = None,
        model: str | None = None,
        budget_usd: float | None = None,
        parent_id: str | None = None,
    ) -> Task:
        task = Task(
            id=new_task_id(),
            title=title.strip()[:200],
            description=description.strip(),
            repo=repo,
            source=source,
            source_ref=source_ref or {},
            status="queued",
            model=model,
            budget_usd=budget_usd,
            parent_id=parent_id,
        )
        await self.db.execute(
            "INSERT INTO tasks (id, title, description, repo, source, source_ref, status,"
            " model, parent_id, budget_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                task.id, task.title, task.description, task.repo, task.source,
                json.dumps(task.source_ref), task.status, task.model, task.parent_id,
                task.budget_usd, task.created_at,
            ),
        )
        await self.db.commit()
        return task

    def _row_to_task(self, row: aiosqlite.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            repo=row["repo"],
            source=row["source"],
            source_ref=json.loads(row["source_ref"]),
            status=row["status"],
            branch=row["branch"],
            pr_url=row["pr_url"],
            error=row["error"],
            model=row["model"],
            parent_id=row["parent_id"],
            budget_usd=row["budget_usd"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    async def get_task(self, task_id: str) -> Task | None:
        async with self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        return self._row_to_task(row) if row else None

    async def list_tasks(self, status: str | None = None, limit: int = 200) -> list[Task]:
        if status:
            q = "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?"
            args: tuple = (status, limit)
        else:
            q = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?"
            args = (limit,)
        async with self.db.execute(q, args) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "branch", "pr_url", "error", "repo", "started_at",
                   "finished_at", "input_tokens", "output_tokens", "cost_usd"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update field {k}")
            if k == "status" and v not in VALID_STATUSES:
                raise ValueError(f"invalid status {v}")
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return
        vals.append(task_id)
        await self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        await self.db.commit()

    async def next_queued_task(self) -> Task | None:
        async with self.db.execute(
            "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_task(row) if row else None

    async def add_chat_spend(self, session_id: str, model: str | None,
                             input_tokens: int, output_tokens: int,
                             cost_usd: float) -> None:
        await self.db.execute(
            "INSERT INTO chat_spend (ts, session_id, model, input_tokens,"
            " output_tokens, cost_usd) VALUES (?,?,?,?,?,?)",
            (time.time(), session_id, model, input_tokens, output_tokens, cost_usd),
        )
        await self.db.commit()

    async def chat_spend_since(self, since_ts: float) -> float:
        async with self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM chat_spend WHERE ts >= ?",
            (since_ts,),
        ) as cur:
            row = await cur.fetchone()
        return float(row["total"] or 0.0)

    async def spend_since(self, since_ts: float) -> float:
        """Total recorded LLM spend (USD) since since_ts: task runs + chat."""
        async with self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM tasks"
            " WHERE started_at >= ? AND cost_usd IS NOT NULL",
            (since_ts,),
        ) as cur:
            row = await cur.fetchone()
        return float(row["total"] or 0.0) + await self.chat_spend_since(since_ts)

    async def stats(self) -> dict[str, int]:
        async with self.db.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status") as cur:
            rows = await cur.fetchall()
        out = {s: 0 for s in VALID_STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        out["total"] = sum(out.values())
        return out

    # ---- events ----

    async def add_event(
        self, task_id: str, kind: str, content: dict[str, Any], agent: str = "orchestrator"
    ) -> dict[str, Any]:
        ts = time.time()
        cur = await self.db.execute(
            "INSERT INTO events (task_id, ts, agent, kind, content) VALUES (?,?,?,?,?)",
            (task_id, ts, agent, kind, json.dumps(content, default=str)),
        )
        await self.db.commit()
        return {"id": cur.lastrowid, "task_id": task_id, "ts": ts, "agent": agent,
                "kind": kind, "content": content}

    async def list_events(self, task_id: str, after_id: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (task_id, after_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r["id"], "task_id": r["task_id"], "ts": r["ts"], "agent": r["agent"],
             "kind": r["kind"], "content": json.loads(r["content"])}
            for r in rows
        ]
