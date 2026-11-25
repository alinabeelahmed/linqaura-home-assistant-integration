"""A LinqAura switch's local protocol v2 (Wi-Fi link) in memory, with the real SPAKE2+ verifier and envelopes."""

from __future__ import annotations

import secrets
from typing import Any

from custom_components.linqaura.api.lqlp import CONTEXT, SwitchCipher, b64, unb64
from custom_components.linqaura.api.spake2p import Verifier, verifier_from_password

KEY = "00112233445566778899aabbccddeeff"
SERIAL = "LQ7C4FAD2BC2F0"
IDENT = "1847dd48"  # SHA-256("LinqAura" + SERIAL)[0:4]


class FakeSwitch:
    def __init__(self, key: str = KEY, serial: str = SERIAL, *, setup: bool = False, cred: str | None = "key") -> None:
        self.serial = serial
        self.setup = setup
        self.cred = cred
        self.salt = secrets.token_bytes(16)
        self.iterations = 1000
        self.w0, self.L = verifier_from_password(key.encode(), self.salt, self.iterations)
        self.on = [False, False, False, False]
        self.locked = False
        self.touch = [False, False, False, False]
        self.name = "Hall"
        self.channel_names = ["Ceiling", "", "Fan", ""]
        self.sessions: dict[str, SwitchCipher] = {}
        self._pending: Verifier | None = None
        self.failed_pake3 = 0
        self.handshakes = 0
        self.requests: list[dict[str, Any]] = []
        self.names_supported = True
        self.locked_out: int | None = None

    def drop_sessions(self) -> None:
        self.sessions.clear()

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg.get("op")
        if op == "hello":
            reply = {
                "ok": True,
                "proto": 2,
                "serial": self.serial,
                "model": "TG4-S3",
                "fw": "0.7.17",
                "mode": "setup" if self.setup else "owner",
                "salt": b64(self.salt),
                "iter": self.iterations,
            }
            if self.cred and not self.setup:
                reply["cred"] = self.cred
            return reply
        if op == "pake1":
            if self.locked_out:
                return {"ok": False, "err": "locked", "retry_s": self.locked_out}
            v = Verifier(CONTEXT, self.w0, self.L)
            cB = v.round_one(unb64(msg["pA"]))
            self._pending = v
            return {"ok": True, "pB": b64(v.pB), "cB": b64(cB)}
        if op == "pake3":
            v, self._pending = self._pending, None
            if v is None or not v.verify_peer(unb64(msg["cA"])):
                self.failed_pake3 += 1
                return {"ok": False, "err": "auth"}
            sid = secrets.token_hex(8)
            self.sessions[sid] = SwitchCipher(v.keys.ke)
            self.handshakes += 1
            return {"ok": True, "sid": sid}
        if op == "sec":
            cipher = self.sessions.get(msg.get("sid", ""))
            if cipher is None:
                return {"ok": False, "err": "no_session"}
            plain = cipher.open(msg)
            self.requests.append(plain)
            return cipher.seal(self._op(plain))
        return {"ok": False, "err": "bad_request"}

    def _states(self) -> dict[str, Any]:
        return {"ok": True, "on": list(self.on), "locked": self.locked, "touch_lock": list(self.touch)}

    def _op(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "channels":
            return self._states()
        if op == "channel_set":
            self.on[req["ch"] - 1] = req["on"]
            return self._states()
        if op == "lock_set":
            self.locked = req["on"]
            return self._states()
        if op == "touch_lock_set":
            self.touch[req["ch"] - 1] = req["on"]
            return self._states()
        if op == "names_get":
            if not self.names_supported:
                return {"ok": False, "err": "unknown_op"}
            return {"ok": True, "name": self.name, "channels": list(self.channel_names)}
        if op == "diag":
            return {"ok": True, "fw": "0.7.17", "uptime_s": 3600, "wifi": {"ssid": "Home", "rssi": -52, "ip": "1.2.3.4"}}
        if op == "info":
            return {"ok": True, "serial": self.serial, "model": "TG4-S3", "fw": "0.7.17", "onboarded": True}
        return {"ok": False, "err": "unknown_op"}
