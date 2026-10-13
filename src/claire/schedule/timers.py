"""Countdown timers for Claire — deliberately NOT calendar events.

R-SCHED-1 models *calendar* events (see ``claire.schedule.calendar``): they have
a start, an end, a date, they repeat, and they live in a diary that outlives the
process. A countdown timer ("wake me in 20 minutes") has none of those
properties, and squeezing it into the calendar model produces real misbehaviour:

  * A timer has no end time. As a calendar event it would have to be stored as a
    zero-length booking, and half-open overlap logic (``aStart < bEnd and
    bStart < aEnd``) would then declare it free for every other booking — the
    timer would look like an empty slot and could be "moved".
  * A timer is a *deadline*, not a wall-clock slot. A diary entry at
    15:00-15:20 is simply a different fact from "20 minutes from right now".
  * Calendar entries are listed for days and persist across restarts. A timer
    that survived a restart would fire days late, announcing a deadline that
    passed while Claire was off.

So timers get their own small model here, and they are process-local. They do
NOT survive a restart of Claire, and this module says so out loud instead of
writing them to disk and pretending to persist:
``TimerManager.PERSISTENCE_NOTE`` is the sentence the voice loop can read back
to the user.

Deadlines are measured with ``time.monotonic()``, never with the wall clock. A
wall-clock deadline (``time.time() + 60``) silently shifts whenever the system
clock is stepped: an NTP correction, a manual clock fix or a DST shuffle
mid-countdown would make a ten-minute timer fire early or late by however far
the clock moved. The monotonic clock only ever moves forward at the rate of real
elapsed time, so "20 minutes from now" really is 20 minutes of waiting. The
price is that a monotonic reading means nothing outside this process — which is
exactly right for a countdown timer and is the same fact that makes timers
impossible to persist honestly.

The clock is injectable (``TimerManager(clock=...)``) and the default resolves
``time.monotonic`` at call time, so tests can inject a fake clock or
monkeypatch ``time.monotonic`` without ever sleeping for real time.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

__all__ = [
    "TimerError",
    "InvalidDurationError",
    "InvalidLabelError",
    "UnknownTimerError",
    "AmbiguousTimerError",
    "Timer",
    "TimerManager",
    "parse_duration",
    "default_manager",
    "set_default_manager",
]


class TimerError(Exception):
    """Base class for every named timer failure in this module."""


class InvalidDurationError(TimerError, ValueError):
    """A duration was missing, unparseable, or not a positive span of time.

    Raised instead of falling back on a default: a misheard "twenty" that
    silently became a default duration would fire at a time the user never
    asked for, and a timer that fires at the wrong time is worse than a timer
    that was refused and explained.
    """


class InvalidLabelError(TimerError, ValueError):
    """A timer label was not a non-empty string."""


class UnknownTimerError(TimerError, KeyError):
    """No active timer matches the given label or number.

    Note that ``factor``/``identifier`` is included in the message rather than
    used as the ``KeyError`` argument so the text stays readable when spoken.
    """


class AmbiguousTimerError(TimerError):
    """A label matches more than one active timer, so cancelling it is unsafe."""


# --------------------------------------------------------------------------
# Duration parsing
# --------------------------------------------------------------------------

_UNIT_SECONDS: Dict[str, float] = {
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
}

# A number, optionally followed by a unit word. Anything between or after these
# parts must be whitespace or a comma; anything else is junk, not a duration.
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]*)")

# "25" with no unit means 25 *minutes*. This is a stated rule, not a guess: a
# voice request that names no unit always means minutes ("set a timer for 25"),
# and reading it as seconds would fire sixty times too early.
_DEFAULT_UNIT = "minutes"


def parse_duration(value: Union[str, int, float]) -> float:
    """Parse ``'90s'``, ``'1h30m'``, ``'25 minutes'`` into seconds.

    Accepted forms (case-insensitive, whitespace and commas optional):
    ``'90s'``, ``'1h30m'``, ``'1h 30m'``, ``'2 hours'``, ``'1 hour 30 minutes'``,
    ``'1.5h'`` and a bare number, which means minutes (``'25'`` -> 1500).

    Raises ``InvalidDurationError`` — never a silent default — for an empty
    string, a bare unit letter with no number (``'t'``, ``'h'``), an unknown
    unit (``'10 fortnights'``), a zero or negative span, and non-finite values.
    """
    if value is None:
        raise InvalidDurationError(
            "I need a duration for the timer, for example '25 minutes'."
        )
    if isinstance(value, bool):
        # bool is an int subclass; True would otherwise become 1 minute.
        raise InvalidDurationError(f"{value!r} is not a duration.")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds):
            raise InvalidDurationError(f"{value!r} is not a duration.")
        if seconds <= 0:
            raise InvalidDurationError(
                f"a timer needs a positive duration, not {seconds:g} seconds."
            )
        return seconds
    if not isinstance(value, str):
        raise InvalidDurationError(
            f"I cannot read a duration from {type(value).__name__}."
        )

    text = value.strip().lower()
    if not text:
        raise InvalidDurationError("I need a duration for the timer, for example '25 minutes'.")
    # A comma between digits is a decimal comma in many locales ("1,5h"), but
    # here commas also separate parts ("1h, 30m"). Guessing wrong would turn
    # 1.5 hours into 5 hours 1 minute, so refuse rather than misread.
    if re.search(r"\d\s*,\s*\d", text):
        raise InvalidDurationError(
            f"I read a comma between digits in '{value}'. Write fractions with a "
            "point ('1.5h') and separate parts with spaces ('1h 30m')."
        )
    # "1 hour and 30 minutes" and "1h 30m" are the same request.
    text = re.sub(r"\band\b", " ", text)

    total = 0.0
    matched_any = False
    pos = 0
    for match in _PART_RE.finditer(text):
        gap = text[pos:match.start()]
        if gap.strip(" \t,"):
            raise InvalidDurationError(
                f"I could not read a duration from '{value}'. "
                "Try '25 minutes', '90s' or '1h30m'."
            )
        number = float(match.group(1))
        unit = match.group(2) or _DEFAULT_UNIT
        if unit not in _UNIT_SECONDS:
            raise InvalidDurationError(
                f"'{unit}' is not a unit of time I know in '{value}'. "
                "I understand hours, minutes and seconds."
            )
        total += number * _UNIT_SECONDS[unit]
        matched_any = True
        pos = match.end()

    if text[pos:].strip(" \t,"):
        raise InvalidDurationError(
            f"I could not read a duration from '{value}'. "
            "Try '25 minutes', '90s' or '1h30m'."
        )
    if not matched_any:
        # e.g. "t" on its own: a unit with no quantity. Inventing a number here
        # would be a silent default, so refuse and say what was heard.
        raise InvalidDurationError(
            f"'{value}' has no number in it, so I do not know how long the timer should be."
        )
    if not math.isfinite(total) or total <= 0:
        raise InvalidDurationError(
            f"a timer needs a positive duration, not '{value}'."
        )
    return total


def _default_clock() -> float:
    """Resolve ``time.monotonic`` at call time so it can be monkeypatched."""
    return time.monotonic()


def _validate_now(now: object) -> float:
    """Validate a caller-supplied clock reading.

    ``now`` must come from the same clock the deadlines were measured with;
    accepting a wall-clock timestamp here by accident would compare two
    unrelated number lines, so a bad value is a named error, not a coerced one.
    """
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise TimerError(
            f"now must be a number of seconds from the same clock, not {type(now).__name__}."
        )
    moment = float(now)
    if not math.isfinite(moment):
        raise TimerError(f"now must be a finite number of seconds, not {now!r}.")
    return moment


# --------------------------------------------------------------------------
# The timer itself
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Timer:
    """One countdown timer: a label, a span, and a monotonic deadline."""

    number: int
    label: str
    seconds: float
    deadline: float
    created_at: float

    def remaining_seconds(self, now: float) -> float:
        """Seconds left, clamped at zero.

        A negative "remaining" is meaningless to speak aloud: once the deadline
        has passed the timer is due, and calling it "-37 seconds" invites the
        voice loop to say something absurd.
        """
        return max(0.0, self.deadline - now)

    def is_due(self, now: float) -> bool:
        return now >= self.deadline

    def format_spoken(self, now: Optional[float] = None) -> str:
        """Say how long is left. ``now`` must come from the same clock this
        timer's deadline was measured with; this object cannot know which clock
        that was, and guessing the real one would report nonsense for a timer
        started against an injected clock.
        """
        if now is None:
            raise TimerError(
                "format_spoken needs a reading from the same clock the deadline "
                "was measured with; pass now=."
            )
        return format_spoken(self.remaining_seconds(_validate_now(now)))

    def format_finished(self) -> str:
        """The one sentence to say when this timer reaches zero.

        An auto-generated label is dropped, because the label already *is* the
        fact being announced: saying "timer 1 — your 10 minute timer is up"
        tells the owner it is a timer twice in one breath and sounds like a
        machine reading a row out of a table. A label the owner chose is kept,
        because that is the word they will recognise: "pasta — your 10 minute
        timer is up" says which timer fired *and* how long it was for.

        The em dash is deliberate. It separates the owner's label from the
        announcement without sounding like a comma (which would read as a list)
        or a colon (which would read as a heading), and it is the same marker
        used everywhere else Claire attributes a sentence to a name.

        The span goes through :func:`_attributive` on the way in, so a 25 minute
        timer is announced as "Your 25 minute timer is up" and not as "25
        minutes timer", which is the one thing about this sentence that would
        sound like a machine.
        """
        span = _attributive(_humanise(self.seconds))
        if self.label == f"timer {self.number}":
            return f"Your {span} timer is up"
        return f"{self.label} — your {span} timer is up"


def _humanise(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    chunks: List[str] = []
    if hours:
        chunks.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        chunks.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    # Seconds are dropped once there is a larger unit: "1 hour 30 minutes 4
    # seconds" is worse to hear than "1 hour 30 minutes", and precision at that
    # scale is not what a spoken countdown is for.
    if secs and not chunks:
        chunks.append(f"{secs} second" + ("s" if secs != 1 else ""))
    if not chunks:
        return "no time"
    return " ".join(chunks)


def format_spoken(remaining_seconds: float) -> str:
    """Turn a remaining span into something worth saying aloud."""
    return f"{_humanise(remaining_seconds)} left"


def _attributive(span: str) -> str:
    """Make a spoken span fit in front of a noun.

    English drops the plural on a unit that qualifies a noun: a person says "a
    25 minute timer", never "a 25 minutes timer".  Every unit ``_humanise`` can
    emit is singularised, so "1 hour 30 minutes" becomes "1 hour 30 minute",
    which is what the ear expects on the way into "... timer is up".
    """
    return " ".join(
        word[:-1] if word in ("hours", "minutes", "seconds") else word
        for word in span.split()
    )


# --------------------------------------------------------------------------
# The manager
# --------------------------------------------------------------------------


class TimerManager:
    """The live set of countdown timers, guarded by a lock.

    Thread-safe enough for the intended split: the voice loop starts and cancels
    timers from one thread while a poller (or a second read) calls
    ``fire_due``/``list_active``. Every read and every mutation happens under
    ``self._lock``, and ``fire_due`` removes a due timer inside the same critical
    section in which it collects it, so two threads polling at once cannot both
    take the same timer.
    """

    # Said out loud, not implied: timers exist only in this process.
    PERSISTENCE_NOTE = (
        "Timers only live in memory while Claire is running; they do not "
        "survive a restart."
    )

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        if clock is not None and not callable(clock):
            raise TimerError("clock must be a callable returning seconds.")
        self._clock: Callable[[], float] = clock if clock is not None else _default_clock
        self._lock = threading.RLock()
        self._timers: Dict[int, Timer] = {}
        self._next_number = 1

    # -- internals ---------------------------------------------------------

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise TimerError(f"the clock returned {now!r}, which is not a usable time.")
        return now

    @staticmethod
    def _normalise_label(label: Optional[str]) -> Optional[str]:
        if label is None:
            return None
        if not isinstance(label, str):
            raise InvalidLabelError(
                f"a timer label must be text, not {type(label).__name__}."
            )
        cleaned = " ".join(label.split())
        if not cleaned:
            raise InvalidLabelError("a timer label cannot be blank.")
        return cleaned

    @staticmethod
    def _identifier_text(identifier: object) -> str:
        if isinstance(identifier, bool):
            raise UnknownTimerError(f"{identifier!r} is not a timer label or number.")
        if isinstance(identifier, int):
            return str(identifier)
        if isinstance(identifier, str):
            return identifier.strip()
        raise UnknownTimerError(
            f"a timer is identified by a label or a number, not {type(identifier).__name__}."
        )

    def _find(self, identifier: object) -> Timer:
        """Resolve a label or an auto-number to exactly one active timer."""
        text = self._identifier_text(identifier)
        if not text:
            raise UnknownTimerError("I need a timer label or number to look up.")
        if text.isdigit():
            number = int(text)
            timer = self._timers.get(number)
            if timer is None:
                raise UnknownTimerError(f"there is no timer number {number} running.")
            return timer
        wanted = text.casefold()
        matches = [t for t in self._timers.values() if t.label.casefold() == wanted]
        if not matches:
            raise UnknownTimerError(f"there is no timer called '{text}' running.")
        if len(matches) > 1:
            numbers = ", ".join(str(t.number) for t in sorted(matches, key=lambda t: t.number))
            raise AmbiguousTimerError(
                f"'{text}' matches {len(matches)} timers (numbers {numbers}); "
                "cancel one by its number."
            )
        return matches[0]

    # -- public API --------------------------------------------------------

    def start(
        self,
        label: Optional[str] = None,
        seconds: Union[str, int, float, None] = None,
    ) -> Timer:
        """Start a timer and return it.

        ``label`` is optional; without one the timer is auto-numbered and named
        ``'timer 1'``, ``'timer 2'``, ... ``seconds`` may be a number of seconds
        or a duration string such as ``'90s'`` or ``'25 minutes'``. A missing or
        unreadable duration raises ``InvalidDurationError``.
        """
        cleaned = self._normalise_label(label)
        if seconds is None:
            raise InvalidDurationError(
                "I need a duration for the timer, for example '25 minutes'."
            )
        span = parse_duration(seconds)
        with self._lock:
            now = self._now()
            number = self._next_number
            # Numbers are never reused. Reusing the number of a cancelled timer
            # would let "cancel 2" quietly hit a *different* timer than the one
            # the user meant, and that is exactly the kind of silent mix-up that
            # makes a voice assistant untrustworthy.
            self._next_number += 1
            timer = Timer(
                number=number,
                label=cleaned if cleaned is not None else f"timer {number}",
                seconds=span,
                deadline=now + span,
                created_at=now,
            )
            self._timers[number] = timer
            return timer

    def cancel(self, identifier: object) -> Timer:
        """Cancel by label or by number and return the cancelled timer.

        Raises ``UnknownTimerError`` if nothing matches and
        ``AmbiguousTimerError`` if a label matches several timers — a silent
        no-op would leave the user believing a timer had been stopped.
        """
        with self._lock:
            timer = self._find(identifier)
            del self._timers[timer.number]
            return timer

    def list_active(self, now: Optional[float] = None) -> List[Timer]:
        """Active timers, soonest deadline first.

        Ties break on the auto-number so the order is deterministic: two timers
        booked for the same second must always be listed (and therefore spoken)
        in the same order.
        """
        with self._lock:
            return sorted(self._timers.values(), key=lambda t: (t.deadline, t.number))

    def fire_due(self, now: Optional[float] = None) -> List[Timer]:
        """Remove and return every timer that has elapsed by ``now``, each once.

        Removal happens under the same lock that selects the timers, so a timer
        can never be handed out twice: a second call cannot re-fire it, and two
        pollers racing cannot announce the same alarm twice. Timers that came
        due while nothing was polling are still waiting here, so they fire once
        on the next poll rather than being skipped.
        """
        with self._lock:
            moment = self._now() if now is None else _validate_now(now)
            due = sorted(
                (t for t in self._timers.values() if t.is_due(moment)),
                key=lambda t: (t.deadline, t.number),
            )
            for timer in due:
                del self._timers[timer.number]
            return due

    def remaining(
        self,
        identifier: object = None,
        now: Optional[float] = None,
    ) -> Optional[float]:
        """Seconds left, or ``None`` when there is no such timer.

        With an ``identifier`` (label or number) this is that timer's remaining
        time. Without one it is the soonest timer's remaining time — the "how
        long is left?" answer — and ``None`` when no timer is running.
        """
        moment = self._now() if now is None else _validate_now(now)
        with self._lock:
            if identifier is None:
                if not self._timers:
                    return None
                soonest = min(self._timers.values(), key=lambda t: (t.deadline, t.number))
                return soonest.remaining_seconds(moment)
            return self._find(identifier).remaining_seconds(moment)

    def format_spoken(self, identifier: object = None, now: Optional[float] = None) -> str:
        """A sentence for TTS, e.g. ``'timer 1 has 4 minutes left'``."""
        moment = self._now() if now is None else _validate_now(now)
        with self._lock:
            if identifier is None:
                if not self._timers:
                    return "no timers are running"
                timer = min(self._timers.values(), key=lambda t: (t.deadline, t.number))
            else:
                timer = self._find(identifier)
            return f"{timer.label} has {_humanise(timer.remaining_seconds(moment))} left"

    def active_count(self) -> int:
        with self._lock:
            return len(self._timers)


# --------------------------------------------------------------------------
# The process-wide shared manager
# --------------------------------------------------------------------------

#: The one manager the process shares. The ``timer`` tool starts and cancels
#: timers here; the announcer loop polls the same object for due ones. Two
#: independent managers would look like a working feature while behaving like a
#: broken one: the owner sets a timer that the loop never sees, and it never
#: fires.
_default_manager: Optional[TimerManager] = None
_default_manager_lock = threading.Lock()


def default_manager() -> TimerManager:
    """The process-wide :class:`TimerManager`, created on first use.

    The daemon's timer tool and the timer announcer must both call this so they
    share one set of timers; a manager built per call site would leave the tool
    writing to a set the loop never polls.

    Tests must call :func:`set_default_manager` (with ``None`` to forget it) in
    setup and teardown, or a timer left behind by one test leaks into the next
    and the two stop being independent.
    """
    global _default_manager
    with _default_manager_lock:
        if _default_manager is None:
            _default_manager = TimerManager()
        return _default_manager


def set_default_manager(manager: Optional[TimerManager]) -> None:
    """Replace the process-wide manager (``None`` forgets it).

    The daemon uses this to install the manager it wired into the tool and the
    announcer; tests use it to inject a manager on a fake clock and to restore
    the shared default afterwards. Passing ``None`` does not create anything —
    the next :func:`default_manager` call makes a fresh empty one, which is the
    "start from a clean slate" the tests need.
    """
    global _default_manager
    with _default_manager_lock:
        _default_manager = manager
