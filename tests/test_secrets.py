"""Per-repo secrets: spec validation, host-env resolution, value redaction
across sinks, confined execution (secret value never in the docker argv), and
the controller-run run_tests tool."""

import base64
from types import SimpleNamespace
from urllib.parse import quote

import pydantic
import pytest

from sde_deepagent import sandbox
from sde_deepagent.bus import EventBus
from sde_deepagent.db import Database
from sde_deepagent.runner import TaskRunner
from sde_deepagent.secrets import (
    Redactor,
    SecretStore,
    resolve_repo_secrets,
    validate_secret_spec,
)

SECRET = "postgres://user:p@ssw0rd@db.internal/app?sslmode=require"


# ---- spec validation ----

def test_validate_accepts_good_spec():
    validate_secret_spec({"DATABASE_URL": "env:BACKEND_DB_URL", "API_KEY": "env:FOO"})


def test_validate_allows_store_reference():
    validate_secret_spec({"DB": "store", "API": "env:FOO"})


@pytest.mark.parametrize("name", ["PATH", "HOME", "GIT_CONFIG_COUNT", "LD_PRELOAD",
                                  "DYLD_INSERT_LIBRARIES", "BASH_ENV"])
def test_validate_rejects_reserved_names(name):
    with pytest.raises(ValueError):
        validate_secret_spec({name: "env:FOO"})


@pytest.mark.parametrize("name", ["lower", "1BAD", "WITH-DASH", "WITH SPACE"])
def test_validate_rejects_bad_names(name):
    with pytest.raises(ValueError):
        validate_secret_spec({name: "env:FOO"})


@pytest.mark.parametrize("ref", ["literal-value", "env:lower", "file:/x", "", "env:"])
def test_validate_rejects_bad_references(ref):
    with pytest.raises(ValueError):
        validate_secret_spec({"DB": ref})


# ---- host-env resolution ----

def test_resolve_reads_env_and_reports_missing():
    repo = SimpleNamespace(secrets={"DB": "env:HOSTDB", "MISS": "env:NOPE",
                                    "BAD": "literal"})
    resolved, missing = resolve_repo_secrets(repo, environ={"HOSTDB": SECRET})
    assert resolved == {"DB": SECRET}
    assert set(missing) == {"MISS", "BAD"}


def test_resolve_empty_for_no_secrets():
    assert resolve_repo_secrets(SimpleNamespace(secrets={}), environ={}) == ({}, [])
    assert resolve_repo_secrets(SimpleNamespace(), environ={}) == ({}, [])


# ---- redactor ----

def test_redactor_masks_raw_base64_and_urlencoded():
    r = Redactor({"DB": SECRET})
    assert r.active
    assert SECRET not in r.redact(f"connecting to {SECRET} now")
    assert "«redacted:DB»" in r.redact(SECRET)
    b64 = base64.b64encode(SECRET.encode()).decode()
    assert b64 not in r.redact(f"as base64: {b64}")
    enc = quote(SECRET, safe="")
    assert enc not in r.redact(f"url-encoded: {enc}")


def test_redactor_empty_is_identity():
    r = Redactor({})
    assert not r.active
    assert r.redact("anything") == "anything"
    assert r.redact_obj({"a": ["x", "y"], "n": 1}) == {"a": ["x", "y"], "n": 1}


def test_redactor_walks_nested_objects():
    r = Redactor({"K": "supersecretvalue"})
    out = r.redact_obj({"output": "got supersecretvalue here", "n": 1,
                        "l": ["leak supersecretvalue"]})
    assert "supersecretvalue" not in str(out)
    assert out["n"] == 1


def test_redactor_ignores_trivially_short_values():
    # too short to mask safely -> nothing registered, identity behaviour
    r = Redactor({"K": "ab"})
    assert not r.active


# ---- confined execution: secret value never appears in the docker argv ----

