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


GITIGNORE_FILE = ".gitignore"

# The project's .gitignore: what each stack repo ignores, for every layer. The patterns
# have no leading slash, so they match in a single-layer project (files at the root) and
# in a multi-layer one (<layer>/...) alike.
GITIGNORE = """\
# Written by `p4n4 init`. Secrets and local state stay out of git; the .env.example
# files, compose files and config are meant to be committed.

# Secrets: every layer's .env
.env
.env.local
.env.*.local

# Machine-specific Compose overrides (a GPU, devices, other ports)
docker-compose.override.yml

# Generated credentials, certificates and keys (iot layer)
**/config/mosquitto/passwd
**/config/mosquitto/acl
**/config/mosquitto/certs/*
!**/config/mosquitto/certs/.gitkeep
**/config/node-red/flows_cred.json
**/config/node-red/flows/flows_cred.json

# Runtime state written into the project
**/config/node-red/flows/.*.backup
**/config/node-red/*.backup
**/config/node-red/.config*
**/config/node-red/lib/
**/config/node-red/node_modules/
**/config/node-red/package-lock.json
**/config/grafana/grafana.db
**/config/grafana/grafana.db-journal
data/

# Model files for the inference runner (ai layer): provided at deploy time
**/edge-impulse/models/*.eim
**/edge-impulse/models/*.eim.bak
**/edge-impulse/models/*.zip
**/onnx/models/*.onnx
**/onnx/models/*.onnx.bak

# p4n4-api's users and JWT secret, when it keeps them in the project (scripts/dev)
.p4n4-api/

# What `p4n4 migrate edge` keeps from the old edge layer, until it's deleted
edge.pre-migrate/

# Editors and operating systems
__pycache__/
*.swp
*~
.DS_Store
Thumbs.db
"""


def write_gitignore(project_dir: Path) -> bool:
    """Write the project's .gitignore; True if written. An existing one (the user's own,
    or a template's) is left as it is."""
    path = project_dir / GITIGNORE_FILE
    if path.exists():
        return False
    path.write_text(GITIGNORE, encoding="utf-8", newline="\n")
    return True


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
    # core.autocrlf=false: Git for Windows defaults to CRLF checkouts, which
    # break shell scripts run inside the Linux containers ('bash\r').
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--config", "core.autocrlf=false", layer.repo_url, tmp],
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
    compose_project_name: str | None = None,
) -> None:
    """Copy a layer's files into project_dir and write its .env.

    With `compose_project_name`, the .env also sets COMPOSE_PROJECT_NAME, so the
    layer's containers and volumes are namespaced per project (see
    `layout.compose_project_name`)."""
    src, tmpdir = fetch_source(layer, source)
    try:
        for name in layer.copy_paths:
            s = src / name
            if not s.exists():
                raise ScaffoldError(f"Expected '{name}' in source repo but it was not found.")
            d = project_dir / name
            if s.is_dir():
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(s, d)

        # Make shell scripts executable
        scripts_dir = project_dir / "scripts"
        if scripts_dir.is_dir():
            for script in scripts_dir.glob("*.sh"):
                script.chmod(script.stat().st_mode | 0o111)

        # Write .env: values override the .env.example template from the repo
        envutil.write(project_dir / ".env", env_values, template_path=src / ".env.example")
        if compose_project_name:
            envutil.set_value(
                project_dir / ".env",
                "COMPOSE_PROJECT_NAME",
                compose_project_name,
                comment="Compose project name, set by p4n4 init: keeps this project's\n"
                "containers' volumes apart from other projects'. Changing it on an\n"
                "existing project switches to new, empty volumes.",
            )
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
