import os

import pytest

import sde_deepagent.settings as settings_mod


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Point all storage at a temp dir and reset the settings singleton."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CONTEXT_DIR", str(tmp_path / "context"))
    monkeypatch.setenv("UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("MAX_CONCURRENT_TASKS", "0")  # keep the worker idle in tests
    # empty strings (not delenv): env vars take precedence over the project's
    # real .env file, which Settings would otherwise read from the test cwd
    for key in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
                "SUPERMEMORY_BASE_URL", "SUPERMEMORY_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.setenv(key, "")
    settings_mod._settings = None
    yield tmp_path
    settings_mod._settings = None
