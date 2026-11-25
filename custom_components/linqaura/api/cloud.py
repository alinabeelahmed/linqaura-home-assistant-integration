"""The LinqAura cloud API (https://api.linqaura.com): sign-in, switches, commands and the live event socket.

Sign-in gives an access token (60 min) and a refresh token that works once: every refresh returns a new pair, and
`on_tokens` is called with it before it is used, so the caller can store it at once. Presenting a spent refresh token
makes the cloud end every session of the account (it looks stolen), so two refreshes never run at the same time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from .exceptions import AuthFailed, CannotConnect, RequestRefused

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.linqaura.com"
TIMEOUT = aiohttp.ClientTimeout(total=20)
WS_PING_S = 30


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    @classmethod
    def from_reply(cls, data: dict[str, Any]) -> Tokens:
        try:
            return cls(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                expires_at=time.time() + int(data.get("expires_in", 3600)),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise CannotConnect("bad sign-in answer") from err

    def as_dict(self) -> dict[str, Any]:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token, "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tokens:
        return cls(str(data["access_token"]), str(data["refresh_token"]), float(data["expires_at"]))


TokenCallback = Callable[[Tokens], Awaitable[None] | None]


class CloudClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = DEFAULT_BASE_URL,
        tokens: Tokens | None = None,
        on_tokens: TokenCallback | None = None,
    ) -> None:
        self._http = session
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self._on_tokens = on_tokens
        self._refresh_lock = asyncio.Lock()

    # --- sign-in ---

    async def _raw(self, method: str, path: str, *, json: Any = None, token: str | None = None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with self._http.request(method, self.base_url + path, json=json, headers=headers, timeout=TIMEOUT) as resp:
                if resp.status == 204:
                    return resp.status, None
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                return resp.status, body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

    async def _store(self, tokens: Tokens) -> None:
        self.tokens = tokens
        if self._on_tokens is not None:
            result = self._on_tokens(tokens)
            if result is not None:
                await result

    async def login(self, email: str, password: str) -> Tokens:
        status, body = await self._raw("POST", "/auth/login", json={"email": email, "password": password})
        if status in (401, 422):
            raise AuthFailed("wrong e-mail or password")
        if status == 429:
            raise RequestRefused("too_many_attempts", status)
        if status != 200 or not isinstance(body, dict):
            raise CannotConnect(f"sign-in failed: HTTP {status}")
        tokens = Tokens.from_reply(body)
        await self._store(tokens)
        return tokens

    async def logout(self) -> None:
        """Ends this sign-in (its refresh token stops working)."""
        if self.tokens is not None:
            await self._raw("POST", "/auth/logout", json={"refresh_token": self.tokens.refresh_token})
            self.tokens = None

    async def _refresh(self, stale: str | None) -> str:
        """A current access token. Only one refresh at a time; a caller whose token was already replaced just gets
        the new one."""
        async with self._refresh_lock:
            if self.tokens is None:
                raise AuthFailed("not signed in")
            if self.tokens.access_token != stale and self.tokens.expires_at - time.time() > 60:
                return self.tokens.access_token
            status, body = await self._raw("POST", "/auth/refresh", json={"refresh_token": self.tokens.refresh_token})
            if status in (401, 422):
                raise AuthFailed("the LinqAura sign-in has ended")
            if status != 200 or not isinstance(body, dict):
                raise CannotConnect(f"token refresh failed: HTTP {status}")
            await self._store(Tokens.from_reply(body))
            return self.tokens.access_token

    async def access_token(self) -> str:
        if self.tokens is None:
            raise AuthFailed("not signed in")
        if self.tokens.expires_at - time.time() <= 60:
            return await self._refresh(self.tokens.access_token)
        return self.tokens.access_token

    async def _call(self, method: str, path: str, json: Any = None) -> Any:
        token = await self.access_token()
        status, body = await self._raw(method, path, json=json, token=token)
        if status == 401:
            token = await self._refresh(token)
            status, body = await self._raw(method, path, json=json, token=token)
            if status == 401:
                raise AuthFailed("the LinqAura sign-in has ended")
        if 200 <= status < 300:
            return body
        detail = body.get("detail") if isinstance(body, dict) else None
        if status >= 500:
            raise CannotConnect(f"HTTP {status}: {detail}")
        raise RequestRefused(str(detail or f"HTTP {status}"), status)

    # --- account and switches ---

    async def me(self) -> dict[str, Any]:
        return await self._call("GET", "/auth/me")

    async def devices(self) -> list[dict[str, Any]]:
        body = await self._call("GET", "/devices")
        return body if isinstance(body, list) else []

    async def local_access(self, device_id: str) -> dict[str, Any]:
        """Includes `local_key`, which opens the switch's direct (local) connection."""
        return await self._call("GET", f"/devices/{device_id}/local-access")

    async def set_channel(self, device_id: str, ch: int, on: bool) -> None:
        await self._call("POST", f"/devices/{device_id}/channels/{ch}", {"state": "ON" if on else "OFF"})

    async def set_child_lock(self, device_id: str, on: bool) -> None:
        await self._call("POST", f"/devices/{device_id}/child-lock", {"state": "ON" if on else "OFF"})

    async def start_update(self, device_id: str, version: str | None = None) -> None:
        await self._call("POST", f"/devices/{device_id}/ota", {"version": version} if version else {})

    # --- live events ---

    @property
    def ws_url(self) -> str:
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://") :] + "/ws"
        return "ws://" + self.base_url.removeprefix("http://") + "/ws"

    async def listen(
        self, on_event: Callable[[dict[str, Any]], None], on_ready: Callable[[], None] | None = None
    ) -> None:
        """Runs one live connection until it closes. Raises AuthFailed when the cloud refuses the sign-in (close
        code 4401) even after a refresh, CannotConnect when the connection drops."""
        token = await self.access_token()
        for attempt in range(2):
            try:
                async with self._http.ws_connect(self.ws_url, heartbeat=WS_PING_S, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    await ws.send_json({"type": "auth", "token": token})
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        try:
                            data = msg.json()
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("type") == "ready":
                            if on_ready is not None:
                                on_ready()
                            continue
                        on_event(data)
                    if ws.close_code == 4401:
                        if attempt == 0:
                            token = await self._refresh(token)
                            continue
                        raise AuthFailed("the LinqAura sign-in has ended")
                    raise CannotConnect(f"live connection closed ({ws.close_code})")
            except (aiohttp.ClientError, TimeoutError) as err:
                raise CannotConnect(str(err) or type(err).__name__) from err
