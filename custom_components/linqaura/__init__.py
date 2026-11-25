"""The LinqAura integration: LinqAura smart switches, directly on the home network or through the LinqAura cloud.

Two kinds of config entry:
- local: one switch, found by zeroconf (_linqaura._tcp) or by address, controlled with local protocol v2;
- cloud: one LinqAura account, every switch on it, with live updates from the cloud.

A switch with a local entry is left out of the cloud entry (the direct connection wins), so it never shows twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import SOURCE_ZEROCONF, ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CloudClient, LocalClient, Tokens
from .const import (
    CONF_BASE_URL,
    CONF_ENTRY_TYPE,
    CONF_LOCAL_KEY,
    CONF_SERIAL,
    CONF_TOKENS,
    DOMAIN,
    ENTRY_CLOUD,
    ENTRY_LOCAL,
)
from .coordinator import CloudCoordinator, LocalCoordinator, tokens_saver

PLATFORMS_LOCAL = [Platform.SENSOR, Platform.SWITCH]
PLATFORMS_CLOUD = [Platform.SWITCH, Platform.UPDATE]


@dataclass
class LinqAuraRuntime:
    local: LocalCoordinator | None = None
    cloud: CloudCoordinator | None = None


type LinqAuraConfigEntry = ConfigEntry[LinqAuraRuntime]


def local_serials(hass: HomeAssistant) -> set[str]:
    """Serials of the switches added directly (and not disabled)."""
    return {
        e.data[CONF_SERIAL]
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTRY_TYPE) == ENTRY_LOCAL and e.disabled_by is None
    }


async def async_setup_entry(hass: HomeAssistant, entry: LinqAuraConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_CLOUD:
        return await _setup_cloud(hass, entry)
    return await _setup_local(hass, entry)


async def _setup_local(hass: HomeAssistant, entry: LinqAuraConfigEntry) -> bool:
    serial = entry.data[CONF_SERIAL]
    # A cloud entry that still shows this switch lets go of it first, so the entities aren't registered twice
    for other in hass.config_entries.async_loaded_entries(DOMAIN):
        cloud = other.runtime_data.cloud if other.data.get(CONF_ENTRY_TYPE) == ENTRY_CLOUD else None
        if cloud is not None and serial in cloud.serials():
            await hass.config_entries.async_reload(other.entry_id)

    client = LocalClient(async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_LOCAL_KEY])
    coordinator = LocalCoordinator(hass, entry, client, serial)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = LinqAuraRuntime(local=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_LOCAL)
    return True


async def _setup_cloud(hass: HomeAssistant, entry: LinqAuraConfigEntry) -> bool:
    client = CloudClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        Tokens.from_dict(entry.data[CONF_TOKENS]),
        tokens_saver(hass, entry),
    )
    coordinator = CloudCoordinator(hass, entry, client)
    coordinator.skip_serials(local_serials(hass))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = LinqAuraRuntime(cloud=coordinator)
    # Discovered switches that are now in Home Assistant through the cloud stop asking to be added
    serials = coordinator.serials()
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        if flow["context"].get("source") == SOURCE_ZEROCONF and flow["context"].get("unique_id") in serials:
            hass.config_entries.flow.async_abort(flow["flow_id"])
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_CLOUD)
    entry.async_create_background_task(hass, coordinator.run_live(), f"{DOMAIN} live {entry.entry_id}")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LinqAuraConfigEntry) -> bool:
    platforms = PLATFORMS_CLOUD if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_CLOUD else PLATFORMS_LOCAL
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unloaded and entry.data.get(CONF_ENTRY_TYPE) == ENTRY_LOCAL:
        entry.runtime_data.local.client.close()
        if entry.disabled_by is not None:
            _reload_cloud(hass)  # the cloud entry shows the switch again
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: LinqAuraConfigEntry) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_LOCAL:
        _reload_cloud(hass)


def _reload_cloud(hass: HomeAssistant) -> None:
    for other in hass.config_entries.async_loaded_entries(DOMAIN):
        if other.data.get(CONF_ENTRY_TYPE) == ENTRY_CLOUD:
            hass.config_entries.async_schedule_reload(other.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: LinqAuraConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A switch can be deleted from the cloud entry once it is gone from the account (it comes back while it's
    there). A direct switch is removed by deleting its entry."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_CLOUD or entry.runtime_data.cloud is None:
        return False
    serials = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    return not serials & entry.runtime_data.cloud.serials()
