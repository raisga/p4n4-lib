"""Manifest (.p4n4.json) read/write utilities."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_FILE = ".p4n4.json"
SCHEMA_VERSION = 1


def find(start: Path | None = None) -> Path | None:
    """Walk up from start (default cwd) to find .p4n4.json."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / MANIFEST_FILE
        if candidate.exists():
            return candidate
    return None


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def create(project: str, layers: list[str]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "layers": layers,
        "created_at": datetime.now(UTC).isoformat(),
    }
