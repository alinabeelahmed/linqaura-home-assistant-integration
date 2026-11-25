"""LinqAura local protocol v2 (linqaura-docs-tasks/docs/contracts/local_protocol_v2.md): the handshake messages and
the AES-128-GCM envelopes with per-direction counters."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exceptions import ProtocolError
from .spake2p import hkdf_sha256

CONTEXT = b"LinqAura LQLP v2"
KEYS_INFO = b"LinqAura LQLP v2 keys"
PROTO = 2


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(text: Any) -> bytes:
    if not isinstance(text, str):
        raise ProtocolError("expected base64 text")
    try:
        return base64.b64decode(text, validate=True)
    except ValueError as err:
        raise ProtocolError("bad base64") from err


def identity(serial: str) -> str:
    """The 8 hex digits a switch announces (mDNS instance lq-<id>, TXT id): SHA-256("LinqAura" + serial)[0:4]."""
    return hashlib.sha256(b"LinqAura" + serial.encode()).digest()[:4].hex()


@dataclass(frozen=True)
class Hello:
    """What a switch says before the handshake (no secrets)."""

    serial: str
    model: str
    firmware: str
    setup_mode: bool
    cred: str | None
    salt: bytes
    iterations: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Hello:
        if data.get("ok") is not True or data.get("proto") != PROTO:
            raise ProtocolError("not a LinqAura local protocol v2 switch")
        serial, iterations = data.get("serial"), data.get("iter")
        if not isinstance(serial, str) or not isinstance(iterations, int) or not 1 <= iterations <= 100_000:
            raise ProtocolError("bad hello")
        return cls(
            serial=serial,
            model=str(data.get("model") or ""),
            firmware=str(data.get("fw") or ""),
            setup_mode=data.get("mode") == "setup",
            cred=data.get("cred") if isinstance(data.get("cred"), str) else None,
            salt=unb64(data.get("salt")),
            iterations=iterations,
        )


class Cipher:
    """Seals requests and opens replies once the handshake is done."""

    def __init__(self, ke: bytes) -> None:
        k = hkdf_sha256(ke, KEYS_INFO, 32)
        self._to_switch = AESGCM(k[:16])
        self._from_switch = AESGCM(k[16:])
        self._sent = 0
        self._received = 0

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return b"\x00" * 4 + counter.to_bytes(8, "big")

    def seal(self, plain: dict[str, Any]) -> dict[str, Any]:
        self._sent += 1
        data = json.dumps(plain, separators=(",", ":")).encode()
        return {"op": "sec", "n": self._sent, "ct": b64(self._to_switch.encrypt(self._nonce(self._sent), data, None))}

    def open(self, env: dict[str, Any]) -> dict[str, Any]:
        n = env.get("n")
        if env.get("op") not in ("sec", "evt") or not isinstance(n, int) or isinstance(n, bool) or n <= self._received:
            raise ProtocolError("unexpected envelope")
        try:
            plain = self._from_switch.decrypt(self._nonce(n), unb64(env.get("ct")), None)
        except InvalidTag as err:
            raise ProtocolError("envelope failed to decrypt") from err
        self._received = n
        try:
            data = json.loads(plain)
        except ValueError as err:
            raise ProtocolError("bad plaintext") from err
        if not isinstance(data, dict):
            raise ProtocolError("bad plaintext")
        return data


class SwitchCipher(Cipher):
    """The switch's side of the envelope (directions swapped), for tests."""

    def __init__(self, ke: bytes) -> None:
        super().__init__(ke)
        self._to_switch, self._from_switch = self._from_switch, self._to_switch
