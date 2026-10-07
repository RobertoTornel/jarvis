"""Tests for countdown timers (``claire.schedule.timers``, R-SCHED-1).

Every test drives time through an injected fake clock or a monkeypatched
``time.monotonic`` — nothing here sleeps for real time, so the failure modes
that matter ("it fires late because the clock was stepped", "it fires twice
because two pollers raced") are reproducible in microseconds instead of being
hidden behind a wall of ``time.sleep``.
"""

from __future__ import annotations

import threading
import time
from typing import List

import pytest

import claire.schedule.timers as timers
from claire.schedule.timers import (
    AmbiguousTimerError,
    InvalidDurationError,
    InvalidLabelError,
    Timer,
    TimerError,
    TimerManager,
    UnknownTimerError,
    parse_duration,
)

# The gate runs `pytest -m unit`; without this marker these tests are
# deselected, so they would pass while never running in CI.
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


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def manager(clock: FakeClock) -> TimerManager:
    return TimerManager(clock=clock)


# ---------------------------------------------------------------------------
# duration parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90s", 90.0),
        ("90 s", 90.0),
        ("90sec", 90.0),
        ("90 seconds", 90.0),
        ("1h30m", 5400.0),
        ("1h 30m", 5400.0),
        ("25 minutes", 1500.0),
        ("25 min", 1500.0),
        ("25m", 1500.0),
        ("2 hours", 7200.0),
        ("1 hour and 30 minutes", 5400.0),
        ("1h, 30m", 5400.0),
        ("1.5h", 5400.0),
        ("0.5 minutes", 30.0),
        ("1h30m45s", 5445.0),
        ("  90S  ", 90.0),
        ("1h 30", 5400.0),  # trailing unit-less number is minutes, like the rest
    ],
)
def test_parse_duration_accepts_each_form(text, expected):
    assert parse_duration(text) == expected


def test_parse_duration_bare_number_means_minutes():
    # A stated rule, not a guess: "set a timer for 25" is 25 minutes. Reading it
    # as seconds would fire sixty times too early.
    assert parse_duration("25") == 1500.0
    assert parse_duration(25) == 25.0  # a real number is already seconds
    assert parse_duration(2.5) == 2.5


@pytest.mark.parametrize(
    "nonsense",
    ["", "   ", "t", "h", "abc", "twenty minutes", "10 fortnights", "1t",
     "1e3", "-5", "0s", "0", "5-", "9..9s", "min"],
)
def test_parse_duration_raises_named_error_on_nonsense(nonsense):
    with pytest.raises(InvalidDurationError) as excinfo:
        parse_duration(nonsense)
    # The error must explain itself, not just be a type.
    assert str(excinfo.value).strip()
    assert isinstance(excinfo.value, TimerError)
    assert isinstance(excinfo.value, ValueError)


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf"), True, False, [], {}])
def test_parse_duration_rejects_unusable_values(bad):
    with pytest.raises(InvalidDurationError):
        parse_duration(bad)


def test_parse_duration_refuses_a_decimal_comma_rather_than_misreading_it():
    # "1,5h" is 1.5 hours in some locales but "1h, 30m"-style parts here; guessing
    # wrong would turn 1.5 hours into 5 hours 1 minute, so it is refused.
    with pytest.raises(InvalidDurationError) as excinfo:
        parse_duration("1,5h")
    assert "comma" in str(excinfo.value).lower()
    # A comma that separates parts is still fine.
    assert parse_duration("1h, 30m") == 5400.0


def test_parse_duration_zero_and_negative_numbers_are_refused():
    with pytest.raises(InvalidDurationError):
        parse_duration(0)
    with pytest.raises(InvalidDurationError):
        parse_duration(-30)
    with pytest.raises(InvalidDurationError):
        parse_duration(-30.5)


# ---------------------------------------------------------------------------
# start / remaining
# ---------------------------------------------------------------------------


