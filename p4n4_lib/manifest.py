"""Manifest (.p4n4.json) read/write utilities."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MANIFEST_FILE = ".p4n4.json"
SCHEMA_VERSION = 1

# Tabs p4n4-dashboard can show; the optional "dashboard" block narrows them
DASHBOARD_TABS = ("services", "edge", "agent", "grafana", "video", "feed")


def find(start: Path | None = None) -> Path | None:
    """Walk up from start (default cwd) to find .p4n4.json."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / MANIFEST_FILE
        if candidate.exists():
            return candidate
    return None


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


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
    - ``home``: Markdown file, relative to the project, that p4n4-dashboard
      shows at the top of its Home tab (default ``HOME.md``), or
      ``HOME.<lang>.md`` beside it for the dashboard's language. Without the
      setting, a missing or empty file shows nothing.
    - ``cameras``: streams the Video tab shows until the user saves their own,
      each ``{"id", "name"}`` plus one of: an absolute http(s) ``url``; a
      ``port`` and ``path`` on the host the dashboard is connected to
      (e.g. go2rtc's ``{"port": 1984, "path": "/api/stream.mjpeg?src=floor"}``);
      or a ``device`` camera, ``"back"`` or ``"front"``, of the phone or tablet
      the dashboard runs on.
    - ``feed``: where the Feed tab's items come from, ``{"url"}``: an absolute
      http(s) URL that p4n4-api reads (``GET``) and posts actions to
      (``POST <url>/<id>/action``), in p4n4-dashboard's feed contract. An
      optional ``token``, ``{"layer", "env"}``, names the layer ``.env``
      variable p4n4-api sends as a bearer token, so it never reaches a browser.

    Only the block's shape is checked here; ``validate.validate_project``
    checks that the theme directory and the home file exist.
    """
    if "dashboard" not in data:
        return []
    block = data["dashboard"]
    if not isinstance(block, dict):
        return [".p4n4.json: dashboard must be an object"]

    errors = []
    for key in sorted(set(block) - {"grafana_path", "tabs", "theme", "home", "cameras", "feed"}):
        errors.append(f".p4n4.json: dashboard.{key} is not a known setting")

    path = block.get("grafana_path")
    if path is not None and not (isinstance(path, str) and path.startswith("/")):
        errors.append('.p4n4.json: dashboard.grafana_path must be a path starting with "/"')

    theme = block.get("theme")
    if theme is not None and not _inside_project(theme):
        errors.append(".p4n4.json: dashboard.theme must be a directory inside the project")

    home = block.get("home")
    if home is not None and not _inside_project(home):
        errors.append(".p4n4.json: dashboard.home must be a file inside the project")

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

    if "cameras" in block:
        errors.extend(camera_errors(block["cameras"]))
    if "feed" in block:
        errors.extend(feed_errors(block["feed"]))
    return errors


def _inside_project(path: object) -> bool:
    """Whether path is a non-empty relative path that stays inside the project."""
    return (
        isinstance(path, str)
        and bool(path.strip())
        and not PurePosixPath(path).is_absolute()
        and ".." not in PurePosixPath(path).parts
    )


def feed_errors(feed: object) -> list[str]:
    """Problems with ``dashboard.feed`` (see :func:`dashboard_errors`)."""
    where = ".p4n4.json: dashboard.feed"
    if not isinstance(feed, dict):
        return [f"{where} must be an object"]
    errors = [
        f"{where}.{key} is not a known setting" for key in sorted(set(feed) - {"url", "token"})
    ]
    url = feed.get("url")
    if not (isinstance(url, str) and re.match(r"^https?://[^/\s]+", url)):
        errors.append(f"{where}.url must be an absolute http(s) URL")
    token = feed.get("token")
    if token is not None:
        if not isinstance(token, dict) or set(token) != {"layer", "env"}:
            errors.append(f'{where}.token must be {{"layer", "env"}}')
        elif not (
            isinstance(token["env"], str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", token["env"])
        ):
            errors.append(f"{where}.token.env must be an environment variable name")
        elif not (isinstance(token["layer"], str) and token["layer"].strip()):
            errors.append(f"{where}.token.layer must be a layer name")
    return errors


def camera_errors(cameras: object) -> list[str]:
    """Problems with ``dashboard.cameras`` (see :func:`dashboard_errors`)."""
    if not isinstance(cameras, list) or not cameras:
        return [".p4n4.json: dashboard.cameras must be a non-empty list"]
    errors, ids = [], set()
    for i, camera in enumerate(cameras):
        where = f".p4n4.json: dashboard.cameras[{i}]"
        if not isinstance(camera, dict):
            errors.append(f"{where} must be an object")
            continue
        for key in sorted(set(camera) - {"id", "name", "url", "port", "path", "device"}):
            errors.append(f"{where}.{key} is not a known setting")
        cam_id, name = camera.get("id"), camera.get("name")
        if not (isinstance(cam_id, str) and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", cam_id)):
            errors.append(f"{where}.id must be lowercase letters, digits and dashes")
        elif cam_id in ids:
            errors.append(f"{where}.id {cam_id!r} is used twice")
        else:
            ids.add(cam_id)
        if not (isinstance(name, str) and name.strip()):
            errors.append(f"{where}.name must be a non-empty string")
        url, port, path = camera.get("url"), camera.get("port"), camera.get("path")
        device = camera.get("device")
        if [url, port, device].count(None) != 2:
            errors.append(f"{where} needs one of url, port (with an optional path) or device")
        elif device is not None:
            if device not in ("back", "front"):
                errors.append(f'{where}.device must be "back" or "front"')
            if path is not None:
                errors.append(f"{where}.path only goes with port")
        elif url is not None:
            if not (isinstance(url, str) and re.match(r"^https?://[^/\s]+", url)):
                errors.append(f"{where}.url must be an absolute http(s) URL")
            if path is not None:
                errors.append(f"{where}.path only goes with port")
        else:
            if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
                errors.append(f"{where}.port must be a TCP port (1-65535)")
            if path is not None and not (isinstance(path, str) and path.startswith("/")):
                errors.append(f'{where}.path must start with "/"')
    return errors
