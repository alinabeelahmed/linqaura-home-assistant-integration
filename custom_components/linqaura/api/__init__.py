"""LinqAura switch clients: direct (local protocol v2 on the home network) and cloud.

No Home Assistant imports here on purpose: this package can move to its own PyPI library (pylinqaura) unchanged,
which Home Assistant core requires before an integration can be added there.
"""

from .cloud import DEFAULT_BASE_URL, CloudClient, Tokens
from .exceptions import (
    AuthFailed,
    CannotConnect,
    LinqAuraError,
    LockedOut,
    NoLocalKey,
    ProtocolError,
    RequestRefused,
    SetupMode,
)
from .local import ChannelStates, LocalClient, Names
from .lqlp import Hello, identity

__all__ = [
    "DEFAULT_BASE_URL",
    "AuthFailed",
    "CannotConnect",
    "ChannelStates",
    "CloudClient",
    "Hello",
    "LinqAuraError",
    "LocalClient",
    "LockedOut",
    "Names",
    "NoLocalKey",
    "ProtocolError",
    "RequestRefused",
    "SetupMode",
    "Tokens",
    "identity",
]
