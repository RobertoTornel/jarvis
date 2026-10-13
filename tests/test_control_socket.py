"""Tests for the daemon's control socket and its client.

The socket exists because of a silent failure.  When systemd runs the daemon, the
desktop app owns neither its stdin (``/dev/null``) nor its stdout (the journal's
socket), so the chat box had no channel at all: typing produced "Claire is
thinking…", then nothing.  These tests pin the channel that closes that gap, and
in particular the three things that make it trustworthy:

* the bytes on the wire are the protocol the daemon already speaks, so every
  existing parser keeps working;
* a misbehaving, disconnecting or absent client cannot disturb the daemon or the
  other clients — the event stream runs on the voice thread;
* the socket is the owner's alone (mode 0600 under a 0700 directory) and a stale
  file from a crash is replaced while a *live* server's file is not stolen.
"""

import json
import os
import socket
import stat
import time
from pathlib import Path

import pytest

from claire.control import (
    CONTROL_SOCKET_ENV_VAR,
    ControlClient,
    ControlSocketError,
    ControlSocketInUseError,
    ControlSocketServer,
    control_socket_candidates,
    resolve_control_socket_path,
)

pytestmark = pytest.mark.unit


class Recorder:
    """A stand-in for the daemon's line handler."""

    def __init__(self, answers=True):
        self.lines = []
        self.answers = answers
        self.raised = []

    def __call__(self, line):
        self.lines.append(line)
        if self.answers == "raise":
            self.raised.append(line)
            raise RuntimeError("handler exploded")
        return self.answers

    def wait_for(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.lines) >= count:
                return True
            time.sleep(0.02)
        return False


@pytest.fixture()
def socket_path(tmp_path):
    return tmp_path / "claire" / "control.sock"


@pytest.fixture()
def server(socket_path):
    handler = Recorder()
    srv = ControlSocketServer(socket_path, handle_line=handler, logger=lambda _m: None)
    srv.start()
    try:
        yield srv, handler
    finally:
        srv.stop()


def connect_client(socket_path, timeout=2.0):
    client = ControlClient(socket_path, timeout=timeout)
    assert client.connect(), "the test server should be reachable"
    return client


