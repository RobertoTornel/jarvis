"""Tests for the timer announcer — the loop that fires and speaks timers.

The countdown model in ``claire.schedule.timers`` was complete and tested while
nothing in the running daemon ever imported it, so a timer the owner set would
reach zero in silence.  These tests pin the loop that closes that gap:

* a due timer is spoken ONCE (exactly ``Timer.format_finished()``'s sentence and
  never twice), because ``fire_due`` removes it from the manager;
* a failed or refused speak does NOT lose the sentence: the timer is kept
  pending and retried, since the manager has already consumed it;
* a busy speech check consumes nothing at all, so a due timer survives to be
  announced rather than being deleted unannounced;
* a manager that raises is survived, the loop starts immediately and stops
  promptly, and the daemon's builder wires the *shared* manager with the engine
  adapter.

Real end-to-end speech (sound through the owner's speakers) is out of scope:
``speak`` is injected, so what is verified is the announcer's contract with it.
"""

from __future__ import annotations

import threading
import time

import pytest

from claire import daemon
from claire.schedule import TIMER_POLL_INTERVAL_SEC, TimerAnnouncer
from claire.schedule.timers import TimerManager, default_manager, set_default_manager

pytestmark = pytest.mark.unit


class FakeClock:
    """A monotonic clock under test control."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> float:
        with self._lock:
            self._now += float(seconds)
            return self._now


class FakeSpeech:
    """A speak/busy pair that reports success, failure or a raise on demand."""

    def __init__(self, *, ok=True, busy=False, raise_on_speak=False):
        self.spoken = []
        self.ok = ok
        self.busy = busy
        self.raise_on_speak = raise_on_speak
        self.spoke_event = threading.Event()

    def speak(self, text):
        self.spoken.append(text)
        self.spoke_event.set()
        if self.raise_on_speak:
            raise RuntimeError("engine exploded")
        return self.ok

    def is_speaking(self):
        return self.busy


@pytest.fixture(autouse=True)
def _clean_default_manager():
    """The shared manager must not leak timers between tests."""
    set_default_manager(None)
    yield
    set_default_manager(None)
    daemon._stop_timer_announcer()


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def manager(clock: FakeClock) -> TimerManager:
    return TimerManager(clock=clock)


def make_announcer(manager, speech, **kwargs) -> TimerAnnouncer:
    logs = kwargs.pop("logs", None)
    if logs is None:
        logs = []
    return TimerAnnouncer(
        speech.speak,
        speech.is_speaking,
        manager=manager,
        logger=logs.append,
        **kwargs,
    )


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --- one poll ---------------------------------------------------------------


def test_a_due_timer_is_spoken_exactly_once(manager, clock):
    speech = FakeSpeech()
    timer = manager.start(label="pasta", seconds=60)
    clock.advance(60)
    announcer = make_announcer(manager, speech)

    assert announcer.poll() == [timer.format_finished()]
    assert speech.spoken == [timer.format_finished()]
    # fire_due consumed it, so a second poll has nothing to say.
    assert announcer.poll() == []
    assert speech.spoken == [timer.format_finished()]
    assert manager.active_count() == 0


def test_the_sentence_is_what_format_finished_produces(manager, clock):
    speech = FakeSpeech()
    timer = manager.start(label="pasta", seconds=90)
    clock.advance(90)
    announcer = make_announcer(manager, speech)

    spoken = announcer.poll()

    assert spoken == [timer.format_finished()]
    assert spoken == ["pasta — your 1 minute timer is up"]


def test_an_auto_named_timer_does_not_say_timer_twice(manager, clock):
    speech = FakeSpeech()
    manager.start(seconds=60)
    clock.advance(60)
    announcer = make_announcer(manager, speech)

    assert announcer.poll() == ["Your 1 minute timer is up"]


def test_a_timer_that_is_not_due_is_not_spoken(manager, clock):
    speech = FakeSpeech()
    manager.start(seconds=60)
    clock.advance(30)
    announcer = make_announcer(manager, speech)

    assert announcer.poll() == []
    assert manager.active_count() == 1


def test_two_due_timers_are_both_spoken_soonest_first(manager, clock):
    speech = FakeSpeech()
    late = manager.start(label="late", seconds=600)
    soon = manager.start(label="soon", seconds=60)
    clock.advance(600)
    announcer = make_announcer(manager, speech)

    spoken = announcer.poll()

    assert spoken == [soon.format_finished(), late.format_finished()]
    assert manager.active_count() == 0


# --- speech busy ------------------------------------------------------------


def test_busy_speech_consumes_nothing_and_the_timer_survives(manager, clock):
    """fire_due removes the timer, so a busy check must come first."""
    speech = FakeSpeech(busy=True)
    timer = manager.start(seconds=60)
    clock.advance(60)
    announcer = make_announcer(manager, speech)

    assert announcer.poll() == []
    assert speech.spoken == []
    assert manager.active_count() == 1, "the due timer was consumed unannounced"

    speech.busy = False
    assert announcer.poll() == [timer.format_finished()]


# --- retries ----------------------------------------------------------------


def test_speech_returning_false_leaves_the_timer_pending_and_it_is_spoken_later(
    manager, clock
):
    speech = FakeSpeech(ok=False)
    timer = manager.start(seconds=60)
    clock.advance(60)
    announcer = make_announcer(manager, speech)

    assert announcer.poll() == []
    assert announcer.pending_count == 1
    assert manager.active_count() == 0  # consumed, held only as a pending sentence

    speech.ok = True
    assert announcer.poll() == [timer.format_finished()]
    assert announcer.pending_count == 0
    assert speech.spoken == [timer.format_finished(), timer.format_finished()]


def test_speech_raising_leaves_the_timer_pending_and_it_is_spoken_later(
    manager, clock
):
    speech = FakeSpeech(raise_on_speak=True)
    timer = manager.start(seconds=60)
    clock.advance(60)
    announcer = make_announcer(manager, speech)

    assert announcer.poll() == []
    assert announcer.pending_count == 1

    speech.raise_on_speak = False
    assert announcer.poll() == [timer.format_finished()]
    assert announcer.pending_count == 0


def test_pending_retries_come_before_newly_due_timers(manager, clock):
    speech = FakeSpeech(ok=False)
    first = manager.start(label="first", seconds=60)
    clock.advance(60)
    announcer = make_announcer(manager, speech)
    assert announcer.poll() == []

    speech.ok = True
    second = manager.start(label="second", seconds=0.5)
    clock.advance(1)
    spoken = announcer.poll()

    assert spoken == [first.format_finished(), second.format_finished()]


def test_a_retry_is_logged_visibly(manager, clock):
    speech = FakeSpeech(ok=False)
    manager.start(seconds=60)
    clock.advance(60)
    logs = []
    announcer = make_announcer(manager, speech, logs=logs)

    announcer.poll()
    speech.ok = True
    announcer.poll()

    joined = " | ".join(logs)
    assert "retrying" in joined
    assert "keeping it pending" in joined


# --- a manager that raises --------------------------------------------------


class ExplodingManager:
    """A manager whose fire_due is broken on demand."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.explode = True

    def fire_due(self, now=None):
        if self.explode:
            raise RuntimeError("manager exploded")
        return self.delegate.fire_due(now=now)

    def list_active(self, now=None):
        return self.delegate.list_active(now=now)

    def remaining(self, identifier=None, now=None):
        return self.delegate.remaining(identifier, now=now)

    def format_spoken(self, identifier=None, now=None):
        return self.delegate.format_spoken(identifier, now=now)