def test_start_returns_timer_with_deadline_on_the_injected_clock(manager, clock):
    timer = manager.start("tea", "5m")
    assert isinstance(timer, Timer)
    assert timer.label == "tea"
    assert timer.seconds == 300.0
    assert timer.deadline == clock() + 300.0
    assert timer.created_at == clock()


def test_start_accepts_a_number_of_seconds(manager, clock):
    timer = manager.start("eggs", 90)
    assert timer.seconds == 90.0
    assert timer.deadline == clock() + 90.0


def test_start_requires_a_duration(manager):
    with pytest.raises(InvalidDurationError) as excinfo:
        manager.start("tea", None)
    assert "duration" in str(excinfo.value).lower()


def test_start_rejects_nonsense_duration_without_creating_a_timer(manager):
    with pytest.raises(InvalidDurationError):
        manager.start("tea", "about twenty")
    # Atomic refusal: nothing was half-created.
    assert manager.list_active() == []
    assert manager.active_count() == 0


@pytest.mark.parametrize("bad_label", [7, 3.5, ["tea"], {"a": 1}])
def test_start_rejects_non_text_label(manager, bad_label):
    with pytest.raises(InvalidLabelError):
        manager.start(bad_label, "5m")
    assert manager.active_count() == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_start_rejects_blank_label(manager, blank):
    with pytest.raises(InvalidLabelError):
        manager.start(blank, "5m")
    assert manager.active_count() == 0


def test_remaining_counts_down_with_the_clock(manager, clock):
    manager.start("tea", 300)
    assert manager.remaining("tea") == 300.0
    clock.advance(120)
    assert manager.remaining("tea") == 180.0
    clock.advance(60)
    assert manager.remaining("tea") == 120.0


def test_remaining_without_identifier_is_the_soonest_timer(manager, clock):
    manager.start("slow", 600)
    manager.start("soon", 60)
    assert manager.remaining() == 60.0
    clock.advance(30)
    assert manager.remaining() == 30.0


def test_remaining_is_none_when_nothing_is_running(manager):
    assert manager.remaining() is None


def test_remaining_is_clamped_at_zero_once_due(manager, clock):
    manager.start("tea", 60)
    clock.advance(120)
    # Due but not yet collected: still "nothing left", never "-60 seconds",
    # because a negative countdown is not something worth speaking.
    assert manager.remaining("tea") == 0.0
    assert manager.list_active()[0].is_due(clock())


def test_remaining_of_unknown_timer_raises(manager):
    with pytest.raises(UnknownTimerError):
        manager.remaining("nope")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_by_label_removes_the_timer(manager, clock):
    manager.start("tea", 60)
    cancelled = manager.cancel("tea")
    assert cancelled.label == "tea"
    assert manager.list_active() == []
    clock.advance(600)
    assert manager.fire_due() == []


def test_cancel_by_number(manager, clock):
    first = manager.start("tea", 60)
    manager.start("coffee", 120)
    cancelled = manager.cancel(first.number)
    assert cancelled.number == first.number
    assert [t.label for t in manager.list_active()] == ["coffee"]


def test_cancel_by_numeric_string(manager):
    manager.start("tea", 60)
    assert manager.cancel("1").label == "tea"
    assert manager.active_count() == 0


def test_cancel_is_case_insensitive(manager):
    manager.start("Tea", 60)
    assert manager.cancel("tea").label == "Tea"


def test_cancel_unknown_raises_named_error(manager):
    with pytest.raises(UnknownTimerError) as excinfo:
        manager.cancel("ghost")
    assert "ghost" in str(excinfo.value)
    with pytest.raises(UnknownTimerError):
        manager.cancel(99)
    with pytest.raises(UnknownTimerError):
        manager.cancel("")


def test_cancel_ambiguous_label_raises_and_changes_nothing(manager):
    manager.start("tea", 60)
    manager.start("tea", 120)
    with pytest.raises(AmbiguousTimerError) as excinfo:
        manager.cancel("tea")
    message = str(excinfo.value)
    assert "1" in message and "2" in message
    # Refusing an ambiguous cancel must not cancel anything as a side effect.
    assert manager.active_count() == 2