def wait_for_clients(srv, count, timeout=5.0):
    """Wait until the server has *accepted* the connections.

    ``connect()`` returns as soon as the kernel queues the connection, which is
    before the server's accept loop picks it up; a broadcast in that window has
    nobody to go to.  That window cannot open in the real flow — a client's own
    query is what makes the daemon answer it — but a test that broadcasts to a
    just-connected client has to wait for the handshake to finish.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if srv.client_count() >= count:
            return True
        time.sleep(0.02)
    return False


# --- the path the socket lives at ------------------------------------------

def test_the_path_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.sock"
    monkeypatch.setenv(CONTROL_SOCKET_ENV_VAR, str(target))
    assert resolve_control_socket_path() == target


def test_the_default_path_is_per_user_and_not_a_port(monkeypatch):
    monkeypatch.delenv(CONTROL_SOCKET_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    assert resolve_control_socket_path() == Path("/run/user/4242/claire/control.sock")


def test_without_a_runtime_dir_it_falls_back_to_the_data_directory(monkeypatch):
    monkeypatch.delenv(CONTROL_SOCKET_ENV_VAR, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    fallback = resolve_control_socket_path()
    assert fallback.name == "control.sock"
    assert "run/user" not in str(fallback)


# --- the transport ---------------------------------------------------------

def test_a_query_line_reaches_the_handler(server, socket_path):
    _srv, handler = server
    client = connect_client(socket_path)

    assert client.send_line('__CHAT_QUERY__:{"text": "hello"}')

    assert handler.wait_for(1)
    assert handler.lines == ['__CHAT_QUERY__:{"text": "hello"}']


def test_the_client_builds_the_exact_protocol_bytes(server, socket_path):
    """Whatever the desktop app already parses must be what arrives here."""
    from claire.daemon import (
        CHAT_CANCEL_IPC_PREFIX,
        CHAT_NEW_SESSION_IPC_PREFIX,
        CHAT_QUERY_IPC_PREFIX,
    )

    _srv, handler = server
    client = connect_client(socket_path)

    assert client.submit_chat("what's the weather")
    assert client.cancel_chat()
    assert client.new_session()
    assert handler.wait_for(3)

    assert handler.lines[0] == f'{CHAT_QUERY_IPC_PREFIX}{json.dumps({"text": "what's the weather"})}'
    assert handler.lines[1] == CHAT_CANCEL_IPC_PREFIX
    assert handler.lines[2] == CHAT_NEW_SESSION_IPC_PREFIX


def test_a_broadcast_reaches_every_connected_client(server, socket_path):
    srv, _handler = server
    first = connect_client(socket_path)
    second = connect_client(socket_path)
    assert wait_for_clients(srv, 2)

    srv.broadcast('__CHAT__:{"type": "started", "data": null}')

    for client in (first, second):
        assert client.read_line(timeout=5.0) == '__CHAT__:{"type": "started", "data": null}'


def test_a_client_that_vanishes_does_not_disturb_the_others(server, socket_path):
    """A UI window closed mid-turn must cost that window and nothing else.

    Closing a Unix socket does not fail the peer's next write on its own: the
    bytes go into the kernel's buffer first.  ``SO_LINGER`` with a zero timeout
    sends a reset instead, which is what a killed process looks like, and the
    broadcast is repeated so at least one write meets the reset.
    """
    import struct

    srv, _handler = server
    doomed = connect_client(socket_path)
    survivor = connect_client(socket_path)
    assert wait_for_clients(srv, 2)

    doomed._socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    doomed.close()

    for _ in range(5):
        srv.broadcast('__CHAT__:{"type": "complete", "data": "still here"}')

    assert survivor.read_line(timeout=5.0) == '__CHAT__:{"type": "complete", "data": "still here"}'
    assert srv.is_running
    deadline = time.monotonic() + 5.0
    while srv.client_count() > 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert srv.client_count() <= 1, "the dead client should have been dropped"


def test_a_socket_that_fails_on_write_is_dropped_without_raising(server, socket_path):
    """The deterministic half of the test above.

    A reset peer only *sometimes* fails the first write, so the end-to-end case
    cannot pin the swallow-and-drop contract on its own.  This injects a client
    whose write always fails — the failure the daemon's voice thread would
    otherwise take on the chin.
    """

    class FailingSocket:
        def __init__(self):
            self.writes = 0

        def sendall(self, _payload):
            self.writes += 1
            raise BrokenPipeError(32, "Broken pipe")

        def shutdown(self, _how):
            raise OSError("already gone")

        def close(self):
            pass

    srv, _handler = server
    survivor = connect_client(socket_path)
    assert wait_for_clients(srv, 1)

    broken = FailingSocket()
    with srv._clients_lock:      # the only seam that makes this deterministic
        srv._clients.add(broken)

    srv.broadcast('__CHAT__:{"type": "started", "data": null}')   # must not raise

    assert broken.writes == 1
    assert survivor.read_line(timeout=5.0) == '__CHAT__:{"type": "started", "data": null}'
    assert srv.client_count() == 1, "the failing client was dropped, the live one kept"


def test_broadcast_never_raises_even_with_no_clients(server):
    srv, _handler = server
    srv.broadcast('__CHAT__:{"type": "started", "data": null}')
    srv.broadcast("")


def test_a_handler_that_raises_does_not_kill_the_connection(socket_path):
    handler = Recorder(answers="raise")
    srv = ControlSocketServer(socket_path, handle_line=handler, logger=lambda _m: None)
    srv.start()
    try:
        client = connect_client(socket_path)
        assert client.send_line("first")
        assert handler.wait_for(1)
        assert client.send_line("second")      # the connection is still usable
        assert handler.wait_for(2)
        assert handler.raised == ["first", "second"]
    finally:
        srv.stop()


def test_an_unknown_line_is_ignored_without_closing_the_connection(socket_path):
    handler = Recorder(answers=False)
    srv = ControlSocketServer(socket_path, handle_line=handler, logger=lambda _m: None)
    srv.start()
    try:
        client = connect_client(socket_path)
        assert client.send_line("something else entirely")
        assert handler.wait_for(1)
        assert client.send_line('__CHAT_QUERY__:{"text": "still fine"}')
        assert handler.wait_for(2)
        assert handler.lines[1] == '__CHAT_QUERY__:{"text": "still fine"}'
    finally:
        srv.stop()


def test_malformed_json_and_binary_junk_do_not_break_the_server(server, socket_path):
    _srv, handler = server
    client = connect_client(socket_path)
    assert client.send_line("__CHAT_QUERY__:{not json")
    assert client.send_line("__CHAT_QUERY__:")
    assert handler.wait_for(2)
    assert client.send_line('__CHAT_QUERY__:{"text": "after the junk"}')
    assert handler.wait_for(3)


def test_a_partial_line_is_buffered_until_the_newline_arrives(server, socket_path):
    _srv, handler = server
    client = connect_client(socket_path)
    payload = ('__CHAT_QUERY__:{"text": "split"}\n').encode()
    half = len(payload) // 2
    client._socket.sendall(payload[:half])
    time.sleep(0.2)
    assert handler.lines == [], "half a line must not be dispatched"
    client._socket.sendall(payload[half:])
    assert handler.wait_for(1)
    assert handler.lines == ['__CHAT_QUERY__:{"text": "split"}']


# --- the filesystem --------------------------------------------------------

def test_the_socket_is_the_owners_alone(server, socket_path):
    assert stat.S_ISSOCK(socket_path.lstat().st_mode)
    assert stat.S_IMODE(socket_path.lstat().st_mode) == 0o600
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700


def test_stop_removes_the_socket_and_its_file(socket_path):
    srv = ControlSocketServer(socket_path, handle_line=Recorder(), logger=lambda _m: None)
    srv.start()
    assert socket_path.exists()

    started = time.monotonic()
    srv.stop()
    elapsed = time.monotonic() - started

    assert not srv.is_running
    assert not socket_path.exists(), "a stopped server must not leave its socket behind"
    assert elapsed < 5.0, f"stop() took {elapsed:.1f}s"


def test_stop_is_safe_twice_and_without_starting(socket_path):
    srv = ControlSocketServer(socket_path, handle_line=Recorder(), logger=lambda _m: None)
    srv.stop()          # never started
    srv.start()
    srv.stop()
    srv.stop()          # twice


def test_a_stale_socket_file_is_replaced(socket_path):
    """A crash leaves the file behind; the next start must not be blocked by it."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()       # the listener is gone; the file is not

    srv = ControlSocketServer(socket_path, handle_line=Recorder(), logger=lambda _m: None)
    srv.start()
    try:
        assert srv.is_running
        client = connect_client(socket_path)
        assert client.connected
    finally:
        srv.stop()


