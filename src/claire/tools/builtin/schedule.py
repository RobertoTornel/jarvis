"""Local-first calendar tool (R-SCHED-1) — the ``schedule`` builtin.

The tool exposes the pure calendar logic in :mod:`claire.schedule.calendar`
(backed by :mod:`claire.schedule.store`) to the reply loop as a single
callable with five actions: ``add``, ``list``, ``move``, ``cancel`` and
``due`` (reminders due now).

Design rules this file is built around — and the bugs that produced them
-----------------------------------------------------------------------
* **Overlap is half-open.** ``rangesOverlap(aStart, aEnd, bStart, bEnd)`` is
  ``aStart < bEnd and bStart < aEnd``. Touching is *not* a conflict: an event
  ending 15:00 and one starting 15:00 do not collide. The reference project
  originally compared with ``<=`` and refused perfectly good bookings that
  happened to butt up against the previous one — a bug users experience as
  "Claire says my calendar is full when it visibly is not". This tool never
  re-derives overlap itself; it asks ``calendar.propose_alternatives`` for the
  ranked free slots and treats *the first proposal* as the answer, so the
  half-open rule is decided in exactly one place.
* **A move must exclude the event being moved from its own conflict check**,
  or the event collides with itself at its current time and every correct move
  is refused. ``_action_move`` therefore passes ``exclude=<the matched event>``
  into the proposal scan.
* **The current occurrence is excluded from the busy set when scanning for
  alternatives**, for the same reason: otherwise a recurring event blocks its
  own next slot and the scan proposes nothing.
* **"The requested time is free" and "here is the nearest alternative" are ONE
  code path.** The scan *starts at the requested time*, so a free requested
  time simply comes back as the first proposal. The tool checks
  ``proposals[0] == requested slot``: equal means free (write), different means
  taken (refuse and offer the proposals). There is no second, separate
  availability query that could disagree with the proposal list.
* **Proposals never leave the allowed window** (default 08:00–21:00) and never
  run past the horizon (default 365 days). A scan that only checks the START
  would cheerfully propose a 60-minute slot at 20:45 that overruns the 21:00
  window end; the whole span must be checked.
* **A gap must be long enough for the WHOLE event.** A 30-minute gap cannot
  host a 90-minute booking, so candidate gaps are filtered by
  ``gap >= duration``, never by "the start fits".
* **ON CONFLICT the event is NOT written** and the result carries the ranked
  free alternatives instead. Refusal is atomic and always comes with
  alternatives — never a partial write, never a silent overwrite.
* **Defaults:** event duration 60 minutes, proposal granularity 15 minutes,
  horizon 365 days, window 08:00–21:00.
* **Recurrence** is daily/weekly with an ``until`` date, plus per-date
  cancellations of one occurrence of a recurring event.
* **Times are local wall-clock minutes from midnight** and **dates are
  ``YYYY-MM-DD`` strings**, so the logic is deterministic and testable with no
  timezone library anywhere in the path.

Date/time phrasing is resolved here, deterministically — the LLM is never
asked to do date arithmetic. ``resolve_when`` accepts ``today``, ``tomorrow``,
``thursday``, ``next thursday``, ``next week``, ``2026-09-24``, ``+3``,
``24 September`` and phrases that carry a time such as ``tomorrow at 4pm``;
``parse_time_of_day`` accepts ``16:00``, ``4pm``, ``4:30pm``, ``9``, ``noon``
and ``midnight``.

Missing or invalid input raises :class:`ScheduleToolError` with a message that
explains itself; it is never silently defaulted. ``run`` converts that named
error into ``ToolExecutionResult(success=False, error_message=...)`` — the
engine hands that text back to the model as a tool error, so the model can ask
the user a sensible question instead of inventing an answer.

The calendar modules are imported lazily, inside :func:`build_default_backend`,
so this tool can be imported (and unit-tested against an injected fake store)
whether or not those modules exist yet. The store is treated as authoritative:
a missing calendar file means "empty calendar", not an error — a first booking
must never be answered with a filesystem complaint. The delivered calendar
speaks a schedule *document* (``{"events": [...], "fired_reminders": [...]}``)
and canonical event dicts, so the container is kept opaque by the tool and only
``_ModuleBackend``/``_unwrap_events`` translate.
"""

from __future__ import annotations

import importlib
import inspect
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

__all__ = [
    "ScheduleTool",
    "ScheduleToolError",
    "FreeSlot",
    "resolve_when",
    "parse_date_text",
    "parse_time_of_day",
    "format_spoken",
    "format_clock",
    "build_default_backend",
    "DEFAULT_DURATION_MINUTES",
    "DEFAULT_GRANULARITY_MINUTES",
    "DEFAULT_HORIZON_DAYS",
    "DEFAULT_PROPOSAL_DAYS",
    "DEFAULT_WINDOW_START_MINUTES",
    "DEFAULT_WINDOW_END_MINUTES",
]

# --- Defaults -----------------------------------------------------------------
# Event duration when the caller does not say; the calendar's own default is the
# same, but the tool states it out loud so the spoken answer can mention it.
DEFAULT_DURATION_MINUTES = 60
# Proposals are offered on a 15-minute grid (08:00, 08:15, 08:30, ...). A finer
# grid produces proposals nobody wants ("16:07"); a coarser one skips real gaps.
DEFAULT_GRANULARITY_MINUTES = 15
# Never propose further than a year out: a "nearest alternative" 400 days away
# is not an alternative, it is a different plan.
DEFAULT_HORIZON_DAYS = 365
# The waking-day window. Proposals stay inside it, and so do explicit bookings —
# Claire should not quietly accept a 03:00 meeting just because it was asked.
DEFAULT_WINDOW_START_MINUTES = 8 * 60
DEFAULT_WINDOW_END_MINUTES = 21 * 60
# How many days "list my calendar" shows when the user does not say.
DEFAULT_LIST_DAYS = 7
# How far ahead the conflict scan looks for an alternative. The calendar caps
# this by the horizon; a week is where a different day stops being "the nearest
# alternative" and becomes a different plan.
DEFAULT_PROPOSAL_DAYS = 7
# How many alternatives are spoken aloud. More than this is not a choice.
MAX_SPOKEN_ALTERNATIVES = 3

MINUTES_PER_DAY = 24 * 60


class ScheduleToolError(Exception):
    """Named error for every schedule failure that the caller can fix.

    Raised for missing/invalid input (no time given, an unparseable date, a
    duration that is not a positive integer), for out-of-range requests
    (outside the daily window, in the past, past the horizon) and for a backend
    that cannot be resolved. Each message explains the problem and usually the
    accepted form, because ``run`` passes it to the model as tool-error text.
    """


# --- Calendar-facing data types ----------------------------------------------

@dataclass(frozen=True)
class FreeSlot:
    """One ranked free slot as the tool understands it.

    ``date`` is a ``YYYY-MM-DD`` string and ``start`` is minutes from local
    midnight, mirroring the calendar's own representations so nothing has to be
    converted on the way in or out.
    """

    date: str
    start: int
    duration_minutes: int = DEFAULT_DURATION_MINUTES

    @property
    def end(self) -> int:
        return self.start + self.duration_minutes

    def spoken(self, include_duration: bool = False) -> str:
        return format_spoken(
            self.date,
            self.start,
            self.duration_minutes if include_duration else None,
        )


@dataclass(frozen=True)
class _EventView:
    """Normalized read-only view over whatever the backend calls an event."""

    date: Optional[str]
    start: Optional[int]
    duration_minutes: int
    title: str
    event_id: Optional[str] = None
    repeat: Optional[str] = None
    until: Optional[str] = None


