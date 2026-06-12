"""Per-task container sandboxing. Arbitrary shell commands (the dangerous
surface — building, testing, and running untrusted repo/LLM-authored code) run
inside a disposable Docker container with the workspace bind-mounted and an
egress policy applied. The controlled deepagents file tools (read/write/edit)
keep operating on the host workspace, which the container sees through the mount.

When a repo opts into sandboxing but Docker is unavailable, the task fails with
a clear error rather than silently falling back to host execution — that would
defeat the security guarantee."""

from __future__ import annotations

import shutil
import subprocess

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse


class SandboxError(RuntimeError):
    pass


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def container_name(task_id: str) -> str:
    return f"sde-task-{task_id}"


def start_container(task_id: str, workspace_path: str, *, image: str,
                    network: str, memory: str, cpus: str) -> str:
    """Start a long-lived task container with the workspace mounted at /workspace.
    Returns the container name. Raises SandboxError on failure."""
    name = container_name(task_id)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # clear any stale one
    if network not in ("none", "bridge"):
        network = "none"
    cmd = [
        "docker", "run", "-d", "--name", name,
        "--network", network,
        "--memory", memory, "--cpus", cpus,
        "--pids-limit", "512",
        "-v", f"{workspace_path}:/workspace",
        "-w", "/workspace",
        image, "sleep", "infinity",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SandboxError(f"could not start sandbox container ({image}): "
                           f"{r.stderr.strip()[:300]}")
    return name


def stop_container(task_id: str) -> None:
    subprocess.run(["docker", "rm", "-f", container_name(task_id)], capture_output=True)


def exec_in_container(name: str, command: str, *, timeout: int,
                      max_output_bytes: int = 60000) -> ExecuteResponse:
    """Run a shell command inside the container, returning a deepagents
    ExecuteResponse (same shape LocalShellBackend produces)."""
    try:
        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", name, "bash", "-lc", command],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ExecuteResponse(
            output=f"Error: Command timed out after {timeout} seconds.",
            exit_code=124, truncated=False)
    except Exception as e:  # noqa: BLE001
        return ExecuteResponse(
            output=f"Error executing command in sandbox ({type(e).__name__}): {e}",
            exit_code=1, truncated=False)

    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.extend(f"[stderr] {ln}" for ln in result.stderr.strip().split("\n"))
    output = "\n".join(parts) if parts else "<no output>"
    truncated = len(output) > max_output_bytes
    if truncated:
        output = output[:max_output_bytes] + f"\n\n... Output truncated at {max_output_bytes} bytes."
    if result.returncode != 0:
        output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
    return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)


class DockerShellBackend(LocalShellBackend):
    """LocalShellBackend whose `execute` runs inside a per-task container.
    Filesystem operations are inherited (host-side on the mounted workspace)."""

    def __init__(self, root_dir, container: str, *, timeout: int = 600,
                 max_output_bytes: int = 60000) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True,
                         timeout=timeout, max_output_bytes=max_output_bytes)
        self._container = container

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(output="Error: Command must be a non-empty string.",
                                   exit_code=1, truncated=False)
        return exec_in_container(
            self._container, command,
            timeout=timeout or self._default_timeout,
            max_output_bytes=self._max_output_bytes)
