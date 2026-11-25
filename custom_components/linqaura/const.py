"""Constants for the LinqAura integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "linqaura"
MANUFACTURER: Final = "LinqAura"

CONF_ENTRY_TYPE: Final = "entry_type"
ENTRY_CLOUD: Final = "cloud"  # one LinqAura account, every switch on it, through the cloud
ENTRY_LOCAL: Final = "local"  # one switch, directly on the home network

CONF_SERIAL: Final = "serial"
CONF_LOCAL_KEY: Final = "local_key"
CONF_MODEL: Final = "model"
CONF_TOKENS: Final = "tokens"
CONF_USER_ID: Final = "user_id"
CONF_BASE_URL: Final = "base_url"

CHANNELS: Final = 4

# The switch can't push over Wi-Fi; the LinqAura app polls every 2 s. Every poll also keeps HA's session (one of the
# switch's three Wi-Fi session slots) from timing out after 120 s.
LOCAL_SCAN_INTERVAL: Final = timedelta(seconds=5)
LOCAL_SLOW_EVERY: Final = 12  # names and diagnostics every 12th poll (one minute)

# Cloud states arrive over the live socket; a full reload now and then catches anything missed.
CLOUD_SCAN_INTERVAL: Final = timedelta(minutes=10)
CLOUD_RECONNECT_MAX_S: Final = 300
