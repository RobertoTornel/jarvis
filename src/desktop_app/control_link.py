"""Reach a Claire the desktop app did not start, over her control socket.

The app can spawn the daemon itself, and then chat travels over the daemon's
stdin and stdout.  When systemd runs the daemon — the owner's arrangement — the
app owns neither stream: it follows the journal to *see* her, and has no way to
*speak* to her.  That is the gap this module fills.  The daemon serves a Unix
socket carrying the same line-framed protocol (:mod:`claire.control`); this is
the app's end of it, and it is deliberately Qt-free so it can be tested without
a display.

Its contract with the chat window is small and worth stating exactly:

* :meth:`start` makes one connection attempt immediately, so the app can decide
  what to tell the user before the window is shown, and then keeps a supervisor
  thread trying for as long as the app lives.  The owner restarts Claire often;
  a chat box that only works if the daemon happened to be up when the app
  launched would be another silent failure waiting to happen.
* :meth:`submit`, :meth:`cancel` and :meth:`new_session` return ``False`` only
  when the message is *known* not to have been delivered.  The chat window turns
  that into a visible message; it is never a silent drop.
* :meth:`available` is the app's answer to "may I enable the chat input?" — the
  event stream a UI needs cannot be faked, so an unreachable socket means chat
  is off, with a stated reason, rather than enabled and inert.
* Lines are forwarded only when they are ``__CHAT__:`` events.  The daemon
  broadcasts its whole IPC stream to the socket, but voice feedback and diary
  chunks already reach this app through the journal it follows; forwarding them
  here as well would handle the same event twice.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

#: How long to wait between reconnection attempts while the daemon is away.
DEFAULT_RETRY_INTERVAL_SEC = 2.0

#: How long one read blocks before the supervisor loop checks whether to stop.
READ_TIMEOUT_SEC = 0.5


class ControlLink:
    """A live connection to a supervised Claire's control socket.

    ``on_line`` receives each ``__CHAT__:`` line on the supervisor thread, so a
    Qt caller must marshal it to the main thread (the app emits it through
    ``ChatIpcSignals.line_received``, exactly as its subprocess log reader does).
    ``on_state_change(available, reason)`` is called only on a transition.
    """

    def __init__(
        self,
        *,
        on_line: Optional[Callable[[str], None]] = None,
        on_state_change: Optional[Callable[[bool, str], None]] = None,
        path: Optional[str] = None,
        retry_interval: float = DEFAULT_RETRY_INTERVAL_SEC,
        client_factory: Optional[Callable[[], Any]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_line = on_line
        self._on_state_change = on_state_change
        self._path = path
        self._retry_interval = retry_interval
        self._client_factory = client_factory
        self._logger = logger

        self._lock = threading.RLock()
        self._client: Optional[Any] = None
        # Tri-state: ``None`` means "we have not looked yet", so the first answer
        # - even a negative one - is announced. Starting at False would make the
        # first failure look like no change and the app would never be told.
        self._available: Optional[bool] = None
        self._reason = "not started yet"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- what the app asks ---------------------------------------------------
    @property
    def available(self) -> bool:
        with self._lock:
            return self._available is True

    @property
    def reason(self) -> str:
        """Why the channel is not available, in words fit for a status line."""
        with self._lock:
            return self._reason

    def start(self) -> bool:
        """Try to connect now and keep trying in the background.  True when up."""
        self._stop.clear()
        connected = self._connect()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._supervise, name="claire-control-link", daemon=True
            )
            self._thread.start()
        return connected

    def close(self) -> None:
        """Stop supervising and drop the connection.  Safe to call twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0 * READ_TIMEOUT_SEC)
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            self._close_client(client)

    # -- what the chat window calls -----------------------------------------
    def submit(self, text: str) -> bool:
        """Send a text turn.  False means it was definitely not delivered."""
        return self._send(lambda client: client.submit_chat(text))

    def cancel(self) -> bool:
        """Abandon the in-flight turn, if any."""
        return self._send(lambda client: client.cancel_chat())

    def new_session(self) -> bool:
        """Ask the daemon to start a fresh conversation."""
        return self._send(lambda client: client.new_session())

    def _send(self, action: Callable[[Any], bool]) -> bool:
        client = self._ensure_client()
        if client is None:
            return False
        try:
            delivered = action(client)
        except Exception as exc:  # noqa: BLE001 - a broken channel is not a crash
            self._log(f"control link send failed ({type(exc).__name__}: {exc})")
            self._drop("the control socket did not accept a message")
            return False
        if delivered is False:
            self._drop("the control socket did not accept a message")
            return False
        return True

    # -- the supervisor ------------------------------------------------------
    def _supervise(self) -> None:
        from claire.daemon import CHAT_IPC_PREFIX

        while not self._stop.is_set():
            client = self._ensure_client()
            if client is None:
                # _connect already announced the failure; wait before retrying.
                self._stop.wait(self._retry_interval)
                continue
            try:
                line = client.read_line(timeout=READ_TIMEOUT_SEC)
            except Exception as exc:  # noqa: BLE001 - never kill the thread
                self._log(f"control link read failed ({type(exc).__name__}: {exc})")
                self._drop("the control socket failed while reading")
                continue
            if line is None:
                if not getattr(client, "connected", False):
                    self._drop("the control socket closed")
                continue
            if line.startswith(CHAT_IPC_PREFIX) and self._on_line is not None:
                try:
                    self._on_line(line)
                except Exception as exc:  # noqa: BLE001 - one bad line, not the link
                    self._log(f"control link handler failed ({type(exc).__name__}: {exc})")

    def _ensure_client(self) -> Optional[Any]:
        """The live client, connecting if needed.  None when there is no daemon."""
        with self._lock:
            client = self._client
            if client is not None and getattr(client, "connected", False):
                return client
        if not self._connect():
            return None
        with self._lock:
            # Return whatever _connect installed, not its boolean: an earlier
            # version of this returned the bool, so every *reconnect* handed the
            # supervisor a True where it expected a client (AttributeError, in a
            # loop, thousands of times a second).
            return self._client

    def _connect(self) -> bool:
        client = None
        try:
            client = (self._client_factory or self._make_client)()
            connected = bool(client.connect())
        except Exception as exc:  # noqa: BLE001 - a missing module is "unavailable"
            if client is not None:
                self._close_client(client)
            self._set_state(False, f"the control socket could not be opened ({type(exc).__name__})")
            self._log(f"control link connect failed ({type(exc).__name__}: {exc})")
            return False

        if not connected:
            self._close_client(client)
            self._set_state(False, "no daemon is answering on her control socket")
            return False

        with self._lock:
            previous, self._client = self._client, client
        if previous is not None and previous is not client:
            self._close_client(previous)
        self._set_state(True, "")
        self._log("control link connected")
        return True

    def _make_client(self) -> Any:
        # Imported here, not at module import: this link is optional plumbing and
        # the app must start whether or not the channel is present.
        from claire.control.client import ControlClient

        return ControlClient(self._path) if self._path else ControlClient()

    def _drop(self, reason: str) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            self._close_client(client)
        self._set_state(False, reason)

    def _set_state(self, available: bool, reason: str) -> None:
        """Record the state and tell the app, but only when availability flips.

        Two different ways of being unavailable in a row (the socket closed, then
        the reconnect attempt found no daemon) are one piece of news, not two: the
        app needs to know whether chat works, and the current reason stays
        readable through :attr:`reason` for the status line.
        """
        with self._lock:
            changed = available != self._available
            self._available = available
            self._reason = reason
        if not changed or self._on_state_change is None:
            return
        try:
            self._on_state_change(available, reason)
        except Exception as exc:  # noqa: BLE001
            self._log(f"control link state callback failed ({type(exc).__name__}: {exc})")

    @staticmethod
    def _close_client(client: Any) -> None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001
            pass
