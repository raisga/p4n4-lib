"""Secret generation and rotation policy shared by all clients."""

from __future__ import annotations

import secrets as _secrets

# Secrets their services read at every start: a new value in .env takes effect
# on the next `p4n4 up`, so they can be rotated there
ROTATABLE_KEYS = (
    # IoT layer
    "NODE_RED_PASSWORD",
    # AI layer
    "LETTA_SERVER_PASSWORD",
)

# Secrets their services read only when they first set up their data: InfluxDB
# and Grafana keep the first password and token, and n8n refuses to start with
# an encryption key that doesn't match the one its credentials were stored
# with. A new value in .env alone would lock clients out, so these are never
# rotated there: change them in the service itself.
SETUP_KEYS = (
    # IoT layer
    "INFLUXDB_PASSWORD",
    "INFLUXDB_TOKEN",
    "GRAFANA_PASSWORD",
    # AI layer
    "N8N_ENCRYPTION_KEY",
    # Unused: n8n dropped basic auth in 1.0 and manages its own users
    "N8N_BASIC_AUTH_PASSWORD",
)

# Every secret p4n4 generates, as `p4n4 secret show` lists them
SECRET_KEYS = ROTATABLE_KEYS + SETUP_KEYS

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
