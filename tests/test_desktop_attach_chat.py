"""The chat box must never be enabled without a channel behind it.

This is the user-visible half of the defect.  ``claire-desktop.service`` attaches
to the systemd daemon, owns neither its stdin (``/dev/null``) nor its stdout (the
journal's socket), and still offered a working-looking chat box.  A typed message
took the "bundled" branch of ``ChatWindow._send``, called
``daemon.submit_text_query`` in a process that has no daemon, and emitted
``complete(None)`` — whose handler appends nothing.  The owner watched "Claire is
thinking…" appear and vanish, with no reply and no error.

So these tests pin two things:

* a message that cannot be delivered says so, in the transcript, and never
  silently evaporates;
* chat is only announced as available when something can actually carry it —
  the control socket, a stdin we own, or a daemon in this very process.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def window(qapp, monkeypatch):
    """A chat window wired the way the app wires one in attach mode."""
    from desktop_app.chat_window import ChatWindow

    monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
    created = []

    def make(**kwargs):
        kwargs.setdefault("in_process_daemon", False)
        win = ChatWindow(**kwargs)
        created.append(win)
        win.show()
        return win

    yield make
    for win in created:
        win.close()


def type_and_send(win, text):
    win.input_widget.setPlainText(text)
    win._send()


def wait_until(predicate, qapp, timeout=5.0):
    """Wait for *predicate*, pumping the Qt event loop so queued signals land.

    The link reads its socket on its own thread and emits a Qt signal; the
    receiving slot only runs once the event loop processes the queued call, which
    is what the app's main loop does continuously.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        qapp.processEvents()
        time.sleep(0.02)
    return bool(predicate())


# --- the silent no-op is gone ----------------------------------------------

def test_an_attached_window_with_no_channel_says_so_instead_of_going_quiet(window, monkeypatch):
    """The exact bug: this used to submit into a process with no daemon."""
    called = []
    monkeypatch.setattr(
        "claire.daemon.submit_text_query",
        lambda *a, **k: called.append(a),
        raising=False,
    )
    win = window(daemon_available=True)      # the daemon is up ...

    type_and_send(win, "are you there?")

    assert called == [], "an attached window must not call the daemon in this process"
    transcript = win.transcript_text()
    assert "are you there?" in transcript
    assert "cannot reach Claire" in transcript, "the failure has to be visible"
    assert win._query_in_flight is False, "not left thinking forever"


def test_a_transport_that_reports_failure_is_believed(window):
    def refuses(_text):
        return False

    win = window(submit_fn=refuses, daemon_available=True)

    type_and_send(win, "into the void")

    assert "cannot reach Claire" in win.transcript_text()
    assert win._query_in_flight is False


def test_a_transport_that_reports_success_stays_quiet(window):
    sent = []

    def delivers(text):
        sent.append(text)
        return True

    win = window(submit_fn=delivers, daemon_available=True)

    type_and_send(win, "a normal message")

    assert sent == ["a normal message"]
    assert "cannot reach Claire" not in win.transcript_text()


def test_a_transport_that_returns_nothing_is_treated_as_delivered(window):
    """Existing transports return None; only an explicit False is a failure."""
    sent = []
    win = window(submit_fn=lambda text: sent.append(text), daemon_available=True)

    type_and_send(win, "older transport")

    assert sent == ["older transport"]
    assert "cannot reach Claire" not in win.transcript_text()


# --- the status vocabulary --------------------------------------------------

def test_an_unreachable_channel_is_its_own_visible_status(window):
    from desktop_app.chat_window import _DAEMON_STATUS_MESSAGES, _DAEMON_STATUS_PLACEHOLDERS

    assert "unreachable" in _DAEMON_STATUS_MESSAGES
    assert "unreachable" in _DAEMON_STATUS_PLACEHOLDERS

    win = window(daemon_available=True)
    win.set_daemon_status("unreachable")

    assert win._daemon_available is False, "an unreachable daemon must not enable chat"
    assert win.input_widget.isEnabled() is False
    assert "control socket" in win._status_label.text()


def test_a_reachable_channel_enables_the_input(window):
    win = window(daemon_available=True)
    win.set_daemon_status("running")
    assert win._daemon_available is True
    assert win.input_widget.isEnabled() is True


