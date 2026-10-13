"""Tests for the reminder announcer — the caller that was missing (R-SCHED-1).

The calendar rules for due reminders were complete and tested while nothing in
the running daemon ever spoke one: an owner could say "remind me at 6 to call
mum" and never hear it.  These tests pin the loop that closes that gap:

* a due reminder is spoken ONCE (exactly ``describe_reminder``'s sentence) and
  committed to a real temporary schedule file, so the round trip through
  ``store`` is genuinely exercised — not mocked;
* failures (speech refused, speech raised, damaged file) are survivable and
  never cost the owner the reminder — it is retried, not dropped, and not
  silently marked as fired;
* the loop never talks over a live turn and stops promptly.

Real end-to-end speech (sound through the owner's speakers) is out of scope
here: ``speak`` is injected, so what is verified is the announcer's contract
with it, not audibility.
"""

import json
import threading
import time
from datetime import datetime

import pytest

from claire import daemon
from claire.schedule import REMINDER_POLL_INTERVAL_SEC, ReminderAnnouncer, calendar as cal
from claire.schedule.store import load_schedule, save_schedule

pytestmark = pytest.mark.unit

# Same pinned date as the calendar tests: 2026-09-24 is a Thursday.
THURSDAY = "2026-09-24"
DUE_AT = datetime(2026, 9, 24, 15, 45)
HAIRCUT = "Reminder: Haircut, Thursday 24 September at 16:00, in 15 minutes."


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


def write_schedule(path, *raw_events):
    schedule = cal.empty_schedule()
    for raw in raw_events:
        outcome = cal.add_event(schedule, raw)
        # add_event refuses on conflict instead of raising: a silent refusal
        # would quietly turn a two-event test into a one-event test.
        assert outcome.get("ok"), f"test setup refused: {outcome.get('error')}"
    save_schedule(schedule, path)
    return schedule


def haircut(reminder_minutes=15):
    return {
        "title": "Haircut",
        "date": THURSDAY,
        "start": "16:00",
        "end": "17:00",
        "reminder_minutes": reminder_minutes,
    }


def make_announcer(path, speech, *, now=DUE_AT, poll_interval=3600.0, logs=None):
    log_lines = logs if logs is not None else []
    return ReminderAnnouncer(
        speech.speak,
        speech.is_speaking,
        schedule_path=path,
        clock=lambda: now,
        poll_interval=poll_interval,
        logger=log_lines.append,
    )


def fired_keys(path):
    return set(load_schedule(path).get("fired_reminders") or [])


# --- the happy path ---------------------------------------------------------

