"""Claire's local control channel.

The daemon serves one Unix socket (:mod:`claire.control.server`) that carries the
same line-framed protocol it speaks on stdin and stdout, and GUI processes use
:mod:`claire.control.client` to talk to it.  The reason it exists is written up
in ``control.spec.md`` next to this file and in the server module's docstring:
when systemd runs the daemon, the desktop app owns neither its stdin nor its
stdout, and the chat window had no way to reach her at all.
"""

from .client import ControlClient
from .server import (
    CONTROL_SOCKET_ENV_VAR,
    STOP_TIMEOUT_SEC,
    ControlSocketError,
    ControlSocketInUseError,
    ControlSocketServer,
    control_socket_candidates,
    describe_socket_line,
    resolve_control_socket_path,
)

__all__ = [
    "CONTROL_SOCKET_ENV_VAR",
    "STOP_TIMEOUT_SEC",
    "ControlClient",
    "ControlSocketError",
    "ControlSocketInUseError",
    "ControlSocketServer",
    "control_socket_candidates",
    "describe_socket_line",
    "resolve_control_socket_path",
]
