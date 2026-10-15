"""Move a legacy layer into the layer that replaced it (`p4n4 migrate edge`).

p4n4-edge's inference runner became p4n4-ai's `ei-runner` profile, and the
leftover repository became p4n4-extras, for optional services. A project's `edge`
layer goes where its services belong now:

- **ai**, when it runs the inference runner (an `ei-runner` service): the runner,
  its model directories, its `.env` values and its service move into the ai layer,
  and `ei-runner` joins the ai layer's COMPOSE_PROFILES. A project without an ai
  layer gets one, scaffolded from p4n4-ai with only the runner enabled.
- **extras**, when it runs other services (a template's own edge layer, e.g. video):
  the layer is renamed and keeps its Compose project name, so its volumes stay.

Nothing is deleted. Whatever the move replaces is kept in `edge.pre-migrate/` at the
project root, until the user checks the result and removes it.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from p4n4_lib import env as envutil
from p4n4_lib import layout, scaffold
from p4n4_lib import manifest as mf
from p4n4_lib.layers import LAYERS, RUNNER_SERVICE

LEGACY = "edge"
BACKUP_DIR = "edge.pre-migrate"
# What p4n4 init copied into an edge layer, plus the files a user adds next to them
EDGE_PATHS = (
    layout.COMPOSE_FILE,
    envutil.ENV_FILE,
    "docker-compose.override.yml",
    "runner",
    "edge-impulse",
    "onnx",
)
# Directories that move into the ai layer with the runner
RUNNER_DIRS = ("runner", "edge-impulse", "onnx")
# Per-layer settings that stay with each layer's own .env
_LAYER_KEYS = frozenset({"COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"})


class MigrateError(Exception):
    """Raised when a project can't be migrated automatically; nothing was changed."""


@dataclass
class Migration:
    """What `migrate_edge` did, for the caller to report."""

    target: str  # "ai" or "extras"
    target_dir: Path
    layers: list[str]  # the manifest's layers afterwards
    backup: Path | None = None  # edge.pre-migrate/, when anything was replaced
    scaffolded: bool = False  # the target layer was created from its stack repo
    services_added: list[str] = field(default_factory=list)
    env_added: list[str] = field(default_factory=list)
    # Keys both layers set to different values: the target's value is kept
    env_conflicts: dict[str, tuple[str, str]] = field(default_factory=dict)
    files_copied: list[str] = field(default_factory=list)
    profile_added: bool = False
    # Compose project name the extras layer keeps (its volumes are named after it)
    compose_project_name: str | None = None