def test_due_reminder_is_spoken_once_marked_and_persisted(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech()
    announcer = make_announcer(path, speech)

    assert announcer.poll() == [HAIRCUT]
    assert speech.spoken == [HAIRCUT]

    # Persisted: the real file on disk now contains the fired key, not just
    # the in-memory dict the poll worked on.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    key = on_disk["events"][0]["id"] + "|" + THURSDAY
    assert key in on_disk["fired_reminders"]
    assert fired_keys(path) == {key}


def test_second_poll_announces_nothing(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech()
    announcer = make_announcer(path, speech)

    assert announcer.poll() == [HAIRCUT]
    assert announcer.poll() == []
    assert speech.spoken == [HAIRCUT]  # exactly once, even across a reload


def test_reminder_not_yet_due_announces_nothing(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech()
    announcer = make_announcer(path, speech, now=datetime(2026, 9, 24, 15, 44))

    assert announcer.poll() == []
    assert speech.spoken == []
    assert fired_keys(path) == set()


def test_event_without_a_reminder_offset_is_untouched(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut(None))
    speech = FakeSpeech()
    announcer = make_announcer(path, speech)

    assert announcer.poll() == []
    assert speech.spoken == []
    assert fired_keys(path) == set()


def test_zero_offset_reminder_fires_during_the_start_minute(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut(0))
    speech = FakeSpeech()
    announcer = make_announcer(path, speech, now=datetime(2026, 9, 24, 16, 0))

    assert announcer.poll() == [
        "Reminder: Haircut, Thursday 24 September at 16:00, starting now."
    ]
    assert announcer.poll() == []


def test_two_due_reminders_are_both_announced(tmp_path):
    path = tmp_path / "schedule.json"
    # Non-overlapping events (add_event refuses clashes) whose reminder
    # moments coincide at 15:45.
    write_schedule(
        path,
        haircut(15),
        {
            "title": "Standup",
            "date": THURSDAY,
            "start": "17:00",
            "end": "17:30",
            "reminder_minutes": 75,
        },
    )
    speech = FakeSpeech()
    announcer = make_announcer(path, speech)

    standup = "Reminder: Standup, Thursday 24 September at 17:00, in 75 minutes."
    assert announcer.poll() == [HAIRCUT, standup]
    assert announcer.poll() == []
    assert len(fired_keys(path)) == 2


# --- failures must not cost the owner a reminder ----------------------------

def test_speech_returning_false_is_not_marked_and_is_retried(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech(ok=False)
    logs = []
    announcer = make_announcer(path, speech, logs=logs)

    assert announcer.poll() == []
    assert fired_keys(path) == set()  # not marked fired while unsaid
    assert any("retry" in line for line in logs)

    speech.ok = True
    assert announcer.poll() == [HAIRCUT]
    assert announcer.poll() == []
    assert len(fired_keys(path)) == 1


def test_speech_raising_is_not_marked_and_is_retried(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech(raise_on_speak=True)
    logs = []
    announcer = make_announcer(path, speech, logs=logs)

    assert announcer.poll() == []
    assert fired_keys(path) == set()
    assert any("failed" in line for line in logs)

    speech.raise_on_speak = False
    assert announcer.poll() == [HAIRCUT]
    assert len(fired_keys(path)) == 1


def test_damaged_schedule_file_is_survived_with_nothing_spoken(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    speech = FakeSpeech()
    logs = []
    announcer = make_announcer(path, speech, logs=logs)

    assert announcer.poll() == []
    assert announcer.poll() == []  # still alive on the next poll too
    assert speech.spoken == []
    assert any("schedule" in line for line in logs)


def test_unreadable_schedule_file_is_survived_with_nothing_spoken(tmp_path):
    # A directory where the file belongs makes read_bytes fail outright.
    path = tmp_path / "schedule.json"
    path.mkdir()
    speech = FakeSpeech()
    announcer = make_announcer(path, speech)

    assert announcer.poll() == []
    assert speech.spoken == []


def test_busy_speech_skips_the_poll_and_is_retried_later(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech(busy=True)
    announcer = make_announcer(path, speech)

    assert announcer.poll() == []
    assert speech.spoken == []
    assert fired_keys(path) == set()  # skipped, not swallowed

    speech.busy = False
    assert announcer.poll() == [HAIRCUT]
    assert len(fired_keys(path)) == 1


def test_a_broken_busy_check_does_not_stall_reminders_forever(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())

    def broken_busy():
        raise RuntimeError("engine state unavailable")

    announcer = ReminderAnnouncer(
        FakeSpeech().speak,
        broken_busy,
        schedule_path=path,
        clock=lambda: DUE_AT,
        poll_interval=3600.0,
        logger=lambda _line: None,
    )
    assert announcer.poll() == [HAIRCUT]


# --- the loop itself --------------------------------------------------------

def test_the_loop_prints_what_she_said_so_the_announcement_is_not_invisible(tmp_path, capsys):
    """A spoken reminder must be visible in the log, not only at debug level.

    The whole bug was invisible: the schedule rules existed, nothing called
    them, and no line in the log said whether she had ever spoken. Her replies
    print, so a reminder she says out loud must print too.
    """
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech()
    announcer = make_announcer(path, speech, poll_interval=3600.0)

    announcer.start()
    try:
        assert speech.spoke_event.wait(timeout=5.0)
        deadline = time.monotonic() + 5.0
        printed = ""
        while time.monotonic() < deadline:
            printed = capsys.readouterr().out
            if HAIRCUT in printed:
                break
            time.sleep(0.05)
    finally:
        announcer.stop()

    assert HAIRCUT in printed, f"the sentence she spoke was never printed; got {printed!r}"
    assert printed.strip().startswith("⏰")


def test_poll_alone_does_not_print(tmp_path, capsys):
    """Printing belongs to the live loop, not to a bare poll."""
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    announcer = make_announcer(path, FakeSpeech())

    assert announcer.poll() == [HAIRCUT]

    assert capsys.readouterr().out == ""


def test_start_polls_immediately_and_stop_does_not_wait_out_the_interval(tmp_path):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    speech = FakeSpeech()
    # One hour between polls: if stop() waited out the interval this test
    # would hang for an hour rather than fail quickly.
    announcer = make_announcer(path, speech, poll_interval=3600.0)

    announcer.start()
    assert announcer.is_running
    assert announcer._thread.daemon is True
    assert speech.spoke_event.wait(timeout=5.0), "the first poll must run immediately"
    assert speech.spoken == [HAIRCUT]

    started = time.monotonic()
    announcer.stop()
    elapsed = time.monotonic() - started
    assert not announcer.is_running
    assert elapsed < 2.0, f"stop() took {elapsed:.2f}s — it must not wait out the interval"
    assert announcer.poll_interval == 3600.0
    assert REMINDER_POLL_INTERVAL_SEC > 0


def test_start_on_an_empty_calendar_stops_cleanly(tmp_path):
    announcer = make_announcer(tmp_path / "absent.json", FakeSpeech(), poll_interval=3600.0)
    announcer.start()
    assert announcer.is_running
    announcer.stop()
    assert not announcer.is_running


# --- daemon wiring: fake engine, no daemon boot -----------------------------

class FakeEngine:
    """Duck-typed TTS engine: queued speak() returns None, exactly like the
    real one, so the daemon's adapter has to be the thing that reports
    success."""

    def __init__(self, enabled=True, busy=False):
        self.enabled = enabled
        self.spoken = []
        self.busy = busy

    def speak(self, text, **_kwargs):
        self.spoken.append(text)
        return None

    def is_speaking(self):
        return self.busy


def build_daemon_announcer(engine, path, monkeypatch):
    """The daemon's own builder, pointed at a temp schedule and pinned now."""
    monkeypatch.setenv("CLAIRE_SCHEDULE_PATH", str(path))
    monkeypatch.setattr("claire.schedule.announcer._default_clock", lambda: DUE_AT)
    return daemon._build_reminder_announcer(engine)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_daemon_adapter_reports_success_even_though_the_engine_queues(
    tmp_path, monkeypatch
):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    engine = FakeEngine()
    announcer = build_daemon_announcer(engine, path, monkeypatch)

    assert announcer.poll_interval == REMINDER_POLL_INTERVAL_SEC
    assert announcer.schedule_path == path
    assert announcer.poll() == [HAIRCUT]  # engine returned None; adapter said yes
    assert engine.spoken == [HAIRCUT]
    assert len(fired_keys(path)) == 1
    assert announcer.poll() == []


def test_daemon_busy_check_comes_from_the_engine(tmp_path, monkeypatch):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    engine = FakeEngine(busy=True)
    announcer = build_daemon_announcer(engine, path, monkeypatch)

    assert announcer.poll() == []
    assert engine.spoken == []
    assert fired_keys(path) == set()

    engine.busy = False
    assert announcer.poll() == [HAIRCUT]
    assert len(fired_keys(path)) == 1


def test_daemon_with_speech_disabled_never_marks_a_reminder_fired(tmp_path, monkeypatch):
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    engine = FakeEngine(enabled=False)
    announcer = build_daemon_announcer(engine, path, monkeypatch)

    assert announcer.poll() == []
    assert engine.spoken == []
    assert fired_keys(path) == set()  # still due: the owner hears it later


def test_daemon_start_speaks_a_due_reminder_through_the_engine(tmp_path, monkeypatch):
    """Full daemon-side chain: helper -> thread -> adapter -> fake engine."""
    path = tmp_path / "schedule.json"
    write_schedule(path, haircut())
    monkeypatch.setenv("CLAIRE_SCHEDULE_PATH", str(path))
    monkeypatch.setattr("claire.schedule.announcer._default_clock", lambda: DUE_AT)
    monkeypatch.setattr(daemon, "_reminder_announcer", None)
    engine = FakeEngine()
    try:
        daemon._start_reminder_announcer(engine)
        announcer = daemon._reminder_announcer
        assert announcer is not None and announcer.is_running
        assert wait_until(lambda: engine.spoken == [HAIRCUT]), "never spoke"
        assert wait_until(lambda: len(fired_keys(path)) == 1), "never persisted"
    finally:
        daemon._stop_reminder_announcer()
    assert daemon._reminder_announcer is None
    assert not announcer.is_running


def test_daemon_start_prints_the_interval_and_start_stop_are_safe(
    tmp_path, monkeypatch, capsys
):
    # Pin the schedule path to an empty temp file: the announcer must never
    # be let loose on the owner's real calendar from a test.
    monkeypatch.setenv("CLAIRE_SCHEDULE_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(daemon, "_reminder_announcer", None)
    try:
        daemon._stop_reminder_announcer()  # no start yet: must be a no-op
        daemon._start_reminder_announcer(FakeEngine())
        started = daemon._reminder_announcer
        assert started is not None and started.is_running
        out, _err = capsys.readouterr()
        assert "Reminder announcer polling every" in out
        assert str(REMINDER_POLL_INTERVAL_SEC) in out

        daemon._start_reminder_announcer(FakeEngine())  # must not restart it
        assert daemon._reminder_announcer is started
    finally:
        daemon._stop_reminder_announcer()
    assert daemon._reminder_announcer is None
    assert not started.is_running