def test_the_bundled_default_is_unchanged(window, monkeypatch):
    """Bundled mode really does have a daemon in this process."""
    called = []
    monkeypatch.setattr(
        "claire.daemon.submit_text_query",
        lambda text, **kw: called.append(text),
        raising=False,
    )
    from desktop_app.chat_window import ChatWindow

    monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
    win = ChatWindow()          # in_process_daemon defaults to True
    win.show()
    try:
        type_and_send(win, "bundled hello")
        assert called == ["bundled hello"]
    finally:
        win.close()


# --- the app decides, and the decision is honest ----------------------------

class AppStub:
    """The slices of the tray these methods touch, without booting the GUI app."""

    def __init__(self, *, is_listening=True, is_bundled=False, control_link=None):
        from desktop_app.app import ControlLinkSignals, LogSignals
        from desktop_app.chat_window import ChatIpcSignals

        self.is_listening = is_listening
        self.is_bundled = is_bundled
        self._control_link = control_link
        self._chat_submit_fn = None
        self._chat_cancel_fn = None
        self._chat_control_fn = None
        self.chat_window = None
        self.log_signals = LogSignals()
        self._chat_ipc_signals = ChatIpcSignals()
        self._control_link_signals = ControlLinkSignals()
        self.logged = []
        self.log_signals.new_log.connect(self.logged.append)


def test_starting_the_link_with_no_daemon_keeps_chat_off_and_says_why(monkeypatch, tmp_path):
    from desktop_app.app import ClaireSystemTray

    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(tmp_path / "absent.sock"))
    stub = AppStub()
    try:
        ClaireSystemTray._start_control_link(stub)

        assert stub._control_link is not None
        assert stub._chat_submit_fn is None, "no channel, so no transport to offer"
        assert stub._chat_cancel_fn is None
        assert ClaireSystemTray._chat_transport_status(stub) == "unreachable"
        assert any("control socket is not answering" in line for line in stub.logged)
    finally:
        ClaireSystemTray._stop_control_link(stub)


def test_starting_the_link_with_a_daemon_wires_chat_through_the_socket(monkeypatch, tmp_path):
    from claire.control import ControlSocketServer
    from desktop_app.app import ClaireSystemTray

    path = tmp_path / "control.sock"
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(path))
    received = []
    server = ControlSocketServer(path, handle_line=lambda line: received.append(line) or True,
                                 logger=lambda _m: None)
    server.start()
    stub = AppStub()
    try:
        ClaireSystemTray._start_control_link(stub)

        assert stub._chat_submit_fn is not None, "chat must be usable when the socket answers"
        assert ClaireSystemTray._chat_transport_status(stub) == "running"
        assert stub._chat_submit_fn("hello over the socket") is True

        deadline = time.monotonic() + 5.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        assert received == ['__CHAT_QUERY__:{"text": "hello over the socket"}']
    finally:
        ClaireSystemTray._stop_control_link(stub)
        server.stop()


def test_the_socket_carries_chat_events_into_the_app_signals(monkeypatch, tmp_path, qapp):
    """The app's own ChatIpcSignals bridge gets the daemon's reply events."""
    from claire.control import ControlSocketServer
    from desktop_app.app import ClaireSystemTray

    path = tmp_path / "control.sock"
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(path))
    server = ControlSocketServer(path, handle_line=lambda _line: True, logger=lambda _m: None)
    server.start()
    stub = AppStub()
    seen = []
    stub._chat_ipc_signals.line_received.connect(seen.append)
    try:
        ClaireSystemTray._start_control_link(stub)
        assert wait_until(lambda: server.client_count() >= 1, qapp), "link never connected"

        server.broadcast('__CHAT__:{"type": "complete", "data": "an answer"}')

        assert wait_until(lambda: seen, qapp), "the chat event never reached the app"
        assert seen == ['__CHAT__:{"type": "complete", "data": "an answer"}']
    finally:
        ClaireSystemTray._stop_control_link(stub)
        server.stop()