def test_number_lookup_wins_over_a_numeric_label(manager):
    # "cancel 5" from the voice loop is overwhelmingly a number, so an explicit
    # numeric label must not shadow it. Documented behaviour, tested on purpose.
    manager.start("5", 60)          # timer 1, label "5"
    numbered = manager.start("x", 60)  # timer 2
    manager.start("y", 60)          # timer 3
    for _ in range(2):
        manager.start("z", 60)      # timers 4, 5
    assert manager.cancel("5").number == 5
    assert manager.active_count() == 4
    assert numbered.number == 2


# ---------------------------------------------------------------------------
# auto-numbering and labels
# ---------------------------------------------------------------------------


def test_automatic_labels_are_numbered(manager):
    first = manager.start(seconds=60)
    second = manager.start(seconds=60)
    assert first.label == "timer 1"
    assert second.label == "timer 2"
    assert (first.number, second.number) == (1, 2)
    assert manager.remaining("timer 1") == 60.0
    assert manager.cancel("timer 2").number == 2


def test_explicit_labels_still_get_a_number(manager):
    timer = manager.start("tea", 60)
    assert timer.number == 1
    assert timer.label == "tea"


def test_numbers_are_not_reused_after_a_cancel(manager):
    # Reusing a cancelled timer's number would let "cancel 1" later hit a
    # different timer than the one the user meant.
    manager.start("first", 60)
    manager.cancel(1)
    fresh = manager.start("second", 60)
    assert fresh.number == 2
    assert fresh.label == "second"


def test_whitespace_in_labels_is_normalised(manager):
    timer = manager.start("  green   tea  ", 60)
    assert timer.label == "green tea"
    assert manager.cancel("green tea").number == timer.number


def test_list_active_shows_every_running_timer(manager, clock):
    manager.start("a", 60)
    manager.start("b", 30)
    clock.advance(10)
    manager.start("c", 60)
    assert [t.label for t in manager.list_active()] == ["b", "a", "c"]


# ---------------------------------------------------------------------------
# list_active ordering
# ---------------------------------------------------------------------------


def test_list_active_is_ordered_by_deadline(manager, clock):
    manager.start("third", 300)
    manager.start("first", 60)
    manager.start("second", 120)
    assert [t.label for t in manager.list_active()] == ["first", "second", "third"]


def test_list_active_breaks_deadline_ties_by_number(manager):
    first = manager.start("same-a", 60)
    second = manager.start("same-b", 60)
    assert [t.number for t in manager.list_active()] == [first.number, second.number]


def test_list_active_is_empty_at_the_start(manager):
    assert manager.list_active() == []


# ---------------------------------------------------------------------------
# fire_due: exactly once, never twice
# ---------------------------------------------------------------------------


def test_fire_due_returns_nothing_before_the_deadline(manager, clock):
    manager.start("tea", 60)
    clock.advance(59.999)
    assert manager.fire_due() == []
    assert manager.active_count() == 1


def test_fire_due_fires_at_exactly_the_deadline(manager, clock):
    manager.start("tea", 60)
    clock.advance(60)
    fired = manager.fire_due()
    assert [t.label for t in fired] == ["tea"]


def test_fire_due_fires_a_timer_exactly_once_and_never_twice(manager, clock):
    timer = manager.start("tea", 60)
    clock.advance(60)
    first = manager.fire_due()
    assert [t.number for t in first] == [timer.number]
    # The timer was removed as it was collected, so a second poll cannot
    # re-announce the same alarm.
    assert manager.fire_due() == []
    assert manager.fire_due() == []
    assert manager.active_count() == 0


def test_fire_due_fires_multiple_due_timers_once_each_in_deadline_order(manager, clock):
    manager.start("later", 120)
    manager.start("sooner", 30)
    clock.advance(120)
    fired = manager.fire_due()
    assert [t.label for t in fired] == ["sooner", "later"]
    assert manager.fire_due() == []
    assert manager.active_count() == 0


