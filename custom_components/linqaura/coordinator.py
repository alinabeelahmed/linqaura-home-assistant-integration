"""Coordinators: one per direct (local) switch, one per cloud account."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AuthFailed,
    CannotConnect,
    ChannelStates,
    CloudClient,
    LinqAuraError,
    LocalClient,
    LockedOut,
    Names,
    NoLocalKey,
    SetupMode,
    Tokens,
)
from .const import (
    CLOUD_RECONNECT_MAX_S,
    CLOUD_SCAN_INTERVAL,
    CONF_TOKENS,
    DOMAIN,
    LOCAL_SCAN_INTERVAL,
    LOCAL_SLOW_EVERY,
)

if TYPE_CHECKING:
    from . import LinqAuraConfigEntry

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------------------- direct (local)


@dataclass
class LocalData:
    states: ChannelStates
    names: Names | None = None
    diag: dict[str, Any] = field(default_factory=dict)


class LocalCoordinator(DataUpdateCoordinator[LocalData]):
    """Polls one switch on the home network."""

    config_entry: LinqAuraConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LinqAuraConfigEntry, client: LocalClient, serial: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {serial}",
            update_interval=LOCAL_SCAN_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.serial = serial
        self._polls = 0

    async def _async_update_data(self) -> LocalData:
        try:
            states = await self.client.channels()
            data = LocalData(states, self.data.names if self.data else None, self.data.diag if self.data else {})
            if self._polls % LOCAL_SLOW_EVERY == 0:
                data.names = await self.client.names()
                try:
                    data.diag = await self.client.diag()
                except LinqAuraError:
                    data.diag = {}
            self._polls += 1
            return data
        except (AuthFailed, NoLocalKey) as err:
            # Never retried: wrong handshakes count towards the switch's lockout
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="local_key_refused"
            ) from err
        except LockedOut as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="locked_out", retry_after=err.retry_s
            ) from err
        except SetupMode as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="setup_mode") from err
        except LinqAuraError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="cannot_reach_switch", translation_placeholders={"error": str(err)}
            ) from err

    @callback
    def apply_states(self, states: ChannelStates) -> None:
        """A command's answer carries the new states: show them at once."""
        if self.data is not None:
            self.async_set_updated_data(replace(self.data, states=states))


# --------------------------------------------------------------------------------------------------------- cloud


class CloudCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Every switch on one LinqAura account, by cloud device id, kept current by the live socket."""

    config_entry: LinqAuraConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LinqAuraConfigEntry, client: CloudClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} cloud",
            update_interval=CLOUD_SCAN_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.connected = False
        self._skip: set[str] = set()
        self._known: set[str] = set()

    def skip_serials(self, serials: set[str]) -> None:
        """Switches added directly (local entries): left out here."""
        self._skip = serials

    def serials(self) -> set[str]:
        return {d["serial"] for d in (self.data or {}).values()}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            devices = await self.client.devices()
        except AuthFailed as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="cloud_signed_out") from err
        except LinqAuraError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="cannot_reach_cloud", translation_placeholders={"error": str(err)}
            ) from err
        data = {
            str(d["id"]): d
            for d in devices
            if isinstance(d, dict) and d.get("id") and d.get("serial") and d.get("serial") not in self._skip
        }
        self._remove_stale(data)
        return data

    def _remove_stale(self, data: dict[str, dict[str, Any]]) -> None:
        """Switches no longer on the account (removed, factory reset, unshared) leave Home Assistant too."""
        current = {d["serial"] for d in data.values()}
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            serials = {ident for domain, ident in device.identifiers if domain == DOMAIN}
            if serials and not serials & current:
                registry.async_update_device(device.id, remove_config_entry_id=self.config_entry.entry_id)
        self._known = current

    # --- live socket ---

    async def run_live(self) -> None:
        """Keeps the live connection up for the entry's lifetime, reconnecting with back-off."""
        delay = 5.0
        while True:
            try:
                await self.client.listen(self._on_event, self._on_ready)
            except AuthFailed:
                self.connected = False
                self.async_update_listeners()
                self.config_entry.async_start_reauth(self.hass)
                return
            except CannotConnect as err:
                _LOGGER.debug("LinqAura live connection lost: %s", err)
            except Exception:  # noqa: BLE001 - the loop must survive anything and try again
                _LOGGER.exception("LinqAura live connection failed")
            if self.connected:
                self.connected = False
                delay = 5.0
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, CLOUD_RECONNECT_MAX_S)

    @callback
    def _on_ready(self) -> None:
        self.connected = True
        # Whatever happened while the socket was down
        self.hass.async_create_task(self.async_request_refresh(), eager_start=False)

    @callback
    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        device_id = str(event.get("device_id") or "")
        if kind in ("homes_changed", "claimed") or (device_id and self.data is not None and device_id not in self.data):
            if kind != "device_removed":
                self.hass.async_create_task(self.async_request_refresh(), eager_start=False)
            return
        if self.data is None or device_id not in self.data:
            return
        device = dict(self.data[device_id])
        if kind == "device_removed":
            data = {k: v for k, v in self.data.items() if k != device_id}
            self._remove_stale(data)
            self.async_set_updated_data(data)
            return
        if kind == "channel":
            device["channels"] = _patch_channel(device, event.get("ch"), state=event.get("state"))
        elif kind == "touch_lock":
            device["channels"] = _patch_channel(device, event.get("ch"), touch_lock=event.get("state") == "ON")
        elif kind == "child_lock":
            device["child_lock"] = event.get("state") == "ON"
        elif kind == "device":
            for key in ("status", "online", "firmware_version", "ota_state", "last_seen", "cloud_via"):
                if key in event:
                    device[key] = event[key]
        elif kind == "names":
            if isinstance(event.get("name"), str):
                device["name"] = event["name"]
            names = event.get("channels") if isinstance(event.get("channels"), dict) else {}
            kinds = event.get("kinds") if isinstance(event.get("kinds"), dict) else {}
            device["channels"] = [
                {**c, "name": names.get(str(c.get("ch")), c.get("name")), "kind": kinds.get(str(c.get("ch")), c.get("kind"))}
                for c in device.get("channels", [])
            ]
        elif kind in ("ota_result", "renamed"):
            self.hass.async_create_task(self.async_request_refresh(), eager_start=False)
            return
        else:
            return
        self.async_set_updated_data({**self.data, device_id: device})

    @callback
    def apply(self, device_id: str, **changes: Any) -> None:
        """Optimistic change after a command the cloud accepted (the switch's report follows on the socket)."""
        if self.data is None or device_id not in self.data:
            return
        self.async_set_updated_data({**self.data, device_id: {**self.data[device_id], **changes}})


def _patch_channel(device: dict[str, Any], ch: Any, **changes: Any) -> list[dict[str, Any]]:
    return [{**c, **changes} if c.get("ch") == ch else c for c in device.get("channels", [])]


def tokens_saver(hass: HomeAssistant, entry: LinqAuraConfigEntry):
    """Stores every new token pair in the entry at once (a refresh token works only once)."""

    def save(tokens: Tokens) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKENS: tokens.as_dict()})

    return save