def test_a_live_server_is_not_stolen(server, socket_path):
    """Two Claires on one machine is a mistake; do not silently take the socket."""
    intruder = ControlSocketServer(socket_path, handle_line=Recorder(), logger=lambda _m: None)

    with pytest.raises(ControlSocketError) as caught:
        intruder.start()

    assert "already serving" in str(caught.value)
    assert socket_path.exists(), "the running server keeps its socket"
    assert not intruder.is_running


def test_a_regular_file_is_never_deleted_to_make_room(socket_path):
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.write_text("not a socket, and not ours to delete")

    srv = ControlSocketServer(socket_path, handle_line=Recorder(), logger=lambda _m: None)
    with pytest.raises(ControlSocketError) as caught:
        srv.start()

    assert "not a socket" in str(caught.value)
    assert socket_path.read_text() == "not a socket, and not ours to delete"


# --- choosing a path --------------------------------------------------------

def test_the_candidates_prefer_the_runtime_directory_then_the_data_directory(monkeypatch):
    monkeypatch.delenv(CONTROL_SOCKET_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    candidates = control_socket_candidates()

    assert candidates[0] == Path("/run/user/4242/claire/control.sock")
    assert len(candidates) == 2
    assert candidates[1].name == "control.sock"
    assert "run/user" not in str(candidates[1])


def test_an_explicit_path_is_the_only_path(monkeypatch, tmp_path):
    """An explicit path must not silently fall back to a different daemon."""
    named = tmp_path / "named.sock"
    monkeypatch.setenv(CONTROL_SOCKET_ENV_VAR, str(named))

    assert control_socket_candidates() == [named]


def test_a_server_moves_on_when_a_path_cannot_be_bound(tmp_path):
    """The sandbox case that made this necessary: the runtime path is EROFS."""
    blocked = tmp_path / "blocked.sock"
    blocked.write_text("a regular file, so no socket can be bound here")
    good = tmp_path / "data" / "control.sock"

    handler = Recorder()
    srv = ControlSocketServer(
        handle_line=handler, logger=lambda _m: None, candidates=[blocked, good]
    )
    srv.start()
    try:
        assert srv.path == good
        assert stat.S_ISSOCK(good.lstat().st_mode)

        client = ControlClient(candidates=[blocked, good])
        assert client.connect() is True
        assert client.path == good, "the client must name the path that answered"
    finally:
        srv.stop()


def test_default_candidates_agree_when_the_runtime_directory_is_unusable(monkeypatch, tmp_path):
    """Both ends walk the same list, so neither has to be told where it landed."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.delenv(CONTROL_SOCKET_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(blocker))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    srv = ControlSocketServer(handle_line=Recorder(), logger=lambda _m: None)
    srv.start()
    try:
        assert srv.path == tmp_path / "data" / "claire" / "control.sock"

        client = ControlClient()
        assert client.connect() is True
        assert client.path == srv.path
    finally:
        srv.stop()


def test_a_path_another_claire_serves_is_not_routed_around(tmp_path):
    """Two servers on two paths is the confusion the channel exists to remove."""
    taken = tmp_path / "one" / "control.sock"
    spare = tmp_path / "two" / "control.sock"
    running = ControlSocketServer(taken, handle_line=Recorder(), logger=lambda _m: None)
    running.start()
    try:
        intruder = ControlSocketServer(
            handle_line=Recorder(), logger=lambda _m: None, candidates=[taken, spare]
        )

        with pytest.raises(ControlSocketInUseError) as caught:
            intruder.start()

        assert "already serving" in str(caught.value)
        assert not spare.exists(), "it must not quietly start a second server elsewhere"
        assert not intruder.is_running
    finally:
        running.stop()


# --- the client ------------------------------------------------------------

def test_connect_returns_false_rather_than_raising(socket_path):
    client = ControlClient(socket_path)
    assert client.connect() is False
    assert client.connected is False
    assert client.submit_chat("anyone there?") is False
    assert client.cancel_chat() is False
    assert client.new_session() is False
    assert client.read_line(timeout=0.1) is None


def test_the_client_connects_on_demand_when_a_server_appears(socket_path):
    client = ControlClient(socket_path)
    assert client.connect() is False

    srv = ControlSocketServer(socket_path, handle_line=Recorder(), logger=lambda _m: None)
    srv.start()
    try:
        assert client.submit_chat("now you are there") is True
        assert client.connected is True
    finally:
        srv.stop()


def test_a_closed_channel_is_distinguishable_from_a_quiet_one(server, socket_path):
    srv, _handler = server
    client = connect_client(socket_path)

    assert client.read_line(timeout=0.1) is None
    assert client.connected is True, "a timeout is not a disconnection"

    srv.stop()

    assert client.read_line(timeout=5.0) is None
    assert client.connected is False, "the daemon closed it; say so"


def test_read_line_returns_a_buffered_line_without_reading_again(server, socket_path):
    srv, _handler = server
    client = connect_client(socket_path)
    assert wait_for_clients(srv, 1)
    srv.broadcast("first\nsecond")

    assert client.read_line(timeout=5.0) == "first"
    assert client.read_line(timeout=0.1) == "second"
