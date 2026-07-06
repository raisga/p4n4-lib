"""Project validation checks shared by all clients."""

from __future__ import annotations

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

    layer_names: list[str] = data.get("layers", [])
    multi = len(layer_names) > 1

    for name in layout.ordered(n for n in layer_names if n in LAYERS):
        layer = LAYERS[name]
        base = layout.layer_dir(project_dir, layer_names, name)
        prefix = f"{name}/" if multi else ""

        for rel in layer.required_files:
            if (base / rel).exists():
                passed.append(f"{prefix}{rel}")
            else:
                errors.append(f"Missing file: {prefix}{rel}")

        if not layer.required_env_keys:
            continue

        env_path = base / envutil.ENV_FILE
        if env_path.exists():
            env = envutil.load(env_path)
            missing = [key for key in layer.required_env_keys if not env.get(key)]
            if missing:
                errors.extend(f"{prefix}.env missing required key: {key}" for key in missing)
            else:
                passed.append(f"{prefix}.env: all required keys present")
        else:
            errors.append(f"{prefix}.env file not found")

    return passed, errors
