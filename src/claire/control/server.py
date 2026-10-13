"""A local control socket for Claire, so an attached UI can reach her brain.

Claire's daemon can be run two ways.  The desktop app can spawn it as its own
subprocess, in which case chat travels over the daemon's **stdin and stdout**:
the app writes ``__CHAT_QUERY__:`` lines and reads ``__CHAT__:`` events back.
Or systemd can run the daemon on its own, and the desktop app merely *attaches*
to it — which is how it runs on the owner's machine.  There the app owns neither
the daemon's stdin (systemd points it at ``/dev/null``) nor its stdout (systemd
owns that socket, and the app can only follow the journal), so the chat window
had nowhere to send a message.  It did not say so: it enabled the input box,
called the chat submission in *its own* process where no daemon was ever booted,
and the resulting ``complete(None)`` appended nothing at all.  A message was
typed, "Claire is thinking…" appeared and vanished, and no reply and no error
ever came.

This module is the missing channel: a Unix domain socket, created by the daemon,
reachable only by the owner of the account, carrying **exactly the same
line-framed protocol** the daemon already speaks on stdin and stdout.  One line
per message, ``PREFIX + json.dumps(payload) + "\\n"``, using the daemon's own
prefix constants, so every parser that already reads those lines keeps working
unchanged and there is no second protocol to keep in step.

Why a Unix socket rather than a port: a socket under ``$XDG_RUNTIME_DIR`` (or the
data directory) has no network surface at all.  There is nothing to firewall, no
address to bind by mistake, and access is enforced by the filesystem — the
socket is mode 0600 in a directory mode 0700, so no other account and no remote
host can reach it, whatever the network looks like.

Honest limits, stated here rather than discovered later:

* The socket carries the daemon's **IPC events** (``__CHAT__:``, ``__VOICE__:``,
  diary chunks).  It is not a general RPC surface, and it deliberately has no
  command that is not already a stdin command.
* A client that dies mid-write is dropped rather than retried: an event stream is
  a live view, and a UI that was not listening at the time is expected to seed
  itself from the dialogue memory or the journal.
* Nothing here persists.  Remove the socket file and the channel is gone; the
  daemon rebuilds it on the next start.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Set

__all__ = [
    "CONTROL_SOCKET_ENV_VAR",
    "STOP_TIMEOUT_SEC",
    "ControlSocketError",
    "ControlSocketInUseError",
    "ControlSocketServer",
    "control_socket_candidates",
    "describe_socket_line",
    "resolve_control_socket_path",
]

#: Overrides the socket path entirely.  Also what lets an isolated test instance
#: run without touching the socket of the Claire the owner is actually using.
CONTROL_SOCKET_ENV_VAR = "CLAIRE_CONTROL_SOCKET"

#: How long ``stop()`` waits for the accept loop to notice and return.
STOP_TIMEOUT_SEC = 5.0

#: How long the accept loop blocks per iteration.  Short, so ``stop()`` is
#: prompt without needing to close the listening socket out from under a blocked
#: ``accept`` (which is what makes this kind of loop hang on some platforms).
ACCEPT_TIMEOUT_SEC = 0.5

#: How long one client read blocks before looping to check the stop flag.
CLIENT_READ_TIMEOUT_SEC = 0.5

#: How long a stale-socket probe waits for a reply before calling it stale.
LIVE_PROBE_TIMEOUT_SEC = 0.5

_SOCKET_DIRNAME = "claire"
_SOCKET_FILENAME = "control.sock"


class ControlSocketError(RuntimeError):
    """The socket could not be created, or is not ours to replace."""


class ControlSocketInUseError(ControlSocketError):
    """Another Claire is already serving this path.

    Distinct from the other failures because it must *not* send the server on to
    its next candidate path: two daemons, one per path, is exactly the confusion
    the control channel exists to remove.
    """


def resolve_control_socket_path() -> Path:
    """Where the socket is *preferred*: the first of :func:`control_socket_candidates`."""
    return control_socket_candidates()[0]


def control_socket_candidates() -> List[Path]:
    """The paths to try for the control socket, best first.

    Both ends walk this list in the same order, which is what lets the daemon
    land somewhere it is allowed to write without the client having to be told
    where it went.

    ``$CLAIRE_CONTROL_SOCKET`` is **exclusive** when set: an explicitly named
    path never falls back to another, or a test instance — or a second Claire —
    could end up talking to the wrong daemon.

    Otherwise the runtime directory is preferred, because that is what it is for:
    per-user, mode 0700, cleared on logout, so a stale file cannot outlive the
    session. The data directory is the fallback, and on this machine it is the
    one that works: ``claire.service`` sets ``ProtectSystem=strict`` with the
    user's home directories as the only writable paths, and under that sandbox
    binding a Unix socket under ``/run/user/<uid>`` fails with ``EROFS`` while the
    data directory — which the unit grants — succeeds. Falling back is better
    than requiring every deployment to get its unit file exactly right.
    """
    override = os.environ.get(CONTROL_SOCKET_ENV_VAR, "").strip()
    if override:
        return [Path(override).expanduser()]

    candidates: List[Path] = []
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        candidates.append(Path(runtime) / _SOCKET_DIRNAME / _SOCKET_FILENAME)

    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    candidates.append(base / _SOCKET_DIRNAME / _SOCKET_FILENAME)
    return candidates


class ControlSocketServer:
    """Serve one line-framed control socket to any number of local clients.

    ``handle_line`` receives each client line and returns whether it recognised
    it; the daemon passes its own stdin handlers, so the socket accepts exactly
    what the stdin monitor accepts and nothing more.  It is injected so a test
    can drive the transport without booting a daemon.

    ``logger`` receives diagnostic text (a stale socket replaced, a client that
    went away, a handler that raised).  A logger that raises is swallowed: this
    object runs on the daemon's voice and chat threads, and no diagnostic is
    worth a broken turn.
    """

    def __init__(
        self,
        path: Optional[Any] = None,
        *,
        handle_line: Optional[Callable[[str], bool]] = None,
        logger: Optional[Callable[[str], None]] = None,
        backlog: int = 8,
        candidates: Optional[Any] = None,
    ) -> None:
        if candidates is not None:
            self._candidates = [Path(candidate).expanduser() for candidate in candidates]
        elif path is not None:
            self._candidates = [Path(path).expanduser()]
        else:
            self._candidates = control_socket_candidates()
        if not self._candidates:
            raise ControlSocketError("no control socket path to try")
        # The preferred path until start() finds which one it can actually use.
        self._path = self._candidates[0]
        self._handle_line = handle_line
        self._logger = logger
        self._backlog = backlog

        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._clients: Set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._client_threads: List[threading.Thread] = []
        self._owns_file = False

    # -- introspection -------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_running(self) -> bool:
        return self._listen_socket() is not None

    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def _listen_socket(self) -> Optional[socket.socket]:
        return self._listener

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        """Bind, listen and begin accepting.  Raises if the socket cannot be ours.

        Walks :func:`control_socket_candidates` in order and keeps the first path
        it can bind, so a deployment whose sandbox forbids the runtime directory
        still gets a channel. A path that another Claire is serving is *not*
        skipped: that is a configuration to fix, not a path to route around.
        """
        if self.is_running:
            return

        failures: List[str] = []
        for candidate in self._candidates:
            try:
                listener = self._bind(candidate)
            except ControlSocketInUseError:
                raise
            except (ControlSocketError, OSError) as exc:
                failures.append(f"{candidate}: {exc}")
                self._log(f"control socket path unusable, trying the next ({candidate}: {exc})")
                continue

            self._path = candidate
            self._owns_file = True
            self._stop.clear()
            self._listener = listener
            thread = threading.Thread(
                target=self._accept_loop, name="claire-control-socket", daemon=True
            )
            self._accept_thread = thread
            thread.start()
            return

        raise ControlSocketError(
            "could not bind the control socket anywhere: " + "; ".join(failures)
        )

    def _bind(self, path: Path) -> socket.socket:
        """Bind and listen on one path, or explain why it is not possible."""
        self._prepare_parent(path)
        self._clear_stale_socket(path)

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            # Before listen(): between bind and chmod the file exists with the
            # process umask, and another local account could connect in that
            # window.  Tightening first closes it.
            os.chmod(path, 0o600)
            listener.listen(self._backlog)
        except OSError as exc:
            listener.close()
            raise ControlSocketError(
                f"could not bind the control socket at {path}: {exc}"
            ) from exc
        return listener

    def stop(self, timeout: float = STOP_TIMEOUT_SEC) -> None:
        """Stop accepting, drop every client and remove our socket file."""
        self._stop.set()

        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            self._close_client(client)

        for worker in self._client_threads:
            if worker.is_alive():
                worker.join(0.2)
        self._client_threads = []

        if self._owns_file:
            self._owns_file = False
            self._unlink_if_socket(self._path)

    # -- accepting and reading ------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                listener.settimeout(ACCEPT_TIMEOUT_SEC)
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                # The listener was closed by stop(); that is the way out.
                return
            self._adopt(connection)

    def _adopt(self, connection: socket.socket) -> None:
        with self._clients_lock:
            self._clients.add(connection)
        worker = threading.Thread(
            target=self._read_client, args=(connection,), name="claire-control-client", daemon=True
        )
        self._client_threads.append(worker)
        worker.start()

    def _read_client(self, connection: socket.socket) -> None:
        buffer = b""
        try:
            connection.settimeout(CLIENT_READ_TIMEOUT_SEC)
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        self._dispatch(line)
        finally:
            with self._clients_lock:
                self._clients.discard(connection)
            self._close_client(connection)

    def _dispatch(self, line: str) -> None:
        if self._handle_line is None:
            self._log(f"control socket line ignored (no handler): {line[:80]!r}")
            return
        try:
            recognised = bool(self._handle_line(line))
        except Exception as exc:  # noqa: BLE001 - one bad line must not drop a client
            self._log(f"control socket handler raised ({type(exc).__name__}: {exc})")
            return
        if not recognised:
            self._log(f"control socket line not recognised: {line[:80]!r}")

    # -- the daemon -> client direction --------------------------------------
    def broadcast(self, line: str) -> None:
        """Send one line to every connected client.  Never raises.

        Called from the daemon's IPC emitter, which runs on the voice thread and
        on chat worker threads: a client that has gone away, or a socket that
        fails mid-write, must cost that client and nothing else.
        """
        if not line:
            return
        payload = (line.rstrip("\n") + "\n").encode("utf-8", "replace")
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.sendall(payload)
            except OSError as exc:
                self._log(f"dropping a control socket client ({type(exc).__name__}: {exc})")
                with self._clients_lock:
                    self._clients.discard(client)
                self._close_client(client)

    # -- filesystem ----------------------------------------------------------
    @staticmethod
    def _prepare_parent(path: Path) -> None:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            # A directory we do not own (someone else's /run/user entry) is not
            # worth failing the channel over; the socket's own mode still holds.
            pass

    def _clear_stale_socket(self, path: Path) -> None:
        """Remove a leftover socket file, but never a live server's or a real file."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ControlSocketError(f"could not inspect {path}: {exc}") from exc

        if not stat.S_ISSOCK(info.st_mode):
            raise ControlSocketError(
                f"{path} exists and is not a socket; refusing to replace it"
            )

        if self._answers(path):
            raise ControlSocketInUseError(
                f"another Claire is already serving {path}; leaving it alone"
            )

        try:
            path.unlink()
            self._log(f"removed a stale control socket at {path}")
        except OSError as exc:
            raise ControlSocketError(f"could not remove the stale socket {path}: {exc}") from exc

    @staticmethod
    def _answers(path: Path) -> bool:
        """True when something is listening: a live server, not a leftover file."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(LIVE_PROBE_TIMEOUT_SEC)
        try:
            probe.connect(str(path))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _unlink_if_socket(self, path: Path) -> None:
        try:
            info = path.lstat()
        except OSError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            return
        try:
            path.unlink()
        except OSError as exc:
            self._log(f"could not remove {path} on shutdown: {exc}")

    # -- small helpers -------------------------------------------------------
    @staticmethod
    def _close_client(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - logging must never break the channel
            pass


def describe_socket_line(prefix: str, payload: Any) -> str:
    """Build one protocol line: prefix, JSON payload, no trailing newline."""
    return f"{prefix}{json.dumps(payload, ensure_ascii=False)}"