def test_a_real_daemon_restart_turns_chat_off_and_back_on(monkeypatch, tmp_path, qapp):
    """End to end: the link notices, the app's slot reacts, chat follows.

    The state signal is delivered to a QObject receiver, exactly as the tray's own
    slot is, so the slot runs on the GUI thread once the event loop spins.
    """
    from PyQt6.QtCore import QObject, pyqtSlot

    from claire.control import ControlSocketServer
    from desktop_app.app import ClaireSystemTray

    class Receiver(QObject):
        def __init__(self, target):
            super().__init__()
            self.target = target

        @pyqtSlot(bool, str)
        def on_state(self, available, reason):
            ClaireSystemTray._on_control_link_state(self.target, available, reason)

    path = tmp_path / "control.sock"
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(path))
    first = ControlSocketServer(path, handle_line=lambda _line: True, logger=lambda _m: None)
    first.start()
    stub = AppStub()
    receiver = Receiver(stub)
    stub._control_link_signals.state_changed.connect(receiver.on_state)
    second = None
    try:
        ClaireSystemTray._start_control_link(stub)
        assert stub._chat_submit_fn is not None
        assert ClaireSystemTray._chat_transport_status(stub) == "running"

        first.stop()

        assert wait_until(
            lambda: stub._chat_submit_fn is None
            and ClaireSystemTray._chat_transport_status(stub) == "unreachable",
            qapp,
            timeout=10.0,
        ), "chat stayed enabled after the daemon went away"

        second = ControlSocketServer(path, handle_line=lambda _line: True, logger=lambda _m: None)
        second.start()

        assert wait_until(
            lambda: stub._chat_submit_fn is not None, qapp, timeout=15.0
        ), "chat never came back after the daemon returned"
        assert ClaireSystemTray._chat_transport_status(stub) == "running"
    finally:
        ClaireSystemTray._stop_control_link(stub)
        if second is not None:
            second.stop()


def test_the_transport_decision_with_the_links_availability_under_control():
    """The decision itself, without a live socket in the way."""
    from desktop_app.app import ClaireSystemTray

    class FakeLink:
        def __init__(self, available):
            self.available = available
            self.reason = "no daemon is answering on her control socket"

    stopped = AppStub(is_listening=False)
    assert ClaireSystemTray._chat_transport_status(stopped) == "stopped"

    attached = AppStub(is_listening=True, control_link=FakeLink(False))
    assert ClaireSystemTray._chat_transport_status(attached) == "unreachable"

    attached_up = AppStub(is_listening=True, control_link=FakeLink(True))
    assert ClaireSystemTray._chat_transport_status(attached_up) == "running"


def test_attaching_to_a_running_daemon_marks_her_as_listening(monkeypatch, tmp_path, qapp):
    """The tray used to say "Stopped" while Claire was running under systemd.

    The attach branch returned before ``is_listening`` was set, so the tray
    offered "Start Listening" (which re-attached and changed nothing) and the
    chat window disabled itself with "Start Listening from the tray to use chat".
    """
    from unittest.mock import MagicMock

    import desktop_app.app as app_mod
    from desktop_app.chat_window import ChatIpcSignals

    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(tmp_path / "absent.sock"))
    tray = app_mod.ClaireSystemTray.__new__(app_mod.ClaireSystemTray)
    tray.log_signals = app_mod.LogSignals()
    tray._chat_ipc_signals = ChatIpcSignals()
    tray._control_link_signals = app_mod.ControlLinkSignals()
    tray._control_link = None
    tray._chat_submit_fn = None
    tray._chat_cancel_fn = None
    tray.chat_window = None
    tray.toggle_action = MagicMock()
    tray.status_action = MagicMock()
    statuses = []
    tray._set_chat_daemon_status = statuses.append
    monkeypatch.setattr(app_mod, "_running_claire_daemon_pid", lambda: 4242)
    monkeypatch.setattr(app_mod.ClaireSystemTray, "_start_daemon_log_tail", lambda self: None)
    monkeypatch.setattr(
        app_mod.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn a second daemon")
    )

    try:
        tray.start_daemon()

        assert tray.is_listening is True, "attaching to a running daemon is listening"
        assert "Stop Listening" in tray.toggle_action.setText.call_args.args[0]
        assert tray._chat_submit_fn is None, "no socket here, so no transport to offer"
        assert statuses[-1] == "unreachable", "chat must not claim to be available"
    finally:
        app_mod.ClaireSystemTray._stop_control_link(tray)


def test_an_open_chat_window_follows_the_link(monkeypatch, tmp_path, qapp):
    from desktop_app.app import ClaireSystemTray
    from desktop_app.chat_window import ChatWindow

    monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(tmp_path / "absent.sock"))
    stub = AppStub()
    stub.chat_window = ChatWindow(in_process_daemon=False, daemon_available=True)
    try:
        ClaireSystemTray._start_control_link(stub)
        ClaireSystemTray._set_chat_daemon_status(stub, ClaireSystemTray._chat_transport_status(stub))

        assert stub.chat_window._daemon_available is False
        assert stub.chat_window.input_widget.isEnabled() is False
        assert "control socket" in stub.chat_window._status_label.text()
    finally:
        ClaireSystemTray._stop_control_link(stub)
        stub.chat_window.close()
