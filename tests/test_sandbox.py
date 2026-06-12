"""Per-repo container sandbox: container reuse vs (re)creation, idle reaping,
command construction, output parsing, and the backend that routes execute()
into the container. Docker itself is mocked here; a live container lifecycle
check runs separately."""

import json
import subprocess
import time
from types import SimpleNamespace

from sde_deepagent import sandbox


def test_container_name_is_per_repo():
    assert sandbox.container_name("backend") == "sde-repo-backend"
    # unsafe characters are slugged, never passed to docker raw
    assert sandbox.container_name("My Repo!") == "sde-repo-my-repo"


# ---- exec ----


def test_exec_in_container_builds_docker_exec(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stdout="hello\n", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    res = sandbox.exec_in_container("sde-repo-x", "echo hello", timeout=30,
                                    workdir="/workspaces/t1/repo")
    assert captured["args"][:5] == ["docker", "exec", "-w",
                                    "/workspaces/t1/repo", "sde-repo-x"]
    assert captured["args"][5:7] == ["bash", "-lc"]
    assert "echo hello" in captured["args"]
    assert res.exit_code == 0 and "hello" in res.output


def test_exec_in_container_merges_stderr_and_exit_code(monkeypatch):
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda a, **k: SimpleNamespace(returncode=2, stdout="out",
                                                       stderr="boom"))
    res = sandbox.exec_in_container("c", "false", timeout=10)
    assert res.exit_code == 2
    assert "[stderr] boom" in res.output and "Exit code: 2" in res.output


def test_exec_in_container_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=5)

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    res = sandbox.exec_in_container("c", "sleep 100", timeout=5)
    assert res.exit_code == 124 and "timed out" in res.output


def test_docker_shell_backend_routes_execute(tmp_path, monkeypatch):
    calls = {}

    def fake_exec(name, cmd, **kw):
        calls["c"] = (name, cmd, kw.get("workdir"))
        return sandbox.ExecuteResponse(output="ok", exit_code=0, truncated=False)

    monkeypatch.setattr(sandbox, "exec_in_container", fake_exec)
    backend = sandbox.DockerShellBackend(tmp_path, "sde-repo-x",
                                         workdir="/workspaces/t9/repo")
    res = backend.execute("pytest -q")
    assert calls["c"] == ("sde-repo-x", "pytest -q", "/workspaces/t9/repo")
    assert res.output == "ok"
    # empty command guarded, never reaches the container
    assert backend.execute("").exit_code == 1


# ---- container lifecycle ----


