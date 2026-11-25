"""Firmware update of a cloud switch: Install starts the same update as the LinqAura app (owner only)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LinqAuraConfigEntry
from .api import LinqAuraError
from .const import DOMAIN
from .coordinator import CloudCoordinator
from .entity import CloudEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: LinqAuraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    cloud = entry.runtime_data.cloud
    if cloud is None:
        return
    added: set[str] = set()

    @callback
    def add_new() -> None:
        added.intersection_update(cloud.data)
        new = [i for i, d in cloud.data.items() if i not in added and d.get("access", "owner") == "owner"]
        added.update(new)
        if new:
            async_add_entities(Firmware(cloud, device_id) for device_id in new)

    add_new()
    entry.async_on_unload(cloud.async_add_listener(add_new))


class Firmware(CloudEntity, UpdateEntity):
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_translation_key = "firmware"

    def __init__(self, coordinator: CloudCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "firmware")

    @property
    def installed_version(self) -> str | None:
        return self.device.get("firmware_version")

    @property
    def latest_version(self) -> str | None:
        if self.device.get("update_available") and self.device.get("latest_firmware"):
            return self.device["latest_firmware"]
        return self.installed_version

    @property
    def in_progress(self) -> bool:
        update = self.device.get("firmware_update")
        return isinstance(update, dict) and update.get("state") in ("pending", "updating")

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.start_update(self.device_id, version or self.device.get("latest_firmware"))
        except LinqAuraError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="update_failed", translation_placeholders={"error": str(err)}
            ) from err
        await self.coordinator.async_request_refresh()
