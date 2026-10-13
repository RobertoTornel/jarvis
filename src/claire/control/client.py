"""The client half of Claire's control socket.

The daemon serves one Unix socket (:mod:`claire.control.server`) carrying the
same line-framed protocol it speaks on stdin and stdout.  This is the other end
of it, for a UI process that did not spawn the daemon and therefore has no stdin
to write to and no stdout to read.

Deliberately stdlib-only and free of Qt, so the desktop app and the web panel can
both use it — and so a test can drive the daemon without a display.  The protocol
prefixes are imported from :mod:`claire.daemon` *inside* the methods that build a
line, not at module import: this client is imported by GUI code at startup, and a
client that drags the whole daemon (and its optional speech stack) into a process
just to send a line would be a poor trade.  Nothing here connects at import time.

Two conventions worth knowing before using it:

* ``submit_chat``, ``cancel_chat`` and ``new_session`` connect on demand.  A
  caller that wants to know whether a channel exists *before* offering the user
  a chat box should call :meth:`connect` and look at the answer; these three are
  for the moment after that, where a failure must be reported rather than raise.
* ``read_line`` returns ``None`` both when nothing arrived in time and when the
  other end closed.  That is why :attr:`connected` exists: after a ``None``, ask
  ``client.connected`` and a closed channel is not mistaken for a quiet one.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Optional

from .server import resolve_control_socket_path

__all__ = ["ControlClient"]

#: How long a connect attempt waits before deciding there is no daemon.
CONNECT_TIMEOUT_SEC = 2.0

#: How long a send waits for buffer space.  A local socket with a live reader
#: never comes close to this; it exists so a wedged reader cannot hang a UI.
SEND_TIMEOUT_SEC = 5.0


class ControlClient:
    """One line-framed connection to the daemon's control socket.

    ``path=None`` uses :func:`claire.control.server.resolve_control_socket_path`,
    so a client follows the same override (``$CLAIRE_CONTROL_SOCKET``) the daemon
    does and an isolated instance is reachable without extra wiring.
    """

    def __init__(self, path: Optional[Any] = None, *, timeout: float = CONNECT_TIMEOUT_SEC) -> None:
        self._path = Path(path).expanduser() if path is not None else resolve_control_socket_path()
        self._timeout = timeout
        self._socket: Optional[socket.socket] = None
        self._buffer = b""

    # -- introspection -------------------------------------------------------
    @staticmethod
    def default_path() -> Path:
        """Where the client would look for the daemon's socket."""
        return resolve_control_socket_path()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connected(self) -> bool:
        return self._socket is not None

    # -- lifecycle -----------------------------------------------------------
    def connect(self) -> bool:
        """Try to reach the daemon.  Returns False instead of raising.

        Every failure mode is the same answer to the caller — there is no daemon
        to talk to — and a UI must be able to ask that question on a timer
        without handling exceptions.
        """
        if self._socket is not None:
            return True
        if not self._path.exists():
            return False

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout)
        try:
            connection.connect(str(self._path))
        except OSError:
            connection.close()
            return False

        # Reads block with their own timeout; the connect timeout still applies
        # to the first write only, which is what send_line resets.
        connection.settimeout(SEND_TIMEOUT_SEC)
        self._socket = connection
        self._buffer = b""
        return True

    def close(self) -> None:
        connection, self._socket = self._socket, None
        self._buffer = b""
        if connection is None:
            return
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    # -- sending -------------------------------------------------------------
    def send_line(self, line: str) -> bool:
        """Write one raw protocol line.  False when it could not be sent."""
        connection = self._socket
        if connection is None:
            return False
        payload = (line.rstrip("\n") + "\n").encode("utf-8", "replace")
        try:
            connection.settimeout(SEND_TIMEOUT_SEC)
            connection.sendall(payload)
        except OSError:
            self.close()
            return False
        return True

    def submit_chat(self, text: str) -> bool:
        """Ask the daemon to answer *text* as a text turn (tools included)."""
        from claire.daemon import CHAT_QUERY_IPC_PREFIX

        if not isinstance(text, str):
            return False
        if not self.connected and not self.connect():
            return False
        return self.send_line(f"{CHAT_QUERY_IPC_PREFIX}{json.dumps({'text': text})}")

    def cancel_chat(self) -> bool:
        """Abandon the in-flight text turn, if there is one."""
        from claire.daemon import CHAT_CANCEL_IPC_PREFIX

        if not self.connected and not self.connect():
            return False
        return self.send_line(CHAT_CANCEL_IPC_PREFIX)

    def new_session(self) -> bool:
        """Start a fresh conversation in the daemon's shared dialogue memory."""
        from claire.daemon import CHAT_NEW_SESSION_IPC_PREFIX

        if not self.connected and not self.connect():
            return False
        return self.send_line(CHAT_NEW_SESSION_IPC_PREFIX)

    # -- receiving -----------------------------------------------------------
    def read_line(self, timeout: Optional[float] = None) -> Optional[str]:
        """Return the next line the daemon sent, or None.

        ``None`` means nothing arrived within *timeout* (blocking forever when it
        is None) **or** that the daemon closed the connection; check
        :attr:`connected` to tell those apart.
        """
        connection = self._socket
        if connection is None:
            return None

        if timeout is not None:
            connection.settimeout(timeout)
        else:
            connection.settimeout(None)

        while True:
            line = self._take_buffered_line()
            if line is not None:
                return line
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                self.close()
                return None
            if not chunk:
                self.close()
                return None
            self._buffer += chunk

    def _take_buffered_line(self) -> Optional[str]:
        if b"\n" not in self._buffer:
            return None
        raw, self._buffer = self._buffer.split(b"\n", 1)
        return raw.decode("utf-8", "replace")
