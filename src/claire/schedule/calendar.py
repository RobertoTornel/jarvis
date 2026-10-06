"""Pure local-first calendar logic for Claire (R-SCHED-1).

This module is deliberately free of file I/O and network access.  Everything it
needs is passed in and everything it produces is a plain ``dict``/``list``, so
the scheduling rules can be tested without a clock, a timezone database or a
disk.  Persistence lives elsewhere and simply stores the dicts this module
hands back.

Time model
----------
* A date is a ``'YYYY-MM-DD'`` string.
* A time is a whole number of minutes from local midnight (``0``..``1440``).
* No timezone object is ever constructed, so behaviour is deterministic and
  identical on every machine.

The rules encoded here were learned the hard way; each one carries a comment
explaining the bug that produced it.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import date as _date, datetime as _datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEDULE_VERSION = "1"

#: Default length of an event that does not say how long it lasts.
DEFAULT_DURATION_MINUTES = 60

#: Default allowed window for proposals: 08:00 -> 21:00, as minutes from midnight.
DEFAULT_WINDOW: Tuple[int, int] = (8 * 60, 21 * 60)

#: Default spacing between candidate proposal start times.
DEFAULT_GRAIN_MINUTES = 15

#: How far ahead a proposal may ever be placed.
DEFAULT_HORIZON_DAYS = 365

_MINUTES_IN_DAY = 24 * 60

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
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

_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_TIME_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_TIME_BARE_HOUR_RE = re.compile(r"^(\d{1,2})$")
_TIME_AMPM_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap]m)$", re.IGNORECASE)

_KNOWN_EVENT_FIELDS = frozenset(
    {
        "id",
        "title",
        "date",
        "start",
        "end",
        "duration",
        "recurrence",
        "cancelled_dates",
        "notes",
        "reminder_minutes",
    }
)
_KNOWN_RECURRENCE_FIELDS = frozenset({"freq", "until"})
_RECURRENCE_FREQS = ("daily", "weekly")

_EPOCH = _date(1970, 1, 1)


class ScheduleError(Exception):
    """Base class for every calendar failure.

    The calendar raises only named subclasses so a caller can tell "this date is
    nonsense" from "that slot is taken" without matching on message text.
    """


class InvalidDateError(ScheduleError, ValueError):
    """A date was missing, malformed or not a real calendar date."""


class InvalidTimeError(ScheduleError, ValueError):
    """A time was missing, malformed or outside 00:00-24:00."""


class InvalidEventError(ScheduleError, ValueError):
    """An event dict was missing a required field or carried nonsense."""


class UnknownEventFieldError(InvalidEventError):
    """An event dict carried a field the calendar does not understand.

    Silently ignoring an unknown key is how a typo (``titel``/``strat``)
    becomes a silently-defaulted event, so we refuse it by name instead.
    """


class RecurrenceError(InvalidEventError):
    """A recurrence rule was malformed or incomplete."""


class InvalidScheduleError(ScheduleError, ValueError):
    """The schedule container itself was not a well-formed mapping."""


class EventNotFoundError(ScheduleError, LookupError):
    """No event with the requested id exists in the schedule."""


class NotAnOccurrenceError(ScheduleError, ValueError):
    """A per-date cancellation named a date the event never occurs on."""


class InvalidWindowError(ScheduleError, ValueError):
    """The allowed window was malformed or empty."""


class InvalidGrainError(ScheduleError, ValueError):
    """The proposal granularity was not a positive number of minutes."""


# ---------------------------------------------------------------------------
# primitive helpers
# ---------------------------------------------------------------------------


def parse_local_date(value: Any) -> str:
    """Return ``value`` as a canonical ``'YYYY-MM-DD'`` string.

    Accepts a string, a :class:`datetime.date` or a :class:`datetime.datetime`.
    Anything else -- and any string that is not a real calendar date -- raises
    :class:`InvalidDateError` rather than falling back to "today".
    """
    if isinstance(value, _datetime):
        value = value.date()
    if isinstance(value, _date):
        return value.isoformat()
    if not isinstance(value, str):
        raise InvalidDateError(
            f"date must be a 'YYYY-MM-DD' string or a date, got {type(value).__name__}"
        )
    text = value.strip()
    if not _DATE_RE.match(text):
        raise InvalidDateError(f"date {value!r} is not in 'YYYY-MM-DD' form")
    year, month, day = (part.zfill(2) for part in text.split("-"))
    try:
        # fromisoformat() needs zero-padded parts, so pad before handing over.
        return _date.fromisoformat(f"{year}-{month}-{day}").isoformat()
    except ValueError as exc:  # e.g. 2026-02-30, 2026-13-01
        raise InvalidDateError(f"date {value!r} is not a real calendar date: {exc}") from exc


def parse_local_time(value: Any) -> int:
    """Return minutes from local midnight for ``value``.

    Accepts an integer (already minutes), ``'16:00'``, ``'16:00'``-style
    strings, ``'4pm'``/``'4:30 pm'``, and a bare hour (``'9'`` -> 09:00, read on
    the 24-hour clock).  ``'24:00'`` is allowed as "end of day" (1440).
    """
    if isinstance(value, bool):
        raise InvalidTimeError(f"time must not be a bool, got {value!r}")
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str):
        text = value.strip().lower()
        if not text:
            raise InvalidTimeError("time is empty")
        minutes = None
        match = _TIME_HM_RE.match(text)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            # '16:75' must not quietly become 17:15.
            if minute > 59:
                raise InvalidTimeError(f"time {value!r} has more than 59 minutes")
            if hour == 24 and minute != 0:
                raise InvalidTimeError(f"time {value!r} is past the end of the day")
            minutes = hour * 60 + minute
        if minutes is None:
            match = _TIME_AMPM_RE.match(text)
            if match:
                hour, minute = int(match.group(1)), int(match.group(2) or 0)
                if not 1 <= hour <= 12:
                    raise InvalidTimeError(f"12-hour time {value!r} has an hour outside 1-12")
                # 12am is midnight, 12pm is noon; every other pm hour is +12.
                if match.group(3).lower() == "pm" and hour != 12:
                    hour += 12
                elif match.group(3).lower() == "am" and hour == 12:
                    hour = 0
                if minute > 59:
                    raise InvalidTimeError(f"time {value!r} has more than 59 minutes")
                minutes = hour * 60 + minute
        if minutes is None:
            match = _TIME_BARE_HOUR_RE.match(text)
            if match:
                minutes = int(match.group(1)) * 60
        if minutes is None:
            raise InvalidTimeError(
                f"time {value!r} is not understood (use '16:00', '4pm' or a bare hour)"
            )
    else:
        raise InvalidTimeError(f"time must be minutes or a string, got {type(value).__name__}")

    if not 0 <= minutes <= _MINUTES_IN_DAY:
        raise InvalidTimeError(f"time {value!r} is outside the 00:00-24:00 day")
    return minutes


def _hhmm(minutes: int) -> str:
    if minutes == _MINUTES_IN_DAY:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def weekday_of(value: Any) -> int:
    """Weekday of a date, Monday = 0 .. Sunday = 6 (``datetime.weekday``)."""
    return _date.fromisoformat(parse_local_date(value)).weekday()


def add_days(value: Any, days: int) -> str:
    """Return the date ``days`` days after ``value`` (negative goes backwards)."""
    if not isinstance(days, int) or isinstance(days, bool):
        raise InvalidDateError(f"days must be an int, got {type(days).__name__}")
    base = _date.fromisoformat(parse_local_date(value))
    return base.fromordinal(base.toordinal() + days).isoformat()


def days_between(start: Any, end: Any) -> int:
    """Whole days from ``start`` to ``end`` (negative when ``end`` is earlier)."""
    a = _date.fromisoformat(parse_local_date(start))
    b = _date.fromisoformat(parse_local_date(end))
    return (b - a).days


def format_spoken(value: Any, start: Any = None, end: Any = None) -> str:
    """Human sentence fragment for TTS output.

    ``format_spoken('2026-09-24', 16 * 60)`` -> ``'Thursday 24 September at 16:00'``.
    ``start=None`` gives just the day; ``end`` adds ``' to 17:00'``.
    """
    parsed = _date.fromisoformat(parse_local_date(value))
    text = f"{_WEEKDAY_NAMES[parsed.weekday()]} {parsed.day} {_MONTH_NAMES[parsed.month - 1]}"
    if start is None:
        if end is not None:
            raise InvalidTimeError("format_spoken() cannot format an end without a start")
        return text
    resolved_start = parse_local_time(start)
    text += f" at {_hhmm(resolved_start)}"
    if end is not None:
        resolved_end = parse_local_time(end)
        if resolved_end <= resolved_start:
            raise InvalidTimeError("format_spoken() end must be after start")
        text += f" to {_hhmm(resolved_end)}"
    return text


def _absolute_minutes(day: Any, minutes: int) -> int:
    """Absolute minute coordinate used to compare moments across dates."""
    return days_between(_EPOCH, day) * _MINUTES_IN_DAY + minutes


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def _unique_id(seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"evt-{digest}"


def _normalise_recurrence(raw: Any, event_date: str) -> Optional[Dict[str, str]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RecurrenceError(
            f"recurrence must be a dict with 'freq' and 'until', got {type(raw).__name__}"
        )
    unknown = sorted(set(raw) - _KNOWN_RECURRENCE_FIELDS)
    if unknown:
        raise RecurrenceError(f"recurrence has unknown field(s): {', '.join(unknown)}")
    freq = raw.get("freq")
    if freq not in _RECURRENCE_FREQS:
        raise RecurrenceError(
            f"recurrence 'freq' must be one of {', '.join(_RECURRENCE_FREQS)}, got {freq!r}"
        )
    if "until" not in raw or raw["until"] is None:
        # An unbounded daily rule would expand to infinity; refuse rather than
        # silently invent a stopping point.
        raise RecurrenceError("recurrence requires an 'until' date")
    try:
        until = parse_local_date(raw["until"])
    except InvalidDateError as exc:
        raise RecurrenceError(f"recurrence 'until' is invalid: {exc}") from exc
    if until < event_date:
        raise RecurrenceError(f"recurrence 'until' {until} is before the event date {event_date}")
    return {"freq": freq, "until": until}


def _event_date(value: Any) -> str:
    """parse_local_date() with the failing field named in the error."""
    try:
        return parse_local_date(value)
    except InvalidDateError as exc:
        raise InvalidEventError(f"event 'date': {exc}") from exc


def _event_time(value: Any, field: str) -> int:
    """parse_local_time() with the failing field named in the error."""
    try:
        return parse_local_time(value)
    except InvalidTimeError as exc:
        raise InvalidEventError(f"event '{field}': {exc}") from exc


def normalize_event(raw: Any) -> Dict[str, Any]:
    """Return the canonical form of a raw event mapping.

    Canonical keys: ``id, title, date, start, end, duration, recurrence,
    cancelled_dates, notes, reminder_minutes``.  ``id``, ``notes`` and the two
    optional rule fields are the only things invented here; every other missing
    or contradictory field raises a named error.
    """
    if not isinstance(raw, dict):
        raise InvalidEventError(f"event must be a dict, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _KNOWN_EVENT_FIELDS)
    if unknown:
        raise UnknownEventFieldError(f"event has unknown field(s): {', '.join(unknown)}")

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise InvalidEventError("event requires a non-empty 'title'")
    title = title.strip()

    if "date" not in raw or raw["date"] is None:
        raise InvalidEventError("event requires a 'date'")
    event_date = _event_date(raw["date"])

    has_start = raw.get("start") is not None
    has_end = raw.get("end") is not None
    has_duration = raw.get("duration") is not None
    if not has_start and not has_end:
        # A duration alone says how long, not when: that start cannot be
        # invented, so refuse instead of silently booking midnight.
        raise InvalidEventError("event requires a 'start' time")

    if has_start:
        start = _event_time(raw["start"], "start")
    else:
        # An end with no start is only resolvable when a duration was given too.
        if not has_duration:
            raise InvalidEventError("event needs a 'start' as well as an 'end'")
        start = _event_time(raw["end"], "end") - _require_positive_int(raw["duration"], "duration")

    if has_end:
        end = _event_time(raw["end"], "end")
    elif has_duration:
        duration = _require_positive_int(raw["duration"], "duration")
        end = start + duration
    else:
        # The one specified default: an event that does not state a length is an
        # hour long.
        end = start + DEFAULT_DURATION_MINUTES

    if has_end and has_duration:
        stated = _require_positive_int(raw["duration"], "duration")
        if stated != end - start:
            raise InvalidEventError(
                f"event 'duration' ({stated}) contradicts 'end' ({end - start} minutes)"
            )
    if end <= start:
        raise InvalidEventError(
            f"event 'end' ({_hhmm(end)}) must be after 'start' ({_hhmm(start)})"
        )
    if start < 0 or end > _MINUTES_IN_DAY:
        raise InvalidEventError(
            f"event {_hhmm(start)}-{_hhmm(end)} falls outside the 00:00-24:00 day"
        )

    cancelled_raw = raw.get("cancelled_dates") or []
    if not isinstance(cancelled_raw, (list, tuple)):
        raise InvalidEventError("'cancelled_dates' must be a list of dates")
    try:
        cancelled = sorted({parse_local_date(item) for item in cancelled_raw})
    except InvalidDateError as exc:
        raise InvalidEventError(f"event 'cancelled_dates': {exc}") from exc

    notes = raw.get("notes") or ""
    if not isinstance(notes, str):
        raise InvalidEventError("'notes' must be a string")

    reminder = raw.get("reminder_minutes")
    if reminder is not None:
        reminder = _require_positive_int(reminder, "reminder_minutes", allow_zero=True)

    event_id = raw.get("id")
    if event_id is None:
        event_id = _unique_id(f"{title}|{event_date}|{start}|{end}")
    elif not isinstance(event_id, str) or not event_id.strip():
        raise InvalidEventError("'id' must be a non-empty string when supplied")
    else:
        event_id = event_id.strip()

    return {
        "id": event_id,
        "title": title,
        "date": event_date,
        "start": start,
        "end": end,
        "duration": end - start,
        "recurrence": _normalise_recurrence(raw.get("recurrence"), event_date),
        "cancelled_dates": cancelled,
        "notes": notes,
        "reminder_minutes": reminder,
    }


def _require_positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise InvalidEventError(f"{field} must be a {qualifier} whole number of minutes")
    return value


# ---------------------------------------------------------------------------
# overlap + expansion
# ---------------------------------------------------------------------------


def ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open overlap: ``aStart < bEnd and bStart < aEnd``.

    Touching is NOT a conflict.  An event ending 15:00 and one starting 15:00 do
    not collide; treating them as a collision is a real bug that makes correct
    bookings get refused, so the comparison is strict on both sides.
    """
    return a_start < b_end and b_start < a_end


