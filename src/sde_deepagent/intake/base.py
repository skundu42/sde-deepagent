"""Shared helpers for task intake channels."""

from __future__ import annotations

import re

from ..db import Task

# Messages may target a repo explicitly:  "[backend] fix the login bug"
# or  "repo=backend fix the login bug"
REPO_PREFIX_RE = re.compile(r"^\s*(?:\[(?P<a>[\w./-]+)\]|repo=(?P<b>[\w./-]+))\s*[:,-]?\s*")


def parse_task_text(text: str) -> tuple[str | None, str, str]:
    """Split an incoming message into (repo|None, title, description)."""
    text = text.strip()
    repo = None
    m = REPO_PREFIX_RE.match(text)
    if m:
        repo = m.group("a") or m.group("b")
        text = text[m.end():].strip()
    first_line, _, rest = text.partition("\n")
    title = first_line.strip()[:200] or "task"
    return repo, title, text


def task_summary(task: Task) -> str:
    """Plain-text completion summary sent back to the channel the task came from."""
    if task.status == "completed" and task.pr_url:
        return f"✅ Task {task.id} done: {task.title}\nPR: {task.pr_url}"
    if task.status == "completed":
        return (f"☑️ Task {task.id} finished without a PR (no code changes were "
                f"needed or the task was blocked): {task.title}")
    if task.status == "cancelled":
        return f"🚫 Task {task.id} was cancelled: {task.title}"
    if task.status == "awaiting_approval":
        return (f"⏸ Task {task.id} is ready and awaiting your approval before it "
                f"ships: {task.title}")
    return f"❌ Task {task.id} failed: {task.title}\nError: {task.error or 'unknown'}"
