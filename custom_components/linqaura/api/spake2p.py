"""SPAKE2+ on P-256 / SHA-256 / HKDF / HMAC, as the Matter SDK's Spake2p_P256_SHA256_HKDF_HMAC (draft-01).

The switch runs the SDK's verifier; this is the prover (and, for tests, a verifier). Same maths as the LinqAura app
(lib/local/spake2p.dart), checked against the SDK's RFC test vectors. The curve arithmetic is plain Python: a
handshake needs a handful of scalar multiplications, and that keeps the package free of native dependencies.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass

# NIST P-256
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

Point = tuple[int, int] | None  # affine; None is the point at infinity


def _on_curve(pt: Point) -> bool:
    if pt is None:
        return False
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def _to_jac(pt: Point) -> tuple[int, int, int]:
    return (1, 1, 0) if pt is None else (pt[0], pt[1], 1)


def _from_jac(j: tuple[int, int, int]) -> Point:
    x, y, z = j
    if z == 0:
        return None
    zi = pow(z, -1, P)
    zi2 = zi * zi % P
    return (x * zi2 % P, y * zi2 * zi % P)


def _jdouble(j: tuple[int, int, int]) -> tuple[int, int, int]:
    x, y, z = j
    if z == 0 or y == 0:
        return (1, 1, 0)
    yy = y * y % P
    s = 4 * x * yy % P
    zz = z * z % P
    m = 3 * (x - zz) * (x + zz) % P  # a = -3
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * yy * yy) % P
    z3 = 2 * y * z % P
    return (x3, y3, z3)


def _jadd(j1: tuple[int, int, int], j2: tuple[int, int, int]) -> tuple[int, int, int]:
    x1, y1, z1 = j1
    x2, y2, z2 = j2
    if z1 == 0:
        return j2
    if z2 == 0:
        return j1
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    if u1 == u2:
        return _jdouble(j1) if s1 == s2 else (1, 1, 0)
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hhh) % P
    z3 = h * z1 * z2 % P
    return (x3, y3, z3)


def point_add(p1: Point, p2: Point) -> Point:
    return _from_jac(_jadd(_to_jac(p1), _to_jac(p2)))


def point_mul(k: int, pt: Point) -> Point:
    k %= N
    if k == 0 or pt is None:
        return None
    acc = (1, 1, 0)
    base = _to_jac(pt)
    for bit in bin(k)[2:]:
        acc = _jdouble(acc)
        if bit == "1":
            acc = _jadd(acc, base)
    return _from_jac(acc)


G: Point = (GX, GY)


def encode_point(pt: Point) -> bytes:
    """Uncompressed SEC1 (65 bytes)."""
    if pt is None:
        raise ValueError("point at infinity")
    return b"\x04" + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def decode_point(data: bytes) -> Point:
    """SEC1, uncompressed or compressed; raises ValueError for anything not on the curve."""
    if len(data) == 65 and data[0] == 4:
        pt = (int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:], "big"))
    elif len(data) == 33 and data[0] in (2, 3):
        x = int.from_bytes(data[1:], "big")
        y = pow((x * x * x + A * x + B) % P, (P + 1) // 4, P)
        if y & 1 != data[0] & 1:
            y = P - y
        pt = (x, y)
    else:
        raise ValueError("bad point encoding")
    if not _on_curve(pt):
        raise ValueError("point not on P-256")
    return pt


# Fixed points from draft-01
M_POINT = decode_point(bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"))
N_POINT = decode_point(bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"))


def hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def hkdf_sha256(ikm: bytes, info: bytes, length: int, salt: bytes = b"") -> bytes:
    prk = hmac_sha256(salt or b"\x00" * 32, ikm)
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac_sha256(prk, block + info + bytes([i]))
        out += block
        i += 1
    return out[:length]


def password_to_w0w1(password: bytes, salt: bytes, iterations: int) -> tuple[int, int]:
    """w0s || w1s = PBKDF2-HMAC-SHA256(password, salt, iterations, 80 bytes); each half mod n."""
    ws = hashlib.pbkdf2_hmac("sha256", password, salt, iterations, 80)
    return int.from_bytes(ws[:40], "big") % N, int.from_bytes(ws[40:], "big") % N


def _random_scalar() -> int:
    return secrets.randbelow(N - 1) + 1


def _transcript(*fields: bytes) -> bytes:
    d = hashlib.sha256()
    for f in fields:
        d.update(struct.pack("<Q", len(f)))
        d.update(f)
    return d.digest()


@dataclass(frozen=True)
class Keys:
    ka: bytes
    ke: bytes
    kc_a: bytes
    kc_b: bytes


def _keys(context: bytes, prover_id: bytes, verifier_id: bytes, x_enc: bytes, y_enc: bytes, z: Point, v: Point, w0: int) -> Keys:
    kae = _transcript(
        context,
        prover_id,
        verifier_id,
        encode_point(M_POINT),
        encode_point(N_POINT),
        x_enc,
        y_enc,
        encode_point(z),
        encode_point(v),
        w0.to_bytes(32, "big"),
    )
    ka, ke = kae[:16], kae[16:32]
    kc = hkdf_sha256(ka, b"ConfirmationKeys", 32)
    return Keys(ka, ke, kc[:16], kc[16:])


class Prover:
    """The app side (here: Home Assistant)."""

    def __init__(
        self,
        context: bytes,
        w0: int,
        w1: int,
        prover_id: bytes = b"",
        verifier_id: bytes = b"",
        x: int | None = None,
    ) -> None:
        self._context, self._pid, self._vid = context, prover_id, verifier_id
        self._w0, self._w1 = w0, w1
        self._x = x if x is not None else _random_scalar()
        self.pA = encode_point(point_add(point_mul(self._x, G), point_mul(w0, M_POINT)))
        self.keys: Keys | None = None

    def round_two(self, pB: bytes) -> bytes:
        """Takes the switch's share Y; returns cA and keeps the keys. Raises ValueError for a bad point."""
        y = decode_point(pB)
        y_minus = point_add(y, point_mul(N - self._w0, N_POINT))
        if y_minus is None:
            raise ValueError("bad share")
        z = point_mul(self._x, y_minus)
        v = point_mul(self._w1, y_minus)
        self.keys = _keys(self._context, self._pid, self._vid, self.pA, pB, z, v, self._w0)
        return hmac_sha256(self.keys.kc_a, pB)

    def verify_peer(self, cB: bytes) -> bool:
        if self.keys is None:
            return False
        return hmac.compare_digest(hmac_sha256(self.keys.kc_b, self.pA), cB)


