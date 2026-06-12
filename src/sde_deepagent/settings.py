"""Environment-driven settings. Only model API keys are required; everything
else (intake channels, GitHub PRs) activates when its key is present."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- model providers (at least one required) ---
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    openai_api_key: str | None = None

    # --- git hosting (needed only for pushing branches / opening PRs) ---
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"  # override for GitHub Enterprise

    # --- intake channels (each optional) ---
    telegram_bot_token: str | None = None
    telegram_allowed_chats: str = ""  # comma-separated chat ids; empty = allow all

    slack_bot_token: str | None = None  # xoxb-...
    slack_app_token: str | None = None  # xapp-... (Socket Mode)

    # --- long-term memory (optional): self-hosted supermemory ---
    supermemory_base_url: str | None = None  # e.g. http://localhost:6767
    supermemory_api_key: str | None = None   # printed by `npx supermemory local`

    # --- web scraping for resource ingestion (optional): firecrawl ---
    # Self-hosted instance URL or https://api.firecrawl.dev (cloud needs the key).
    # When unset, devagent's built-in fetcher is used (no JS rendering).
    firecrawl_api_url: str | None = None
    firecrawl_api_key: str | None = None

    @property
    def firecrawl_url(self) -> str | None:
        """Effective firecrawl endpoint: explicit URL, or cloud if only a key is set."""
        if self.firecrawl_api_url:
            return self.firecrawl_api_url.rstrip("/")
        if self.firecrawl_api_key:
            return "https://api.firecrawl.dev"
        return None

    linear_api_key: str | None = None
    linear_label: str = "agent"  # issues with this label get picked up
    linear_poll_seconds: int = 30
    linear_webhook_secret: str | None = None

    # --- github PR review → auto-revision (optional) ---
    github_review_polling: bool = False
    github_review_poll_seconds: int = 120

    # --- server / runtime ---
    host: str = "0.0.0.0"
    port: int = 8321
    auth_token: str | None = None  # if set, API + SSE require this bearer token
    data_dir: Path = Path("data")
    config_dir: Path = Path("config")
    context_dir: Path = Path("context")
    ui_dir: Path = Path("ui")
    max_concurrent_tasks: int = 2
    task_timeout_seconds: int = 3600
    recursion_limit: int = 1000
    auto_finalize: bool = True  # if agent leaves uncommitted work, commit/push/PR it
    require_approval: bool = False  # global default: hold work for approval before push/PR
    workspace_retention: int = 10  # keep N most recent task workspaces on disk

    # --- sandboxing (per-task container isolation) ---
    sandbox_default: bool = False     # default for repos that don't set `sandbox`
    sandbox_image: str = "python:3.12-slim"
    sandbox_network: str = "none"     # none | bridge — egress policy inside the sandbox
    sandbox_memory: str = "2g"
    sandbox_cpus: str = "2"

    # --- LLM cost budgets (USD; 0 = unlimited) ---
    task_budget_usd: float = 0.0   # default per-task cap (overridable per task)
    daily_budget_usd: float = 0.0  # global cap; queue pauses when today's spend hits it

    @property
    def db_path(self) -> Path:
        return self.data_dir / "devagent.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    def telegram_allowed_chat_ids(self) -> set[int]:
        return {int(c) for c in self.telegram_allowed_chats.split(",") if c.strip()}


_settings: Settings | None = None


def _export_provider_keys(s: Settings) -> None:
    """Keys loaded from .env must reach os.environ — the LangChain provider
    clients (OpenAI/Anthropic/Google SDKs) read the environment, not Settings.
    (The agent's shell still never sees them: see SAFE_ENV_KEYS.)"""
    for env_name, value in [
        ("ANTHROPIC_API_KEY", s.anthropic_api_key),
        ("GOOGLE_API_KEY", s.google_api_key),
        ("OPENAI_API_KEY", s.openai_api_key),
    ]:
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
        _settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
        _export_provider_keys(_settings)
    return _settings