def test_a_raising_manager_is_logged_and_survived(manager, clock):
    speech = FakeSpeech()
    real = manager
    manager_on_fake_clock = real.start(seconds=60)
    clock.advance(60)
    logs = []
    announcer = make_announcer(ExplodingManager(real), speech, logs=logs)

    # The poll must not raise, and the failure must be visible in the log.
    assert announcer.poll() == []
    assert any("manager exploded" in line for line in logs)
    assert speech.spoken == []
    assert real.active_count() == 1, "the timer was lost with the failed poll"

    announcer._manager.explode = False
    assert announcer.poll() == [manager_on_fake_clock.format_finished()]


def test_a_broken_busy_check_does_not_stall_timers_forever(manager, clock):
    speech = FakeSpeech()
    timer = manager.start(seconds=60)
    clock.advance(60)

    def broken_busy():
        raise RuntimeError("engine confused")

    announcer = TimerAnnouncer(speech.speak, broken_busy, manager=manager)

    assert announcer.poll() == [timer.format_finished()]


# --- the loop ---------------------------------------------------------------


def test_the_loop_prints_what_she_said_so_the_timer_is_not_invisible(
    manager, clock, capsys
):
    speech = FakeSpeech()
    timer = manager.start(seconds=60)
    clock.advance(60)
    # An hour between polls: the first poll runs immediately, so this test would
    # hang rather than fail if that were not true.
    announcer = make_announcer(manager, speech, poll_interval=3600.0)

    announcer.start()
    printed = ""
    try:
        assert speech.spoke_event.wait(timeout=5.0)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            printed += capsys.readouterr().out
            if timer.format_finished() in printed:
                break
            time.sleep(0.02)
    finally:
        announcer.stop()

    assert timer.format_finished() in printed, (
        f"the sentence she spoke was never printed; got {printed!r}"
    )
    assert "⏳" in printed


