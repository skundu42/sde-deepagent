"""Environment-driven settings. Only model API keys are required; everything
else (intake channels, GitHub PRs) activates when its key is present."""

from __future__ import annotations

import ipaddress
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
    # comma-separated chat ids; empty = deny all; "*" is an explicit allow-all
    telegram_allowed_chats: str = ""

    slack_bot_token: str | None = None  # xoxb-...
    slack_app_token: str | None = None  # xapp-... (Socket Mode)
    # comma-separated Slack user ids; empty = deny all; "*" is explicit allow-all
    slack_allowed_users: str = ""

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

    # Seatless setup: set linear_webhook_secret (inbound, no seat) + optionally
    # linear_oauth_token (an OAuth app token, actor=app) to post comments as the
    # app/bot without occupying a seat. linear_api_key is the legacy path: a
    # personal key (a seat) that also enables polling.
    linear_api_key: str | None = None
    linear_oauth_token: str | None = None  # OAuth app token (actor=app); seatless write-back
    # "label": pick up issues carrying linear_label. "assignee": pick up issues
    # assigned to the api key's own user (use a key from a dedicated agent member).
    # assignee mode requires linear_api_key; webhook-only falls back to label.
    linear_trigger: str = "label"
    linear_label: str = "agent"  # issues with this label get picked up (trigger=label)
    linear_poll_seconds: int = 30
    linear_webhook_secret: str | None = None

    # --- github PR review → auto-revision (optional) ---
    github_review_polling: bool = False
    github_review_poll_seconds: int = 120

    # --- server / runtime ---
    host: str = "127.0.0.1"
    port: int = 8321
    auth_token: str | None = None  # if set, API + SSE require this bearer token
    # master key for the encrypted per-repo secret store (any high-entropy
    # string; a data key is derived from it). Unset = encrypted secrets disabled.
    secrets_key: str | None = None
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

    # --- crash resume ---
    # (history summarization near the context limit is already on by default via
    # deepagents' built-in summarization middleware — nothing to configure here)
    checkpoint_resume: bool = True       # persist agent state; resume interrupted tasks after a restart

    # --- sandboxing (one container per repo, reused across tasks) ---
    # Zero-config by default: every repo's tasks run in that repo's container
    # (needs Docker; SANDBOX_DEFAULT=false to run on the host instead), and the
    # agent installs whatever toolchain/dependencies the repo needs in there —
    # which requires egress, hence network=bridge. Set SANDBOX_NETWORK=none for
    # airtight egress at the cost of self-bootstrapping environments.
    sandbox_default: bool = True      # default for repos that don't set `sandbox`
    # generic Debian build image (git/gcc/make/curl, no language runtimes) —
    # setup/agent installs the toolchain once, then the container is reused
    sandbox_image: str = "buildpack-deps:bookworm"
    sandbox_network: str = "bridge"   # bridge | none — egress policy inside the sandbox
    sandbox_memory: str = "2g"
    sandbox_cpus: str = "2"
    sandbox_idle_hours: float = 168.0  # remove a repo's container idle this long (7 days)

    # --- LLM cost budgets (USD; 0 = unlimited) ---
    task_budget_usd: float = 0.0   # default per-task cap (overridable per task)
    daily_budget_usd: float = 0.0  # global cap; queue pauses when today's spend hits it
    # persist an in-flight run's token spend to the DB this often, so a hard
    # crash can't lose it from the daily total (0 = every model response)
    usage_flush_seconds: float = 30.0

    # --- chat code-reading reference clones ---
    # re-fetch a ref clone when its last fetch is older than this (0 = never refresh)
    ref_clone_ttl_minutes: float = 15.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "devagent.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def control_git_dir(self) -> Path:
        """Trusted Git metadata, deliberately outside sandbox-mounted workspaces."""
        return self.data_dir / "control-git"

    @property
    def ref_clones_dir(self) -> Path:
        """Read-only reference clones for the chat assistant's code-reading tools.
        Kept out of workspaces_dir so the task-workspace reaper never touches them."""
        return self.data_dir / "ref-clones"

    @property
    def sandbox_state_path(self) -> Path:
        """Last-use timestamps for sandbox containers (drives the idle reaper)."""
        return self.data_dir / "sandbox-usage.json"

    @property
    def secrets_store_path(self) -> Path:
        """Encrypted per-repo secret values (Fernet tokens; needs SECRETS_KEY)."""
        return self.data_dir / "secrets.enc"

    def telegram_allowed_chat_ids(self) -> set[int]:
        return {
            int(c) for c in self.telegram_allowed_chats.split(",")
            if c.strip() and c.strip() != "*"
        }

    @property
    def telegram_allow_all(self) -> bool:
        return "*" in {c.strip() for c in self.telegram_allowed_chats.split(",")}

    def slack_allowed_user_ids(self) -> set[str]:
        return {
            user.strip() for user in self.slack_allowed_users.split(",")
            if user.strip() and user.strip() != "*"
        }

    @property
    def slack_allow_all(self) -> bool:
        return "*" in {u.strip() for u in self.slack_allowed_users.split(",")}


def is_loopback_host(host: str) -> bool:
    """Whether a bind host is limited to this machine."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_control_plane_security(settings: Settings) -> None:
    """Refuse an unauthenticated control plane reachable from the network."""
    if not settings.auth_token and not is_loopback_host(settings.host):
        raise RuntimeError(
            f"refusing unauthenticated network bind on {settings.host!r}; "
            "set AUTH_TOKEN or bind HOST to 127.0.0.1/::1"
        )


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
        _settings.control_git_dir.mkdir(parents=True, exist_ok=True)
        _export_provider_keys(_settings)
    return _settings
