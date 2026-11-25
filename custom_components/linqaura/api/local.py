"""Direct connection to a switch on the home network: POST http://<address>/api with local protocol v2.

One encrypted session per client, opened with the switch's local key (FR-3). Requests on one session must go one at a
time (the counters must strictly increase), so every call runs under one lock. The switch keeps up to three Wi-Fi
sessions (shared with the owner's phones) and closes one after 120 s without requests; a request for a closed session
gets a plaintext `no_session`, and the client then does one new handshake.

A wrong key is never retried here: five wrong handshakes lock the switch's Wi-Fi link for 15 minutes or more, for the
owner's phones too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .exceptions import (
    AuthFailed,
    CannotConnect,
    LockedOut,
    NoLocalKey,
    ProtocolError,
    RequestRefused,
    SetupMode,
)
from .lqlp import CONTEXT, Cipher, Hello, b64, unb64
from .spake2p import Prover, password_to_w0w1

_LOGGER = logging.getLogger(__name__)

# Measured on a switch (fw 0.7.17): hello 3.4 s, pake1 1.9 s, pake3 1.4 s, then 0.1 s per request
TIMEOUT = aiohttp.ClientTimeout(total=15)
CHANNELS = 4


@dataclass(frozen=True)
class ChannelStates:
    on: list[bool]
    locked: bool | None  # child lock (Interface Contract v0.17); None from older firmware
    touch_lock: list[bool] | None  # per button (v0.20); None from older firmware

    @classmethod
    def from_reply(cls, r: dict[str, Any]) -> ChannelStates:
        on = r.get("on")
        if not isinstance(on, list) or len(on) != CHANNELS:
            raise ProtocolError("bad channel states")
        locked = r.get("locked")
        touch = r.get("touch_lock")
        return cls(
            on=[v is True for v in on],
            locked=locked if isinstance(locked, bool) else None,
            touch_lock=[v is True for v in touch] if isinstance(touch, list) and len(touch) == CHANNELS else None,
        )


@dataclass(frozen=True)
class Names:
    name: str  # "" = the default name
    channels: list[str]


class LocalClient:
    """Talks to one switch. `key` is its 32-hex-digit local key."""

    def __init__(self, session: aiohttp.ClientSession, host: str, key: str) -> None:
        self._http = session
        self.host = host
        self._key = key
        self._lock = asyncio.Lock()
        self._cipher: Cipher | None = None
        self._sid: str | None = None
        self.hello: Hello | None = None

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"http://{host}/api"

    async def _post(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._http.post(self.url, json=message, timeout=TIMEOUT) as resp:
                text = await resp.text()
                if resp.status == 403:
                    raise SetupMode("the switch answers its hotspot only (setup mode)")
                if resp.status != 200:
                    raise CannotConnect(f"HTTP {resp.status}: {text[:80]}")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err
        try:
            data = json.loads(text)
        except ValueError as err:
            raise ProtocolError("reply is not JSON") from err
        if not isinstance(data, dict):
            raise ProtocolError("reply is not an object")
        return data

    @staticmethod
    def _check(reply: dict[str, Any]) -> dict[str, Any]:
        if reply.get("ok") is True:
            return reply
        err = str(reply.get("err") or "error")
        if err == "auth":
            raise AuthFailed("the switch refused the local key")
        if err == "locked":
            retry = reply.get("retry_s")
            raise LockedOut(retry if isinstance(retry, int) else None)
        raise RequestRefused(err)

    async def read_hello(self) -> Hello:
        """The switch's plaintext hello (serial, model, firmware). Needs no key and opens no session."""
        hello = Hello.from_json(await self._post({"op": "hello", "cred": "key"}))
        self.hello = hello
        return hello

    async def _handshake(self) -> None:
        """Opens a session. The salt from an earlier hello is reused (a hello costs seconds on the switch); if the
        switch's confirmation then doesn't match, it is read again once before the key counts as refused."""
        self._cipher = None
        self._sid = None
        fresh = self.hello is None
        hello = self.hello if self.hello is not None else await self.read_hello()
        try:
            await self._pake(hello)
        except AuthFailed:
            if fresh:
                raise
            await self._pake(await self.read_hello())

    async def _pake(self, hello: Hello) -> None:
        if hello.setup_mode:
            raise SetupMode("the switch is in setup mode")
        if hello.cred != "key":
            raise NoLocalKey("the switch has no local key")
        w0, w1 = await asyncio.get_running_loop().run_in_executor(
            None, password_to_w0w1, self._key.encode(), hello.salt, hello.iterations
        )
        prover = Prover(CONTEXT, w0, w1)
        r1 = self._check(await self._post({"op": "pake1", "pA": b64(prover.pA)}))
        try:
            cA = prover.round_two(unb64(r1.get("pB")))
        except ValueError as err:
            raise ProtocolError("bad share from the switch") from err
        if not prover.verify_peer(unb64(r1.get("cB"))):
            # Wrong key: stop before pake3, so nothing counts towards the switch's lockout
            raise AuthFailed("the switch's confirmation does not match the local key")
        r2 = self._check(await self._post({"op": "pake3", "cA": b64(cA)}))
        sid = r2.get("sid")
        self._sid = sid if isinstance(sid, str) else None
        assert prover.keys is not None
        self._cipher = Cipher(prover.keys.ke)
        _LOGGER.debug("session open with %s (%s)", hello.serial, self.host)

    async def _send(self, plain: dict[str, Any]) -> dict[str, Any] | None:
        """One request on the open session; None when the switch says the session is gone."""
        assert self._cipher is not None
        env = self._cipher.seal(plain)
        if self._sid:
            env["sid"] = self._sid
        reply = await self._post(env)
        if reply.get("op") not in ("sec", "evt"):
            if reply.get("err") == "no_session":
                return None
            self._check(reply)
            raise ProtocolError("plaintext reply to an encrypted request")
        return self._check(self._cipher.open(reply))

    async def request(self, plain: dict[str, Any]) -> dict[str, Any]:
        """Sends one request, opening (or reopening) the session when needed."""
        async with self._lock:
            if self._cipher is None:
                await self._handshake()
            try:
                reply = await self._send(plain)
            except ProtocolError:
                self._cipher = None  # out of step: start over next time
                raise
            except CannotConnect:
                self._cipher = None  # the switch may have restarted: its counters are gone
                raise
            if reply is None:
                _LOGGER.debug("session with %s was closed by the switch; opening a new one", self.host)
                await self._handshake()
                reply = await self._send(plain)
                if reply is None:
                    self._cipher = None
                    raise CannotConnect("the switch keeps closing the session")
            return reply

    def close(self) -> None:
        """Forgets the session (the switch drops it after 120 s idle)."""
        self._cipher = None
        self._sid = None

    # --- operations (owner sessions) ---

    async def info(self) -> dict[str, Any]:
        return await self.request({"op": "info"})

    async def diag(self) -> dict[str, Any]:
        return await self.request({"op": "diag"})

    async def channels(self) -> ChannelStates:
        return ChannelStates.from_reply(await self.request({"op": "channels"}))

    async def set_channel(self, ch: int, on: bool) -> ChannelStates:
        return ChannelStates.from_reply(await self.request({"op": "channel_set", "ch": ch, "on": on}))

    async def set_child_lock(self, on: bool) -> ChannelStates:
        return ChannelStates.from_reply(await self.request({"op": "lock_set", "on": on}))

    async def set_touch_lock(self, ch: int, on: bool) -> ChannelStates:
        return ChannelStates.from_reply(await self.request({"op": "touch_lock_set", "ch": ch, "on": on}))

    async def names(self) -> Names | None:
        """The switch's own names, or None from firmware without names_get (before Interface Contract v0.22)."""
        try:
            r = await self.request({"op": "names_get"})
        except RequestRefused as err:
            if err.code in ("unknown_op", "bad_request"):
                return None
            raise
        chans = r.get("channels")
        if not isinstance(chans, list) or len(chans) != CHANNELS:
            return None
        return Names(name=str(r.get("name") or ""), channels=[str(c or "") for c in chans])