def load_compose(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise MigrateError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise MigrateError(f"{path} is not a Compose file")
    return data


def target_of(edge_compose: dict) -> str:
    """Where an edge layer's services belong now: ai when it runs the inference runner."""
    return "ai" if RUNNER_SERVICE in (edge_compose.get("services") or {}) else "extras"


def _project_name(env: dict[str, str], directory: Path) -> str:
    """The Compose project name a stack runs under: COMPOSE_PROJECT_NAME, else its
    directory's name, normalized as Compose does."""
    name = env.get("COMPOSE_PROJECT_NAME") or directory.name
    return re.sub(r"[^a-z0-9_-]", "", name.lower()).lstrip("_-")


def _indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "".join(pad + line if line.strip() else line for line in text.splitlines(True))


def splice_services(target_compose: Path, edge: dict) -> list[str]:
    """
    Add the edge compose file's services and named volumes to `target_compose`,
    keeping the target's text (and comments) as it is. Returns the services added;
    services the target already defines are left alone.
    """
    text = target_compose.read_text(encoding="utf-8")
    target = load_compose(target_compose)
    have_services = target.get("services") or {}
    have_volumes = target.get("volumes") or {}
    have_networks = set(target.get("networks") or {})

    new_services = {
        name: svc for name, svc in (edge.get("services") or {}).items() if name not in have_services
    }
    if not new_services:
        return []
    for name, svc in new_services.items():
        nets = svc.get("networks") or []
        missing = [
            n for n in (nets if isinstance(nets, list) else list(nets)) if n not in have_networks
        ]
        if missing:
            raise MigrateError(
                f"{target_compose} has no network {', '.join(missing)}, which {name} joins: "
                "add it there, then migrate again"
            )
    new_volumes = {
        name: spec for name, spec in (edge.get("volumes") or {}).items() if name not in have_volumes
    }

    lines = text.splitlines(True)
    services_at = next(
        (i for i, line in enumerate(lines) if re.match(r"^services:\s*(#.*)?$", line)), None
    )
    if services_at is None:
        raise MigrateError(
            f"{target_compose}: no top-level `services:` block to add {', '.join(new_services)} to"
        )
    block = "\n  # Moved from the edge layer by `p4n4 migrate edge`\n" + _indent(
        yaml.safe_dump(new_services, sort_keys=False, width=100, allow_unicode=True)
    )
    lines.insert(services_at + 1, block)

    if new_volumes:
        entries = "".join(
            f"  {name}:\n" if not spec else _indent(yaml.safe_dump({name: spec}, sort_keys=False))
            for name, spec in new_volumes.items()
        )
        volumes_at = next(
            (i for i, line in enumerate(lines) if re.match(r"^volumes:\s*(#.*)?$", line)), None
        )
        if volumes_at is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append("\nvolumes:\n" + entries)
        else:
            lines.insert(volumes_at + 1, entries)

    result = "".join(lines)
    merged = yaml.safe_load(result) or {}
    services = merged.get("services") or {}
    volumes = merged.get("volumes") or {}
    if not (set(have_services) | set(new_services)) <= set(services) or not (
        set(have_volumes) | set(new_volumes)
    ) <= set(volumes):
        raise MigrateError(f"could not add the edge services to {target_compose} automatically")
    target_compose.write_text(result, encoding="utf-8", newline="\n")
    return list(new_services)


def merge_env(target_env: Path, edge_env: dict[str, str]) -> tuple[list[str], dict]:
    """Append the edge .env's keys the target lacks (comments and layout kept);
    report keys both set differently, keeping the target's value."""
    have = envutil.load(target_env) if target_env.exists() else {}
    added, conflicts = [], {}
    for key, value in edge_env.items():
        if key in _LAYER_KEYS:
            continue
        if key not in have:
            comment = None if added else "Moved from the edge layer's .env by `p4n4 migrate edge`"
            envutil.set_value(target_env, key, value, comment=comment)
            added.append(key)
        elif have[key] != value:
            conflicts[key] = (have[key], value)
    return added, conflicts


def _copy_missing(src: Path, dst: Path, rel: str) -> list[str]:
    """Copy src into dst: the whole tree when dst is absent, else only the files dst
    lacks (models), never overwriting. Returns the paths copied, relative to dst's parent."""
    if not src.exists():
        return []
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    if not dst.exists():
        shutil.copytree(src, dst, ignore=ignore)
        return [f"{rel}/"]
    copied = []
    for path in sorted(src.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or path.name == ".gitkeep":
            continue
        out = dst / path.relative_to(src)
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
            copied.append(f"{rel}/{path.relative_to(src).as_posix()}")
    return copied


def _wants_runner(edge_env: dict[str, str]) -> bool:
    """Whether the runner was meant to run: an edge .env without COMPOSE_PROFILES ran
    it, as does one that lists it."""
    if "COMPOSE_PROFILES" not in edge_env:
        return True
    return RUNNER_SERVICE in envutil.parse_profiles(edge_env["COMPOSE_PROFILES"])


def migrate_edge(
    project_dir: Path,
    source_ai: str | Path | None = None,
    new_env: dict[str, str] | None = None,
) -> Migration:
    """
    Move the project's legacy edge layer into ai or extras (see the module docstring).

    `source_ai` is a p4n4-ai checkout to scaffold from when the project has no ai layer
    (default: clone it), and `new_env` the values that new ai layer's .env gets on top of
    the edge layer's (its generated secrets). The edge layer's containers must be stopped.
    Raises MigrateError, with nothing changed, when it can't be done automatically.
    """
    manifest_path = project_dir / mf.MANIFEST_FILE
    data = mf.load(manifest_path)
    layers: list[str] = list(data.get("layers", []))
    if LEGACY not in layers:
        raise MigrateError("this project has no edge layer")

    flat = (project_dir / layout.COMPOSE_FILE).exists() and len(layers) == 1
    edge_dir = layout.layer_dir(project_dir, layers, LEGACY)
    edge_compose_path = edge_dir / layout.COMPOSE_FILE
    if not edge_compose_path.exists():
        raise MigrateError(f"no {edge_compose_path.relative_to(project_dir)} to migrate")
    edge_compose = load_compose(edge_compose_path)
    edge_env_path = edge_dir / envutil.ENV_FILE
    edge_env = envutil.load(edge_env_path) if edge_env_path.exists() else {}
    target = target_of(edge_compose)

    new_layers = layout.ordered([*(name for name in layers if name != LEGACY), target])
    target_exists = target in layers
    # Where the target layer is now; a project left with one layer is flattened below
    target_dir = layout.layer_dir(project_dir, layers if target_exists else new_layers, target)
    flatten = target_exists and len(new_layers) == 1 and target_dir != project_dir
    backup = project_dir / BACKUP_DIR
    if (target_exists or target == "ai") and backup.exists():
        raise MigrateError(f"{BACKUP_DIR}/ already exists: remove or rename it first")
    if not flat and not target_exists and target_dir.exists():
        raise MigrateError(f"{target_dir.relative_to(project_dir)}/ already exists")
    if target_exists and not (target_dir / layout.COMPOSE_FILE).exists():
        raise MigrateError(f"no {(target_dir / layout.COMPOSE_FILE).relative_to(project_dir)}")
    if flatten:
        clash = sorted(
            p.name
            for p in target_dir.iterdir()
            if (project_dir / p.name).exists() and p.name != LEGACY
        )
        if clash:
            raise MigrateError(
                f"the project root already has {', '.join(clash)}, which {target}/ would move "
                "there (a project with one layer keeps it at the root)"
            )

    result = Migration(target=target, target_dir=target_dir, layers=new_layers)

    if target == "extras" and not target_exists:
        # A rename: the services, files and Compose project (so the volumes) stay
        name = _project_name(edge_env, edge_dir)
        if not flat:
            edge_dir.rename(target_dir)
            envutil.set_value(
                target_dir / envutil.ENV_FILE,
                "COMPOSE_PROJECT_NAME",
                name,
                comment="Compose project name kept from the edge layer by `p4n4 migrate edge`,\n"
                "so this layer's containers keep their volumes",
            )
            result.compose_project_name = name
        result.services_added = list(edge_compose.get("services") or {})
    else:
        # Keep everything the move replaces in edge.pre-migrate/
        if flat:
            backup.mkdir()
            for rel in EDGE_PATHS:
                if (project_dir / rel).exists():
                    (project_dir / rel).rename(backup / rel)
        else:
            edge_dir.rename(backup)
        result.backup = backup
        backup_env = backup / envutil.ENV_FILE
        old_env = envutil.load(backup_env) if backup_env.exists() else {}
        # The target's files this edits, restored if the migration fails
        snapshot = {
            path: path.read_bytes()
            for path in (target_dir / layout.COMPOSE_FILE, target_dir / envutil.ENV_FILE)
            if target_exists and path.exists()
        }
        created = None if target_exists or flat else target_dir

        try:
            if target_exists:
                result.services_added = splice_services(
                    target_dir / layout.COMPOSE_FILE, load_compose(backup / layout.COMPOSE_FILE)
                )
                result.env_added, result.env_conflicts = merge_env(
                    target_dir / envutil.ENV_FILE, old_env
                )
            else:
                # The ai layer from p4n4-ai, its .env filled from the edge layer's
                values = {k: v for k, v in old_env.items() if k not in _LAYER_KEYS}
                values.update(new_env or {})
                values["COMPOSE_PROFILES"] = RUNNER_SERVICE if _wants_runner(old_env) else ""
                target_dir.mkdir(parents=True, exist_ok=True)
                scaffold.scaffold_layer(
                    target_dir,
                    LAYERS[target],
                    values,
                    source=source_ai,
                    compose_project_name=layout.compose_project_name(
                        project_dir.name, new_layers, target
                    ),
                )
                result.scaffolded = True
                result.services_added = [RUNNER_SERVICE]
                # Keys the stack's .env.example doesn't list (the user's own) come along too
                result.env_added, _ = merge_env(target_dir / envutil.ENV_FILE, old_env)
            if target == "ai":
                for rel in RUNNER_DIRS:
                    result.files_copied += _copy_missing(backup / rel, target_dir / rel, rel)
                if target_exists and _wants_runner(old_env):
                    env_path = target_dir / envutil.ENV_FILE
                    current = envutil.load(env_path).get("COMPOSE_PROFILES", "")
                    if RUNNER_SERVICE not in envutil.parse_profiles(current):
                        envutil.set_value(
                            env_path,
                            "COMPOSE_PROFILES",
                            envutil.add_profile(current, RUNNER_SERVICE),
                        )
                        result.profile_added = True
        except (MigrateError, scaffold.ScaffoldError, OSError) as exc:
            for path, content in snapshot.items():
                path.write_bytes(content)
            _restore(project_dir, backup, edge_dir, flat, created)
            raise MigrateError(str(exc)) from exc

    if flatten:
        # One layer left: its files go to the project root, as p4n4 init lays them out.
        # Its .env keeps COMPOSE_PROJECT_NAME, so its containers keep their volumes.
        for path in list(target_dir.iterdir()):
            path.rename(project_dir / path.name)
        target_dir.rmdir()
        result.target_dir = project_dir

    data["layers"] = new_layers
    mf.save(manifest_path, data)
    return result


def _restore(
    project_dir: Path, backup: Path, edge_dir: Path, flat: bool, created: Path | None
) -> None:
    """Undo a failed migration's moves: remove the layer it created, put the edge layer back."""
    if created is not None and created.exists():
        shutil.rmtree(created, ignore_errors=True)
    if flat:
        for rel in EDGE_PATHS:
            src = backup / rel
            if src.exists():
                dst = project_dir / rel
                if dst.is_dir():
                    shutil.rmtree(dst)
                elif dst.exists():
                    dst.unlink()
                src.rename(dst)
        shutil.rmtree(backup, ignore_errors=True)
    elif backup.exists():
        backup.rename(edge_dir)
