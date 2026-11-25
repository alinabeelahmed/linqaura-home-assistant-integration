"""Errors raised by the LinqAura clients."""

from __future__ import annotations


class LinqAuraError(Exception):
    """Anything that went wrong talking to a switch or the cloud."""


class CannotConnect(LinqAuraError):
    """The switch or the cloud did not answer (network, timeout, busy)."""


class AuthFailed(LinqAuraError):
    """Wrong local key / password, or the cloud sign-in is no longer valid. Never retry on your own: each wrong
    handshake counts towards the switch's lockout, which the owner's phones share."""


class LockedOut(LinqAuraError):
    """The switch refuses handshakes for a while after too many wrong passwords."""

    def __init__(self, retry_s: int | None) -> None:
        super().__init__(f"locked out, retry in {retry_s} s" if retry_s else "locked out")
        self.retry_s = retry_s


class NoLocalKey(LinqAuraError):
    """The switch has no local key (firmware before FR-3, or never got it from the cloud)."""


class SetupMode(LinqAuraError):
    """The switch is in setup mode (just factory reset): add it in the LinqAura app first."""


class RequestRefused(LinqAuraError):
    """The switch or the cloud understood the request and said no."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class ProtocolError(LinqAuraError):
    """An answer that does not follow the protocol."""
