"""Layer registry: per-layer stack metadata shared by all clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Layer:
    name: str
    repo_url: str
    # Files/dirs copied from the stack repo into a new project
    copy_paths: tuple[str, ...] = ()
    # Files a valid project must contain
    required_files: tuple[str, ...] = ()
    # Keys a valid project .env must define
    required_env_keys: tuple[str, ...] = ()
    # Files and keys required only while the layer's .env lists the Compose profile
    # (COMPOSE_PROFILES), for opt-in services that need their own files
    profile_files: dict[str, tuple[str, ...]] = field(default_factory=dict)
    profile_env_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Opt-in: left out of `--layer all`, scaffolded only when named
    optional: bool = False

    @property
    def clone_prefix(self) -> str:
        return f"p4n4-{self.name}-"


_sources: dict = yaml.safe_load(
    (Path(__file__).parent / "sources.yaml").read_text(encoding="utf-8")
)

LAYERS: dict[str, Layer] = {
    "iot": Layer(
        name="iot",
        repo_url=_sources["iot_repo_url"],
        copy_paths=("docker-compose.yml", "config", "scripts"),
        required_files=(
            "docker-compose.yml",
            ".env",
            "config/mosquitto/mosquitto.conf",
            "config/mosquitto/bridge.sh",
            "config/node-red/settings.js",
            "config/node-red/flows/flows.json",
            "config/grafana/provisioning/datasources/datasources.yml",
            "scripts/init-buckets.sh",
        ),
        required_env_keys=(
            "TZ",
            "INFLUXDB_USERNAME",
            "INFLUXDB_PASSWORD",
            "INFLUXDB_ORG",
            "INFLUXDB_TOKEN",
            "INFLUXDB_BUCKET",
            "GRAFANA_USER",
            "GRAFANA_PASSWORD",
            "NODE_RED_USER",
            "NODE_RED_PASSWORD",
        ),
    ),
    # GenAI services plus the edge inference runner (profile ei-runner), which was
    # the edge layer (p4n4-edge) until it merged into this one
    "ai": Layer(
        name="ai",
        repo_url=_sources["ai_repo_url"],
        copy_paths=("docker-compose.yml", "config", "scripts", "runner", "edge-impulse", "onnx"),
        required_files=(
            "docker-compose.yml",
            ".env",
            "config/letta/letta.conf",
            "scripts/pull-models.sh",
        ),
        required_env_keys=(
            "LETTA_SERVER_PASSWORD",
            "N8N_BASIC_AUTH_USER",
            "N8N_BASIC_AUTH_PASSWORD",
            "N8N_ENCRYPTION_KEY",
            "N8N_HOST",
            "INFLUXDB_TOKEN",
            "INFLUXDB_ORG",
            "INFLUXDB_BUCKET",
        ),
        profile_files={
            "ei-runner": (
                "runner/Dockerfile",
                "runner/runner.py",
                "edge-impulse/models",
                "onnx/models",
            ),
        },
        profile_env_keys={
            "ei-runner": ("MODEL_BACKEND", "MQTT_HOST", "MQTT_PORT", "INFLUXDB_BUCKET_AI_EVENTS"),
        },
    ),
    # Optional services outside the core (p4n4-extras): nothing starts until the
    # layer's COMPOSE_PROFILES lists it, and no core service depends on one
    "extras": Layer(
        name="extras",
        repo_url=_sources["extras_repo_url"],
        # Its extras are directories its compose file includes; a new project gets
        # none, and the skeleton to copy for one
        copy_paths=("docker-compose.yml", "_template"),
        required_files=("docker-compose.yml", ".env"),
        optional=True,
    ),
    # The web UI (p4n4-dashboard): one container from a released image, so
    # only its compose file is copied, not the Flutter sources. Last in the
    # order, so `up` starts it after the stacks it shows and `down` stops it first.
    "dashboard": Layer(
        name="dashboard",
        repo_url=_sources["dashboard_repo_url"],
        copy_paths=("docker-compose.yml",),
        required_files=("docker-compose.yml", ".env"),
        required_env_keys=("DASHBOARD_VERSION", "DASHBOARD_PORT"),
    ),
}

LAYER_NAMES: tuple[str, ...] = tuple(LAYERS)

# Layers `--layer all` scaffolds: every layer but the opt-in ones
DEFAULT_LAYERS: tuple[str, ...] = tuple(
    name for name, layer in LAYERS.items() if not layer.optional
)

# Layers p4n4 no longer scaffolds, mapped to the layer that replaced them. Projects
# made before keep running them (`layout` still orders and finds them) until
# `p4n4 migrate <name>` moves them into the replacement.
LEGACY_LAYERS: dict[str, str] = {
    # p4n4-edge's inference runner is p4n4-ai's ei-runner profile
    "edge": "ai",
}

# The edge inference runner: its service and Compose profile in the ai layer
RUNNER_SERVICE = "ei-runner"
