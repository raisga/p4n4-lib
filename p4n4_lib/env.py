"""Dotenv read/write utilities."""

from __future__ import annotations

from pathlib import Path

ENV_FILE = ".env"


def load(path: Path) -> dict[str, str]:
    """Parse .env file into a dict, skipping comments and blank lines."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env


def write(path: Path, values: dict[str, str], template_path: Path | None = None) -> None:
    """Write values to .env, preserving template comments/structure when a template is given."""
    if template_path and template_path.exists():
        lines = template_path.read_text().splitlines()
        out = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, _, _ = stripped.partition("=")
                key = key.strip()
                if key in values:
                    # Strip inline comments from template line before writing value
                    out.append(f"{key}={values[key]}")
                    continue
            out.append(line)
        path.write_text("\n".join(out) + "\n")
    else:
        path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