class Verifier:
    """The switch side, from w0 and L = w1·G. Used by the tests' fake switch."""

    def __init__(
        self,
        context: bytes,
        w0: int,
        L: bytes,
        prover_id: bytes = b"",
        verifier_id: bytes = b"",
        y: int | None = None,
    ) -> None:
        self._context, self._pid, self._vid = context, prover_id, verifier_id
        self._w0 = w0
        self._L = decode_point(L)
        self._y = y if y is not None else _random_scalar()
        self.pB = encode_point(point_add(point_mul(self._y, G), point_mul(w0, N_POINT)))
        self.keys: Keys | None = None
        self._pA = b""

    def round_one(self, pA: bytes) -> bytes:
        """Takes X; returns cB."""
        x = decode_point(pA)
        x_minus = point_add(x, point_mul(N - self._w0, M_POINT))
        if x_minus is None:
            raise ValueError("bad share")
        z = point_mul(self._y, x_minus)
        v = point_mul(self._y, self._L)
        self.keys = _keys(self._context, self._pid, self._vid, pA, self.pB, z, v, self._w0)
        self._pA = pA
        return hmac_sha256(self.keys.kc_b, pA)

    def verify_peer(self, cA: bytes) -> bool:
        if self.keys is None:
            return False
        return hmac.compare_digest(hmac_sha256(self.keys.kc_a, self.pB), cA)


def verifier_from_password(password: bytes, salt: bytes, iterations: int) -> tuple[int, bytes]:
    """(w0, L) as the switch stores them."""
    w0, w1 = password_to_w0w1(password, salt, iterations)
    return w0, encode_point(point_mul(w1, G))
