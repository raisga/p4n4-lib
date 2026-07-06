"""Docker Compose subprocess wrappers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _base(args: list[str], cwd: Path) -> int:
    result = subprocess.run(["docker", "compose", *args], cwd=cwd, check=False)
    return result.returncode


def up(cwd: Path, build: bool = False, pull: bool = False, detach: bool = True) -> int:
    args = ["up"]
    if detach:
        args.append("-d")
    if build:
        args.append("--build")
    if pull:
        args.append("--pull=always")
    return _base(args, cwd)


def down(cwd: Path, volumes: bool = False) -> int:
    args = ["down"]
    if volumes:
        args.append("-v")
    return _base(args, cwd)


def ps(cwd: Path) -> list[dict]:
    """Return parsed service list from `docker compose ps --format json`."""
    result = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
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
