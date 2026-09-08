"""Thin wrapper over the Docker CLI.

Shelling out rather than using the SDK keeps the dependency list short and
makes every operation reproducible by hand when something goes wrong on the
server — which matters when the person debugging at 6pm is you, alone.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import config

LogSink = Callable[[str], None]


class DockerError(RuntimeError):
    """A docker command failed."""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


def _stream(cmd: list[str], sink: LogSink, timeout: int) -> int:
    """Run a command, forwarding combined output to `sink` line by line."""
    sink(f"$ {' '.join(cmd)}\n")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sink(line)
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        sink(f"\n[launcher] Timed out after {timeout} seconds.\n")
        return 124
    finally:
        if proc.stdout:
            proc.stdout.close()


def _run(cmd: list[str], timeout: int = 60) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise DockerError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def build_image(context: Path, tag: str, sink: LogSink) -> int:
    """Build the app image. Returns the docker exit code."""
    return _stream(
        ["docker", "build", "--tag", tag, "--progress", "plain", str(context)],
        sink,
        config.BUILD_TIMEOUT_SECONDS,
    )


def run_container(tag: str, name: str, host_port: int) -> str:
    """Start the app, published on `host_port`, and return the container id.

    The container-internal port is always 80 (nginx); it can be the same for
    every app because each container has its own network namespace.
    """
    container_id = _run([
        "docker", "run", "--detach",
        "--name", name,
        "--restart", "unless-stopped",
        "--publish", f"{host_port}:{config.INTERNAL_HTTP_PORT}",
        "--memory", config.APP_MEMORY_LIMIT,
        "--cpus", config.APP_CPU_LIMIT,
        # Defensive defaults: these apps are uploaded by anyone on the LAN.
        "--security-opt", "no-new-privileges",
        "--pids-limit", "512",
        "--label", "applauncher=1",
        "--label", f"applauncher.app={name}",
        tag,
    ])
    return container_id


def stop_container(container_id: str, remove: bool = True) -> None:
    if not container_id:
        return
    try:
        _run(["docker", "stop", "--time", "10", container_id], timeout=60)
    except DockerError:
        pass  # already stopped or gone
    if remove:
        try:
            _run(["docker", "rm", "--force", container_id])
        except DockerError:
            pass


def remove_container_by_name(name: str) -> None:
    try:
        _run(["docker", "rm", "--force", name])
    except DockerError:
        pass


def container_state(container_id: str) -> str:
    """One of: running, exited, restarting, missing."""
    if not container_id:
        return "missing"
    try:
        return _run(["docker", "inspect", "-f", "{{.State.Status}}", container_id])
    except DockerError:
        return "missing"


def container_logs(container_id: str, tail: int = 200) -> str:
    if not container_id:
        return ""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container_id],
            capture_output=True, text=True, timeout=30,
        )
        return (result.stdout or "") + (result.stderr or "")
    except subprocess.SubprocessError:
        return ""


def listening_ports(container_id: str) -> list[int]:
    """Ports the app is actually listening on inside its container.

    Used to catch the case where generated code ignored our instructions and
    bound 5000 or 8080 instead of 8000, or bound 127.0.0.1 only.
    """
    probe = (
        "ss -tlnH 2>/dev/null || netstat -tln 2>/dev/null || true"
    )
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "sh", "-c", probe],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.SubprocessError:
        return []

    ports: set[int] = set()
    for line in result.stdout.splitlines():
        for field in line.split():
            if ":" in field and field.rsplit(":", 1)[-1].isdigit():
                ports.add(int(field.rsplit(":", 1)[-1]))
    return sorted(ports)


def remove_image(tag: str) -> None:
    try:
        _run(["docker", "rmi", "--force", tag], timeout=120)
    except DockerError:
        pass


def prune_images() -> str:
    """Reclaim disk from dangling images. Safe to run on a schedule."""
    try:
        return _run(["docker", "image", "prune", "--force"], timeout=300)
    except DockerError as exc:
        return str(exc)
