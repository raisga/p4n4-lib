"""Project layout: where each layer's stack files live inside a project.

Single-layer projects keep stack files at the project root. Multi-layer
projects give each layer its own subdirectory so the stacks run as separate
Compose projects (p4n4-ai attaches to the p4n4-net network that p4n4-iot
creates, so their compose files must not be merged).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from p4n4_lib.layers import LAYERS

COMPOSE_FILE = "docker-compose.yml"

# The network every stack joins. p4n4-iot creates it (with this subnet); the
# other stacks declare it external, so it must exist before they start.
NETWORK = "p4n4-net"
NETWORK_SUBNET = "172.20.0.0/16"


def ordered(names: Iterable[str]) -> list[str]:
    """Sort layer names into dependency order (iot before ai before edge)."""
    wanted = set(names)
    return [name for name in LAYERS if name in wanted]


def layer_dir(project_dir: Path, layers: Sequence[str], name: str) -> Path:
    """Directory a layer's stack files live in."""
    return project_dir / name if len(layers) > 1 else project_dir


def compose_project_name(project: str, layers: Sequence[str], name: str) -> str:
    """
    Compose project name for a layer: the project name for single-layer projects,
    `<project>-<layer>` for multi-layer ones. Without it, Compose names a layer after
    its directory (`iot`, `ai`, ...), so every multi-layer project on a host would
    share the same volumes. Lowercased, with characters Compose rejects replaced by `-`.
    """
    raw = project if len(layers) <= 1 else f"{project}-{name}"
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).lstrip("-_")
    return cleaned or "p4n4"


def compose_dirs(project_dir: Path, layers: Sequence[str]) -> list[tuple[str, Path]]:
    """
    Return (layer, directory) pairs that contain a compose file, in
    dependency order. Single-layer projects resolve to the project root.
    """
    if (project_dir / COMPOSE_FILE).exists():
        names = ordered(layers)
        return [(names[0] if names else "", project_dir)]
    return [
        (name, project_dir / name)
        for name in ordered(layers)
        if (project_dir / name / COMPOSE_FILE).exists()
    ]
