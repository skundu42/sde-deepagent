"""Per-task container sandbox: command construction, output parsing, and the
backend that routes execute() into the container. Docker itself is mocked here;
a live container lifecycle check runs separately."""

import subprocess
from types import SimpleNamespace

from sde_deepagent import sandbox


def test_container_name():
    assert sandbox.container_name("abc123") == "sde-task-abc123"


def test_exec_in_container_builds_docker_exec(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stdout="hello\n", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    res = sandbox.exec_in_container("sde-task-x", "echo hello", timeout=30)
    assert captured["args"][:5] == ["docker", "exec", "-w", "/workspace", "sde-task-x"]
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
        calls["c"] = (name, cmd)
        return sandbox.ExecuteResponse(output="ok", exit_code=0, truncated=False)

    monkeypatch.setattr(sandbox, "exec_in_container", fake_exec)
    backend = sandbox.DockerShellBackend(tmp_path, "sde-task-9")
    res = backend.execute("pytest -q")
    assert calls["c"] == ("sde-task-9", "pytest -q")
    assert res.output == "ok"
    # empty command guarded, never reaches the container
    assert backend.execute("").exit_code == 1


def test_start_container_command(monkeypatch):
    seen = []
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda args, **k: seen.append(args) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    name = sandbox.start_container("t1", "data/workspaces/t1/repo",
                                   image="python:3.12-slim",
                                   network="none", memory="2g", cpus="2")
    assert name == "sde-task-t1"
    run_cmd = seen[-1]
    assert "--network" in run_cmd and run_cmd[run_cmd.index("--network") + 1] == "none"
    # the bind mount must be an ABSOLUTE path (relative reads as a named volume)
    mount = run_cmd[run_cmd.index("-v") + 1]
    assert mount.startswith("/") and mount.endswith(":/workspace")
    assert "python:3.12-slim" in run_cmd


def test_start_container_rejects_bad_network(monkeypatch):
    seen = []
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda args, **k: seen.append(args) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    sandbox.start_container("t2", "/ws", image="i", network="host",  # disallowed
                            memory="1g", cpus="1")
    run_cmd = seen[-1]
    assert run_cmd[run_cmd.index("--network") + 1] == "none"  # forced safe
