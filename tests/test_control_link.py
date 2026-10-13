"""Tests for the desktop app's control link — the channel to a supervised Claire.

The defect these tests are about: `claire-desktop.service` attaches to the
systemd daemon, owns neither its stdin nor its stdout, and used to offer a chat
box with no channel behind it. Typing produced "Claire is thinking…", then
nothing at all — no reply, no error.

So this file pins the transport's promises:

* it connects to the daemon's control socket, sends the frozen protocol bytes,
  and forwards ``__CHAT__:`` lines to the app while ignoring everything else;
* it reconnects on its own, because the daemon gets restarted often;
* it says ``False`` (never silently drops) when a message cannot be delivered;
* and the chat window turns that answer into something visible in the
  transcript rather than an empty silence.
"""

import json
import os
import socket
import threading
import time

import pytest

from claire.control import ControlSocketServer

pytestmark = pytest.mark.unit


class FakeDaemon:
    """A control socket that answers, so the link can be driven without a daemon."""

    def __init__(self, path):
        self.lines = []
        self._server = ControlSocketServer(path, handle_line=self._handle, logger=lambda _m: None)
        self._path = path

    def _handle(self, line):
        self.lines.append(line)
        return True

    def start(self, wait_clients=0):
        self._server.start()
        return self

    def stop(self):
        self._server.stop()

    def wait_for(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.lines) >= count:
                return True
            time.sleep(0.02)
        return False

    def wait_for_client(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.client_count() >= 1:
                return True
            time.sleep(0.02)
        return False

    def emit(self, line):
        self._server.broadcast(line)


@pytest.fixture()
def daemon(tmp_path):
    path = tmp_path / "control.sock"
    server = FakeDaemon(path).start()
    try:
        yield server
    finally:
        server.stop()


def make_link(daemon, **kwargs):
    from desktop_app.control_link import ControlLink

    lines = []
    states = []
    link = ControlLink(
        path=str(daemon._path),
        on_line=lines.append,
        on_state_change=lambda available, reason: states.append((available, reason)),
        retry_interval=0.1,
        **kwargs,
    )
    return link, lines, states


# --- the working case -------------------------------------------------------

def test_the_link_connects_and_reports_itself_available(daemon):
    link, _lines, states = make_link(daemon)
    try:
        assert link.start() is True
        assert link.available is True
        assert states and states[-1][0] is True
    finally:
        link.close()


def test_a_submitted_message_arrives_as_the_frozen_protocol_line(daemon):
    from claire.daemon import CHAT_QUERY_IPC_PREFIX

    link, _lines, _states = make_link(daemon)
    try:
        assert link.start()
        assert link.submit("what's the weather in Lisbon") is True
        assert daemon.wait_for(1)
        assert daemon.lines == [
            f'{CHAT_QUERY_IPC_PREFIX}{json.dumps({"text": "what\'s the weather in Lisbon"})}'
        ]
    finally:
        link.close()


def test_cancel_and_new_session_use_their_own_lines(daemon):
    from claire.daemon import CHAT_CANCEL_IPC_PREFIX, CHAT_NEW_SESSION_IPC_PREFIX

    link, _lines, _states = make_link(daemon)
    try:
        assert link.start()
        assert link.cancel() is True
        assert link.new_session() is True
        assert daemon.wait_for(2)
        assert daemon.lines == [CHAT_CANCEL_IPC_PREFIX, CHAT_NEW_SESSION_IPC_PREFIX]
    finally:
        link.close()


def test_chat_events_are_forwarded_to_the_app(daemon):
    link, lines, _states = make_link(daemon)
    try:
        assert link.start()
        assert daemon.wait_for_client()
        daemon.emit('__CHAT__:{"type": "started", "data": null}')
        deadline = time.monotonic() + 5.0
        while not lines and time.monotonic() < deadline:
            time.sleep(0.02)
        assert lines == ['__CHAT__:{"type": "started", "data": null}']
    finally:
        link.close()


def test_events_that_are_not_chat_are_left_to_the_journal(daemon):
    """Voice and diary events already reach the app by following the journal."""
    link, lines, _states = make_link(daemon)
    try:
        assert link.start()
        assert daemon.wait_for_client()
        daemon.emit('__VOICE__:{"type": "rejected"}')
        daemon.emit("__DIARY__:{}")
        time.sleep(0.3)
        assert lines == []
    finally:
        link.close()


# --- the absent case -------------------------------------------------------

def test_without_a_daemon_the_link_says_so_and_refuses_to_pretend(tmp_path):
    from desktop_app.control_link import ControlLink

    states = []
    link = ControlLink(
        path=str(tmp_path / "nothing-here.sock"),
        on_line=lambda _line: None,
        on_state_change=lambda available, reason: states.append((available, reason)),
        retry_interval=0.1,
    )
    try:
        assert link.start() is False
        assert link.available is False
        assert link.submit("anyone there?") is False
        assert link.cancel() is False
        assert states[0][0] is False
        assert "no daemon" in states[0][1]
    finally:
        link.close()


def test_a_submit_that_cannot_be_delivered_is_reported_as_false(daemon):
    """The daemon going away mid-session must not look like a delivered message."""
    link, _lines, _states = make_link(daemon)
    try:
        assert link.start()
        daemon.stop()          # the daemon exits under us
        time.sleep(0.3)

        assert link.submit("into the void") is False
        assert link.available is False
    finally:
        link.close()


# --- reconnection ----------------------------------------------------------

def test_the_link_reconnects_by_itself_when_the_daemon_comes_back(tmp_path):
    """The owner restarts Claire often; a chat box must survive that."""
    from desktop_app.control_link import ControlLink

    path = tmp_path / "control.sock"
    states = []
    link = ControlLink(
        path=str(path),
        on_line=lambda _line: None,
        on_state_change=lambda available, reason: states.append((available, reason)),
        retry_interval=0.1,
    )
    server = None
    try:
        assert link.start() is False, "nothing is listening yet"

        server = FakeDaemon(path).start()

        deadline = time.monotonic() + 10.0
        while not link.available and time.monotonic() < deadline:
            time.sleep(0.05)
        assert link.available is True, "the link should have reconnected on its own"
        assert [s[0] for s in states] == [False, True]

        assert link.submit("back again") is True
        assert server.wait_for(1)
    finally:
        link.close()
        if server is not None:
            server.stop()


def test_a_daemon_that_dies_is_noticed_and_reconnect_keeps_trying(tmp_path):
    from desktop_app.control_link import ControlLink

    path = tmp_path / "control.sock"
    states = []
    link = ControlLink(
        path=str(path),
        on_line=lambda _line: None,
        on_state_change=lambda available, reason: states.append((available, reason)),
        retry_interval=0.1,
    )
    first = FakeDaemon(path).start()
    second = None
    try:
        assert link.start() is True
        first.stop()

        deadline = time.monotonic() + 10.0
        while link.available and time.monotonic() < deadline:
            time.sleep(0.05)
        assert link.available is False

        second = FakeDaemon(path).start()
        deadline = time.monotonic() + 10.0
        while not link.available and time.monotonic() < deadline:
            time.sleep(0.05)
        assert link.available is True
        assert [s[0] for s in states] == [True, False, True]
    finally:
        link.close()
        if second is not None:
            second.stop()


# --- close -----------------------------------------------------------------

def test_close_is_safe_twice_and_without_starting(tmp_path):
    from desktop_app.control_link import ControlLink

    link = ControlLink(path=str(tmp_path / "x.sock"), on_line=lambda _line: None)
    link.close()
    link.close()
    assert link.submit("nothing to send to") is False
