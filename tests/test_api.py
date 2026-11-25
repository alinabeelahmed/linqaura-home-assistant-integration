"""The protocol package on its own (no Home Assistant)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.linqaura.api import AuthFailed, LocalClient, LockedOut, NoLocalKey, SetupMode, identity
from custom_components.linqaura.api import spake2p
from custom_components.linqaura.api.exceptions import ProtocolError
from custom_components.linqaura.api.lqlp import Cipher

from .fake_switch import IDENT, KEY, SERIAL, FakeSwitch

VECTORS = json.loads((Path(__file__).parent / "spake2p_vectors.json").read_text())


@pytest.mark.parametrize("v", VECTORS)
def test_spake2p_matches_matter_sdk_vectors(v: dict[str, str]) -> None:
    h = bytes.fromhex
    p = spake2p.Prover(h(v["context"]), int(v["w0"], 16), int(v["w1"], 16), h(v["prover_identity"]), h(v["verifier_identity"]), x=int(v["x"], 16))
    assert p.pA.hex() == v["X"]
    cA = p.round_two(h(v["Y"]))
    assert p.keys is not None
    assert (p.keys.ka.hex(), p.keys.ke.hex(), p.keys.kc_a.hex(), p.keys.kc_b.hex()) == (v["Ka"], v["Ke"], v["KcA"], v["KcB"])
    assert cA.hex() == v["MAC_KcA"]
    assert p.verify_peer(h(v["MAC_KcB"]))
    assert not p.verify_peer(bytes(32))
    sw = spake2p.Verifier(h(v["context"]), int(v["w0"], 16), h(v["L"]), h(v["prover_identity"]), h(v["verifier_identity"]), y=int(v["y"], 16))
    assert sw.pB.hex() == v["Y"]
    assert sw.round_one(p.pA).hex() == v["MAC_KcB"]
    assert sw.verify_peer(cA)


def test_bad_points_are_refused() -> None:
    with pytest.raises(ValueError):
        spake2p.decode_point(b"\x04" + bytes(64))
    with pytest.raises(ValueError):
        spake2p.decode_point(b"\x05" + bytes(64))


def test_identity_matches_the_switch() -> None:
    assert identity(SERIAL) == IDENT


def test_cipher_refuses_replays_and_tampering() -> None:
    c = Cipher(bytes(16))
    with pytest.raises(ProtocolError):
        c.open({"op": "sec", "n": 0, "ct": ""})
    with pytest.raises(ProtocolError):
        c.open({"op": "sec", "n": 1, "ct": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="})


class _Resp:
    def __init__(self, status: int, body: str) -> None:
        self.status, self._body = status, body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class FakeSession:
    """Just enough of aiohttp.ClientSession for LocalClient."""

    def __init__(self, switch: FakeSwitch) -> None:
        self.switch = switch
        self.status = 200
        self.drop_next = 0

    def post(self, url: str, json: dict[str, Any], timeout: Any = None) -> _Resp:
        import json as _json

        if self.drop_next:
            self.drop_next -= 1
            raise aiohttp.ServerDisconnectedError()
        if self.status != 200:
            return _Resp(self.status, "no")
        return _Resp(200, _json.dumps(self.switch.handle(json)))


async def test_session_then_encrypted_requests() -> None:
    sw = FakeSwitch()
    client = LocalClient(FakeSession(sw), "1.2.3.4", KEY)  # type: ignore[arg-type]
    states = await client.set_channel(2, True)
    assert states.on == [False, True, False, False]
    assert (await client.channels()).locked is False
    names = await client.names()
    assert names is not None and names.channels[0] == "Ceiling"
    assert sw.handshakes == 1


async def test_reopens_a_session_the_switch_closed() -> None:
    sw = FakeSwitch()
    client = LocalClient(FakeSession(sw), "1.2.3.4", KEY)  # type: ignore[arg-type]
    await client.channels()
    sw.drop_sessions()
    assert (await client.set_child_lock(True)).locked is True
    assert sw.handshakes == 2


async def test_wrong_key_never_reaches_pake3() -> None:
    sw = FakeSwitch()
    client = LocalClient(FakeSession(sw), "1.2.3.4", "ffeeddccbbaa99887766554433221100")  # type: ignore[arg-type]
    with pytest.raises(AuthFailed):
        await client.channels()
    assert sw.failed_pake3 == 0  # nothing counted towards the lockout


async def test_hello_errors() -> None:
    with pytest.raises(NoLocalKey):
        await LocalClient(FakeSession(FakeSwitch(cred="password")), "h", KEY).channels()  # type: ignore[arg-type]
    with pytest.raises(SetupMode):
        await LocalClient(FakeSession(FakeSwitch(setup=True)), "h", KEY).channels()  # type: ignore[arg-type]
    sw = FakeSwitch()
    sw.locked_out = 900
    with pytest.raises(LockedOut) as err:
        await LocalClient(FakeSession(sw), "h", KEY).channels()  # type: ignore[arg-type]
    assert err.value.retry_s == 900


async def test_old_firmware_without_names() -> None:
    sw = FakeSwitch()
    sw.names_supported = False
    assert await LocalClient(FakeSession(sw), "h", KEY).names() is None  # type: ignore[arg-type]


async def test_reuses_the_salt_and_rereads_it_when_it_changed() -> None:
    sw = FakeSwitch()
    client = LocalClient(FakeSession(sw), "1.2.3.4", KEY)  # type: ignore[arg-type]
    await client.channels()
    hellos = []
    orig = sw.handle
    sw.handle = lambda m: (hellos.append(m) if m.get("op") == "hello" else None) or orig(m)  # type: ignore[method-assign]
    sw.drop_sessions()
    await client.channels()
    assert hellos == []  # the salt from the first hello was used
    # The switch's credential changed (new salt, same key, e.g. after a reflash): one fresh hello, no lockout count
    from custom_components.linqaura.api.spake2p import verifier_from_password

    sw.salt = b"" * 16
    sw.w0, sw.L = verifier_from_password(KEY.encode(), sw.salt, sw.iterations)
    sw.drop_sessions()
    await client.channels()
    assert len(hellos) == 1
    assert sw.failed_pake3 == 0


async def test_a_phone_handshaking_at_the_same_moment_is_not_a_wrong_key() -> None:
    sw = FakeSwitch()
    sw.interrupt_next = 1
    client = LocalClient(FakeSession(sw), "1.2.3.4", KEY)  # type: ignore[arg-type]
    assert (await client.channels()).on == [False] * 4  # retried once
    from custom_components.linqaura.api import CannotConnect

    sw.interrupt_next = 2
    sw.drop_sessions()
    with pytest.raises(CannotConnect):  # still not AuthFailed: no reauth
        await client.channels()


async def test_a_dropped_connection_keeps_the_session() -> None:
    sw = FakeSwitch()
    session = FakeSession(sw)
    client = LocalClient(session, "1.2.3.4", KEY)  # type: ignore[arg-type]
    await client.channels()
    session.drop_next = 1
    assert (await client.set_channel(1, True)).on[0] is True
    assert sw.handshakes == 1  # no new handshake
    session.drop_next = 2
    from custom_components.linqaura.api import CannotConnect

    with pytest.raises(CannotConnect):
        await client.channels()
    assert (await client.channels()).on[0] is True
    assert sw.handshakes == 1
