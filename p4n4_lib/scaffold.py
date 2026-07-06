"""Project scaffolding: fetch stack sources and copy them into a project."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from p4n4_lib import env as envutil
from p4n4_lib.layers import Layer


class ScaffoldError(Exception):
    """Raised when fetching or copying a stack source fails."""


def fetch_source(layer: Layer, source: str | Path | None = None) -> tuple[Path, str | None]:
    """
    Return a (Path, tmpdir_or_None) to a stack source directory.

    If `source` is a local path it is used directly; otherwise the layer repo is
    cloned at shallow depth into a temp dir that the caller must clean up.
    """
    if source:
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise ScaffoldError(f"source path does not exist: {path}")
        return path, None

    tmp = tempfile.mkdtemp(prefix=layer.clone_prefix)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", layer.repo_url, tmp],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ScaffoldError(
            f"git clone failed:\n{result.stderr.strip()}\n\n"
            f"Tip: pass a local checkout path to scaffold offline."
        )
    return Path(tmp), tmp


def scaffold_layer(
    project_dir: Path,
    layer: Layer,
    env_values: dict[str, str],
    source: str | Path | None = None,
) -> None:
    """Copy a layer's files into project_dir and write its .env."""
    src, tmpdir = fetch_source(layer, source)
    try:
        for name in layer.copy_paths:
            s = src / name
            if not s.exists():
                raise ScaffoldError(f"Expected '{name}' in source repo but it was not found.")
            d = project_dir / name
            if s.is_dir():
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)

        # Make shell scripts executable
        scripts_dir = project_dir / "scripts"
        if scripts_dir.is_dir():
            for script in scripts_dir.glob("*.sh"):
                script.chmod(script.stat().st_mode | 0o111)

        # Write .env: values override the .env.example template from the repo
        envutil.write(project_dir / ".env", env_values, template_path=src / ".env.example")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
