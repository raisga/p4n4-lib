"""Docker Compose subprocess wrappers.

Prefers the Compose v2 plugin (``docker compose``) and falls back to the
standalone ``docker-compose`` binary. Hosts such as NVIDIA Jetson (JetPack
ships Ubuntu's ``docker.io`` without the plugin) often only have the latter;
there ``docker compose up -d`` fails with "unknown shorthand flag: 'd' in -d".
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml

from p4n4_lib import env as envutil


class ComposeNotFoundError(RuntimeError):
    """Neither `docker compose` nor `docker-compose` is available."""


class DockerError(RuntimeError):
    """The docker CLI is missing, or the daemon refused a request."""


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
    # Stack services sit in Compose profiles, and `down` only stops the ones
    # in active profiles; enable them all so services started by name (or
    # dropped from COMPOSE_PROFILES since) are stopped too. docker-compose v1
    # has no "*" profile.
    args = ["down"] if _is_legacy() else ["--profile", "*", "down"]
    if volumes:
        args.append("-v")
    return _base(args, cwd)


def _ps_run(cmd: list[str], cwd: Path) -> str:
    """stdout of a `ps` command; raises DockerError when it fails, so a daemon
    that's down or an invalid compose file doesn't look like an empty stack."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exited with {result.returncode}"
        raise DockerError(f"`{' '.join(cmd)}` failed: {detail}")
    return result.stdout


def ensure_network(name: str, subnet: str) -> bool:
    """
    Create the shared bridge network `name` when it doesn't exist; True if it was
    created. Stacks that declare it external can't start without it. It carries the
    label Compose gives a network it creates, so a stack that declares it itself
    (p4n4-iot) adopts it instead of refusing it ("incorrect label").
    """
    try:
        inspect = subprocess.run(
            ["docker", "network", "inspect", name], capture_output=True, text=True, check=False
        )
        if inspect.returncode == 0:
            return False
        create = subprocess.run(
            [
                *("docker", "network", "create", "--driver", "bridge", "--subnet", subnet),
                *("--label", f"com.docker.compose.network={name}", name),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerError("Docker not found: install it, or add `docker` to PATH.") from exc
    if create.returncode != 0:
        detail = (create.stderr or "").strip() or f"exited with {create.returncode}"
        raise DockerError(f"Could not create the {name} network: {detail}")
    return True


def ps(cwd: Path) -> list[dict]:
    """
    Parsed service list from `docker compose ps --all --format json`. --all keeps
    stopped containers, so a crashed service shows as exited, with its exit code,
    instead of disappearing. Raises DockerError when the command fails.
    """
    if _is_legacy():
        return _ps_legacy(cwd)
    stdout = _ps_run([*compose_cmd(), "ps", "--all", "--format", "json"], cwd)
    services = []
    for line in stdout.splitlines():
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
    """docker-compose v1 has no JSON output; build v2-shaped dicts via `docker inspect`.
    v1's `ps` lists stopped containers too."""
    ids = _ps_run(["docker-compose", "ps", "-q"], cwd).split()
    services = []
    for c in _inspect("container", ids):
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
                "ID": c.get("Id", ""),
                "Name": c.get("Name", "").lstrip("/"),
                "Service": labels.get("com.docker.compose.service", ""),
                "State": state.get("Status", ""),
                "Health": (state.get("Health") or {}).get("Status", ""),
                "ExitCode": state.get("ExitCode", 0),
                "Publishers": publishers,
            }
        )
    return services


@dataclass(frozen=True)
class NameConflict:
    """A container name the stack in `cwd` sets that another container already holds."""

    name: str
    state: str
    project: str | None  # Compose project of the holder; None if not started by Compose
    working_dir: Path | None


