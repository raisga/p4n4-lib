"""Layer registry: per-layer stack metadata shared by all clients."""

from __future__ import annotations

from dataclasses import dataclass
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

    @property
    def clone_prefix(self) -> str:
        return f"p4n4-{self.name}-"


_sources: dict = yaml.safe_load((Path(__file__).parent / "sources.yaml").read_text())

LAYERS: dict[str, Layer] = {
    "iot": Layer(
        name="iot",
        repo_url=_sources["iot_repo_url"],
        copy_paths=("docker-compose.yml", "config", "scripts"),
        required_files=(
            "docker-compose.yml",
            ".env",
            "config/mosquitto/mosquitto.conf",
            "config/node-red/settings.js",
            "config/node-red/flows.json",
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
        ),
    ),
    "ai": Layer(
        name="ai",
        repo_url=_sources["ai_repo_url"],
        copy_paths=("docker-compose.yml", "config", "scripts"),
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
    ),
    "edge": Layer(
        name="edge",
        repo_url=_sources["edge_repo_url"],
    ),
}

LAYER_NAMES: tuple[str, ...] = tuple(LAYERS)
