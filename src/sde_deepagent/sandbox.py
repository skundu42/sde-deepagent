"""Per-repo container sandboxing. Arbitrary shell commands (the dangerous
surface — building, testing, and running untrusted repo/LLM-authored code) run
inside a Docker container with the repo's workspaces bind-mounted and an egress
policy applied. The controlled deepagents file tools (read/write/edit) keep
operating on the host workspace, which the container sees through the mount.

Each repo gets ONE container (`sde-repo-<slug>`), reused across tasks so
installed toolchains and dependency caches survive between runs. The container
mounts the repo's workspace parent (`workspaces/<repo_slug>`) at /workspaces;
each task's commands execute with the working directory pinned to its own
subtree. A reaper removes containers that have been idle past the TTL
(SANDBOX_IDLE_HOURS, default 7 days since the last task touched them).

Zero-config environments: the default image is a generic Debian build image
(buildpack-deps:bookworm — git, gcc, make, curl, common dev libraries),
deliberately language-agnostic. The agent bootstraps the rest itself — its
prompt tells it to install whatever toolchain/dependencies the repo needs
(hence the default egress policy is `bridge`), and container reuse makes that
a one-off cost per repo. A repo's optional `setup` command still runs first,
and `sandbox_image` (or SANDBOX_IMAGE) pins a stack-specific image instead.

When a repo opts into sandboxing but Docker is unavailable, the task fails with
a clear error rather than silently falling back to host execution — that would
defeat the security guarantee."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from .config import repo_slug

logger = logging.getLogger(__name__)

SANDBOX_LABEL = "sde-deepagent.sandbox"
CFG_LABEL = "sde-deepagent.cfg"


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


def container_name(repo_name: str) -> str:
    return f"sde-repo-{repo_slug(repo_name)}"


# ---- container lifecycle ----------------------------------------------------


def _cfg_hash(image: str, network: str, memory: str, cpus: str, mount: str) -> str:
    raw = f"{image}|{network}|{memory}|{cpus}|{mount}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _container_state(name: str) -> tuple[bool, str] | None:
    """(running, cfg-label) for an existing container, or None if absent."""
    r = subprocess.run(
        ["docker", "inspect", "-f",
         '{{.State.Running}}\t{{index .Config.Labels "' + CFG_LABEL + '"}}', name],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    running, _, cfg = r.stdout.strip().partition("\t")
    return running == "true", cfg


def _path_visible(name: str, container_path: str) -> bool:
    """True if `container_path` exists inside the container. Guards against a
    stale bind mount: if the host directory was deleted and recreated while the
    container kept running, the container still sees the old (dead) inode."""
    r = subprocess.run(["docker", "exec", name, "test", "-d", container_path],
                       capture_output=True, timeout=30)
    return r.returncode == 0


def ensure_container(repo_name: str, mount_dir: str, *, image: str,
                     network: str, memory: str, cpus: str,
                     probe_subdir: str | None = None) -> tuple[str, bool]:
    """Reuse the repo's sandbox container, or (re)create it. The repo's
    workspace parent `mount_dir` is mounted at /workspaces. Returns
    (container name, created) — created is False when an existing container
    was reused. Raises SandboxError on failure.

    The container is recreated when its recorded config (image, network,
    limits, mount path) differs from what's requested, or when the mount has
    gone stale (probe_subdir no longer visible inside)."""
    name = container_name(repo_name)
    if network not in ("none", "bridge"):
        network = "none"
    # docker -v needs an absolute host path, else it's read as a named volume
    abs_mount = str(Path(mount_dir).resolve())
    cfg = _cfg_hash(image, network, memory, cpus, abs_mount)

    state = _container_state(name)
    if state is not None and state[1] == cfg:
        running = state[0]
        if not running:
            r = subprocess.run(["docker", "start", name], capture_output=True,
                               text=True, timeout=60)
            running = r.returncode == 0
        if running and (probe_subdir is None
                        or _path_visible(name, f"/workspaces/{probe_subdir}")):
            return name, False
        # unstartable or stale mount — fall through and recreate

    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    cmd = [
        "docker", "run", "-d", "--name", name,
        "--label", f"{SANDBOX_LABEL}=1",
        "--label", f"{CFG_LABEL}={cfg}",
        "--network", network,
        "--memory", memory, "--cpus", cpus,
        "--pids-limit", "512",
        "-v", f"{abs_mount}:/workspaces",
        "-w", "/workspaces",
        image, "sleep", "infinity",
    ]
    # generous timeout: the image may need to be pulled on first use
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        # a concurrent task on the same repo may have won the create race
        state = _container_state(name)
        if state is not None and state[0] and state[1] == cfg:
            return name, False
        raise SandboxError(f"could not start sandbox container ({image}): "
                           f"{r.stderr.strip()[:300]}")
    return name, True


def remove_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ---- idle-TTL tracking and reaping ----------------------------------------


# Guards the state-file read-modify-write cycle. mark_used() runs on the event
# loop while reap_idle() runs in a worker thread (asyncio.to_thread), so without
# this lock their interleaved load/save would lose container timestamps —
# leaking containers or reaping them early.
_STATE_LOCK = threading.Lock()


def _load_state(state_file: Path) -> dict[str, float]:
    try:
        return {str(k): float(v)
                for k, v in json.loads(state_file.read_text()).items()}
    except Exception:  # noqa: BLE001 — missing or corrupt file: start fresh
        return {}


def _save_state(state_file: Path, state: dict[str, float]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_file)


def mark_used(name: str, state_file: Path) -> None:
    """Stamp the container's idle clock (called at task start and end)."""
    with _STATE_LOCK:
        state = _load_state(state_file)
        state[name] = time.time()
        _save_state(state_file, state)