def _inspect(kind: str, ids: list[str]) -> list[dict]:
    """`docker <kind> inspect`; exits non-zero when any id is missing, but still prints the rest."""
    if not ids:
        return []
    result = subprocess.run(
        ["docker", kind, "inspect", *ids],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _project_name(cwd: Path, config: dict) -> str:
    # Compose v2 resolves the name into the config; v1 leaves it out
    if config.get("name"):
        return config["name"]
    # Like v1's get_project_name: the shell's COMPOSE_PROJECT_NAME beats the .env one,
    # then the directory name; v1 normalizes whichever it picks the same way.
    env_path = cwd / envutil.ENV_FILE
    env = envutil.load(env_path) if env_path.exists() else {}
    name = os.environ.get("COMPOSE_PROJECT_NAME") or env.get("COMPOSE_PROJECT_NAME") or cwd.name
    return re.sub(r"[^-_a-z0-9]", "", name.lower())


def _stack_container_names(cwd: Path) -> tuple[str, list[str]]:
    """
    The Compose project of the stack in `cwd` and the `container_name`s it sets in its
    active profiles; no names when its config can't be read.
    """
    result = subprocess.run(
        [*compose_cmd(), "config"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "", []
    try:
        config = yaml.safe_load(result.stdout) or {}
    except yaml.YAMLError:
        return "", []
    names = [
        svc["container_name"]
        for svc in (config.get("services") or {}).values()
        if isinstance(svc, dict) and svc.get("container_name")
    ]
    return _project_name(cwd, config), names


def name_conflicts(*dirs: Path) -> list[NameConflict]:
    """
    Containers that would make `up` fail with "container name already in use" for the
    stacks in `dirs`: ones holding a `container_name` a stack sets (in its active
    profiles) that belong to a different Compose project, or to none. A stack whose
    config can't be read is skipped; `up` then reports whatever is wrong itself.

    The configs render concurrently and every name is inspected in one call, so a
    multi-layer project takes about as long to check as one stack.
    """
    compose_cmd()  # detect Compose once, before the threads look it up
    with ThreadPoolExecutor() as pool:
        stacks = list(pool.map(_stack_container_names, dirs))
    project_of = {name: project for project, names in stacks for name in names}
    if not project_of:
        return []

    conflicts = []
    for c in _inspect("container", list(project_of)):
        name = c.get("Name", "").lstrip("/")
        labels = (c.get("Config") or {}).get("Labels") or {}
        owner = labels.get("com.docker.compose.project")
        if owner == project_of.get(name):
            continue
        working_dir = labels.get("com.docker.compose.project.working_dir")
        conflicts.append(
            NameConflict(
                name=name,
                state=(c.get("State") or {}).get("Status", ""),
                project=owner,
                working_dir=Path(working_dir) if working_dir else None,
            )
        )
    return conflicts


CONTAINER_PREFIX = "p4n4-"


@dataclass(frozen=True)
class HostProject:
    """A Compose project on this host that runs p4n4 containers."""

    name: str | None  # None: p4n4 containers that Compose didn't start
    working_dir: Path | None
    containers: tuple[tuple[str, str], ...]  # (name, state) of its p4n4 containers


def host_projects() -> list[HostProject]:
    """
    Every project on this host with a container named p4n4-*, from any directory,
    ordered for shutdown: a project that owns a network others' containers join
    (iot's p4n4-net) comes after them, so its network can be removed.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"name=^{CONTAINER_PREFIX}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerError("Docker not found: install it, or add `docker` to PATH.") from exc
    # A stopped daemon or a missing permission must not read as "nothing running"
    if result.returncode != 0:
        raise DockerError((result.stderr or "").strip() or "`docker ps` failed.")
    ids = result.stdout.split()

    containers: dict[str | None, list[tuple[str, str]]] = {}
    working_dirs: dict[str | None, Path | None] = {}
    joined: dict[str | None, set[str]] = {}
    for c in _inspect("container", ids):
        labels = (c.get("Config") or {}).get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        name = c.get("Name", "").lstrip("/")
        state = (c.get("State") or {}).get("Status", "")
        containers.setdefault(project, []).append((name, state))
        wd = labels.get("com.docker.compose.project.working_dir")
        working_dirs.setdefault(project, Path(wd) if wd else None)
        networks = ((c.get("NetworkSettings") or {}).get("Networks") or {}).keys()
        joined.setdefault(project, set()).update(networks)

    every_network = sorted(set().union(*joined.values())) if joined else []
    network_owner = {
        n.get("Name"): ((n.get("Labels") or {}).get("com.docker.compose.project"))
        for n in _inspect("network", every_network)
    }
    hosts_others = {
        owner
        for project, networks in joined.items()
        for network in networks
        if (owner := network_owner.get(network)) and owner != project
    }

    projects = [
        HostProject(name, working_dirs[name], tuple(sorted(found)))
        for name, found in containers.items()
    ]
    return sorted(projects, key=lambda p: (p.name in hosts_others, p.name or ""))


def down_project(project: HostProject, volumes: bool = False) -> int:
    """Stop and remove `project` by name, whether or not its compose files still exist."""
    # docker-compose v1 can't act on a project without its compose files
    files_gone = project.working_dir is None or not project.working_dir.is_dir()
    if project.name is None or (_is_legacy() and files_gone):
        names = [name for name, _ in project.containers]
        return subprocess.run(["docker", "rm", "-f", *names], check=False).returncode
    if _is_legacy():
        return down(project.working_dir, volumes=volumes)
    args = ["-p", project.name, "down"]
    if volumes:
        args.append("-v")
    # By name alone Compose finds the project's containers, including those of every
    # profile, through their labels. Run it from an empty directory so no compose
    # file there gets loaded in place of the project's own.
    with tempfile.TemporaryDirectory() as empty:
        return _base(args, Path(empty))


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
