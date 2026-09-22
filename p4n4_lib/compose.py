"""Docker Compose subprocess wrappers.

Prefers the Compose v2 plugin (``docker compose``) and falls back to the
standalone ``docker-compose`` binary. Hosts such as NVIDIA Jetson (JetPack
ships Ubuntu's ``docker.io`` without the plugin) often only have the latter;
there ``docker compose up -d`` fails with "unknown shorthand flag: 'd' in -d".
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path


class ComposeNotFoundError(RuntimeError):
    """Neither `docker compose` nor `docker-compose` is available."""


@functools.cache
def compose_cmd() -> tuple[str, ...]:
    """Return the Compose invocation prefix for this host."""
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return ("docker", "compose")
    if shutil.which("docker-compose"):
        return ("docker-compose",)
    raise ComposeNotFoundError(
        "Docker Compose not found. Install the Compose v2 plugin "
        "(e.g. `sudo apt install docker-compose-plugin`) or the standalone "
        "`docker-compose` binary."
    )


def _is_legacy() -> bool:
    return compose_cmd() == ("docker-compose",)


def _base(args: list[str], cwd: Path) -> int:
    result = subprocess.run([*compose_cmd(), *args], cwd=cwd, check=False)
    return result.returncode


def up(cwd: Path, build: bool = False, pull: bool = False, detach: bool = True) -> int:
    args = ["up"]
    if detach:
        args.append("-d")
    if build:
        args.append("--build")
    if pull:
        if _is_legacy():
            # docker-compose v1 has no `up --pull`; pull explicitly first
            rc = _base(["pull"], cwd)
            if rc != 0:
                return rc
        else:
            args.append("--pull=always")
    return _base(args, cwd)


def down(cwd: Path, volumes: bool = False) -> int:
    args = ["down"]
    if volumes:
        args.append("-v")
    return _base(args, cwd)


def ps(cwd: Path) -> list[dict]:
    """Return parsed service list from `docker compose ps --format json`."""
    if _is_legacy():
        return _ps_legacy(cwd)
    result = subprocess.run(
        [*compose_cmd(), "ps", "--format", "json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    services = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, list):
                services.extend(obj)
            else:
                services.append(obj)
        except json.JSONDecodeError:
            continue
    return services


def _ps_legacy(cwd: Path) -> list[dict]:
    """docker-compose v1 has no JSON output; build v2-shaped dicts via `docker inspect`."""
    ids = subprocess.run(
        ["docker-compose", "ps", "-q"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if not ids:
        return []
    result = subprocess.run(
        ["docker", "inspect", *ids],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        containers = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    services = []
    for c in containers:
        state = c.get("State") or {}
        labels = (c.get("Config") or {}).get("Labels") or {}
        publishers = []
        for key, bindings in ((c.get("NetworkSettings") or {}).get("Ports") or {}).items():
            target, _, proto = key.partition("/")
            for b in bindings or []:
                publishers.append(
                    {
                        "PublishedPort": int(b.get("HostPort") or 0),
                        "TargetPort": int(target),
                        "Protocol": proto or "tcp",
                    }
                )
        services.append(
            {
                "Name": c.get("Name", "").lstrip("/"),
                "Service": labels.get("com.docker.compose.service", ""),
                "State": state.get("Status", ""),
                "Health": (state.get("Health") or {}).get("Status", ""),
                "Publishers": publishers,
            }
        )
    return services


def logs(
    cwd: Path,
    service: str | None = None,
    tail: int | None = None,
    follow: bool = True,
) -> int:
    args = ["logs"]
    if follow:
        args.append("-f")
    if tail is not None:
        args.extend(["--tail", str(tail)])
    if service:
        args.append(service)
    return _base(args, cwd)
