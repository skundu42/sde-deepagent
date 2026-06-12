from sde_deepagent.config import ConfigStore, RepoConfig
from sde_deepagent.llm import normalize_model_id

import pytest


def test_defaults_created(tmp_path):
    cfg = ConfigStore(tmp_path)
    assert (tmp_path / "agents.yaml").exists()
    assert (tmp_path / "repos.yaml").exists()
    agents = cfg.agents()
    assert agents.orchestrator_model.startswith("anthropic:")
    names = {s.name for s in agents.subagents}
    assert {"explorer", "coder", "tester", "reviewer"} <= names
    assert agents.mcp_servers == {}


def test_repo_roundtrip(tmp_path):
    cfg = ConfigStore(tmp_path)
    assert cfg.repos() == {}
    cfg.upsert_repo(RepoConfig(name="backend", url="git@github.com:acme/backend.git",
                               description="the api", test="pytest", context=["docs/*.md"]))
    repos = cfg.repos()
    assert repos["backend"].test == "pytest"
    assert repos["backend"].context == ["docs/*.md"]

    # update in place
    cfg.upsert_repo(RepoConfig(name="backend", url="git@github.com:acme/backend.git",
                               default_branch="develop"))
    assert cfg.repos()["backend"].default_branch == "develop"

    assert cfg.delete_repo("backend") is True
    assert cfg.delete_repo("backend") is False
    assert cfg.repos() == {}


def test_agents_update(tmp_path):
    cfg = ConfigStore(tmp_path)
    raw = cfg.agents_raw()
    raw["orchestrator"]["model"] = "google_genai:gemini-2.5-pro"
    raw["subagents"]["coder"]["model"] = "anthropic:claude-opus-4-8"
    cfg.update_agents(raw)
    agents = cfg.agents()
    assert agents.orchestrator_model == "google_genai:gemini-2.5-pro"
    coder = next(s for s in agents.subagents if s.name == "coder")
    assert coder.model == "anthropic:claude-opus-4-8"


@pytest.mark.parametrize("given,expected", [
    ("claude-sonnet-4-6", "anthropic:claude-sonnet-4-6"),
    ("gemini-2.5-flash", "google_genai:gemini-2.5-flash"),
    ("anthropic:claude-opus-4-8", "anthropic:claude-opus-4-8"),
    ("google_genai:gemini-2.5-pro", "google_genai:gemini-2.5-pro"),
    ("openai:gpt-5.4", "openai:gpt-5.4"),
    ("gpt-5.4", "openai:gpt-5.4"),
    ("gpt-4.1-mini", "openai:gpt-4.1-mini"),
    ("o4-mini", "openai:o4-mini"),
    ("o3", "openai:o3"),
])
def test_model_normalization(given, expected):
    assert normalize_model_id(given) == expected


@pytest.mark.parametrize("bad", ["llama3", "mistral:large", "cohere:command",
                                 "ollama-local"])
def test_model_normalization_rejects_unsupported(bad):
    with pytest.raises(ValueError):
        normalize_model_id(bad)


def test_env_file_keys_reach_process_env(temp_env, tmp_path, monkeypatch):
    """Keys living only in .env must be exported for the provider SDKs."""
    import os

    import sde_deepagent.settings as settings_mod
    from sde_deepagent.settings import get_settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-dotenv\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings_mod._settings = None
    s = get_settings()
    assert s.openai_api_key == "sk-from-dotenv"
    assert os.environ["OPENAI_API_KEY"] == "sk-from-dotenv"
