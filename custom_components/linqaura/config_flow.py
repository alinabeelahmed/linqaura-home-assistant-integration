"""Config flow: a LinqAura account (cloud) or one switch directly (local, also found by zeroconf).

A direct switch needs its local key, which nobody types or sees: Home Assistant reads it once from the LinqAura
account (the cloud entry when there is one, else a one-off sign-in whose session is ended straight away) and then
talks to the switch only on the home network. The password is never stored; the cloud entry keeps only its tokens.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import (
    DEFAULT_BASE_URL,
    AuthFailed,
    CloudClient,
    Hello,
    LinqAuraError,
    LocalClient,
    LockedOut,
    NoLocalKey,
    ProtocolError,
    RequestRefused,
    SetupMode,
    identity,
)
from .const import (
    CONF_BASE_URL,
    CONF_ENTRY_TYPE,
    CONF_LOCAL_KEY,
    CONF_MODEL,
    CONF_SERIAL,
    CONF_TOKENS,
    CONF_USER_ID,
    DOMAIN,
    ENTRY_CLOUD,
    ENTRY_LOCAL,
)
from .entity import default_name

_LOGGER = logging.getLogger(__name__)

EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))


def _sign_in_schema(email: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL, default=email) if email else vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        }
    )


class KeyNotFound(Exception):
    """The account has no switch with that serial."""


class LinqAuraConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._hello: Hello | None = None

    def _base_url(self) -> str:
        """The cloud entry's server on reauth / reconfigure, else the LinqAura cloud."""
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry().data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry().data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        return DEFAULT_BASE_URL

    # ------------------------------------------------------------------------------------------- entry points

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["cloud", "local"])

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        props = discovery_info.properties
        if props.get("proto") != "2" or not props.get("id"):
            return self.async_abort(reason="not_supported")
        host = str(discovery_info.ip_address)
        try:
            hello = await self._read_hello(host)
        except SetupMode:
            return self.async_abort(reason="setup_mode")
        except LinqAuraError:
            return self.async_abort(reason="cannot_connect")
        if identity(hello.serial) != props["id"]:
            return self.async_abort(reason="not_supported")
        if hello.setup_mode:
            return self.async_abort(reason="setup_mode")
        await self.async_set_unique_id(hello.serial)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        if any(hello.serial in e.runtime_data.cloud.serials() for e in self._cloud_entries()):
            return self.async_abort(reason="already_in_cloud")  # one way or the other, never both
        self._host, self._hello = host, hello
        self.context["title_placeholders"] = {"name": default_name(hello.serial)}
        return await self.async_step_discovered()

    async def async_step_discovered(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A switch was found: connect it directly (local control) or through the cloud."""
        assert self._hello is not None
        return self.async_show_menu(
            step_id="discovered",
            menu_options=["local_key", "cloud"],
            description_placeholders={"name": default_name(self._hello.serial)},
        )

    # ----------------------------------------------------------------------------------------------- cloud

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = self._base_url()
            client = CloudClient(async_get_clientsession(self.hass), base_url)
            try:
                await client.login(user_input[CONF_EMAIL].strip(), user_input[CONF_PASSWORD])
                me = await client.me()
            except AuthFailed:
                errors["base"] = "invalid_auth"
            except RequestRefused:
                errors["base"] = "too_many_attempts"
            except LinqAuraError:
                errors["base"] = "cannot_connect"
            else:
                assert client.tokens is not None
                # From a discovered switch the flow's id was the switch's serial; a cloud entry is the account
                await self.async_set_unique_id(str(me["id"]), raise_on_progress=False)
                data = {
                    CONF_ENTRY_TYPE: ENTRY_CLOUD,
                    CONF_EMAIL: str(me.get("email") or user_input[CONF_EMAIL]),
                    CONF_USER_ID: str(me["id"]),
                    CONF_BASE_URL: base_url,
                    CONF_TOKENS: client.tokens.as_dict(),
                }
                if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
                    entry = self._get_reauth_entry() if self.source == SOURCE_REAUTH else self._get_reconfigure_entry()
                    if entry.unique_id != str(me["id"]):
                        await client.logout()
                        return self.async_abort(reason="wrong_account")
                    return self.async_update_reload_and_abort(entry, data=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=data[CONF_EMAIL], data=data)
        email = None
        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            entry = self._get_reauth_entry() if self.source == SOURCE_REAUTH else self._get_reconfigure_entry()
            email = entry.data.get(CONF_EMAIL)
        return self.async_show_form(
            step_id="cloud",
            data_schema=self.add_suggested_values_to_schema(
                _sign_in_schema(email), user_input or {}
            ),
            errors=errors,
        )

    # ---------------------------------------------------------------------------------------------- direct

    async def _read_hello(self, host: str) -> Hello:
        return await LocalClient(async_get_clientsession(self.hass), host, "").read_hello()

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                hello = await self._read_hello(host)
            except SetupMode:
                errors["base"] = "setup_mode"
            except ProtocolError:
                errors["base"] = "not_linqaura"
            except LinqAuraError:
                errors["base"] = "cannot_connect"
            else:
                if hello.setup_mode:
                    errors["base"] = "setup_mode"
                else:
                    await self.async_set_unique_id(hello.serial)
                    self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                    self._host, self._hello = host, hello
                    return await self.async_step_local_key()
        return self.async_show_form(
            step_id="local",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_HOST): str}), user_input or {}
            ),
            errors=errors,
        )

    def _cloud_entries(self) -> list[Any]:
        return [
            e
            for e in self.hass.config_entries.async_loaded_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_CLOUD and e.runtime_data.cloud is not None
        ]

    @staticmethod
    async def _key_from(client: CloudClient, serial: str) -> str:
        for device in await client.devices():
            if device.get("serial") == serial:
                key = (await client.local_access(str(device["id"]))).get("local_key")
                if isinstance(key, str) and key:
                    return key
                raise NoLocalKey("the account has no local key for this switch")
        raise KeyNotFound

    async def _key_from_cloud_entries(self, serial: str) -> str | None:
        for entry in self._cloud_entries():
            try:
                return await self._key_from(entry.runtime_data.cloud.client, serial)
            except (KeyNotFound, LinqAuraError):
                continue
        return None

    async def async_step_local_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Gets the local key: from a LinqAura account already added, else from a one-off sign-in."""
        assert self._hello is not None
        key = await self._key_from_cloud_entries(self._hello.serial)
        if key is not None:
            return await self._finish_local(key)
        return await self.async_step_sign_in()

    async def async_step_sign_in(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._hello is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = self._base_url()
            client = CloudClient(async_get_clientsession(self.hass), base_url)
            try:
                await client.login(user_input[CONF_EMAIL].strip(), user_input[CONF_PASSWORD])
                try:
                    key = await self._key_from(client, self._hello.serial)
                finally:
                    try:
                        await client.logout()  # nothing of this sign-in is kept
                    except LinqAuraError:
                        pass
            except AuthFailed:
                errors["base"] = "invalid_auth"
            except KeyNotFound:
                errors["base"] = "not_on_account"
            except NoLocalKey:
                return self.async_abort(reason="no_local_key")
            except RequestRefused as err:
                errors["base"] = "too_many_attempts" if err.code == "too_many_attempts" else "cannot_connect"
            except LinqAuraError:
                errors["base"] = "cannot_connect"
            else:
                return await self._finish_local(key)
        return self.async_show_form(
            step_id="sign_in",
            data_schema=self.add_suggested_values_to_schema(_sign_in_schema(), user_input or {}),
            errors=errors,
            description_placeholders={"name": default_name(self._hello.serial)},
        )

    async def _finish_local(self, key: str) -> ConfigFlowResult:
        """Opens one session with the key before saving it."""
        assert self._hello is not None and self._host is not None
        client = LocalClient(async_get_clientsession(self.hass), self._host, key)
        try:
            await client.channels()
        except (AuthFailed, NoLocalKey):
            # The account's key and the switch's differ (the switch hasn't had the new one from the cloud yet)
            return self.async_abort(reason="key_refused")
        except LockedOut:
            return self.async_abort(reason="locked_out")
        except LinqAuraError:
            return self.async_abort(reason="cannot_connect")
        finally:
            client.close()
        data = {
            CONF_ENTRY_TYPE: ENTRY_LOCAL,
            CONF_HOST: self._host,
            CONF_SERIAL: self._hello.serial,
            CONF_MODEL: self._hello.model,
            CONF_LOCAL_KEY: key,
        }
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        return self.async_create_entry(title=default_name(self._hello.serial), data=data)

    # -------------------------------------------------------------------------------------- reauth / reconfigure

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_CLOUD:
            return await self.async_step_cloud()
        # A direct switch whose key changed (someone lost access, so the cloud made a new one): read it again
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", description_placeholders={"name": entry.title}
            )
        self._host = entry.data[CONF_HOST]
        try:
            self._hello = await self._read_hello(self._host)
        except LinqAuraError:
            return self.async_abort(reason="cannot_connect")
        if self._hello.serial != entry.data[CONF_SERIAL]:
            return self.async_abort(reason="wrong_switch")
        return await self.async_step_local_key()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_CLOUD:
            return await self.async_step_cloud()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                hello = await self._read_hello(host)
            except LinqAuraError:
                errors["base"] = "cannot_connect"
            else:
                if hello.serial != entry.data[CONF_SERIAL]:
                    return self.async_abort(reason="wrong_switch")
                return self.async_update_reload_and_abort(entry, data_updates={CONF_HOST: host})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_HOST): str}), user_input or {CONF_HOST: entry.data[CONF_HOST]}
            ),
            errors=errors,
        )
