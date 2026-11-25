"""Diagnostic sensors of a switch added directly (from its `diag` answer)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LinqAuraConfigEntry
from .coordinator import LocalCoordinator
from .entity import LocalEntity

PARALLEL_UPDATES = 0  # read from the coordinator


@dataclass(frozen=True, kw_only=True)
class DiagSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]


def _wifi(diag: dict[str, Any], key: str) -> Any:
    wifi = diag.get("wifi")
    return wifi.get(key) if isinstance(wifi, dict) else None


SENSORS: tuple[DiagSensorDescription, ...] = (
    DiagSensorDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda d: _wifi(d, "rssi"),
    ),
    DiagSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda d: d.get("uptime_s"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LinqAuraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    local = entry.runtime_data.local
    if local is None:
        return
    async_add_entities(DiagSensor(local, description) for description in SENSORS)


class DiagSensor(LocalEntity, SensorEntity):
    entity_description: DiagSensorDescription

    def __init__(self, coordinator: LocalCoordinator, description: DiagSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        value = self.entity_description.value(self.coordinator.data.diag)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