def test_fire_due_leaves_not_yet_due_timers_alone(manager, clock):
    manager.start("due", 30)
    manager.start("later", 300)
    clock.advance(30)
    assert [t.label for t in manager.fire_due()] == ["due"]
    assert [t.label for t in manager.list_active()] == ["later"]


def test_a_deadline_that_passed_while_nothing_polled_still_fires_once(manager, clock):
    timer = manager.start("overdue", 60)
    # Nothing polls for a long time; the timer must not be skipped, and must not
    # be reported once per missed poll either.
    clock.advance(86_400)
    fired = manager.fire_due()
    assert [t.number for t in fired] == [timer.number]
    assert manager.fire_due() == []
    assert manager.active_count() == 0


def test_fire_due_can_be_given_an_explicit_now(manager, clock):
    manager.start("tea", 60)
    assert manager.fire_due(now=clock() - 1) == []
    assert [t.label for t in manager.fire_due(now=clock() + 61)] == ["tea"]


@pytest.mark.parametrize("identifier", ["tea", 1])
def test_cancelled_timers_never_fire(manager, clock, identifier):
    manager.start("tea", 60)
    manager.cancel(identifier)
    clock.advance(10_000)
    assert manager.fire_due() == []
    assert manager.active_count() == 0


def test_fire_due_and_list_active_do_not_disturb_live_timers(manager, clock):
    manager.start("live", 600)
    manager.start("expired", 60)
    clock.advance(120)
    assert [t.label for t in manager.fire_due()] == ["expired"]
    assert [t.label for t in manager.list_active()] == ["live"]
    assert manager.remaining("live") == 480.0


# ---------------------------------------------------------------------------
# the clock is monotonic, and the wall clock is irrelevant
# ---------------------------------------------------------------------------


def test_deadline_uses_monotonic_not_wall_clock(clock, monkeypatch):
    monotonic_now = 5_000.0
    monkeypatch.setattr(timers.time, "monotonic", lambda: monotonic_now)
    manager = TimerManager()  # default clock, resolved at call time
    timer = manager.start("tea", 60)
    assert timer.deadline == monotonic_now + 60.0


def test_stepping_the_wall_clock_does_not_move_a_deadline(manager, clock, monkeypatch):
    # An NTP correction / manual clock fix mid-countdown: a wall-clock deadline
    # would shift by an hour and fire early or late. A monotonic one cannot.
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    timer = manager.start("tea", 60)
    monkeypatch.setattr(time, "time", lambda: 1_700_003_600.0)
    assert manager.remaining("tea") == 60.0
    assert manager.fire_due() == []
    clock.advance(60)
    assert [t.number for t in manager.fire_due()] == [timer.number]


