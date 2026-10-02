"""Manifest (.p4n4.json) read/write utilities."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MANIFEST_FILE = ".p4n4.json"
SCHEMA_VERSION = 1

# Tabs p4n4-dashboard can show; the optional "dashboard" block narrows them
DASHBOARD_TABS = ("services", "edge", "agent", "grafana", "video")


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


def dashboard_errors(data: dict) -> list[str]:
    """
    Problems with the optional ``dashboard`` block, which tells p4n4-dashboard
    how to present the project:

    - ``grafana_path``: Grafana page the Grafana tab opens, e.g.
      ``/d/<uid>/<slug>``.
    - ``tabs``: the dashboard tabs this project can serve (see
      ``DASHBOARD_TABS``); the dashboard hides the others while connected.
    - ``theme``: directory, relative to the project, holding the white-label
      theme the project's dashboard is built with (``brand.json``, icon,
      fonts). Installed with ``dart run tool/brand.dart install <project>``.

    Only the block's shape is checked here; ``validate.validate_project``
    checks that the theme directory exists.
    """
    if "dashboard" not in data:
        return []
    block = data["dashboard"]
    if not isinstance(block, dict):
        return [".p4n4.json: dashboard must be an object"]

    errors = []
    for key in sorted(set(block) - {"grafana_path", "tabs", "theme"}):
        errors.append(f".p4n4.json: dashboard.{key} is not a known setting")

    path = block.get("grafana_path")
    if path is not None and not (isinstance(path, str) and path.startswith("/")):
        errors.append('.p4n4.json: dashboard.grafana_path must be a path starting with "/"')

    theme = block.get("theme")
    if theme is not None and not (
        isinstance(theme, str)
        and theme.strip()
        and not PurePosixPath(theme).is_absolute()
        and ".." not in PurePosixPath(theme).parts
    ):
        errors.append(".p4n4.json: dashboard.theme must be a directory inside the project")

    tabs = block.get("tabs")
    if tabs is not None:
        if not isinstance(tabs, list) or not tabs:
            errors.append(".p4n4.json: dashboard.tabs must be a non-empty list")
        else:
            unknown = [t for t in tabs if t not in DASHBOARD_TABS]
            if unknown:
                errors.append(
                    f".p4n4.json: unknown dashboard.tabs {unknown} "
                    f"(expected any of {', '.join(DASHBOARD_TABS)})"
                )
    return errors
