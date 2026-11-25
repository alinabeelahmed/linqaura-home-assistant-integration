"""Switches: the four channels, the child lock and (direct switches) each button's touch lock."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LinqAuraConfigEntry
from .api import ChannelStates, LinqAuraError, RequestRefused
from .const import CHANNELS, DOMAIN
from .coordinator import CloudCoordinator, LocalCoordinator
from .entity import CloudEntity, LocalEntity

PARALLEL_UPDATES = 1  # one request at a time per switch session; commands queue behind each other


async def async_setup_entry(
    hass: HomeAssistant, entry: LinqAuraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    if runtime.local is not None:
        local = runtime.local
        entities: list[SwitchEntity] = [LocalChannel(local, ch) for ch in range(1, CHANNELS + 1)]
        if local.data.states.locked is not None:
            entities.append(LocalChildLock(local))
        if local.data.states.touch_lock is not None:
            entities.extend(LocalTouchLock(local, ch) for ch in range(1, CHANNELS + 1))
        async_add_entities(entities)
        return

    cloud = runtime.cloud
    assert cloud is not None
    added: set[str] = set()

    @callback
    def add_new() -> None:
        added.intersection_update(cloud.data)  # a switch that left can come back later
        new = [device_id for device_id in cloud.data if device_id not in added]
        if not new:
            return
        entities: list[SwitchEntity] = []
        for device_id in new:
            added.add(device_id)
            entities.extend(CloudChannel(cloud, device_id, ch) for ch in range(1, CHANNELS + 1))
            if cloud.data[device_id].get("access", "owner") in ("owner", "full"):
                entities.append(CloudChildLock(cloud, device_id))
        async_add_entities(entities)

    add_new()
    entry.async_on_unload(cloud.async_add_listener(add_new))


async def _run(call: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await call()
    except RequestRefused as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="command_refused", translation_placeholders={"error": err.code}
        ) from err
    except LinqAuraError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="command_failed", translation_placeholders={"error": str(err)}
        ) from err


# ----------------------------------------------------------------------------------------------------- direct


class LocalChannel(LocalEntity, SwitchEntity):
    _attr_translation_key = "channel"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: LocalCoordinator, ch: int) -> None:
        super().__init__(coordinator, f"ch{ch}")
        self._ch = ch
        self._attr_translation_placeholders = {"number": str(ch)}

    @property
    def name(self) -> str | None:
        names = self.coordinator.data.names
        if names and names.channels[self._ch - 1]:
            return names.channels[self._ch - 1]
        return super().name

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.states.on[self._ch - 1]

    async def _set(self, on: bool) -> None:
        states: ChannelStates = await _run(lambda: self.coordinator.client.set_channel(self._ch, on))
        self.coordinator.apply_states(states)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class LocalChildLock(LocalEntity, SwitchEntity):
    """Touch buttons locked (the app, Matter and Home Assistant still switch)."""

    _attr_translation_key = "child_lock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: LocalCoordinator) -> None:
        super().__init__(coordinator, "child_lock")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.states.locked is True

    async def _set(self, on: bool) -> None:
        self.coordinator.apply_states(await _run(lambda: self.coordinator.client.set_child_lock(on)))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class LocalTouchLock(LocalEntity, SwitchEntity):
    """One button's own touch lock (FR-2)."""

    _attr_translation_key = "touch_lock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: LocalCoordinator, ch: int) -> None:
        super().__init__(coordinator, f"ch{ch}-touch_lock")
        self._ch = ch
        self._attr_translation_placeholders = {"number": str(ch)}

    @property
    def is_on(self) -> bool:
        locks = self.coordinator.data.states.touch_lock
        return bool(locks and locks[self._ch - 1])

    async def _set(self, on: bool) -> None:
        self.coordinator.apply_states(await _run(lambda: self.coordinator.client.set_touch_lock(self._ch, on)))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


# ------------------------------------------------------------------------------------------------------ cloud


class CloudChannel(CloudEntity, SwitchEntity):
    _attr_translation_key = "channel"

    def __init__(self, coordinator: CloudCoordinator, device_id: str, ch: int) -> None:
        super().__init__(coordinator, device_id, f"ch{ch}")
        self._ch = ch
        self._attr_translation_placeholders = {"number": str(ch)}

    @property
    def name(self) -> str | None:
        return self.channel(self._ch).get("name") or super().name

    @property
    def device_class(self) -> SwitchDeviceClass:
        return SwitchDeviceClass.OUTLET if self.channel(self._ch).get("kind") == "socket" else SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool | None:
        state = self.channel(self._ch).get("state")
        return None if state is None else state == "ON"

    async def _set(self, on: bool) -> None:
        await _run(lambda: self.coordinator.client.set_channel(self.device_id, self._ch, on))
        channels = [{**c, "state": "ON" if on else "OFF"} if c.get("ch") == self._ch else c for c in self.device["channels"]]
        self.coordinator.apply(self.device_id, channels=channels)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class CloudChildLock(CloudEntity, SwitchEntity):
    _attr_translation_key = "child_lock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CloudCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "child_lock")

    @property
    def is_on(self) -> bool:
        return self.device.get("child_lock") is True

    async def _set(self, on: bool) -> None:
        await _run(lambda: self.coordinator.client.set_child_lock(self.device_id, on))
        self.coordinator.apply(self.device_id, child_lock=on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