def test_manager_rejects_a_non_callable_clock():
    with pytest.raises(TimerError):
        TimerManager(clock=42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# not persisted, and honest about it
# ---------------------------------------------------------------------------


def test_persistence_note_says_timers_do_not_survive_a_restart():
    note = TimerManager.PERSISTENCE_NOTE
    assert "restart" in note.lower()
    assert "memory" in note.lower() or "not survive" in note.lower()


def test_manager_offers_no_persistence_api(manager):
    # Nothing that pretends a timer outlives the process.
    for name in ("save", "load", "persist", "to_dict", "from_dict", "dump", "restore"):
        assert not hasattr(manager, name), name


def test_a_fresh_manager_after_restart_knows_nothing(manager, clock):
    manager.start("tea", 60)
    restarted = TimerManager(clock=clock)  # same clock, new in-memory manager
    assert restarted.list_active() == []
    assert restarted.remaining() is None


# ---------------------------------------------------------------------------
# locking: one reader, one voice loop
# ---------------------------------------------------------------------------


def test_concurrent_start_assigns_unique_numbers_once_each(clock):
    manager = TimerManager(clock=clock)
    count = 60
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        barrier.wait()
        manager.start(f"t{index}", 60 + index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    active = manager.list_active()
    assert len(active) == count
    assert manager.active_count() == count
    numbers = sorted(t.number for t in active)
    assert numbers == list(range(1, count + 1))
    assert len({t.label for t in active}) == count


def test_concurrent_fire_due_hands_each_timer_to_exactly_one_thread(clock):
    manager = TimerManager(clock=clock)
    count = 40
    for i in range(count):
        manager.start(f"t{i}", 1)
    clock.advance(5)  # every deadline is now in the past, frozen

    pollers = 8
    barrier = threading.Barrier(pollers)
    fired: List[int] = []
    lock = threading.Lock()

    def poller() -> None:
        barrier.wait()
        for _ in range(5):
            for timer in manager.fire_due():
                with lock:
                    fired.append(timer.number)

    threads = [threading.Thread(target=poller) for _ in range(pollers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(fired) == list(range(1, count + 1))
    assert len(fired) == count  # no duplicates: each timer fired exactly once
    assert manager.active_count() == 0


def test_concurrent_cancels_report_one_winner_per_timer(clock):
    manager = TimerManager(clock=clock)
    for i in range(20):
        manager.start(f"t{i}", 60)
    won: List[int] = []
    lost: List[UnknownTimerError] = []
    lock = threading.Lock()

    def canceller(number: int) -> None:
        try:
            timer = manager.cancel(number)
        except UnknownTimerError as exc:
            with lock:
                lost.append(exc)
        else:
            with lock:
                won.append(timer.number)

    threads = [
        threading.Thread(target=canceller, args=(number,))
        for _ in range(2)
        for number in range(1, 21)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(won) == list(range(1, 21))
    assert len(lost) == 20  # the second cancel of each timer reports failure
    assert manager.active_count() == 0


# ---------------------------------------------------------------------------
# spoken output
# ---------------------------------------------------------------------------


def test_format_spoken_describes_the_soonest_timer(manager, clock):
    manager.start("tea", 300)
    assert manager.format_spoken() == "tea has 5 minutes left"
    clock.advance(60)
    assert manager.format_spoken() == "tea has 4 minutes left"


def test_format_spoken_with_no_timers(manager):
    assert manager.format_spoken() == "no timers are running"


def test_format_spoken_uses_hours_and_seconds_readably(manager, clock):
    manager.start("roast", 5400)
    assert manager.format_spoken() == "roast has 1 hour 30 minutes left"
    clock.advance(5390)
    assert manager.format_spoken() == "roast has 10 seconds left"


def test_timer_format_spoken_uses_the_manager_clock_only_when_given(manager):
    timer = manager.start("tea", 60)
    assert timer.format_spoken(now=timer.deadline - 30) == "30 seconds left"


def test_timer_format_spoken_refuses_to_guess_a_clock(manager):
    # A Timer cannot know which clock measured its deadline; falling back on the
    # real monotonic clock would report nonsense for a timer started on an
    # injected one, so it asks instead of guessing.
    timer = manager.start("tea", 60)
    with pytest.raises(TimerError) as excinfo:
        timer.format_spoken()
    assert "clock" in str(excinfo.value).lower()


@pytest.mark.parametrize("bad_now", ["abc", object(), float("nan"), float("inf")])
def test_explicit_now_must_be_a_finite_number(manager, bad_now):
    manager.start("tea", 60)
    for call in (
        lambda: manager.fire_due(now=bad_now),
        lambda: manager.remaining(now=bad_now),
        lambda: manager.format_spoken(now=bad_now),
    ):
        with pytest.raises(TimerError):
            call()


def test_explicit_now_none_means_reuse_the_injected_clock(manager, clock):
    manager.start("tea", 60)
    assert manager.remaining(now=None) == 60.0
    clock.advance(30)
    assert manager.remaining(now=None) == 30.0


def test_no_test_in_this_file_sleeps_for_real_time(manager, clock):
    # A guard on the suite itself: this whole file runs on a fake clock.
    started = time.perf_counter()
    manager.start("quick", "1s")
    clock.advance(10_000)
    assert len(manager.fire_due()) == 1
    assert time.perf_counter() - started < 1.0