def test_exec_in_container_passes_secrets_by_reference(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        captured["env"] = kw.get("env")
        return 0, b"ok", b"", False

    monkeypatch.setattr(sandbox, "_run_capped", fake_run)
    sandbox.exec_in_container("c", "pytest", timeout=30,
                              secrets={"DB": "sekretvalue"})
    args = captured["args"]
    assert "sekretvalue" not in args             # value never in argv / ps
    assert "--env" in args and "DB" in args      # only the NAME is forwarded
    assert captured["env"]["DB"] == "sekretvalue"  # value travels via CLI env


def test_exec_in_container_no_env_flags_without_secrets(monkeypatch):
    captured = {}
    monkeypatch.setattr(sandbox, "_run_capped",
                        lambda a, **k: captured.update(args=a, env=k.get("env"))
                        or (0, b"ok", b"", False))
    sandbox.exec_in_container("c", "echo hi", timeout=10)
    assert "--env" not in captured["args"]
    assert captured["env"] is None  # untouched default environment


# ---- run_tests tool (controller-run, registered command, redacted output) ----

async def test_run_tests_runs_registered_command_with_secrets(monkeypatch, tmp_path):
    from sde_deepagent.agent_factory import make_run_tests_tool

    calls = {}

    def fake_exec(name, cmd, **kw):
        calls["cmd"] = cmd
        calls["secrets"] = kw.get("secrets")
        return sandbox.ExecuteResponse(output="ran, leaked sekretvalue in log",
                                       exit_code=0, truncated=False)

    monkeypatch.setattr(sandbox, "exec_in_container", fake_exec)
    redactor = Redactor({"DB": "sekretvalue"})
    ws = SimpleNamespace(path=tmp_path, repo=SimpleNamespace(test="pytest -q"))
    tool = make_run_tests_tool(
        test_cmd="pytest -q", ws=ws, sandbox_container="c",
        sandbox_workdir="/workspaces/t/repo", secrets={"DB": "sekretvalue"},
        redact=redactor.redact, on_event=None)

    out = await tool.ainvoke({"selector": ""})
    assert "pytest -q" in calls["cmd"]
    assert calls["secrets"] == {"DB": "sekretvalue"}
    assert "sekretvalue" not in out          # output redacted before the LLM sees it
    assert "passed" in out

    # an unsafe selector is dropped; the registered base command still runs
    await tool.ainvoke({"selector": "; cat /etc/passwd"})
    assert calls["cmd"] == "pytest -q"

    # a safe selector is appended
    await tool.ainvoke({"selector": "tests/test_x.py -k foo"})
    assert calls["cmd"] == "pytest -q tests/test_x.py -k foo"


async def test_run_tests_no_command_registered(tmp_path):
    from sde_deepagent.agent_factory import make_run_tests_tool
    ws = SimpleNamespace(path=tmp_path, repo=SimpleNamespace(test=None))
    tool = make_run_tests_tool(test_cmd=None, ws=ws, sandbox_container=None,
                               sandbox_workdir=None, secrets={}, redact=None,
                               on_event=None)
    out = await tool.ainvoke({"selector": ""})
    assert "no test command" in out.lower()


# ---- redaction at the emit() choke point and the _redact helper ----

async def test_emit_redacts_secret_values(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.connect()
    runner = TaskRunner.__new__(TaskRunner)  # only emit's collaborators needed
    runner.db, runner.bus, runner._redactors = db, EventBus(), {}
    runner._redactors["t1"] = Redactor({"DB": "sekretvalue"})

    await runner.emit("t1", "tool_result", {"output": "saw sekretvalue here"})
    events = await db.list_events("t1")
    assert events and "sekretvalue" not in events[0]["content"]["output"]
    assert "«redacted:DB»" in events[0]["content"]["output"]
    await db.close()


def test_redacting_local_backend_masks_shell_output(tmp_path):
    # real deepagents backend (no mocks): a secret written to the workspace and
    # read back via the agent's own (secret-free) shell is still redacted
    (tmp_path / "leak.txt").write_text("token=topsecretvalue\n")
    backend = sandbox.RedactingLocalShellBackend(
        root_dir=tmp_path, virtual_mode=True, timeout=30,
        max_output_bytes=60000, redact=Redactor({"TOK": "topsecretvalue"}).redact)
    resp = backend.execute("cat leak.txt")
    assert "topsecretvalue" not in resp.output
    assert "«redacted:TOK»" in resp.output


def test_runner_redact_helper():
    runner = TaskRunner.__new__(TaskRunner)
    runner._redactors = {"t1": Redactor({"K": "longsecretvalue"})}
    assert runner._redact("t1", "x longsecretvalue y") == "x «redacted:K» y"
    # a task with no armed redactor passes through unchanged
    assert runner._redact("other", "longsecretvalue") == "longsecretvalue"


# ---- config + API surface ----

def test_repoconfig_roundtrips_secret_references(tmp_path):
    from sde_deepagent.config import ConfigStore, RepoConfig
    store = ConfigStore(tmp_path / "config")
    store.upsert_repo(RepoConfig(name="backend", url="https://x/r",
                                 secrets={"DATABASE_URL": "env:BACKEND_DB_URL"}))
    repos = store.repos()
    assert repos["backend"].secrets == {"DATABASE_URL": "env:BACKEND_DB_URL"}
    # references (never values) are what gets serialized/served
    assert repos["backend"].to_dict()["secrets"] == {"DATABASE_URL": "env:BACKEND_DB_URL"}


def test_invalid_secret_dropped_on_load(tmp_path):
    from sde_deepagent.config import ConfigStore
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "repos.yaml").write_text(
        "repos:\n  r:\n    url: https://x/r\n    secrets:\n"
        "      GOOD: env:FOO\n      PATH: env:BAR\n      lower: env:BAZ\n")
    store = ConfigStore(cfgdir)
    assert store.repos()["r"].secrets == {"GOOD": "env:FOO"}  # PATH/lower dropped


def test_repocreate_validates_secrets():
    from sde_deepagent.server import RepoCreate
    RepoCreate(name="r", url="https://x", secrets={"DB": "env:FOO"})   # ok
    RepoCreate(name="r", url="https://x", secrets={"DB": "store"})     # ok
    with pytest.raises(pydantic.ValidationError):
        RepoCreate(name="r", url="https://x", secrets={"PATH": "env:FOO"})
    with pytest.raises(pydantic.ValidationError):
        RepoCreate(name="r", url="https://x", secrets={"DB": "literal-not-a-ref"})


# ---- encrypted SecretStore ----

def test_secret_store_roundtrip_encrypts_at_rest(tmp_path):
    store = SecretStore(tmp_path / "secrets.enc", "master-key")
    assert store.available
    store.set_many("backend", {"A": "value-one", "B": "value-two"})
    assert store.names("backend") == ["A", "B"]            # names without decrypting
    assert store.resolve("backend") == {"A": "value-one", "B": "value-two"}
    raw = (tmp_path / "secrets.enc").read_text()
    assert "value-one" not in raw and "value-two" not in raw  # ciphertext on disk
    store.delete("backend", "A")
    assert store.names("backend") == ["B"]
    store.delete("backend")
    assert store.names("backend") == []


def test_secret_store_wrong_key_cannot_decrypt(tmp_path):
    SecretStore(tmp_path / "s.enc", "key-one").set_many("r", {"A": "secret"})
    assert SecretStore(tmp_path / "s.enc", "key-two").resolve("r") == {}  # dropped


def test_secret_store_unavailable_without_key(tmp_path):
    store = SecretStore(tmp_path / "s.enc", None)
    assert not store.available
    assert store.resolve("r") == {}
    with pytest.raises(RuntimeError):
        store.set_many("r", {"A": "v"})


def test_resolve_merges_store_and_env_refs(tmp_path):
    store = SecretStore(tmp_path / "s.enc", "k")
    store.set_many("backend", {"DB": "pg-value"})
    repo = SimpleNamespace(name="backend", secrets={"DB": "store", "TOKEN": "env:HV"})
    resolved, missing = resolve_repo_secrets(repo, environ={"HV": "env-value"}, store=store)
    assert resolved == {"DB": "pg-value", "TOKEN": "env-value"} and missing == []
    # store unavailable -> store-backed names fail closed (missing), env still works
    nostore = SecretStore(tmp_path / "absent.enc", None)
    resolved2, missing2 = resolve_repo_secrets(repo, environ={"HV": "env-value"},
                                               store=nostore)
    assert resolved2 == {"TOKEN": "env-value"} and missing2 == ["DB"]


# ---- secret-value API endpoints (write-only; values never read back) ----

async def _repo_app(monkeypatch, key="test-master-key"):
    import sde_deepagent.settings as settings_mod
    from sde_deepagent.server import create_app
    monkeypatch.setenv("SECRETS_KEY", key)
    settings_mod._settings = None
    return create_app()


async def test_secret_endpoints_store_get_delete(temp_env, monkeypatch):
    import httpx
    app = await _repo_app(monkeypatch)
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                assert (await client.post(
                    "/api/repos", json={"name": "backend", "url": "https://x/r"})
                ).status_code == 201

                r = await client.put("/api/repos/backend/secrets",
                                     json={"values": {"DATABASE_URL": "pg://secret-value"}})
                assert r.status_code == 200 and r.json()["set"] == ["DATABASE_URL"]

                # listing shows name + set state, NEVER the value
                r = await client.get("/api/repos/backend/secrets")
                assert r.json()["DATABASE_URL"] == {"source": "store", "set": True}
                assert "secret-value" not in r.text

                # repos.yaml now records it as store-backed (reference only)
                repos = (await client.get("/api/repos")).json()
                assert repos["backend"]["secrets"]["DATABASE_URL"] == "store"
                assert "secret-value" not in (await client.get("/api/repos")).text

                # delete removes it from store and config
                assert (await client.delete(
                    "/api/repos/backend/secrets/DATABASE_URL")).status_code == 200
                assert "DATABASE_URL" not in (await client.get(
                    "/api/repos/backend/secrets")).json()


async def test_secret_put_fails_closed_without_key(temp_env, monkeypatch):
    import httpx
    app = await _repo_app(monkeypatch, key="")  # SECRETS_KEY unset
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                await client.post("/api/repos", json={"name": "r", "url": "https://x"})
                r = await client.put("/api/repos/r/secrets",
                                     json={"values": {"DB": "v"}})
                assert r.status_code == 400 and "SECRETS_KEY" in r.json()["detail"]
