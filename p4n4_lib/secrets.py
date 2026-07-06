"""Secret generation and rotation policy shared by all clients."""

from __future__ import annotations

import secrets as _secrets

ROTATABLE_KEYS = (
    # IoT layer
    "INFLUXDB_PASSWORD",
    "INFLUXDB_TOKEN",
    "GRAFANA_PASSWORD",
    # AI layer
    "LETTA_SERVER_PASSWORD",
    "N8N_BASIC_AUTH_PASSWORD",
    "N8N_ENCRYPTION_KEY",
)


def token(n: int = 32) -> str:
    """Return a hex token from n random bytes (2n characters)."""
    return _secrets.token_hex(n)


def rotation_value(key: str) -> str:
    """Generate a replacement value sized for the given rotatable key."""
    return token(16 if "PASSWORD" in key else 32)
