"""Fixtures: a fake switch on http://1.2.3.4/api and a fake LinqAura cloud."""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.linqaura.api import DEFAULT_BASE_URL
from custom_components.linqaura.const import (
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

from .fake_switch import KEY, SERIAL, FakeSwitch

HOST = "1.2.3.4"
API = DEFAULT_BASE_URL
USER_ID = "7d3c0b7e-0000-4000-8000-000000000001"
DEVICE_ID = "5a1b2c3d-0000-4000-8000-0000000000aa"
OTHER_ID = "5a1b2c3d-0000-4000-8000-0000000000bb"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Generator[None]:
    yield


@pytest.fixture(autouse=True)
def unreachable(aioclient_mock: AiohttpClientMocker) -> None:
    """Nothing answers at 7.7.7.7."""
    aioclient_mock.post("http://7.7.7.7/api", exc=aiohttp.ClientConnectionError())


@pytest.fixture
def switch(aioclient_mock: AiohttpClientMocker) -> FakeSwitch:
    sw = FakeSwitch()

    async def respond(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, json=sw.handle(data))

    aioclient_mock.post(f"http://{HOST}/api", side_effect=respond)
    return sw


def device(device_id: str = DEVICE_ID, serial: str = SERIAL, **extra: Any) -> dict[str, Any]:
    return {
        "id": device_id,
        "serial": serial,
        "name": "Hall switch",
        "model": "TG4-S3",
        "status": "active",
        "access": "owner",
        "online": True,
        "firmware_version": "0.7.16",
        "latest_firmware": "0.7.17",
        "update_available": True,
        "firmware_update": None,
        "child_lock": False,
        "channels": [
            {"ch": 1, "name": "Ceiling", "state": "ON", "kind": "light"},
            {"ch": 2, "name": "Lamp", "state": "OFF", "kind": "socket"},
            {"ch": 3, "name": "Fan", "state": "OFF", "kind": "fan"},
            {"ch": 4, "name": "Spare", "state": None, "kind": "other"},
        ],
        **extra,
    }


class FakeCloud:
    def __init__(self, aioclient_mock: AiohttpClientMocker) -> None:
        self.devices = [device()]
        self.calls: list[tuple[str, str, Any]] = []
        self.password = "correct-horse"
        self.logged_out: list[str] = []
        self.refreshes = 0
        self.local_key = KEY
        m = aioclient_mock

        async def login(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
            if data["password"] != self.password:
                return AiohttpClientMockResponse(method, url, status=401, json={"detail": "Wrong e-mail or password"})
            return AiohttpClientMockResponse(method, url, json=_tokens("a1", "r1"))

        async def refresh(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
            self.refreshes += 1
            return AiohttpClientMockResponse(method, url, json=_tokens(f"a{self.refreshes + 1}", f"r{self.refreshes + 1}"))

        async def logout(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
            self.logged_out.append(data["refresh_token"])
            return AiohttpClientMockResponse(method, url, status=204)

        async def devices(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
            return AiohttpClientMockResponse(method, url, json=self.devices)

        async def local_access(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
            return AiohttpClientMockResponse(method, url, json={"ble_enabled": True, "local_key": self.local_key})

        async def command(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
            self.calls.append((method, str(url), data))
            return AiohttpClientMockResponse(method, url, status=202, json={"accepted": True})

        m.post(f"{API}/auth/login", side_effect=login)
        m.post(f"{API}/auth/refresh", side_effect=refresh)
        m.post(f"{API}/auth/logout", side_effect=logout)
        m.get(f"{API}/auth/me", json={"id": USER_ID, "email": "owner@example.com"})
        m.get(f"{API}/devices", side_effect=devices)
        for d in (DEVICE_ID, OTHER_ID):
            m.get(f"{API}/devices/{d}/local-access", side_effect=local_access)
            for ch in range(1, 5):
                m.post(f"{API}/devices/{d}/channels/{ch}", side_effect=command)
            m.post(f"{API}/devices/{d}/child-lock", side_effect=command)
            m.post(f"{API}/devices/{d}/ota", side_effect=command)


def _tokens(access: str, refresh: str) -> dict[str, Any]:
    return {"access_token": access, "token_type": "bearer", "expires_in": 3600, "refresh_token": refresh, "refresh_expires_in": 5184000}


@pytest.fixture
def cloud(aioclient_mock: AiohttpClientMocker) -> FakeCloud:
    return FakeCloud(aioclient_mock)


@pytest.fixture
def live() -> Generator[list[Any]]:
    """The live socket: tests push events through the handlers it was given."""
    handlers: list[Any] = []

    async def listen(self: Any, on_event: Any, on_ready: Any = None) -> None:
        handlers.append(on_event)
        if on_ready:
            on_ready()
        import asyncio

        await asyncio.Event().wait()

    with patch("custom_components.linqaura.api.cloud.CloudClient.listen", listen):
        yield handlers


def local_entry(**data: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        title="LinqAura C2F0",
        data={
            CONF_ENTRY_TYPE: ENTRY_LOCAL,
            "host": HOST,
            CONF_SERIAL: SERIAL,
            CONF_MODEL: "TG4-S3",
            CONF_LOCAL_KEY: KEY,
            **data,
        },
    )


def cloud_entry(expires_in: float = 3600, unique_id: str = USER_ID) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        title="owner@example.com",
        data={
            CONF_ENTRY_TYPE: ENTRY_CLOUD,
            "email": "owner@example.com",
            CONF_USER_ID: USER_ID,
            CONF_BASE_URL: API,
            CONF_TOKENS: {"access_token": "a0", "refresh_token": "r0", "expires_at": time.time() + expires_in},
        },
    )
