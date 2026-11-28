"""Setting up entries, entities, commands, live events and token storage."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.linqaura.const import CONF_TOKENS, DOMAIN

from .conftest import DEVICE_ID, OTHER_ID, FakeCloud, cloud_entry, device, local_entry
from .fake_switch import KEY, SERIAL, FakeSwitch


async def _setup(hass: HomeAssistant, entry: Any) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _call(hass: HomeAssistant, domain: str, service: str, entity_id: str) -> None:
    await hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True)


# ------------------------------------------------------------------------------------------------- direct


async def test_local_entities_and_commands(
    hass: HomeAssistant, switch: FakeSwitch, entity_registry: er.EntityRegistry, device_registry: dr.DeviceRegistry
) -> None:
    entry = local_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    devs = device_registry.async_get_devices(identifiers={(DOMAIN, SERIAL)})
    assert len(devs) == 1
    dev = devs[0]
    assert dev.name == "Hall" and dev.manufacturer == "LinqAura" and dev.sw_version == "0.7.17"

    # Names from the switch; an unnamed button gets "Button 2"
    assert hass.states.get("switch.hall_ceiling").state == STATE_OFF
    assert hass.states.get("switch.hall_button_2") is not None
    assert hass.states.get("switch.hall_child_lock").state == STATE_OFF
    assert entity_registry.async_get("switch.hall_button_1_touch_lock").disabled_by is not None
    assert entity_registry.async_get("sensor.hall_wi_fi_signal").disabled_by is not None

    await _call(hass, "switch", "turn_on", "switch.hall_ceiling")
    assert switch.on[0] is True
    assert hass.states.get("switch.hall_ceiling").state == STATE_ON
    await _call(hass, "switch", "turn_on", "switch.hall_child_lock")
    assert switch.locked is True
    assert hass.states.get("switch.hall_child_lock").state == STATE_ON

    assert switch.handshakes == 1  # one session for everything


async def test_local_poll_sees_changes_and_reconnects(
    hass: HomeAssistant, switch: FakeSwitch, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, local_entry())
    switch.on[2] = True  # someone touched the glass
    switch.drop_sessions()  # and the switch closed HA's session
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("switch.hall_fan").state == STATE_ON
    assert switch.handshakes == 2


async def test_local_unreachable_then_back(
    hass: HomeAssistant, switch: FakeSwitch, aioclient_mock: Any, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, local_entry())
    from unittest.mock import patch

    from custom_components.linqaura.api import CannotConnect

    with patch("custom_components.linqaura.api.local.LocalClient._post", side_effect=CannotConnect("timeout")):
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert hass.states.get("switch.hall_ceiling").state == STATE_UNAVAILABLE
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("switch.hall_ceiling").state == STATE_OFF


async def test_local_not_ready(hass: HomeAssistant, aioclient_mock: Any) -> None:
    entry = local_entry(host="7.7.7.7")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_local_diagnostics_redact(hass: HomeAssistant, switch: FakeSwitch, hass_client: ClientSessionGenerator) -> None:
    entry = local_entry()
    await _setup(hass, entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert KEY not in str(diag)
    assert diag["states"]["on"] == [False, False, False, False]


# ------------------------------------------------------------------------------------------------- cloud


async def test_cloud_entities_commands_and_live_events(
    hass: HomeAssistant, cloud: FakeCloud, live: list[Any]
) -> None:
    await _setup(hass, cloud_entry())
    assert hass.states.get("switch.hall_switch_ceiling").state == STATE_ON
    assert hass.states.get("switch.hall_switch_lamp").attributes["device_class"] == "outlet"
    assert hass.states.get("switch.hall_switch_spare").state == "unknown"
    fw = hass.states.get("update.hall_switch_firmware")
    assert fw.state == STATE_ON and fw.attributes["latest_version"] == "0.7.17"

    await _call(hass, "switch", "turn_off", "switch.hall_switch_ceiling")
    assert cloud.calls[-1][1].endswith(f"/devices/{DEVICE_ID}/channels/1")
    assert cloud.calls[-1][2] == {"state": "OFF"}
    assert hass.states.get("switch.hall_switch_ceiling").state == STATE_OFF

    push = live[0]
    push({"type": "channel", "device_id": DEVICE_ID, "ch": 3, "state": "ON"})
    push({"type": "child_lock", "device_id": DEVICE_ID, "state": "ON"})
    await hass.async_block_till_done()
    assert hass.states.get("switch.hall_switch_fan").state == STATE_ON
    assert hass.states.get("switch.hall_switch_child_lock").state == STATE_ON

    push({"type": "device", "device_id": DEVICE_ID, "online": False})
    await hass.async_block_till_done()
    assert hass.states.get("switch.hall_switch_fan").state == STATE_UNAVAILABLE

    await hass.services.async_call(
        "update", "install", {ATTR_ENTITY_ID: "update.hall_switch_firmware"}, blocking=False
    )


async def test_cloud_new_and_removed_switches(
    hass: HomeAssistant,
    cloud: FakeCloud,
    live: list[Any],
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = cloud_entry()
    await _setup(hass, entry)
    cloud.devices.append(device(OTHER_ID, "LQ0000000000BB", name="Kitchen", access="control"))
    live[0]({"type": "channel", "device_id": OTHER_ID, "ch": 1, "state": "ON"})  # unknown switch: reload the list
    freezer.tick(timedelta(seconds=11))  # past the refresh debounce
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("switch.kitchen_ceiling").state == STATE_ON
    assert hass.states.get("switch.kitchen_child_lock") is None  # a member with Control can't lock
    assert hass.states.get("update.kitchen_firmware") is None  # nor update

    live[0]({"type": "device_removed", "device_id": OTHER_ID, "reason": "factory_reset"})
    await hass.async_block_till_done()
    assert device_registry.async_get_devices(identifiers={(DOMAIN, "LQ0000000000BB")}) == []


async def test_cloud_refreshes_and_stores_tokens(hass: HomeAssistant, cloud: FakeCloud, live: list[Any]) -> None:
    entry = cloud_entry(expires_in=0)
    await _setup(hass, entry)
    assert cloud.refreshes == 1
    assert entry.data[CONF_TOKENS]["refresh_token"] == "r2"


async def test_cloud_signed_out_starts_reauth(hass: HomeAssistant, aioclient_mock: Any, live: list[Any]) -> None:
    aioclient_mock.get("https://api.linqaura.com/devices", status=401)
    aioclient_mock.post("https://api.linqaura.com/auth/refresh", status=401)
    entry = cloud_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_local_entry_takes_the_switch_from_the_cloud(
    hass: HomeAssistant, cloud: FakeCloud, switch: FakeSwitch, live: list[Any], entity_registry: er.EntityRegistry
) -> None:
    cloud_e = cloud_entry()
    await _setup(hass, cloud_e)
    local_e = local_entry()
    await _setup(hass, local_e)
    entities = er.async_entries_for_config_entry(entity_registry, cloud_e.entry_id)
    assert entities == []  # nothing left on the cloud entry
    assert entity_registry.async_get("switch.hall_switch_ceiling").config_entry_id == local_e.entry_id

    # Removing the direct entry hands the switch back to the cloud entry
    await hass.config_entries.async_remove(local_e.entry_id)
    await hass.async_block_till_done()
    assert er.async_entries_for_config_entry(entity_registry, cloud_e.entry_id) != []


async def test_unload(hass: HomeAssistant, cloud: FakeCloud, live: list[Any]) -> None:
    entry = cloud_entry()
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_cloud_rename_reaches_the_device_list(hass: HomeAssistant, cloud: FakeCloud, live: list[Any]) -> None:
    await _setup(hass, cloud_entry())
    registry = dr.async_get(hass)

    def _dev(r: dr.DeviceRegistry) -> dr.DeviceEntry:
        (found,) = r.async_get_devices(identifiers={(DOMAIN, SERIAL)})
        return found

    assert _dev(registry).name == "Hall switch"
    live[0]({"type": "names", "device_id": DEVICE_ID, "name": "Kitchen", "channels": {}})
    await hass.async_block_till_done()
    assert _dev(registry).name == "Kitchen"
    # a name given in Home Assistant itself stays in front
    dev = _dev(registry)
    registry.async_update_device(dev.id, name_by_user="Mine")
    live[0]({"type": "names", "device_id": DEVICE_ID, "name": "Hall", "channels": {}})
    await hass.async_block_till_done()
    dev = _dev(registry)
    assert dev.name == "Hall" and dev.name_by_user == "Mine"


def test_default_name() -> None:
    from custom_components.linqaura.entity import default_name

    assert default_name("LQ28848548E964") == "LINQAURA SWITCH E964"
    assert default_name("TG4X", "28:84:85:48:E9:64") == "LINQAURA SWITCH E964"