class FakeDocker:
    """Routes sandbox.subprocess.run docker calls to canned responses and
    records every invocation."""

    def __init__(self, inspect_out: str | None = None, run_rc: int = 0):
        self.calls: list[list[str]] = []
        self.inspect_out = inspect_out  # None = container doesn't exist
        self.run_rc = run_rc

    def __call__(self, args, **kw):
        self.calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            if self.inspect_out is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such")
            return SimpleNamespace(returncode=0, stdout=self.inspect_out, stderr="")
        if args[:2] == ["docker", "exec"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=self.run_rc, stdout="cid", stderr="err")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def by_prefix(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def test_ensure_container_creates_when_absent(tmp_path, monkeypatch):
    fake = FakeDocker(inspect_out=None)
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    name, created = sandbox.ensure_container(
        "backend", str(tmp_path), image="node:lts-slim", network="none",
        memory="2g", cpus="2")
    assert (name, created) == ("sde-repo-backend", True)
    run_cmd = fake.by_prefix("docker", "run")[0]
    assert run_cmd[run_cmd.index("--network") + 1] == "none"
    # the bind mount must be an ABSOLUTE path (relative reads as a named volume)
    mount = run_cmd[run_cmd.index("-v") + 1]
    assert mount.startswith("/") and mount.endswith(":/workspaces")
    assert "node:lts-slim" in run_cmd
    # labelled so the reaper can find it, with the config fingerprint
    labels = [run_cmd[i + 1] for i, a in enumerate(run_cmd) if a == "--label"]
    assert f"{sandbox.SANDBOX_LABEL}=1" in labels
    assert any(lab.startswith(f"{sandbox.CFG_LABEL}=") for lab in labels)


def test_ensure_container_reuses_matching_running(tmp_path, monkeypatch):
    cfg = sandbox._cfg_hash("img", "none", "2g", "2", str(tmp_path.resolve()))
    fake = FakeDocker(inspect_out=f"true\t{cfg}\n")
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    name, created = sandbox.ensure_container(
        "backend", str(tmp_path), image="img", network="none",
        memory="2g", cpus="2", probe_subdir="t1/repo")
    assert (name, created) == ("sde-repo-backend", False)
    assert fake.by_prefix("docker", "run") == []  # no new container
    assert fake.by_prefix("docker", "rm") == []   # existing one untouched
    # the task workspace was probed for visibility inside the mount
    probe = fake.by_prefix("docker", "exec")[0]
    assert "/workspaces/t1/repo" in probe


def test_ensure_container_recreates_on_config_change(tmp_path, monkeypatch):
    fake = FakeDocker(inspect_out="true\tdeadbeef0000\n")  # stale config label
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    name, created = sandbox.ensure_container(
        "backend", str(tmp_path), image="img", network="none",
        memory="2g", cpus="2")
    assert created is True
    assert fake.by_prefix("docker", "rm") != []  # old one removed first
    assert fake.by_prefix("docker", "run") != []


def test_ensure_container_rejects_bad_network(tmp_path, monkeypatch):
    fake = FakeDocker(inspect_out=None)
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    sandbox.ensure_container("r", str(tmp_path), image="i", network="host",
                             memory="1g", cpus="1")  # disallowed network
    run_cmd = fake.by_prefix("docker", "run")[0]
    assert run_cmd[run_cmd.index("--network") + 1] == "none"  # forced safe


def test_ensure_container_create_failure_raises(tmp_path, monkeypatch):
    fake = FakeDocker(inspect_out=None, run_rc=1)
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    try:
        sandbox.ensure_container("r", str(tmp_path), image="i", network="none",
                                 memory="1g", cpus="1")
        raise AssertionError("expected SandboxError")
    except sandbox.SandboxError as e:
        assert "i" in str(e)


# ---- idle reaping ----


def test_mark_used_and_reap_idle(tmp_path, monkeypatch):
    state = tmp_path / "usage.json"
    sandbox.mark_used("sde-repo-fresh", state)
    # stale container: last used 25h ago
    data = json.loads(state.read_text())
    data["sde-repo-stale"] = time.time() - 25 * 3600
    data["sde-repo-gone"] = time.time()  # no longer exists in docker
    state.write_text(json.dumps(data))

    removed_names = []

    def fake_run(args, **kw):
        if args[:2] == ["docker", "ps"] and f"label={sandbox.SANDBOX_LABEL}" in args:
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout="sde-repo-fresh\nsde-repo-stale\nsde-repo-new\n")
        if args[:2] == ["docker", "ps"]:  # legacy sde-task-* sweep
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["docker", "rm", "-f"]:
            removed_names.append(args[3])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    removed = sandbox.reap_idle(state, ttl_seconds=24 * 3600)

    assert removed == ["sde-repo-stale"] and removed_names == ["sde-repo-stale"]
    final = json.loads(state.read_text())
    assert "sde-repo-fresh" in final          # recently used: kept
    assert "sde-repo-stale" not in final      # reaped
    assert "sde-repo-gone" not in final       # vanished out-of-band: forgotten
    assert "sde-repo-new" in final            # unknown container: clock started


def test_reap_idle_sweeps_legacy_task_containers(tmp_path, monkeypatch):
    removed_names = []

    def fake_run(args, **kw):
        if args[:2] == ["docker", "ps"] and f"label={sandbox.SANDBOX_LABEL}" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="sde-task-old1\n", stderr="")
        if args[:3] == ["docker", "rm", "-f"]:
            removed_names.append(args[3])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    removed = sandbox.reap_idle(tmp_path / "usage.json", ttl_seconds=10)
    assert removed == ["sde-task-old1"] and removed_names == ["sde-task-old1"]