def reap_idle(state_file: Path, ttl_seconds: float) -> list[str]:
    """Remove sandbox containers idle longer than the TTL. A container found
    without a recorded last-use (e.g. after a state-file loss) gets a fresh
    clock rather than being killed. Also clears any legacy per-task containers
    (sde-task-*) left behind by crashes of older versions."""
    r = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label={SANDBOX_LABEL}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return []
    names = [n for n in r.stdout.strip().split("\n") if n]

    now = time.time()
    # Decide what to reap under the lock (a quick state read-modify-write), then
    # run the slow `docker rm` calls outside it so mark_used() isn't blocked.
    to_remove: list[str] = []
    with _STATE_LOCK:
        state = _load_state(state_file)
        for name in names:
            last = state.get(name)
            if last is None:
                state[name] = now
            elif now - last > ttl_seconds:
                to_remove.append(name)
                state.pop(name, None)
        for gone in set(state) - set(names):  # containers removed out-of-band
            state.pop(gone, None)
        _save_state(state_file, state)

    removed = []
    for name in to_remove:
        remove_container(name)
        removed.append(name)

    legacy = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=sde-task-",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30)
    if legacy.returncode == 0:
        for name in (n for n in legacy.stdout.strip().split("\n") if n):
            remove_container(name)
            removed.append(name)
    return removed


class SandboxReaper:
    """Background loop that reaps idle sandbox containers."""

    def __init__(self, state_file: Path, ttl_seconds: float,
                 interval_seconds: float = 1800,
                 initial_delay_seconds: float = 60) -> None:
        self.state_file = state_file
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        # first pass is delayed so short-lived processes (tests, --help runs)
        # never touch the Docker daemon
        self.initial_delay_seconds = initial_delay_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="sandbox-reaper")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(self.initial_delay_seconds)
        while True:
            try:
                if await asyncio.to_thread(docker_available):
                    removed = await asyncio.to_thread(
                        reap_idle, self.state_file, self.ttl_seconds)
                    if removed:
                        logger.info("reaped %d idle sandbox container(s): %s",
                                    len(removed), ", ".join(removed))
            except Exception:  # noqa: BLE001 — housekeeping must never die
                logger.exception("sandbox reaper pass failed")
            await asyncio.sleep(self.interval_seconds)


# ---- command execution ------------------------------------------------------


def exec_in_container(name: str, command: str, *, timeout: int,
                      workdir: str = "/workspaces",
                      max_output_bytes: int = 60000,
                      secrets: dict[str, str] | None = None) -> ExecuteResponse:
    """Run a shell command inside the container, returning a deepagents
    ExecuteResponse (same shape LocalShellBackend produces).

    `secrets` (NAME->value) are forwarded into the container as environment
    variables. Only the NAMES appear in the argv (`docker exec --env NAME`); the
    values are passed through the docker CLI's own environment, so they never
    land in the process list or on disk — the same ephemeral pattern gitops uses
    for git credentials. Used only for controller-run setup/test commands, never
    for the agent's own shell."""
    cmd = ["docker", "exec", "-w", workdir]
    run_env = None
    if secrets:
        for key in secrets:
            cmd += ["--env", key]
        # docker reads each `--env NAME` value from its own environment
        run_env = {**os.environ, **secrets}
    cmd += [name, "bash", "-lc", command]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=run_env,
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


Redact = Callable[[str], str]


def _redact_response(resp: ExecuteResponse, redact: Redact | None) -> ExecuteResponse:
    if redact is None or not isinstance(getattr(resp, "output", None), str):
        return resp
    return ExecuteResponse(output=redact(resp.output),
                           exit_code=resp.exit_code, truncated=resp.truncated)


def _redact_text(text, redact: Redact | None):
    return redact(text) if (redact is not None and isinstance(text, str)) else text


class DockerShellBackend(LocalShellBackend):
    """LocalShellBackend whose `execute` runs inside the repo's container,
    pinned to this task's workspace subdir. Filesystem operations are
    inherited (host-side on the mounted workspace).

    The agent's shell never receives any repo secret (only the controller-run
    setup/test path does). `redact` masks any secret value that surfaces through
    command output or a file read — covering, e.g., test code that writes a
    secret to the workspace which the agent then reads back."""

    def __init__(self, root_dir, container: str, *, workdir: str = "/workspaces",
                 timeout: int = 600, max_output_bytes: int = 60000,
                 redact: Redact | None = None) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True,
                         timeout=timeout, max_output_bytes=max_output_bytes)
        self._container = container
        self._workdir = workdir
        self._redact = redact

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(output="Error: Command must be a non-empty string.",
                                   exit_code=1, truncated=False)
        resp = exec_in_container(
            self._container, command,
            timeout=timeout or self._default_timeout,
            workdir=self._workdir,
            max_output_bytes=self._max_output_bytes)
        return _redact_response(resp, self._redact)

    def read(self, *args, **kwargs):
        return _redact_text(super().read(*args, **kwargs), self._redact)

    def grep(self, *args, **kwargs):
        return _redact_text(super().grep(*args, **kwargs), self._redact)


class RedactingLocalShellBackend(LocalShellBackend):
    """Host-execution backend (no sandbox) that redacts secret values from
    command output and file reads before they reach the model — the non-sandbox
    counterpart to DockerShellBackend's redaction. The async tool entrypoints
    (aexecute/aread/agrep) delegate to these sync methods via asyncio.to_thread,
    so overriding the sync methods covers every path."""

    def __init__(self, *args, redact: Redact | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._redact = redact

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return _redact_response(super().execute(command, timeout=timeout), self._redact)

    def read(self, *args, **kwargs):
        return _redact_text(super().read(*args, **kwargs), self._redact)

    def grep(self, *args, **kwargs):
        return _redact_text(super().grep(*args, **kwargs), self._redact)
