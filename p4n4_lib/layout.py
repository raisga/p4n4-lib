"""Project layout: where each layer's stack files live inside a project.

Single-layer projects keep stack files at the project root. Multi-layer
projects give each layer its own subdirectory so the stacks run as separate
Compose projects (p4n4-ai attaches to the p4n4-net network that p4n4-iot
creates, so their compose files must not be merged).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from p4n4_lib.layers import LAYERS

COMPOSE_FILE = "docker-compose.yml"


def ordered(names: Iterable[str]) -> list[str]:
    """Sort layer names into dependency order (iot before ai before edge)."""
    wanted = set(names)
    return [name for name in LAYERS if name in wanted]


def layer_dir(project_dir: Path, layers: Sequence[str], name: str) -> Path:
    """Directory a layer's stack files live in."""
    return project_dir / name if len(layers) > 1 else project_dir


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