def _occurrence(event: Dict[str, Any], day: str) -> Dict[str, Any]:
    return {
        "event_id": event["id"],
        "title": event["title"],
        "date": day,
        "start": event["start"],
        "end": event["end"],
        "duration": event["duration"],
        "notes": event.get("notes", ""),
        "reminder_minutes": event.get("reminder_minutes"),
        "is_recurring": event.get("recurrence") is not None,
    }


def expand_event(event: Any, from_date: Any, to_date: Any) -> List[Dict[str, Any]]:
    """Concrete occurrences of ``event`` between ``from_date`` and ``to_date``.

    Honours the recurrence rule and skips every date in ``cancelled_dates``.
    ``from_date``/``to_date`` are inclusive.
    """
    event = normalize_event(event)
    start_bound = parse_local_date(from_date)
    end_bound = parse_local_date(to_date)
    if end_bound < start_bound:
        return []

    recurrence = event["recurrence"]
    cancelled = set(event["cancelled_dates"])
    base = event["date"]

    if recurrence is None:
        days = [base] if start_bound <= base <= end_bound else []
    else:
        step = 1 if recurrence["freq"] == "daily" else 7
        last = min(recurrence["until"], end_bound)
        first = max(base, start_bound)
        # Jump straight to the first occurrence at or after the window instead
        # of walking every day from the base date.
        offset = days_between(base, first)
        if offset > 0:
            jumps = -(-offset // step)  # ceiling division
            current = add_days(base, jumps * step)
        else:
            current = base
        days = []
        while current <= last:
            days.append(current)
            current = add_days(current, step)

    return [_occurrence(event, day) for day in days if day not in cancelled]


def schedule_instances(schedule: Any, from_date: Any, to_date: Any) -> List[Dict[str, Any]]:
    """Every concrete occurrence in ``schedule`` within the inclusive range."""
    _require_schedule(schedule)
    start_bound = parse_local_date(from_date)
    end_bound = parse_local_date(to_date)
    instances: List[Dict[str, Any]] = []
    for event in schedule["events"]:
        instances.extend(expand_event(event, start_bound, end_bound))
    return sorted(instances, key=lambda item: (item["date"], item["start"], item["title"]))


def _coerce_query(
    start: Any, end: Any, day: Optional[Any]
) -> Tuple[Optional[str], int, int]:
    """Resolve the (date, start, end) triple accepted by find_conflicts()."""
    if isinstance(start, dict):
        if day is None:
            day = start.get("date")
        item_start = start.get("start")
        item_end = start.get("end")
        if item_end is None and item_start is not None and start.get("duration") is not None:
            item_end = item_start + start["duration"]
    elif isinstance(start, (tuple, list)) and len(start) == 3:
        if day is None:
            day = start[0]
        item_start, item_end = start[1], start[2]
    else:
        item_start, item_end = start, end
    if item_start is None or item_end is None:
        raise InvalidEventError("find_conflicts() requires both a start and an end")
    resolved_start = parse_local_time(item_start)
    resolved_end = parse_local_time(item_end)
    if resolved_end <= resolved_start:
        raise InvalidEventError("find_conflicts() end must be after start")
    return (parse_local_date(day) if day is not None else None), resolved_start, resolved_end


def find_conflicts(
    instances: Sequence[Dict[str, Any]],
    start: Any,
    end: Any = None,
    *,
    exclude_id: Optional[str] = None,
    date: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Instances that collide with the queried range.

    ``start``/``end`` may be minutes, ``'HH:MM'`` strings, an occurrence dict, or
    a ``(date, start, end)`` triple.  ``exclude_id`` drops that event's own
    occurrences from the result -- without it a MOVE collides with itself and
    every correct move is refused.

    When neither the ``date`` argument nor a date-bearing ``start`` is given, the
    range is compared against each instance's own day, so callers spanning
    several days should pass ``date=`` or pre-filter their instances.
    """
    day, resolved_start, resolved_end = _coerce_query(start, end, date)
    conflicts = [
        instance
        for instance in instances
        if not (exclude_id is not None and instance.get("event_id") == exclude_id)
        and (day is None or instance.get("date") == day)
        and ranges_overlap(resolved_start, resolved_end, instance["start"], instance["end"])
    ]
    return sorted(conflicts, key=lambda item: (item["date"], item["start"], item["title"]))


def _event_span_end(event: Dict[str, Any]) -> str:
    recurrence = event.get("recurrence")
    return recurrence["until"] if recurrence else event["date"]


def _conflicts_for_event(
    schedule: Dict[str, Any], candidate: Dict[str, Any], exclude_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Conflicts for every occurrence of a candidate event."""
    instances = schedule_instances(schedule, candidate["date"], _event_span_end(candidate))
    found: List[Dict[str, Any]] = []
    for occurrence in expand_event(candidate, candidate["date"], _event_span_end(candidate)):
        found.extend(find_conflicts(instances, occurrence, exclude_id=exclude_id))
    seen = set()
    unique: List[Dict[str, Any]] = []
    for conflict in found:
        key = (conflict["event_id"], conflict["date"], conflict["start"])
        if key not in seen:
            seen.add(key)
            unique.append(conflict)
    return sorted(unique, key=lambda item: (item["date"], item["start"], item["title"]))


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


def _resolve_window(window: Any) -> Tuple[int, int]:
    if window is None:
        return DEFAULT_WINDOW
    if not isinstance(window, (tuple, list)) or len(window) != 2:
        raise InvalidWindowError("window must be a (start, end) pair of times")
    start = parse_local_time(window[0])
    end = parse_local_time(window[1])
    if end <= start:
        raise InvalidWindowError("window end must be after window start")
    return start, end


def propose_alternatives(
    schedule_or_instances: Any,
    event: Any,
    days: int = 7,
    *,
    window: Any = None,
    grain: Any = None,
    exclude_id: Optional[str] = None,
    horizon: Any = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Ranked free slots for ``event``, nearest to the requested time first.

    "The requested time is free" and "here is the nearest alternative" are ONE
    code path: the scan starts at the requested time, so when that moment is free
    the first proposal IS the requested time.

    Every candidate is checked as a whole span: proposals never start before the
    window opens and never run past the window end (checking only the START would
    happily propose a slot that overruns the window), and never land beyond the
    horizon.
    """
    candidate = normalize_event(event)
    window_start, window_end = _resolve_window(window)
    grain_minutes = DEFAULT_GRAIN_MINUTES if grain is None else grain
    if isinstance(grain_minutes, bool) or not isinstance(grain_minutes, int) or grain_minutes <= 0:
        raise InvalidGrainError(f"grain must be a positive number of minutes, got {grain!r}")
    horizon_days = DEFAULT_HORIZON_DAYS if horizon is None else horizon
    if (
        isinstance(horizon_days, bool)
        or not isinstance(horizon_days, int)
        or horizon_days < 0
    ):
        raise InvalidWindowError(f"horizon must be a non-negative number of days, got {horizon!r}")
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise InvalidWindowError(f"days must be a non-negative whole number, got {days!r}")

    duration = candidate["duration"]
    requested_start = candidate["start"]
    requested_date = candidate["date"]
    # The horizon caps the scan: never propose further out than allowed.
    scan_days = min(days, horizon_days)
    last_date = add_days(requested_date, scan_days)

    if isinstance(schedule_or_instances, dict):
        busy = schedule_instances(schedule_or_instances, requested_date, last_date)
    elif isinstance(schedule_or_instances, (list, tuple)):
        busy = list(schedule_or_instances)
    else:
        raise InvalidScheduleError(
            "propose_alternatives() needs a schedule dict or a list of instances"
        )

    # Exclude the occurrence being moved/duplicated, or it blocks its own slot.
    if exclude_id is None:
        exclude_id = candidate.get("id")

    proposals: List[Dict[str, Any]] = []
    for offset in range(scan_days + 1):
        day = add_days(requested_date, offset)
        first_time = requested_start if offset == 0 else window_start
        moment = first_time
        while moment + duration <= window_end:
            # Whole-span window check: a start inside the window is not enough.
            if moment >= window_start and moment + duration <= window_end:
                free = not find_conflicts(
                    busy,
                    moment,
                    moment + duration,
                    exclude_id=exclude_id,
                    date=day,
                )
                if free:
                    distance = offset * _MINUTES_IN_DAY + moment - requested_start
                    proposals.append(
                        {
                            "date": day,
                            "start": moment,
                            "end": moment + duration,
                            "duration": duration,
                            "rank": len(proposals) + 1,
                            "is_requested": distance == 0,
                            "distance_minutes": distance,
                            "label": _proposal_label(day, moment, moment + duration, distance),
                        }
                    )
                    if limit is not None and len(proposals) >= limit:
                        return proposals
            moment += grain_minutes
    return proposals


def _proposal_label(day: str, start: int, end: int, distance: int) -> str:
    when = format_spoken(day, start, end)
    if distance == 0:
        return f"{when} (requested time is free)"
    return f"{when} (+{distance} min)"


# ---------------------------------------------------------------------------
# schedule container
# ---------------------------------------------------------------------------


def empty_schedule() -> Dict[str, Any]:
    """A fresh, valid schedule."""
    return {"version": SCHEDULE_VERSION, "events": [], "fired_reminders": []}


def _require_schedule(schedule: Any) -> None:
    if not isinstance(schedule, dict):
        raise InvalidScheduleError(f"schedule must be a dict, got {type(schedule).__name__}")
    if "events" not in schedule:
        raise InvalidScheduleError("schedule is missing its 'events' list")
    if not isinstance(schedule["events"], list):
        raise InvalidScheduleError("schedule 'events' must be a list")
    fired = schedule.get("fired_reminders", [])
    if not isinstance(fired, list):
        raise InvalidScheduleError("schedule 'fired_reminders' must be a list")


def _store(schedule: Dict[str, Any], new_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Swap in new events while keeping the caller's dict object identity."""
    schedule["events"] = new_events
    schedule.setdefault("version", SCHEDULE_VERSION)
    schedule.setdefault("fired_reminders", [])
    return schedule


def _find_event(schedule: Dict[str, Any], event_id: Any) -> Dict[str, Any]:
    if not isinstance(event_id, str) or not event_id.strip():
        raise EventNotFoundError(f"event id must be a non-empty string, got {event_id!r}")
    for event in schedule["events"]:
        if event.get("id") == event_id:
            return event
    raise EventNotFoundError(f"no event with id {event_id!r}")


def _free_id(schedule: Dict[str, Any], base: str) -> str:
    taken = {event.get("id") for event in schedule["events"]}
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


def add_event(schedule: Any, raw: Any, *, days: int = 7) -> Dict[str, Any]:
    """Add ``raw`` to ``schedule`` unless it collides.

    ON CONFLICT the event is NOT written and the result carries the proposals
    instead: atomic refusal with alternatives, never a partial write.
    """
    _require_schedule(schedule)
    event = normalize_event(raw)
    event["id"] = _free_id(schedule, event["id"])
    conflicts = _conflicts_for_event(schedule, event, event["id"])
    if conflicts:
        return {
            "ok": False,
            "action": "add",
            "event": event,
            "conflicts": conflicts,
            "proposals": propose_alternatives(schedule, event, days, exclude_id=event["id"]),
            "error": (
                f"'{event['title']}' clashes with "
                f"{len(conflicts)} existing occurrence(s); nothing was written"
            ),
        }
    _store(schedule, list(schedule["events"]) + [event])
    return {
        "ok": True,
        "action": "add",
        "event": event,
        "conflicts": [],
        "proposals": [],
        "error": None,
    }


def move_event(
    schedule: Any,
    event_id: str,
    new_date: Any = None,
    new_start: Any = None,
    *,
    new_end: Any = None,
    new_duration: Any = None,
    days: int = 7,
) -> Dict[str, Any]:
    """Move one event, keeping its length unless told otherwise.

    The event being moved is excluded from its own conflict check (and from the
    alternative scan), so a move that merely shifts it is not refused for
    colliding with itself.
    """
    _require_schedule(schedule)
    existing = _find_event(schedule, event_id)

    raw = copy.deepcopy(existing)
    if new_date is not None:
        raw["date"] = new_date
    if new_start is not None:
        raw["start"] = parse_local_time(new_start)
    if new_duration is not None:
        raw["duration"] = new_duration
        raw.pop("end", None)
    elif new_end is not None:
        raw["end"] = parse_local_time(new_end)
        raw.pop("duration", None)
    elif new_start is not None:
        # Preserve the original length across the shift.
        raw["end"] = parse_local_time(new_start) + existing["duration"]
    candidate = normalize_event(raw)
    candidate["id"] = existing["id"]

    conflicts = _conflicts_for_event(schedule, candidate, existing["id"])
    if conflicts:
        return {
            "ok": False,
            "action": "move",
            "event": candidate,
            "previous": copy.deepcopy(existing),
            "conflicts": conflicts,
            "proposals": propose_alternatives(
                schedule, candidate, days, exclude_id=existing["id"]
            ),
            "error": (
                f"cannot move '{candidate['title']}': it clashes with "
                f"{len(conflicts)} occurrence(s); nothing was written"
            ),
        }

    new_events = [candidate if event.get("id") == existing["id"] else event for event in schedule["events"]]
    _store(schedule, new_events)
    return {
        "ok": True,
        "action": "move",
        "event": candidate,
        "previous": copy.deepcopy(existing),
        "conflicts": [],
        "proposals": [],
        "error": None,
    }


def cancel_event(schedule: Any, event_id: str, date: Any = None) -> Dict[str, Any]:
    """Cancel a whole event, or one occurrence of a recurring event.

    With ``date`` the date is appended to ``cancelled_dates``; naming a date the
    event never occurs on raises :class:`NotAnOccurrenceError` instead of quietly
    recording a cancellation that does nothing.
    """
    _require_schedule(schedule)
    existing = _find_event(schedule, event_id)

    if date is None:
        new_events = [event for event in schedule["events"] if event.get("id") != existing["id"]]
        _store(schedule, new_events)
        return {
            "ok": True,
            "action": "cancel",
            "cancelled": "event",
            "event": copy.deepcopy(existing),
            "occurrence": None,
            "error": None,
        }

    day = parse_local_date(date)
    occurrences = {item["date"] for item in expand_event(existing, existing["date"], _event_span_end(existing))}
    if day not in occurrences:
        raise NotAnOccurrenceError(
            f"'{existing['title']}' does not occur on {day}, so that date cannot be cancelled"
        )
    updated = copy.deepcopy(existing)
    updated["cancelled_dates"] = sorted(set(updated["cancelled_dates"]) | {day})
    _store(
        schedule,
        [updated if event.get("id") == existing["id"] else event for event in schedule["events"]],
    )
    return {
        "ok": True,
        "action": "cancel",
        "cancelled": "occurrence",
        "event": updated,
        "occurrence": day,
        "error": None,
    }


def list_events(schedule: Any, from_date: Any = None, to_date: Any = None) -> Dict[str, Any]:
    """Occurrences in ``schedule``, expanded and ordered by date then start."""
    _require_schedule(schedule)
    start_bound = parse_local_date(from_date) if from_date is not None else "0001-01-01"
    end_bound = parse_local_date(to_date) if to_date is not None else "9999-12-31"
    occurrences = schedule_instances(schedule, start_bound, end_bound)
    return {
        "ok": True,
        "action": "list",
        "occurrences": occurrences,
        "count": len(occurrences),
        "error": None,
    }


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------


def _coerce_now(now: Any) -> Tuple[str, int]:
    if isinstance(now, _datetime):
        return now.date().isoformat(), now.hour * 60 + now.minute
    if isinstance(now, _date):
        return now.isoformat(), 0
    if isinstance(now, str):
        text = now.strip().replace("T", " ")
        if " " in text:
            day_text, time_text = text.split(" ", 1)
        else:
            day_text, time_text = text, "00:00"
        return parse_local_date(day_text), parse_local_time(time_text)
    if isinstance(now, dict):
        return parse_local_date(now["date"]), parse_local_time(now["start"])
    raise InvalidDateError(
        f"now must be a date, datetime, 'YYYY-MM-DD HH:MM' string or dict, got {type(now).__name__}"
    )


def due_reminders(schedule: Any, now: Any) -> List[Dict[str, Any]]:
    """Reminders whose moment has arrived and whose event has not started yet.

    Pure: it does not mark anything as fired.  Keys already present in
    ``schedule['fired_reminders']`` are skipped so a caller can commit with
    :func:`mark_reminder_fired`.
    """
    _require_schedule(schedule)
    now_date, now_minutes = _coerce_now(now)
    now_absolute = _absolute_minutes(now_date, now_minutes)
    already_fired = set(schedule.get("fired_reminders") or [])

    due: List[Dict[str, Any]] = []
    for event in schedule["events"]:
        reminder_minutes = event.get("reminder_minutes")
        if reminder_minutes is None:
            continue
        for occurrence in expand_event(event, event["date"], now_date):
            start_absolute = _absolute_minutes(occurrence["date"], occurrence["start"])
            remind_absolute = start_absolute - reminder_minutes
            if not remind_absolute <= now_absolute < start_absolute:
                continue
            key = f"{occurrence['event_id']}|{occurrence['date']}"
            if key in already_fired:
                continue
            due.append(
                {
                    "key": key,
                    "event_id": occurrence["event_id"],
                    "title": occurrence["title"],
                    "date": occurrence["date"],
                    "start": occurrence["start"],
                    "end": occurrence["end"],
                    "reminder_minutes": reminder_minutes,
                    "remind_at_minutes": occurrence["start"] - reminder_minutes,
                    "minutes_until_start": start_absolute - now_absolute,
                    "label": format_spoken(occurrence["date"], occurrence["start"], occurrence["end"]),
                }
            )
    return sorted(due, key=lambda item: (item["date"], item["remind_at_minutes"], item["title"]))


def describe_reminder(reminder: Dict[str, Any], now: Any = None) -> str:
    """One spoken sentence for a reminder produced by :func:`due_reminders`."""
    if not isinstance(reminder, dict):
        raise InvalidEventError("reminder must be a dict")
    if "date" not in reminder or "start" not in reminder:
        raise InvalidEventError("reminder requires 'date' and 'start'")
    title = reminder.get("title") or "event"
    when = format_spoken(reminder["date"], reminder["start"])
    minutes = reminder.get("minutes_until_start")
    if now is not None:
        now_date, now_minutes = _coerce_now(now)
        minutes = _absolute_minutes(reminder["date"], parse_local_time(reminder["start"])) - _absolute_minutes(
            now_date, now_minutes
        )
    if minutes is None:
        return f"Reminder: {title}, {when}."
    if minutes <= 0:
        return f"Reminder: {title}, {when}, starting now."
    if minutes == 1:
        return f"Reminder: {title}, {when}, in 1 minute."
    return f"Reminder: {title}, {when}, in {minutes} minutes."


def mark_reminder_fired(schedule: Any, key: str) -> Dict[str, Any]:
    """Record ``key`` in the schedule's fired list so it is reported only once."""
    _require_schedule(schedule)
    if not isinstance(key, str) or not key.strip():
        raise InvalidEventError("reminder key must be a non-empty string")
    fired = list(schedule.get("fired_reminders") or [])
    if key not in fired:
        fired.append(key)
    _store(schedule, list(schedule["events"]))
    schedule["fired_reminders"] = fired
    return {"ok": True, "action": "fire_reminder", "key": key, "error": None}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_schedule(schedule: Any) -> List[str]:
    """Return a list of human-readable problems (empty means valid).

    This never raises: a corrupt schedule that was loaded from disk should be
    reportable, not fatal.
    """
    problems: List[str] = []
    if not isinstance(schedule, dict):
        return [f"schedule must be a dict, got {type(schedule).__name__}"]
    version = schedule.get("version")
    if version != SCHEDULE_VERSION:
        problems.append(f"unknown schedule version: {version!r}")
    events = schedule.get("events")
    if not isinstance(events, list):
        return problems + ["schedule 'events' must be a list"]
    fired = schedule.get("fired_reminders", [])
    if not isinstance(fired, list):
        problems.append("schedule 'fired_reminders' must be a list")

    canonical: List[Dict[str, Any]] = []
    seen_ids: Dict[str, int] = {}
    for index, raw in enumerate(events):
        try:
            event = normalize_event(raw)
        except ScheduleError as exc:
            problems.append(f"events[{index}]: {exc}")
            continue
        if event["id"] in seen_ids:
            problems.append(
                f"events[{index}]: duplicate event id {event['id']!r} "
                f"(first seen at index {seen_ids[event['id']]})"
            )
        else:
            seen_ids[event["id"]] = index
        canonical.append(event)

    for index, event in enumerate(canonical):
        for later in canonical[index + 1 :]:
            left = expand_event(event, event["date"], _event_span_end(event))
            right = expand_event(later, later["date"], _event_span_end(later))
            # Bucket by date: two events on the same day collide when their
            # ranges overlap, whether or not they start at the same minute.
            right_by_date: Dict[str, List[Dict[str, Any]]] = {}
            for occurrence in right:
                right_by_date.setdefault(occurrence["date"], []).append(occurrence)
            for occurrence in left:
                for other in right_by_date.get(occurrence["date"], []):
                    if ranges_overlap(
                        occurrence["start"], occurrence["end"], other["start"], other["end"]
                    ):
                        problems.append(
                            f"events {event['id']!r} and {later['id']!r} overlap on "
                            f"{occurrence['date']} ({_hhmm(occurrence['start'])}-"
                            f"{_hhmm(occurrence['end'])} vs {_hhmm(other['start'])}-"
                            f"{_hhmm(other['end'])})"
                        )
                        break
    return problems


__all__ = [
    "SCHEDULE_VERSION",
    "DEFAULT_DURATION_MINUTES",
    "DEFAULT_WINDOW",
    "DEFAULT_GRAIN_MINUTES",
    "DEFAULT_HORIZON_DAYS",
    "ScheduleError",
    "InvalidDateError",
    "InvalidTimeError",
    "InvalidEventError",
    "UnknownEventFieldError",
    "RecurrenceError",
    "InvalidScheduleError",
    "EventNotFoundError",
    "NotAnOccurrenceError",
    "InvalidWindowError",
    "InvalidGrainError",
    "parse_local_date",
    "parse_local_time",
    "format_spoken",
    "weekday_of",
    "add_days",
    "days_between",
    "normalize_event",
    "ranges_overlap",
    "expand_event",
    "schedule_instances",
    "find_conflicts",
    "propose_alternatives",
    "empty_schedule",
    "add_event",
    "move_event",
    "cancel_event",
    "list_events",
    "due_reminders",
    "describe_reminder",
    "mark_reminder_fired",
    "validate_schedule",
]
