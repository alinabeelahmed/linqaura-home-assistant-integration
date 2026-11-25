"""Config flow: cloud sign-in, direct switch by address and by zeroconf, reauth and reconfigure."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.linqaura.const import CONF_LOCAL_KEY, CONF_TOKENS, DOMAIN, ENTRY_CLOUD, ENTRY_LOCAL

from .conftest import HOST, USER_ID, FakeCloud, cloud_entry, device, local_entry
from .fake_switch import IDENT, KEY, SERIAL, FakeSwitch

SIGN_IN = {"email": "owner@example.com", "password": "correct-horse"}


def _zeroconf(host: str = HOST, ident: str = IDENT, proto: str = "2") -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        hostname="esp32s3.local.",
        name=f"lq-{ident}._linqaura._tcp.local.",
        port=80,
        type="_linqaura._tcp.local.",
        properties={"id": ident, "proto": proto},
    )


async def _menu(hass: HomeAssistant, choice: str) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": choice})


async def test_cloud_sign_in(hass: HomeAssistant, cloud: FakeCloud, live: list[Any]) -> None:
    result = await _menu(hass, "cloud")
    assert result["step_id"] == "cloud"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SIGN_IN, "password": "wrong-pass"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "owner@example.com"
    assert result["result"].unique_id == USER_ID
    data = result["data"]
    assert data["entry_type"] == ENTRY_CLOUD
    assert data[CONF_TOKENS]["refresh_token"] == "r1"
    assert "password" not in data


async def test_cloud_already_configured(hass: HomeAssistant, cloud: FakeCloud, live: list[Any]) -> None:
    cloud_entry().add_to_hass(hass)
    result = await _menu(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cloud_unreachable(hass: HomeAssistant, aioclient_mock: Any) -> None:
    aioclient_mock.post("https://api.linqaura.com/auth/login", status=502)
    result = await _menu(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_local_by_address_with_sign_in(hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud) -> None:
    result = await _menu(hass, "local")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    assert result["step_id"] == "sign_in"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SIGN_IN, "password": "nope-nope"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL
    assert result["data"][CONF_LOCAL_KEY] == KEY
    assert result["data"]["entry_type"] == ENTRY_LOCAL
    assert cloud.logged_out == ["r1"]  # the one-off sign-in was ended
    assert switch.handshakes >= 1  # checked before saving (and the new entry has its own session)


async def test_local_switch_not_on_account(hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud) -> None:
    cloud.devices = [device(serial="LQ000000000000")]
    result = await _menu(hass, "local")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["errors"] == {"base": "not_on_account"}


async def test_local_key_refused(hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud) -> None:
    cloud.local_key = "ffeeddccbbaa99887766554433221100"
    result = await _menu(hass, "local")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "key_refused"
    assert switch.failed_pake3 == 0


async def test_local_errors(hass: HomeAssistant, aioclient_mock: Any) -> None:
    aioclient_mock.post("http://9.9.9.9/api", json={"hello": "there"})
    aioclient_mock.post("http://8.8.8.8/api", status=403)
    result = await _menu(hass, "local")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "7.7.7.7"})
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "9.9.9.9"})
    assert result["errors"] == {"base": "not_linqaura"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "8.8.8.8"})
    assert result["errors"] == {"base": "setup_mode"}


async def _discover(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "discovered"
    assert result["menu_options"] == ["local_key", "cloud"]
    assert HOST not in str(result)  # no address shown
    return result


async def test_zeroconf_direct(hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud) -> None:
    result = await _discover(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "local_key"})
    assert result["step_id"] == "sign_in"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["entry_type"] == ENTRY_LOCAL
    assert result["data"][CONF_LOCAL_KEY] == KEY
    assert result["result"].unique_id == SERIAL


async def test_zeroconf_cloud(hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud, live: list[Any]) -> None:
    result = await _discover(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    assert result["step_id"] == "cloud"
    assert list(result["data_schema"].schema) == ["email", "password"]  # no server address
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["entry_type"] == ENTRY_CLOUD
    assert result["result"].unique_id == USER_ID
    await hass.async_block_till_done()
    # Heard again later: it's in Home Assistant through the cloud now, so it isn't offered again
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_cloud"


async def test_cloud_entry_ends_pending_discoveries(
    hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud, live: list[Any]
) -> None:
    await _discover(hass)
    entry = cloud_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_direct_by_address_uses_the_cloud_entry_for_the_key(
    hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud, live: list[Any]
) -> None:
    entry = cloud_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.hall_switch_ceiling") is not None

    result = await _menu(hass, "local")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    assert result["type"] is FlowResultType.CREATE_ENTRY  # no sign-in needed
    assert result["data"][CONF_LOCAL_KEY] == KEY
    await hass.async_block_till_done()
    # The switch moved from the cloud entry to the direct one: same entity, now from the local entry
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    assert reg.async_get("switch.hall_switch_ceiling").config_entry_id == result["result"].entry_id


async def test_zeroconf_updates_the_address(hass: HomeAssistant, aioclient_mock: Any) -> None:
    sw = FakeSwitch()

    async def respond(method: str, url: Any, data: Any) -> Any:
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

        return AiohttpClientMockResponse(method, url, json=sw.handle(data))

    aioclient_mock.post("http://1.2.3.99/api", side_effect=respond)
    entry = local_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf("1.2.3.99")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "1.2.3.99"


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (_zeroconf(proto="1"), "not_supported"),
        (_zeroconf(ident="00000000"), "not_supported"),
        (_zeroconf(host="7.7.7.7"), "cannot_connect"),
    ],
)
async def test_zeroconf_aborts(hass: HomeAssistant, switch: FakeSwitch, info: ZeroconfServiceInfo, reason: str) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_zeroconf_setup_mode(hass: HomeAssistant, switch: FakeSwitch) -> None:
    switch.setup = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["reason"] == "setup_mode"


async def test_local_reauth_reads_the_new_key(hass: HomeAssistant, switch: FakeSwitch, cloud: FakeCloud) -> None:
    entry = local_entry(local_key="ffeeddccbbaa99887766554433221100")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert switch.failed_pake3 == 0
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
    assert result["step_id"] == "sign_in"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_LOCAL_KEY] == KEY


async def test_cloud_reauth(hass: HomeAssistant, cloud: FakeCloud, live: list[Any]) -> None:
    entry = cloud_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "cloud"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKENS]["refresh_token"] == "r1"


async def test_cloud_reauth_other_account(hass: HomeAssistant, cloud: FakeCloud, aioclient_mock: Any) -> None:
    entry = cloud_entry(unique_id="someone-else")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SIGN_IN)
    assert result["reason"] == "wrong_account"


async def test_local_reconfigure(hass: HomeAssistant, switch: FakeSwitch, aioclient_mock: Any) -> None:
    entry = local_entry(host="5.5.5.5")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "7.7.7.7"})
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == HOST