def test_poll_alone_does_not_print(manager, clock, capsys):
    speech = FakeSpeech()
    manager.start(seconds=60)
    clock.advance(60)
    announcer = make_announcer(manager, speech)

    assert announcer.poll() == [speech.spoken[0]]

    assert capsys.readouterr().out == ""


def test_start_polls_immediately_and_stop_does_not_wait_out_the_interval():
    speech = FakeSpeech()
    # A real (default) clock and a real 20 ms timer, because this test is about
    # the thread itself; the interval is an hour, so stop() must wake the loop
    # rather than wait it out.
    manager = TimerManager()
    manager.start(seconds=0.02)
    time.sleep(0.05)
    announcer = make_announcer(manager, speech, poll_interval=3600.0)

    announcer.start()
    try:
        assert announcer.is_running
        assert announcer._thread.daemon is True
        assert speech.spoke_event.wait(timeout=5.0), "a due timer was never spoken"
    finally:
        started = time.monotonic()
        announcer.stop()
        elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"stop() waited {elapsed:.2f}s out of a 3600 s interval"
    assert announcer.is_running is False


def test_start_on_an_empty_manager_stops_cleanly(manager):
    speech = FakeSpeech()
    announcer = make_announcer(manager, speech, poll_interval=0.01)

    announcer.start()
    announcer.stop()

    assert announcer.is_running is False
    assert speech.spoken == []


# --- daemon wiring ----------------------------------------------------------


class FakeEngine:
    """Duck-typed TTS engine: queued speak() returns None, like the real one."""

    def __init__(self, enabled=True, busy=False):
        self.enabled = enabled
        self.spoken = []
        self.busy = busy

    def speak(self, text, **_kwargs):
        self.spoken.append(text)
        return None

    def is_speaking(self):
        return self.busy


def test_daemon_builder_wires_the_shared_manager_and_the_engine_adapter(manager, clock):
    """The tool and the loop must look at the same timers."""
    set_default_manager(manager)
    engine = FakeEngine()
    timer = manager.start(label="pasta", seconds=60)
    clock.advance(60)

    announcer = daemon._build_timer_announcer(engine)

    assert announcer.poll_interval == TIMER_POLL_INTERVAL_SEC
    assert announcer.manager is manager
    assert announcer.poll() == [timer.format_finished()]
    assert engine.spoken == [timer.format_finished()]


def test_daemon_adapter_reports_success_even_though_the_engine_queues(manager, clock):
    set_default_manager(manager)
    engine = FakeEngine()
    timer = manager.start(seconds=60)
    clock.advance(60)
    announcer = daemon._build_timer_announcer(engine)

    assert announcer.poll() == [timer.format_finished()]  # engine returned None
    assert engine.spoken == [timer.format_finished()]
    assert announcer.poll() == []


def test_daemon_busy_check_comes_from_the_engine(manager, clock):
    set_default_manager(manager)
    engine = FakeEngine(busy=True)
    timer = manager.start(seconds=60)
    clock.advance(60)
    announcer = daemon._build_timer_announcer(engine)

    assert announcer.poll() == []
    assert engine.spoken == []
    assert manager.active_count() == 1

    engine.busy = False
    assert announcer.poll() == [timer.format_finished()]


def test_daemon_with_speech_disabled_keeps_the_timer_pending(manager, clock):
    set_default_manager(manager)
    engine = FakeEngine(enabled=False)
    timer = manager.start(seconds=60)
    clock.advance(60)
    announcer = daemon._build_timer_announcer(engine)

    assert announcer.poll() == []
    assert engine.spoken == []
    assert announcer.pending_count == 1

    engine.enabled = True
    assert announcer.poll() == [timer.format_finished()]


def test_daemon_start_and_stop_helpers_use_a_fake_engine(manager, clock, capsys):
    set_default_manager(manager)
    engine = FakeEngine()
    manager.start(seconds=60)
    clock.advance(60)

    daemon._start_timer_announcer(engine)
    try:
        assert wait_until(lambda: engine.spoken != [])
    finally:
        daemon._stop_timer_announcer()

    assert daemon._timer_announcer is None
    printed = capsys.readouterr().out
    assert "Timer announcer armed" in printed


def test_daemon_start_is_idempotent(manager):
    set_default_manager(manager)
    engine = FakeEngine()

    daemon._start_timer_announcer(engine)
    first = daemon._timer_announcer
    try:
        daemon._start_timer_announcer(engine)
        assert daemon._timer_announcer is first
    finally:
        daemon._stop_timer_announcer()


# --- the shared default manager ---------------------------------------------


def test_default_manager_returns_the_same_object_twice():
    first = default_manager()
    assert default_manager() is first


def test_setting_the_default_manager_none_gives_a_fresh_one():
    first = default_manager()
    set_default_manager(None)

    second = default_manager()

    assert second is not first
    assert second.active_count() == 0
