"""Base entities for direct and cloud switches."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MODEL, DOMAIN, MANUFACTURER
from .coordinator import CloudCoordinator, LocalCoordinator


def default_name(serial: str, mac: str | None = None) -> str:
    """What a switch is called until its owner renames it, the same everywhere (app, cloud, the switch, Matter):
    "LINQAURA SWITCH " + the last 4 hex digits of its Wi-Fi MAC (the serial's last 4 when the MAC isn't known)."""
    tail = (mac or "").replace(":", "").replace("-", "")[-4:] or serial[-4:]
    return f"LINQAURA SWITCH {tail.upper()}"


class LocalEntity(CoordinatorEntity[LocalCoordinator]):
    """An entity of a switch added directly."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LocalCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        serial = coordinator.serial
        self._attr_unique_id = f"{serial}-{key}"
        hello = coordinator.client.hello
        names = coordinator.data.names if coordinator.data else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer=MANUFACTURER,
            model=(hello.model if hello else None) or entry.data.get(CONF_MODEL),
            name=(names.name if names and names.name else None) or default_name(serial),
            serial_number=serial,
            sw_version=hello.firmware if hello else None,
        )


class CloudEntity(CoordinatorEntity[CloudCoordinator]):
    """An entity of a switch on the LinqAura account."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CloudCoordinator, device_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.device_id = device_id
        device = coordinator.data[device_id]
        serial = device["serial"]
        self._attr_unique_id = f"{serial}-{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer=MANUFACTURER,
            model=device.get("model") or None,
            name=device.get("name") or default_name(serial, device.get("mac")),
            serial_number=serial,
            sw_version=device.get("firmware_version"),
        )

    @property
    def device(self) -> dict[str, Any]:
        return self.coordinator.data.get(self.device_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.device.get("online") is True

    def channel(self, ch: int) -> dict[str, Any]:
        return next((c for c in self.device.get("channels", []) if c.get("ch") == ch), {})