# --- Field access over dict-shaped or attribute-shaped objects ----------------
# The calendar may hand back dataclasses; a JSON store, or a test fake, may hand
# back dicts. Both are read through these helpers so the tool does not care.
_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "date": ("date", "day", "isodate", "start_date"),
    "start": ("start", "start_minutes", "start_minute", "start_time_minutes"),
    "duration_minutes": ("duration_minutes", "duration", "length_minutes"),
    "title": ("title", "summary", "name", "text", "message"),
    "event_id": ("event_id", "id", "uid", "key"),
    "repeat": ("repeat", "recurrence", "frequency"),
    "until": ("until", "repeat_until", "recurrence_until", "end_date"),
}


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` (with its known aliases) off a dict-like or plain object."""
    for candidate in _FIELD_ALIASES.get(name, (name,)):
        if isinstance(obj, dict):
            if candidate in obj and obj[candidate] is not None:
                return obj[candidate]
        else:
            value = getattr(obj, candidate, None)
            if value is not None:
                return value
    return default


def _as_int(value: Any) -> Optional[int]:
    """Best-effort integer coercion for backend values; ``None`` if hopeless."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def _as_date_string(value: Any) -> Optional[str]:
    """Normalize a date-ish value to ``YYYY-MM-DD``; ``None`` if hopeless."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        # Tolerate a datetime string; the calendar deals in whole days.
        head = text.split("T", 1)[0].split(" ", 1)[0]
        try:
            return date.fromisoformat(head).isoformat()
        except ValueError:
            return None
    return None


def _event_view(raw: Any) -> _EventView:
    """Normalize one backend event into :class:`_EventView`."""
    duration = _as_int(_get_field(raw, "duration_minutes"))
    return _EventView(
        date=_as_date_string(_get_field(raw, "date")),
        start=_as_int(_get_field(raw, "start")),
        duration_minutes=duration if duration and duration > 0 else DEFAULT_DURATION_MINUTES,
        title=str(_get_field(raw, "title", "") or "").strip(),
        event_id=(
            None
            if _get_field(raw, "event_id") is None
            else str(_get_field(raw, "event_id"))
        ),
        repeat=_get_field(raw, "repeat"),
        until=_as_date_string(_get_field(raw, "until")),
    )


# --- Time-of-day parsing ------------------------------------------------------

_CLOCK_24H = re.compile(r"^(\d{1,2}):(\d{2})$")
_CLOCK_AMPM = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$")
_CLOCK_BARE = re.compile(r"^(\d{1,2})$")
_WORD_TIMES = {
    "noon": 12 * 60,
    "midday": 12 * 60,
    "midnight": 0,
}


def format_clock(minutes: int) -> str:
    """Render minutes-from-midnight as ``HH:MM`` (24-hour, unambiguous in TTS)."""
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ScheduleToolError(
            f"'{minutes}' is not a time of day: expected minutes from midnight as an integer."
        )
    if not 0 <= minutes < MINUTES_PER_DAY:
        raise ScheduleToolError(
            f"'{minutes}' is not a valid time of day: expected 0-{MINUTES_PER_DAY - 1} "
            "minutes from midnight."
        )
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_time_of_day(text: Any) -> int:
    """Resolve a spoken clock time to minutes from midnight.

    Accepted: ``16:00`` (24-hour), ``4pm``/``4:30 pm``/``9am`` (12-hour),
    ``9`` (a bare hour is read as the 24-hour clock, so 9 means 09:00 — say
    ``9pm`` for the evening), ``noon`` and ``midnight``.

    Anything else raises :class:`ScheduleToolError`: guessing a time from an
    unclear phrase is exactly the silent default that books a meeting at the
    wrong hour.
    """
    if isinstance(text, int) and not isinstance(text, bool):
        # An already-resolved minute count (e.g. straight from the calendar):
        # format_clock validates the range and raises the named error otherwise.
        format_clock(text)
        return text
    if not isinstance(text, str):
        raise ScheduleToolError(
            f"I need a time of day, not {type(text).__name__} "
            "(try '16:00', '4pm', '9', 'noon' or 'midnight')."
        )
    text = text.strip().lower().replace(".", ":")
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise ScheduleToolError(
            "I need a time of day for that (try '16:00', '4pm', '9', 'noon' or 'midnight')."
        )
    if text in _WORD_TIMES:
        return _WORD_TIMES[text]
    if text.startswith("at "):
        return parse_time_of_day(text[3:])

    match = _CLOCK_24H.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ScheduleToolError(
                f"'{text}' is not a valid clock time: hours run 00-23 and minutes 00-59."
            )
        return hour * 60 + minute

    match = _CLOCK_AMPM.match(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if not 1 <= hour <= 12:
            raise ScheduleToolError(
                f"'{text}' is not a valid 12-hour clock time: the hour must be 1-12."
            )
        if minute > 59:
            raise ScheduleToolError(f"'{text}' is not a valid clock time: minutes run 00-59.")
        # 12am is midnight and 12pm is noon — the one place 12-hour arithmetic
        # trips people up, so it is spelled out rather than left to modulo.
        hour = 0 if hour == 12 else hour
        if match.group(3) == "pm":
            hour += 12
        return hour * 60 + minute

    match = _CLOCK_BARE.match(text)
    if match:
        hour = int(match.group(1))
        if hour > 23:
            raise ScheduleToolError(
                f"'{text}' is not a valid hour: use 0-23, or say '4pm' for the afternoon."
            )
        return hour * 60

    raise ScheduleToolError(
        f"I couldn't read '{text}' as a time. Try '16:00', '4pm', '9', 'noon' or 'midnight'."
    )


# --- Date parsing -------------------------------------------------------------

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_WEEKDAYS = {
    name.lower(): index for index, name in enumerate(_WEEKDAY_NAMES)
}
_WEEKDAYS.update({name[:3].lower(): index for index, name in enumerate(_WEEKDAY_NAMES)})
_WEEKDAYS.update({"tues": 1, "thur": 3, "thurs": 3, "weds": 2})

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTHS = {name.lower(): index for index, name in enumerate(_MONTH_NAMES, start=1)}
_MONTHS.update({name[:3].lower(): index for index, name in enumerate(_MONTH_NAMES, start=1)})
_MONTHS.update({"sept": 9})

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_DATE = re.compile(r"^\+\s*(\d{1,4})$")
_DAY_MONTH = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)$")
_MONTH_DAY = re.compile(r"^([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?$")
_NEXT_WEEKDAY = re.compile(r"^next\s+([a-z]+)$")


def _weekday_after(today: date, target: int, including_today: bool = False) -> date:
    """The next ``target`` weekday relative to ``today``.

    ``thursday`` said on a Wednesday means tomorrow; said *on* a Thursday it
    means the Thursday a week later — the same weekday name must never mean
    "today" here, because a booking silently landing on the current day is the
    kind of surprise that makes a calendar untrustworthy.
    """
    offset = (target - today.weekday()) % 7
    if offset == 0 and not including_today:
        offset = 7
    return today + timedelta(days=offset)


def _month_name_date(text: str, today: date) -> Optional[date]:
    """Resolve ``24 September`` / ``September 24`` to a concrete day.

    A month/day with no year is this year when it is still ahead, otherwise
    next year: "24 September" said in October means the next one, not a date in
    the past. February 29 in a non-leap year raises rather than rolling to
    March 1 — quietly changing the day would be a silent default.
    """
    match = _DAY_MONTH.match(text)
    if match:
        day, month_token = int(match.group(1)), match.group(2)
    else:
        match = _MONTH_DAY.match(text)
        if not match:
            return None
        month_token, day = match.group(1), int(match.group(2))
    if month_token not in _MONTHS:
        return None
    month = _MONTHS[month_token]
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        raise ScheduleToolError(
            f"'{text}' is not a real calendar date in {today.year}."
        ) from None
    if candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            raise ScheduleToolError(
                f"'{text}' only exists in a leap year; please give the year explicitly."
            ) from None
    return candidate


def parse_date_text(text: Any, today: Optional[date] = None) -> date:
    """Resolve a date phrase to a :class:`datetime.date`.

    Accepted: ``today``, ``tomorrow``, a weekday name (``thursday``, ``thu``),
    ``next thursday`` (the one in the following week), ``next week`` (today + 7),
    ``2026-09-24``, ``+3`` (three days from now), ``24 September`` and
    ``September 24``. Anything else raises :class:`ScheduleToolError`.
    """
    if today is None:
        today = date.today()
    if not isinstance(text, str):
        raise ScheduleToolError(
            f"I need a date, not {type(text).__name__} "
            "(try 'today', 'tomorrow', 'thursday', 'next week', '2026-09-24' or '+3')."
        )
    token = re.sub(r"\s+", " ", text.strip().lower())
    if not token:
        raise ScheduleToolError(
            "I need a date for that (try 'today', 'tomorrow', 'thursday', "
            "'next week', '2026-09-24' or '+3')."
        )
    if token in ("today", "tonight", "now"):
        return today
    if token in ("tomorrow", "tmrw", "tmw", "tom"):
        return today + timedelta(days=1)
    if token in ("next week", "nextweek"):
        return today + timedelta(days=7)
    if token in _WEEKDAYS:
        return _weekday_after(today, _WEEKDAYS[token])
    next_match = _NEXT_WEEKDAY.match(token)
    if next_match and next_match.group(1) in _WEEKDAYS:
        # "next thursday" is deliberately the week AFTER the nearest one, so the
        # two phrasings stay distinguishable instead of collapsing together.
        return _weekday_after(today, _WEEKDAYS[next_match.group(1)]) + timedelta(days=7)
    offset_match = _OFFSET_DATE.match(token)
    if offset_match:
        return today + timedelta(days=int(offset_match.group(1)))
    if _ISO_DATE.match(token):
        try:
            return date.fromisoformat(token)
        except ValueError:
            raise ScheduleToolError(
                f"'{text}' is not a real date; use YYYY-MM-DD (for example 2026-09-24)."
            ) from None
    month_day = _month_name_date(token, today)
    if month_day is not None:
        return month_day
    raise ScheduleToolError(
        f"I couldn't understand the date '{text}'. Try 'today', 'tomorrow', "
        "'thursday', 'next week', '2026-09-24' or '+3'."
    )


def resolve_when(
    text: Any,
    today: Optional[date] = None,
) -> Tuple[date, Optional[int]]:
    """Resolve a when-phrase to ``(date, start_minutes or None)``.

    One call handles both "which day" and "and also what time", because users
    say "tomorrow at 4pm" as a single phrase: a leading ``at`` means today at
    that time, an embedded `` at `` splits date from time, and a trailing clock
    ("thursday 16:00") is peeled off the end. A phrase that carries no time
    returns ``None`` for the time rather than inventing one.
    """
    if today is None:
        today = date.today()
    if not isinstance(text, str):
        raise ScheduleToolError(
            f"I need a date or a when-phrase, not {type(text).__name__}."
        )
    token = re.sub(r"\s+", " ", text.strip())
    if not token:
        raise ScheduleToolError(
            "I need a date for that (try 'today', 'tomorrow', 'thursday', "
            "'next week', '2026-09-24' or '+3')."
        )
    lowered = token.lower()
    if lowered.startswith("at "):
        return today, parse_time_of_day(lowered[3:])
    if " at " in lowered:
        day_part, _, time_part = lowered.partition(" at ")
        return parse_date_text(day_part, today), parse_time_of_day(time_part)
    if lowered.startswith("in the ") and lowered.endswith("s"):
        # "in the morning/afternoon/evening" — the day is today and the time is
        # a convention, stated here so it is visible rather than implied.
        part = lowered[len("in the "):-1]
        if part in ("morning", "afternoon", "evening"):
            return today, {"morning": 9 * 60, "afternoon": 14 * 60, "evening": 19 * 60}[part]
    try:
        return parse_date_text(lowered, today), None
    except ScheduleToolError as date_error:
        time_only = None
        try:
            time_only = parse_time_of_day(lowered)
        except ScheduleToolError:
            time_only = None
        if time_only is not None:
            return today, time_only
        # "thursday 16:00" / "tomorrow 4pm": date and clock in one breath.
        trailing = re.match(r"^(.*?)[\s,]+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)$", lowered)
        if trailing:
            head, tail = trailing.group(1).strip(), trailing.group(2).strip()
            try:
                return parse_date_text(head, today), parse_time_of_day(tail)
            except ScheduleToolError:
                pass
        raise date_error


# --- Spoken formatting --------------------------------------------------------

def _spoken_duration(minutes: int) -> str:
    """Describe a length of time the way a person would say it."""
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    hours, rest = divmod(minutes, 60)
    if hours == 0:
        return f"{rest} minutes"
    hour_text = "1 hour" if hours == 1 else f"{hours} hours"
    return f"{hour_text} {rest} minutes"


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    normalized = _as_date_string(value)
    if normalized is None:
        raise ScheduleToolError(
            f"'{value}' is not a date the calendar can speak: expected YYYY-MM-DD."
        )
    return date.fromisoformat(normalized)


def format_spoken(
    when: Any,
    start_minutes: Optional[int] = None,
    duration_minutes: Optional[int] = None,
) -> str:
    """Format a date (and optionally a time and length) for text-to-speech.

    ``format_spoken("2026-09-24", 16 * 60)`` is
    ``"Thursday 24 September at 16:00"`` — weekday first because that is what a
    person plans around, and a 24-hour clock because "4pm" read out by a voice
    is unambiguous while "at 4" is not.
    """
    day = _coerce_date(when)
    text = f"{_WEEKDAY_NAMES[day.weekday()]} {day.day} {_MONTH_NAMES[day.month - 1]}"
    if start_minutes is not None:
        text += f" at {format_clock(start_minutes)}"
    if duration_minutes is not None:
        text += f" for {_spoken_duration(duration_minutes)}"
    return text


# --- Backend seam -------------------------------------------------------------

def _invoke(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Call ``fn`` with the keyword arguments its signature actually accepts.

    The calendar and store modules are written in parallel with this file, so
    the adapter passes every known name for a value (``duration_minutes`` and
    ``duration``, ``events`` and ``schedule``) and lets the signature decide.
    Best-effort by design: an unresolvable call raises :class:`ScheduleToolError`
    naming the function, rather than silently doing nothing.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        try:
            return fn(**kwargs)
        except TypeError as exc:
            raise ScheduleToolError(f"Could not call {getattr(fn, '__name__', fn)}: {exc}") from exc
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs:
        return fn(**kwargs)
    call_kwargs = {name: value for name, value in kwargs.items() if name in signature.parameters}
    required = [
        name
        for name, parameter in signature.parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in call_kwargs
    ]
    if required:
        raise ScheduleToolError(
            f"Could not call {getattr(fn, '__name__', fn)}: it needs {', '.join(required)}, "
            "which the schedule tool has no value for."
        )
    return fn(**call_kwargs)


def _entry_points(module: Any, *candidates: str) -> Callable[..., Any]:
    """First present callable among ``candidates`` on a lazily-imported module."""
    for name in candidates:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    raise ScheduleToolError(
        f"The calendar module does not provide any of {', '.join(candidates)}; "
        "the schedule tool cannot continue."
    )


def _unwrap_events(container: Any) -> List[Any]:
    """The event list inside whatever container the backend deals in.

    The real backend hands back a schedule *document*
    (``{"events": [...], "fired_reminders": [...]}``) so the calendar can expand
    recurrence and skip per-date cancellations; a test fake may hand back the
    list itself. Everything that reads events goes through here, so nothing else
    has to care which shape is in play.
    """
    if isinstance(container, dict):
        for key in ("events", "items", "occurrences"):
            value = container.get(key)
            if isinstance(value, list):
                return value
        return []
    return list(container or [])


def _field_id(event: Any) -> Optional[str]:
    """The event's id as a string, or ``None`` when it has none yet."""
    if event is None:
        return None
    value = _get_field(event, "event_id")
    return None if value is None else str(value)


def _resolve_event_id(schedule: Any, ref: Any, event: Any = None) -> str:
    """Turn an event reference (an id or a title) into the id the calendar wants.

    The tool matches events for speech, but ``move_event``/``cancel_event`` take
    an id; titles are what users say, so both are accepted here. An
    unresolvable reference is a named error rather than a silent no-op.
    """
    direct = _field_id(event)
    if direct:
        return direct
    wanted = str(ref).strip()
    events = _unwrap_events(schedule)
    for raw in events:
        raw_id = _field_id(raw)
        if raw_id is not None and raw_id == wanted:
            return raw_id
    for raw in events:
        title = _get_field(raw, "title")
        if isinstance(title, str) and title.strip().casefold() == wanted.casefold():
            raw_id = _field_id(raw)
            if raw_id is not None:
                return raw_id
    raise ScheduleToolError(f"I couldn't find an event called '{wanted}' in the calendar.")


class _ModuleBackend:
    """Adapter from the tool's backend protocol to the real calendar + store.

    Deliberately thin: it owns no scheduling policy — no overlap test, no gap
    scan, no ranking, no conflict decision — because all of that lives in
    ``claire.schedule.calendar`` and must exist in exactly one place. Everything
    here is plumbing: the tool speaks ``(date, start, duration)`` plus an event
    reference, the calendar speaks canonical event dicts and a schedule
    document, and this class translates between the two.
    """

    def __init__(self, calendar_module: Any, store_module: Any):
        self._calendar = calendar_module
        self._store = store_module
        # How far ahead the alternative scan looks. The calendar caps this by the
        # horizon itself; this is only "how far am I willing to move this
        # meeting", and a week is the point where a different day is a different
        # plan rather than an alternative.
        self._proposal_days = DEFAULT_PROPOSAL_DAYS

    # -- persistence ---------------------------------------------------------
    def load(self) -> Any:
        """Load the schedule; a missing calendar file means "nothing booked".

        ``load_schedule`` already treats a missing file as an empty calendar
        (``existed is False``); ``FileNotFoundError`` and ``None`` are tolerated
        too, so a first-ever booking is never answered with a filesystem
        complaint.
        """
        load = _entry_points(self._store, "load_schedule", "load")
        try:
            schedule = _invoke(load)
        except FileNotFoundError:
            schedule = None
        if schedule is None:
            empty = getattr(self._store, "empty_schedule", None)
            if callable(empty):
                return _invoke(empty)
            return {"events": [], "fired_reminders": []}
        return schedule

    def save(self, schedule: Any) -> None:
        save = getattr(self._store, "save_schedule", None)
        if not callable(save):
            raise ScheduleToolError(
                "The calendar store provides no way to save a schedule "
                "(expected save_schedule)."
            )
        _invoke(save, schedule=schedule, events=schedule)

    # -- pure logic ----------------------------------------------------------
    def add(
        self,
        schedule: Any,
        *,
        title: str,
        date: str,
        start: int,
        duration_minutes: int,
        repeat: Optional[str] = None,
        until: Optional[str] = None,
        reminder_minutes: Optional[int] = None,
    ) -> Any:
        """Build a canonical event and let the calendar apply its own conflict rule."""
        raw: Dict[str, Any] = {
            "title": title,
            "date": date,
            "start": start,
            "duration": duration_minutes,
        }
        if repeat is not None:
            raw["recurrence"] = {"freq": repeat, "until": until} if until else {"freq": repeat}
        if reminder_minutes is not None:
            raw["reminder_minutes"] = reminder_minutes
        fn = _entry_points(self._calendar, "add_event")
        return _invoke(fn, schedule=schedule, raw=raw, event=raw, days=self._proposal_days)

    def move(
        self,
        schedule: Any,
        *,
        event_ref: str,
        date: str,
        start: int,
        duration_minutes: int,
        event: Any = None,
    ) -> Any:
        fn = _entry_points(self._calendar, "move_event")
        event_id = _resolve_event_id(schedule, event_ref, event)
        return _invoke(
            fn,
            schedule=schedule,
            event_id=event_id,
            new_date=date,
            new_start=start,
            new_duration=duration_minutes,
            days=self._proposal_days,
        )

    def cancel(
        self,
        schedule: Any,
        *,
        event_ref: str,
        date: Optional[str] = None,
        event: Any = None,
    ) -> Any:
        fn = _entry_points(self._calendar, "cancel_event")
        event_id = _resolve_event_id(schedule, event_ref, event)
        return _invoke(fn, schedule=schedule, event_id=event_id, date=date)

    def propose(self, schedule: Any, **fields: Any) -> List[FreeSlot]:
        """Ranked free slots, straight from the calendar's own scan.

        ``exclude`` names the event being moved: its id is passed through as
        ``exclude_id`` so the event cannot block its own slot, and the scan — not
        this adapter — decides what counts as a conflict.
        """
        duration = fields.get("duration_minutes") or DEFAULT_DURATION_MINUTES
        exclude = fields.get("exclude")
        exclude_id = _field_id(exclude)
        candidate: Dict[str, Any] = {
            # The calendar normalizes a candidate event and insists on a title;
            # only date/start/duration (and the excluded id) affect the scan, so
            # a moving event reuses its own title and a new one gets a placeholder.
            "title": _get_field(exclude, "title") or fields.get("title") or "New event",
            "date": fields.get("date"),
            "start": fields.get("start"),
            "duration": duration,
        }
        if exclude_id is not None:
            candidate["id"] = exclude_id
        fn = _entry_points(self._calendar, "propose_alternatives")
        raw = _invoke(
            fn,
            schedule=schedule,
            schedule_or_instances=schedule,
            event=candidate,
            days=fields.get("days") or self._proposal_days,
            window=(fields.get("window_start"), fields.get("window_end")),
            grain=fields.get("granularity_minutes"),
            exclude_id=exclude_id,
            horizon=fields.get("horizon_days"),
            limit=fields.get("limit"),
        )
        return _normalize_proposals(raw, duration)

    def list_events(self, schedule: Any, **fields: Any) -> List[Any]:
        """Occurrences with recurrence expanded and cancelled dates skipped."""
        fn = getattr(self._calendar, "list_events", None)
        if not callable(fn):
            return _unwrap_events(schedule)
        start_date = fields.get("start_date")
        days = fields.get("days")
        to_date = None
        if start_date is not None and days:
            day = _coerce_date(start_date)
            to_date = (day + timedelta(days=int(days) - 1)).isoformat()
        result = _invoke(fn, schedule=schedule, from_date=start_date, to_date=to_date)
        if isinstance(result, dict):
            return list(result.get("occurrences") or [])
        return list(result or [])

    def due(self, schedule: Any, **fields: Any) -> List[Any]:
        fn = _entry_points(self._calendar, "due_reminders")
        return list(_invoke(fn, schedule=schedule, now=fields.get("now")) or [])

    def mark_fired(self, schedule: Any, reminders: Sequence[Any]) -> int:
        """Record reported reminders so they are spoken only once.

        A reminder repeated on every turn is noise, so the tool commits the keys
        as it speaks them; the calendar owns the bookkeeping. Returns how many
        were marked — 0 when the calendar has no such facility, because
        reporting reminders must still work without it.
        """
        mark = getattr(self._calendar, "mark_reminder_fired", None)
        if not callable(mark):
            return 0
        marked = 0
        for reminder in reminders:
            key = _get_field(reminder, "key")
            if isinstance(key, str) and key.strip():
                _invoke(mark, schedule=schedule, key=key)
                marked += 1
        return marked


def _normalize_proposals(raw: Any, duration: Any = None) -> List[FreeSlot]:
    """Normalize a proposal list from the calendar into ranked :class:`FreeSlot` s.

    Accepts dataclass-like objects, dicts, ``(date, start)`` tuples and
    ``"YYYY-MM-DD HH:MM"`` strings so the scan's return shape is not a hidden
    coupling. The list is sorted and de-duplicated: "ranked" must mean
    soonest-first no matter what order the scan produced.
    """
    if raw is None:
        return []
    items: Iterable[Any]
    if isinstance(raw, dict):
        items = raw.get("proposals") or raw.get("alternatives") or raw.get("slots") or []
    else:
        items = raw if isinstance(raw, (list, tuple)) else [raw]
    default_duration = _as_int(duration) or DEFAULT_DURATION_MINUTES
    slots: List[FreeSlot] = []
    for item in items:
        slot: Optional[FreeSlot] = None
        if isinstance(item, FreeSlot):
            slot = item
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            day = _as_date_string(item[0])
            start = _as_int(item[1])
            if day is not None and start is not None:
                length = _as_int(item[2]) if len(item) > 2 else None
                slot = FreeSlot(day, start, length or default_duration)
        elif isinstance(item, str):
            match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})$", item.strip())
            if match:
                slot = FreeSlot(
                    match.group(1),
                    int(match.group(2)) * 60 + int(match.group(3)),
                    default_duration,
                )
        else:
            day = _as_date_string(_get_field(item, "date"))
            start = _as_int(_get_field(item, "start"))
            if day is not None and start is not None:
                length = _as_int(_get_field(item, "duration_minutes"))
                slot = FreeSlot(day, start, length or default_duration)
        if slot is None:
            raise ScheduleToolError(
                f"The calendar returned a free slot the tool cannot read: {item!r}. "
                "Expected a date and a start time."
            )
        if not 0 <= slot.start < MINUTES_PER_DAY:
            raise ScheduleToolError(
                f"The calendar proposed a start time of {slot.start} minutes, "
                "which is outside a single day."
            )
        slots.append(slot)
    unique = {(slot.date, slot.start): slot for slot in slots}
    return sorted(unique.values(), key=lambda slot: (slot.date, slot.start))


def build_default_backend() -> Any:
    """Lazily import the calendar + store modules and adapt them.

    Imported here rather than at module import time so this tool — and its unit
    tests — work whether or not those modules have landed yet, and so the
    tool's own import never depends on the store's file layout.

    The canonical package is ``claire.schedule`` (``src`` is on the path in this
    project), with ``src.claire.schedule`` accepted as a fallback, and this
    module's own prefix tried last so the tool never talks to a second copy of
    the calendar under a different prefix when the canonical one is importable.
    """
    derived = __name__.rsplit(".tools.builtin.schedule", 1)[0]
    prefixes = [p for p in ("claire", "src.claire", derived) if p]
    first_error: Optional[ImportError] = None
    for prefix in dict.fromkeys(prefixes):
        try:
            calendar_module = importlib.import_module(f"{prefix}.schedule.calendar")
            store_module = importlib.import_module(f"{prefix}.schedule.store")
        except ImportError as exc:
            if first_error is None:
                first_error = exc
            continue
        return _ModuleBackend(calendar_module, store_module)
    raise ScheduleToolError(
        "The local calendar module is not available "
        f"({first_error}); I can't read or change the schedule right now."
    )


def _named_backend_error_types() -> Tuple[type, ...]:
    """The calendar's and store's own named error bases, when importable."""
    found: List[type] = []
    derived = __name__.rsplit(".tools.builtin.schedule", 1)[0]
    for prefix in dict.fromkeys(p for p in ("claire", "src.claire", derived) if p):
        for module_name, attribute in (
            (f"{prefix}.schedule.calendar", "ScheduleError"),
            (f"{prefix}.schedule.store", "ScheduleStoreError"),
        ):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            base = getattr(module, attribute, None)
            if isinstance(base, type) and base not in found:
                found.append(base)
    return tuple(found)


def _is_named_backend_error(exc: BaseException) -> bool:
    """True when ``exc`` is one of the calendar's/store's named errors."""
    return isinstance(exc, _named_backend_error_types())


# --- The tool -----------------------------------------------------------------

class ScheduleTool(Tool):
    """Add, list, move and cancel local calendar events, and report reminders.

    All dates and times are resolved inside the tool (never by the model), all
    scheduling decisions are delegated to the injected backend, and a conflict
    is answered with ranked free alternatives instead of a write.
    """

    def __init__(
        self,
        backend: Any = None,
        today_provider: Optional[Callable[[], date]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        default_duration_minutes: int = DEFAULT_DURATION_MINUTES,
        granularity_minutes: int = DEFAULT_GRANULARITY_MINUTES,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        window_start_minutes: int = DEFAULT_WINDOW_START_MINUTES,
        window_end_minutes: int = DEFAULT_WINDOW_END_MINUTES,
        list_days: int = DEFAULT_LIST_DAYS,
        proposal_days: int = DEFAULT_PROPOSAL_DAYS,
    ):
        # The backend is resolved on first use, not here: the registry builds
        # every builtin at import time, and that must not touch the filesystem.
        self._backend = backend
        self._today_provider = today_provider or date.today
        self._now_provider = now_provider or datetime.now
        self._default_duration_minutes = default_duration_minutes
        self._granularity_minutes = granularity_minutes
        self._horizon_days = horizon_days
        self._window_start_minutes = window_start_minutes
        self._window_end_minutes = window_end_minutes
        self._list_days = list_days
        self._proposal_days = proposal_days

    # -- Tool interface ------------------------------------------------------
    @property
    def name(self) -> str:
        return "schedule"

    @property
    def description(self) -> str:
        return (
            "Claire's own local calendar. Use it to add an event, list what is "
            "coming up, move or cancel an event, or read reminders that are due "
            "now. Dates and times are given as plain phrases ('tomorrow at 4pm', "
            "'thursday', '2026-09-24', '+3'). If the requested time is already "
            "taken nothing is booked and the result lists the nearest free "
            "times, which you should offer to the user."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "move", "cancel", "due"],
                    "description": (
                        "'add' books an event, 'list' shows upcoming events, "
                        "'move' reschedules one, 'cancel' removes one, 'due' "
                        "reads the reminders that are due now."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Event title, for example 'Dentist'. Required for add.",
                },
                "when": {
                    "type": "string",
                    "description": (
                        "The day: 'today', 'tomorrow', 'thursday', 'next thursday', "
                        "'next week', '2026-09-24', '+3' (three days from now), or a "
                        "phrase carrying a time such as 'tomorrow at 4pm'. For list it "
                        "is the first day to show."
                    ),
                },
                "time": {
                    "type": "string",
                    "description": (
                        "Clock time: '16:00', '4pm' or '9'. Required for add and move "
                        "unless 'when' already contains a time."
                    ),
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": "How long the event lasts, in minutes. Default 60.",
                },
                "event_id": {
                    "type": "string",
                    "description": "Which event to move or cancel: its id from 'list', or its title.",
                },
                "date": {
                    "type": "string",
                    "description": (
                        "For cancel: the single 'YYYY-MM-DD' occurrence to remove from a "
                        "repeating event, leaving the rest of the series alone."
                    ),
                },
                "days": {
                    "type": "integer",
                    "description": "For list: how many days ahead to show. Default 7.",
                },
                "repeat": {
                    "type": "string",
                    "enum": ["daily", "weekly"],
                    "description": "Repeat the event daily or weekly.",
                },
                "until": {
                    "type": "string",
                    "description": "'YYYY-MM-DD' last date of a repeating event.",
                },
                "reminder_minutes": {
                    "type": "integer",
                    "description": (
                        "For add: minutes before the event to remind the user, spoken "
                        "by Claire when due. Set it for reminder requests ('remind "
                        "me...'); 0 means at the time the event starts. Leave it out "
                        "for a plain calendar entry with no reminder."
                    ),
                },
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        """Dispatch one action, converting named errors into tool-error results."""
        try:
            if not isinstance(args, dict):
                raise ScheduleToolError(
                    "schedule needs a JSON object with an 'action' "
                    "(one of add, list, move, cancel, due)."
                )
            action = str(args.get("action") or "").strip().lower()
            if not action:
                raise ScheduleToolError(
                    "schedule needs an 'action': one of add, list, move, cancel, due."
                )
            if action in ("add", "create", "book", "schedule"):
                return self._action_add(args, context)
            if action in ("list", "agenda", "upcoming", "whats_on"):
                return self._action_list(args)
            if action in ("move", "reschedule", "shift"):
                return self._action_move(args)
            if action in ("cancel", "delete", "remove", "drop"):
                return self._action_cancel(args)
            if action in ("due", "reminders", "reminders_due", "duereminders", "due_now"):
                return self._action_due(args)
            raise ScheduleToolError(
                f"'{action}' is not a schedule action. Use one of: add, list, move, cancel, due."
            )
        except ScheduleToolError as exc:
            # The engine feeds error_message to the model as tool-error text, so
            # the message is written as an explanation the model can act on.
            return ToolExecutionResult(success=False, reply_text=None, error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 - narrowed to named backend errors below
            # The calendar and the store raise their own NAMED errors
            # (ScheduleError, ScheduleStoreError and their subclasses: a damaged
            # file, an unknown event, a date that is not an occurrence, an
            # invalid window). They already explain themselves, so an arguably
            # correct refusal becomes a tool-error result for the model instead
            # of an exception escaping into the reply loop.
            if _is_named_backend_error(exc):
                return ToolExecutionResult(
                    success=False, reply_text=None, error_message=str(exc)
                )
            raise

    # -- Backend helpers -----------------------------------------------------
    def _get_backend(self) -> Any:
        if self._backend is None:
            self._backend = build_default_backend()
        return self._backend

    def _load_events(self) -> Any:
        """Load the backend's container: the real one is a schedule document.

        Kept opaque — a test fake may hand back a plain list, the delivered
        calendar/store hand back ``{"events": [...], ...}`` — because the tool
        never needs to know. Reading events goes through :func:`_unwrap_events`.
        """
        return self._get_backend().load()

    def _save_events(self, events: Any) -> None:
        self._get_backend().save(events)

    def _backend_list(self, events: Any, **fields: Any) -> List[Any]:
        list_fn = getattr(self._get_backend(), "list_events", None)
        if callable(list_fn):
            return list(list_fn(events, **fields) or [])
        return _unwrap_events(events)

    def _backend_propose(self, events: Any, **fields: Any) -> List[FreeSlot]:
        raw = self._get_backend().propose(events, **fields)
        return _normalize_proposals(raw, fields.get("duration_minutes"))

    def _backend_due(self, events: Any, now: datetime) -> List[Any]:
        return list(self._get_backend().due(events, now=now) or [])

    def _backend_mark_fired(self, events: Any, reminders: Sequence[Any]) -> int:
        mark = getattr(self._get_backend(), "mark_fired", None)
        if not callable(mark):
            return 0
        return int(mark(events, reminders) or 0)

    def _outcome_conflict(self, outcome: Any, day: date, start: int, duration: int) -> Optional[str]:
        """Turn a backend refusal into the conflict reply, or ``None`` if it wrote.

        The calendar is the authority on conflicts (its ``add_event``/``move_event``
        refuse atomically and return the proposals). The tool's own pre-check
        already decided this in the common case, so a refusal here means the
        backend disagreed — and then the backend's alternatives win.
        """
        if not isinstance(outcome, dict) or outcome.get("ok") is not False:
            return None
        proposals = _normalize_proposals(outcome.get("proposals"), duration)
        return self._conflict_reply(day, start, duration, proposals)

    # -- Argument helpers ----------------------------------------------------
    @staticmethod
    def _require_text(args: Dict[str, Any], key: str, action: str, instead: str = "") -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            hint = f" {instead}" if instead else ""
            raise ScheduleToolError(
                f"schedule {action} needs '{key}' — a value is missing.{hint}"
            )
        return value.strip()

    def _duration_arg(self, args: Dict[str, Any], fallback: Optional[int] = None) -> int:
        raw = args.get("duration_minutes")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if fallback is not None:
                return fallback
            return self._default_duration_minutes
        duration = _as_int(raw)
        if duration is None or duration <= 0:
            raise ScheduleToolError(
                f"'{raw}' is not a valid duration: give a positive number of minutes "
                "(for example 30 or 90)."
            )
        if duration > self._window_end_minutes - self._window_start_minutes:
            raise ScheduleToolError(
                f"A {duration}-minute event cannot fit in the "
                f"{format_clock(self._window_start_minutes)}-"
                f"{format_clock(self._window_end_minutes)} scheduling window."
            )
        return duration

    def _days_arg(self, args: Dict[str, Any]) -> int:
        raw = args.get("days")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return self._list_days
        days = _as_int(raw)
        if days is None or days <= 0:
            raise ScheduleToolError(
                f"'{raw}' is not a valid number of days: give a positive whole number."
            )
        return days

    @staticmethod
    def _reminder_arg(args: Dict[str, Any]) -> Optional[int]:
        """Optional minutes-before reminder; ``None`` means "no reminder"."""
        raw = args.get("reminder_minutes")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        minutes = _as_int(raw)
        if minutes is None or minutes < 0:
            raise ScheduleToolError(
                f"'{raw}' is not a valid reminder: give minutes before the event "
                "(for example 15), or leave it out for no reminder."
            )
        return minutes

    @staticmethod
    def _request_is_a_reminder(context: Optional[ToolContext]) -> bool:
        """Whether the owner's own words asked for a reminder.

        The utterance arrives on the tool context as ``original_prompt``
        (``redacted_text`` is the redacted fallback of the same text). Outside
        the voice path — evals, plain tool calls — neither carries a string,
        and there is no reminder phrasing to act on, so the answer is False.
        """
        for attr in ("original_prompt", "redacted_text"):
            value = getattr(context, attr, None)
            if isinstance(value, str) and re.search(r"\bremind", value, re.IGNORECASE):
                return True
        return False

    def _resolve_slot(
        self,
        args: Dict[str, Any],
        action: str,
        today: date,
        fallback_duration: Optional[int] = None,
    ) -> Tuple[date, int, int]:
        """Resolve the requested ``(day, start, duration)``, validating the window.

        Validation lives here so an impossible request fails with a clear named
        error BEFORE anything is written, and so the window rule is applied to
        explicit bookings exactly as it is to proposed alternatives.
        """
        when = args.get("when")
        start: Optional[int] = None
        day: Optional[date] = None
        if isinstance(when, str) and when.strip():
            day, start = resolve_when(when, today)
        time_arg = args.get("time")
        if isinstance(time_arg, str) and time_arg.strip():
            start = parse_time_of_day(time_arg)
        elif isinstance(time_arg, int) and not isinstance(time_arg, bool):
            # A bare number from a model is read the way a person reads it: an
            # hour when it can be one (0-23), minutes from midnight otherwise.
            start = time_arg * 60 if 0 <= time_arg <= 23 else parse_time_of_day(time_arg)
        elif time_arg is not None:
            raise ScheduleToolError(
                f"'{time_arg}' is not a time of day: give it as text such as "
                "'16:00', '4pm' or '9'."
            )
        if start is None:
            raise ScheduleToolError(
                f"schedule {action} needs a time — for example "
                "'{\"action\": \"add\", \"title\": \"Dentist\", \"when\": \"tomorrow\", "
                "\"time\": \"16:00\"}'. I won't guess a time and book the wrong slot."
            )
        if day is None:
            day = today
        duration = self._duration_arg(args, fallback_duration)
        self._validate_within_window(day, start, duration, today)
        return day, start, duration

    def _validate_within_window(self, day: date, start: int, duration: int, today: date) -> None:
        if day < today:
            raise ScheduleToolError(
                f"{format_spoken(day)} is in the past; I can only schedule from "
                f"{format_spoken(today)} onwards."
            )
        if (day - today).days > self._horizon_days:
            raise ScheduleToolError(
                f"{format_spoken(day)} is more than {self._horizon_days} days away, "
                "past the scheduling horizon."
            )
        if start < self._window_start_minutes or start + duration > self._window_end_minutes:
            raise ScheduleToolError(
                f"{format_spoken(day, start, duration)} falls outside the "
                f"{format_clock(self._window_start_minutes)}-"
                f"{format_clock(self._window_end_minutes)} scheduling window. "
                "Check the start AND the end of the event before booking it."
            )

    @staticmethod
    def _match_event(events: Any, ref: str) -> Tuple[Any, Optional[_EventView]]:
        """Find an event by id, then by exact title, then by title prefix.

        Reads through :func:`_unwrap_events`, so it works whether the backend
        handed back a schedule document or a plain list.
        """
        wanted = str(ref).strip()
        views = [(_event_view(raw), raw) for raw in _unwrap_events(events)]
        for view, raw in views:
            if view.event_id is not None and str(view.event_id) == wanted:
                return raw, view
        for view, raw in views:
            if view.title and view.title.casefold() == wanted.casefold():
                return raw, view
        for view, raw in views:
            if view.title and view.title.casefold().startswith(wanted.casefold()):
                return raw, view
        return None, None

    @staticmethod
    def _conflict_reply(
        day: date,
        start: int,
        duration: int,
        proposals: Sequence[FreeSlot],
    ) -> str:
        """Phrase a refusal that carries the ranked free alternatives.

        This is the point of the tool: "taken" without alternatives leaves the
        user to guess, and a partial write would be worse than either.
        """
        spoken = format_spoken(day, start, duration)
        if not proposals:
            return (
                f"{spoken} clashes with another event, so I haven't booked anything. "
                "I couldn't find a free slot of that length nearby — try a shorter "
                "event or another day."
            )
        offered = proposals[:MAX_SPOKEN_ALTERNATIVES]
        parts = [slot.spoken(include_duration=True) for slot in offered]
        text = ", ".join(parts[:-1] + [f"or {parts[-1]}"] if len(parts) > 1 else parts)
        # The scan is asked for one slot more than is spoken, so "other free
        # times" is only claimed when the list really was longer. Counting them
        # aloud would be noise: a week-long scan can find hundreds.
        more = ", and other free times after that" if len(proposals) > len(offered) else ""
        return (
            f"{spoken} is already taken, so I haven't booked anything. The nearest free "
            f"times are {text}{more}. Would you like one of those?"
        )

    # -- Actions -------------------------------------------------------------
    def _action_add(
        self, args: Dict[str, Any], context: Optional[ToolContext] = None
    ) -> ToolExecutionResult:
        title = self._require_text(
            args, "title", "add", "Give the event a short name, for example 'Dentist'."
        )
        today = self._today_provider()
        day, start, duration = self._resolve_slot(args, "add", today)
        repeat = args.get("repeat")
        repeat = str(repeat).strip().lower() if isinstance(repeat, str) and repeat.strip() else None
        if repeat is not None and repeat not in ("daily", "weekly"):
            raise ScheduleToolError(
                f"'{repeat}' is not a repeat rule: use 'daily' or 'weekly'."
            )
        until_raw = args.get("until")
        until: Optional[str] = None
        if isinstance(until_raw, str) and until_raw.strip():
            until_date = parse_date_text(until_raw, today)
            if until_date < day:
                raise ScheduleToolError(
                    f"The repeat end date {format_spoken(until_date)} is before the event "
                    f"itself on {format_spoken(day)}."
                )
            until = until_date.isoformat()
        elif repeat is not None:
            raise ScheduleToolError(
                f"A repeating event needs an end date: add 'until' as YYYY-MM-DD "
                f"(for example {(day + timedelta(days=30)).isoformat()})."
            )

        reminder_minutes = self._reminder_arg(args)
        if reminder_minutes is None and self._request_is_a_reminder(context):
            # The owner asked to BE reminded but no offset was passed. `None`
            # means "no reminder", so the request would be silently dead: the
            # words are right here on the context, and a voice reminder with no
            # stated lead time means "when the thing happens" -> fire at the
            # event's start (reminder_minutes 0, which due_reminders now
            # honours). A plain calendar entry without reminder phrasing keeps
            # `None` and gets no reminder at all, exactly as before.
            reminder_minutes = 0
        events = self._load_events()
        proposals = self._backend_propose(
            events,
            date=day.isoformat(),
            start=start,
            duration_minutes=duration,
            window_start=self._window_start_minutes,
            window_end=self._window_end_minutes,
            granularity_minutes=self._granularity_minutes,
            horizon_days=self._horizon_days,
            days=self._proposal_days,
            title=title,
            limit=MAX_SPOKEN_ALTERNATIVES + 1,
            exclude=None,
        )
        # ONE code path: the scan starts at the requested time, so a free
        # requested time comes back as the first proposal. Equal means free.
        if not proposals or proposals[0].date != day.isoformat() or proposals[0].start != start:
            return ToolExecutionResult(
                success=True,
                reply_text=self._conflict_reply(day, start, duration, proposals),
            )

        outcome = self._get_backend().add(
            events,
            title=title,
            date=day.isoformat(),
            start=start,
            duration_minutes=duration,
            repeat=repeat,
            until=until,
            reminder_minutes=reminder_minutes,
        )
        # The calendar is the authority: if it refused, nothing was written and
        # its own proposals are the answer. "On conflict: not written."
        refusal = self._outcome_conflict(outcome, day, start, duration)
        if refusal is not None:
            return ToolExecutionResult(success=True, reply_text=refusal)
        self._save_events(events)
        created = outcome.get("event") if isinstance(outcome, dict) and "event" in outcome else outcome
        view = _event_view(created) if created is not None else None
        spoken_day = view.date if view and view.date else day.isoformat()
        spoken_start = view.start if view and view.start is not None else start
        spoken_duration = view.duration_minutes if view else duration
        reminder = f" It repeats {repeat} until {format_spoken(until)}." if repeat and until else ""
        if reminder_minutes == 0:
            # "0 minutes before" is not a sentence a person says; the offset
            # means "when it starts", so say that.
            reminder += " I'll remind you when it starts."
        elif reminder_minutes is not None:
            reminder += f" I'll remind you {reminder_minutes} minutes before."
        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"Added '{view.title if view and view.title else title}' on "
                f"{format_spoken(spoken_day, spoken_start, spoken_duration)}.{reminder}"
            ),
        )

    def _action_list(self, args: Dict[str, Any]) -> ToolExecutionResult:
        today = self._today_provider()
        when = args.get("when")
        start_day = today
        if isinstance(when, str) and when.strip():
            start_day, _ = resolve_when(when, today)
        days = self._days_arg(args)
        events = self._load_events()
        listed = self._backend_list(
            events,
            start_date=start_day.isoformat(),
            days=days,
        )
        window_end = start_day + timedelta(days=days)
        upcoming: List[_EventView] = []
        for raw in listed:
            view = _event_view(raw)
            if view.date is None:
                continue
            if start_day.isoformat() <= view.date < window_end.isoformat():
                upcoming.append(view)
        upcoming.sort(key=lambda view: (view.date or "", view.start if view.start is not None else 0))
        span = f"{format_spoken(start_day)} to {format_spoken(window_end - timedelta(days=1))}"
        if not upcoming:
            return ToolExecutionResult(
                success=True,
                reply_text=f"Nothing is scheduled between {span}.",
            )
        parts: List[str] = []
        for view in upcoming:
            when_text = format_spoken(view.date, view.start)
            label = view.title or "an untitled event"
            repeat = f" (repeats {view.repeat})" if view.repeat else ""
            parts.append(f"{when_text} — {label}{repeat}")
        count = len(parts)
        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"You have {count} event{'s' if count != 1 else ''} between {span}: "
                + "; ".join(parts)
                + "."
            ),
        )

    def _action_move(self, args: Dict[str, Any]) -> ToolExecutionResult:
        ref = args.get("event_id") or args.get("title")
        if not isinstance(ref, str) or not ref.strip():
            raise ScheduleToolError(
                "schedule move needs 'event_id' — which event to move. Use the id or "
                "the title from 'list'."
            )
        ref = ref.strip()
        today = self._today_provider()
        events = self._load_events()
        matched_raw, matched = self._match_event(events, ref)
        if matched is None:
            raise ScheduleToolError(
                f"I couldn't find an event called '{ref}' to move. List the calendar "
                "first, then give the exact title or id."
            )
        fallback_duration = matched.duration_minutes
        day, start, duration = self._resolve_slot(args, "move", today, fallback_duration)
        proposals = self._backend_propose(
            events,
            date=day.isoformat(),
            start=start,
            duration_minutes=duration,
            window_start=self._window_start_minutes,
            window_end=self._window_end_minutes,
            granularity_minutes=self._granularity_minutes,
            horizon_days=self._horizon_days,
            days=self._proposal_days,
            title=matched.title or ref,
            limit=MAX_SPOKEN_ALTERNATIVES + 1,
            # Exclude the event being moved from its own conflict check, or it
            # collides with itself and every correct move is refused.
            exclude=matched_raw,
        )
        if not proposals or proposals[0].date != day.isoformat() or proposals[0].start != start:
            return ToolExecutionResult(
                success=True,
                reply_text=self._conflict_reply(day, start, duration, proposals),
            )
        outcome = self._get_backend().move(
            events,
            event_ref=ref,
            event=matched_raw,
            date=day.isoformat(),
            start=start,
            duration_minutes=duration,
        )
        refusal = self._outcome_conflict(outcome, day, start, duration)
        if refusal is not None:
            return ToolExecutionResult(success=True, reply_text=refusal)
        self._save_events(events)
        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"Moved '{matched.title or ref}' to {format_spoken(day, start, duration)}."
            ),
        )

    def _action_cancel(self, args: Dict[str, Any]) -> ToolExecutionResult:
        ref = args.get("event_id") or args.get("title")
        if not isinstance(ref, str) or not ref.strip():
            raise ScheduleToolError(
                "schedule cancel needs 'event_id' — which event to cancel. Use the id "
                "or the title from 'list'."
            )
        ref = ref.strip()
        today = self._today_provider()
        occurrence: Optional[str] = None
        occurrence_raw = args.get("date")
        if not (isinstance(occurrence_raw, str) and occurrence_raw.strip()):
            when_raw = args.get("when")
            if isinstance(when_raw, str) and _ISO_DATE.match(when_raw.strip()):
                occurrence_raw = when_raw
        if isinstance(occurrence_raw, str) and occurrence_raw.strip():
            occurrence = parse_date_text(occurrence_raw, today).isoformat()
        events = self._load_events()
        matched_raw, matched = self._match_event(events, ref)
        if matched is None:
            raise ScheduleToolError(
                f"I couldn't find an event called '{ref}' to cancel. List the calendar "
                "first, then give the exact title or id."
            )
        # A per-date cancellation of a one-off event is refused, not silently
        # ignored: the calendar records the date against the series, so a
        # non-repeating event would seem cancelled while still sitting in the
        # calendar — the worst possible answer.
        if occurrence is not None and not matched.repeat:
            raise ScheduleToolError(
                f"'{matched.title or ref}' is not a repeating event, so there is no single "
                "occurrence to cancel — cancel it without a date to remove it."
            )
        self._get_backend().cancel(
            events,
            event_ref=ref,
            event=matched_raw,
            date=occurrence,
        )
        self._save_events(events)
        label = matched.title or ref
        if occurrence is not None and matched.repeat:
            return ToolExecutionResult(
                success=True,
                reply_text=(
                    f"Cancelled the {format_spoken(occurrence)} occurrence of '{label}'; "
                    "the rest of the series is unchanged."
                ),
            )
        existing_when = (
            format_spoken(matched.date, matched.start) if matched.date else "the calendar"
        )
        return ToolExecutionResult(
            success=True,
            reply_text=f"Cancelled '{label}' on {existing_when}.",
        )

    def _action_due(self, args: Dict[str, Any]) -> ToolExecutionResult:
        events = self._load_events()
        now = self._now_provider()
        due = self._backend_due(events, now)
        if not due:
            return ToolExecutionResult(
                success=True,
                reply_text="Nothing is due right now.",
            )
        parts: List[str] = []
        for raw in due:
            view = _event_view(raw)
            label = view.title or "a reminder"
            text = label
            if view.date:
                text += f", {format_spoken(view.date, view.start)}"
            minutes_until = _as_int(_get_field(raw, "minutes_until_start"))
            if minutes_until is not None and minutes_until > 0:
                unit = "minute" if minutes_until == 1 else "minutes"
                text += f", in {minutes_until} {unit}"
            parts.append(text)
        count = len(parts)
        # Speak them once: a reminder that repeats every turn is noise, so the
        # keys are committed as they are spoken. Failing to commit must not lose
        # the reminder the user is hearing right now, so this is best effort.
        marked = self._backend_mark_fired(events, due)
        if marked:
            self._save_events(events)
        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"{count} reminder{'s' if count != 1 else ''} due now: "
                + "; ".join(parts)
                + "."
            ),
        )
