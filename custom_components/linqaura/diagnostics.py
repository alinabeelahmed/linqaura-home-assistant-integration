"""Diagnostics download, with keys, tokens and addresses left out."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_HOST
from homeassistant.core import HomeAssistant

from . import LinqAuraConfigEntry
from .const import CONF_BASE_URL, CONF_LOCAL_KEY, CONF_TOKENS, CONF_USER_ID

TO_REDACT = {
    CONF_LOCAL_KEY,
    CONF_TOKENS,
    CONF_EMAIL,
    CONF_USER_ID,
    CONF_HOST,
    CONF_BASE_URL,
    "host",
    "port",
    "ip",
    "ssid",
    "local_key",
    "access_token",
    "refresh_token",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LinqAuraConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    out: dict[str, Any] = {"entry": async_redact_data(dict(entry.data), TO_REDACT)}
    if runtime.local is not None:
        local = runtime.local
        hello = local.client.hello
        out["hello"] = (
            {"serial": hello.serial, "model": hello.model, "firmware": hello.firmware, "cred": hello.cred}
            if hello
            else None
        )
        if local.data is not None:
            out["states"] = asdict(local.data.states)
            out["names"] = asdict(local.data.names) if local.data.names else None
            out["diag"] = async_redact_data(local.data.diag, TO_REDACT)
    if runtime.cloud is not None:
        out["live_connected"] = runtime.cloud.connected
        out["devices"] = async_redact_data(list((runtime.cloud.data or {}).values()), TO_REDACT)
    return out
