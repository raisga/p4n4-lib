"""Secret generation and rotation policy shared by all clients."""

from __future__ import annotations

import secrets as _secrets

ROTATABLE_KEYS = (
    # IoT layer
    "INFLUXDB_PASSWORD",
    "INFLUXDB_TOKEN",
    "GRAFANA_PASSWORD",
    "NODE_RED_PASSWORD",
    # AI layer
    "LETTA_SERVER_PASSWORD",
    "N8N_BASIC_AUTH_PASSWORD",
    "N8N_ENCRYPTION_KEY",
)

# Secrets issued by someone else (an external broker's credentials): shown
# masked alongside the others, never rotated or generated, since a new value
# would no longer match the remote side
EXTERNAL_KEYS = (
    # IoT layer: bridge to an external MQTT broker
    "MQTT_REMOTE_PASSWORD",
)


def token(n: int = 32) -> str:
    """Return a hex token from n random bytes (2n characters)."""
    return _secrets.token_hex(n)


def rotation_value(key: str) -> str:
    """Generate a replacement value sized for the given rotatable key."""
    return token(16 if "PASSWORD" in key else 32)
