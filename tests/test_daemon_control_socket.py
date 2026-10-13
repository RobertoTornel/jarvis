"""Tests that the daemon actually serves the control socket and feeds it.

The socket itself is tested in ``test_control_socket.py``.  This file pins the
*daemon's* half of the contract: that the lines it accepts are the lines stdin
already accepts, that its IPC events are copied onto the socket as well as to
stdout (an attached UI cannot read a stdout that belongs to the journal), that
the channel announces itself at startup and can be switched off, and that none of
this changes the existing stdin/stdout behaviour.
"""

import json
import threading
import time

import pytest

from claire import daemon

pytestmark = pytest.mark.unit


@pytest.fixture()
def clean_control_globals(monkeypatch):
    """Leave the daemon's control-socket global as we found it."""
    monkeypatch.setattr(daemon, "_control_socket_server", None, raising=False)
    yield
    server = getattr(daemon, "_control_socket_server", None)
    if server is not None:
        server.stop()
        monkeypatch.setattr(daemon, "_control_socket_server", None, raising=False)


# --- the lines the socket accepts ------------------------------------------

def test_a_query_line_reaches_the_chat_submission(monkeypatch):
    calls = []
    monkeypatch.setattr(
        daemon,
        "submit_text_query",
        lambda text, **kwargs: calls.append((text, kwargs)),
        raising=False,
    )

    handled = daemon._handle_control_line('__CHAT_QUERY__:{"text": "what is the weather"}')

    assert handled is True
    assert calls == [("what is the weather", {"use_ipc": True})]


def test_cancellation_and_session_lines_route_to_their_own_handlers(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "cancel_active_chat_query", lambda *a, **k: calls.append("cancel"), raising=False)
    monkeypatch.setattr(daemon, "new_chat_session", lambda *a, **k: calls.append("new_session"), raising=False)

    assert daemon._handle_control_line(daemon.CHAT_CANCEL_IPC_PREFIX) is True
    assert daemon._handle_control_line(daemon.CHAT_NEW_SESSION_IPC_PREFIX) is True

    assert calls == ["cancel", "new_session"]


def test_a_line_that_is_not_part_of_the_protocol_is_not_claimed():
    assert daemon._handle_control_line("just some text") is False
    assert daemon._handle_control_line("") is False


def test_a_broken_line_does_not_raise_and_does_not_fall_through(monkeypatch):
    """A malformed query is still *ours*; it is handled, not passed on."""
    calls = []
    monkeypatch.setattr(daemon, "submit_text_query", lambda *a, **k: calls.append(a), raising=False)

    assert daemon._handle_control_line('__CHAT_QUERY__:{"text": 12}') is True
    assert calls == [], "a non-string text must never reach the submission"


def test_a_handler_that_raises_is_contained(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("submission exploded")

    monkeypatch.setattr(daemon, "submit_text_query", explode, raising=False)

    assert daemon._handle_control_line('__CHAT_QUERY__:{"text": "boom"}') is True


# --- starting and stopping -------------------------------------------------

def test_the_channel_starts_serves_and_announces_itself(monkeypatch, tmp_path, capsys):
    path = tmp_path / "runtime" / "control.sock"
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(path))

    daemon._start_control_socket_server()

    printed = capsys.readouterr().out
    assert daemon._control_socket_server is not None
    assert path.exists()
    assert str(path) in printed and "Control socket ready" in printed

    daemon._stop_control_socket_server()
    assert daemon._control_socket_server is None
    assert not path.exists()


def test_the_channel_can_be_switched_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setenv("CLAIRE_CONTROL_CHANNEL", "0")

    daemon._start_control_socket_server()

    assert daemon._control_socket_server is None
    assert "disabled" in capsys.readouterr().out.lower()


def test_a_channel_that_cannot_start_says_so_instead_of_failing_silently(monkeypatch, tmp_path, capsys):
    """The whole point is that an absent channel must not look like a working one."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a regular file where the socket's directory should be")
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(blocker / "control.sock"))

    daemon._start_control_socket_server()   # must not raise

    assert daemon._control_socket_server is None
    out = capsys.readouterr().out
    assert "Control socket unavailable" in out


def test_stopping_a_channel_that_never_started_is_harmless(clean_control_globals):
    daemon._stop_control_socket_server()
    daemon._stop_control_socket_server()
    assert daemon._control_socket_server is None


def test_starting_twice_keeps_one_server(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    daemon._start_control_socket_server()
    try:
        first = daemon._control_socket_server
        daemon._start_control_socket_server()
        assert daemon._control_socket_server is first
    finally:
        daemon._stop_control_socket_server()


# --- the event stream ------------------------------------------------------

class FakeServer:
    """Stands in for a running control socket server."""

    def __init__(self):
        self.lines = []

    def broadcast(self, line):
        self.lines.append(line)


def test_ipc_events_are_copied_onto_the_socket_as_well_as_stdout(monkeypatch, capsys):
    fake = FakeServer()
    monkeypatch.setattr(daemon, "_control_socket_server", fake, raising=False)

    daemon._emit_ipc_event(daemon.CHAT_IPC_PREFIX, "started", None, "chat_ipc")

    printed = capsys.readouterr().out.strip()
    assert fake.lines == [f'{daemon.CHAT_IPC_PREFIX}{json.dumps({"type": "started", "data": None})}']
    assert printed == fake.lines[0], "stdout must stay byte-identical for subprocess mode"


def test_a_broadcast_that_explodes_does_not_break_the_emitter(monkeypatch, capsys):
    class Hostile(FakeServer):
        def broadcast(self, line):
            raise RuntimeError("socket is on fire")

    monkeypatch.setattr(daemon, "_control_socket_server", Hostile(), raising=False)

    daemon._emit_ipc_event(daemon.CHAT_IPC_PREFIX, "complete", "an answer", "chat_ipc")

    assert "an answer" in capsys.readouterr().out, "stdout still got the event"


def test_a_real_socket_carries_an_event_from_the_daemon(monkeypatch, tmp_path):
    """End to end through the daemon's own wiring, not a fake server."""
    from claire.control import ControlClient

    path = tmp_path / "control.sock"
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(path))
    daemon._start_control_socket_server()
    try:
        client = ControlClient(path)
        assert client.connect()

        # connect() returns before the accept loop adopts the connection; a
        # broadcast in that window has nowhere to go.
        deadline = time.monotonic() + 5.0
        while daemon._control_socket_server.client_count() < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert daemon._control_socket_server.client_count() == 1

        thread = threading.Thread(
            target=daemon._emit_ipc_event,
            args=(daemon.CHAT_IPC_PREFIX, "token", "Hel", "chat_ipc"),
            daemon=True,
        )
        thread.start()
        thread.join(5.0)

        assert client.read_line(timeout=5.0) == f'{daemon.CHAT_IPC_PREFIX}{json.dumps({"type": "token", "data": "Hel"})}'
    finally:
        daemon._stop_control_socket_server()


# --- nothing existing changed ----------------------------------------------

def test_the_stdin_path_still_behaves_exactly_as_before(monkeypatch):
    """The socket must be an addition, never a replacement."""
    calls = []
    monkeypatch.setattr(
        daemon,
        "submit_text_query",
        lambda text, **kwargs: calls.append((text, kwargs)),
        raising=False,
    )

    assert daemon.handle_chat_query_stdin_line('__CHAT_QUERY__:{"text": "over stdin"}') is True
    assert calls == [("over stdin", {"use_ipc": True})]
    assert daemon.handle_chat_query_stdin_line("__VOICE__:{}") is False
