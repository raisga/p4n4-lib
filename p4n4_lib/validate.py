"""Project validation checks shared by all clients."""

from __future__ import annotations

import json
from pathlib import Path

from p4n4_lib import env as envutil
from p4n4_lib import layout
from p4n4_lib import manifest as mf
from p4n4_lib.layers import LAYERS


def validate_project(project_dir: Path, data: dict) -> tuple[list[str], list[str]]:
    """
    Validate a loaded manifest against the project tree.

    Returns (passed, errors) as human-readable check labels.
    """
    passed: list[str] = []
    errors: list[str] = []

    if data.get("schema_version") != mf.SCHEMA_VERSION:
        errors.append(
            f".p4n4.json schema_version mismatch "
            f"(got {data.get('schema_version')}, expected {mf.SCHEMA_VERSION})"
        )
    else:
        passed.append(".p4n4.json: valid")

    dashboard_errors = mf.dashboard_errors(data)
    errors.extend(dashboard_errors)
    if "dashboard" in data and not dashboard_errors:
        passed.append(".p4n4.json: dashboard settings valid")
        if theme := data["dashboard"].get("theme"):
            errors_before = len(errors)
            _check_theme(project_dir / theme, theme, errors)
            if len(errors) == errors_before:
                passed.append(f"{theme}/brand.json: dashboard theme")
        # The default HOME.md is optional; a file the manifest names should be there.
        if home := data["dashboard"].get("home"):
            if (project_dir / home).is_file():
                passed.append(f"{home}: dashboard home page")
            else:
                errors.append(f"Missing file: {home} (.p4n4.json dashboard.home)")
        vision = data["dashboard"].get("vision")
        if isinstance(vision, dict) and isinstance(vision.get("file"), str):
            _check_vision(project_dir / vision["file"], vision["file"], passed, errors)

    layer_names: list[str] = data.get("layers", [])
    multi = len(layer_names) > 1
    # A template ships its own variant of a stack (e.g. MQTT → Telegraf instead
    # of Node-RED), so the base stack's file list doesn't apply. The template
    # registry validates and smoke-tests the template's own files; here only
    # its compose file and the variables its .env.example documents are checked.
    from_template = isinstance(data.get("template"), dict)

    for name in layout.ordered(n for n in layer_names if n in LAYERS):
        layer = LAYERS[name]
        base = layout.layer_dir(project_dir, layer_names, name)
        prefix = f"{name}/" if multi else ""

        env_path = base / envutil.ENV_FILE
        if from_template:
            required_files: tuple[str, ...] = (layout.COMPOSE_FILE, envutil.ENV_FILE)
            example = base / f"{envutil.ENV_FILE}.example"
            required_keys = tuple(envutil.load(example)) if example.exists() else ()
        else:
            required_files, required_keys = layer.required_files, layer.required_env_keys
            # Opt-in services' own files and keys, while their profile is enabled
            enabled = envutil.parse_profiles(
                envutil.load(env_path).get("COMPOSE_PROFILES", "") if env_path.exists() else ""
            )
            for profile in enabled:
                required_files += layer.profile_files.get(profile, ())
                required_keys += layer.profile_env_keys.get(profile, ())

        for rel in required_files:
            if (base / rel).exists():
                passed.append(f"{prefix}{rel}")
            else:
                errors.append(f"Missing file: {prefix}{rel}")

        if not required_keys:
            continue

        if env_path.exists():
            env = envutil.load(env_path)
            # Template variables may be deliberately empty (COMPOSE_PROFILES=)
            missing = [
                key
                for key in required_keys
                if (key not in env if from_template else not env.get(key))
            ]
            if missing:
                errors.extend(f"{prefix}.env missing required key: {key}" for key in missing)
            else:
                passed.append(f"{prefix}.env: all required keys present")
        else:
            errors.append(f"{prefix}.env file not found")

    return passed, errors


def _check_vision(path: Path, label: str, passed: list[str], errors: list[str]) -> None:
    """The vision overlay must be a JSON object; p4n4-dashboard checks the rest."""
    if not path.is_file():
        errors.append(f"Missing file: {label} (.p4n4.json dashboard.vision.file)")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        errors.append(f"{label}: not valid JSON ({e})")
        return
    if isinstance(data, dict) and isinstance(data.get("cameras"), dict):
        passed.append(f"{label}: vision overlay ({len(data['cameras'])} cameras)")
    else:
        errors.append(f'{label}: a vision overlay is an object with "cameras"')


def _check_theme(theme_dir: Path, label: str, errors: list[str]) -> None:
    """
    The theme must be a directory with a parseable brand.json naming its id.
    Full checks (colors, contrast, native IDs, fonts) are the dashboard's
    `tool/brand.dart check`; this only catches a missing or broken theme.
    """
    brand = theme_dir / "brand.json"
    if not brand.is_file():
        errors.append(f"Missing file: {label}/brand.json (.p4n4.json dashboard.theme)")
        return
    try:
        data = json.loads(brand.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{label}/brand.json is not valid JSON: {exc}")
        return
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
        errors.append(f'{label}/brand.json must have an "id"')
