"""Dotenv read/write utilities."""

from __future__ import annotations

import re
from pathlib import Path

ENV_FILE = ".env"

# Characters Compose would interpolate, strip or split on in an unquoted value
_NEEDS_QUOTES = set(" \t#$'\"\\")


def quote(value: str) -> str:
    """
    Quote a value for a Compose .env file when it needs it. Generated secrets
    are plain hex and stay bare; user-supplied ones (an external broker
    password) may hold anything. Single quotes are literal in Compose; a value
    containing one is double-quoted, with backslash, double quote and $ escaped.
    """
    if not _NEEDS_QUOTES.intersection(value):
        return value
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def unquote(value: str) -> str:
    """
    Reverse quote(): strip matching quotes and undo double-quote escapes. As
    in Compose, an unquoted value ends at an inline " #" comment.
    """
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        out, chars = [], iter(value[1:-1])
        for ch in chars:
            out.append(next(chars, "") if ch == "\\" else ch)
        return "".join(out)
    return re.split(r"\s#", value, maxsplit=1)[0].rstrip()


def load(path: Path) -> dict[str, str]:
    """Parse .env file into a dict, skipping comments and blank lines."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = unquote(val.strip())
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
                    out.append(f"{key}={quote(values[key])}")
                    continue
            out.append(line)
        path.write_text("\n".join(out) + "\n")
    else:
        path.write_text("\n".join(f"{k}={quote(v)}" for k, v in values.items()) + "\n")
