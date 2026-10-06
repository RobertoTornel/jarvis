# Local-First Calendar (R-SCHED-1)

Pure scheduling logic for Claire's diary: add, list, move and cancel events, detect
conflicts and propose alternatives — all locally, with no timezone library and no LLM.

## Scope

- File: `src/claire/schedule/calendar.py` (pure logic), re-exported by
  `src/claire/schedule/__init__.py`.
- Tests: `tests/test_schedule_calendar.py`.
- No file I/O, no network, no clock reads. Every function takes and returns plain
  dicts/lists so another agent can persist them and tests can drive them directly.
  Persistence is **not** this module's job.

## Time model

- A **date** is a `'YYYY-MM-DD'` string; a **time** is whole minutes from local midnight
  (`0`..`1440`). No `tzinfo` is ever constructed.
- Times are wall-clock local, so behaviour is deterministic and testable everywhere.
- `parse_local_date` accepts strings, `date` and `datetime`; `parse_local_time` accepts
  minutes, `'16:00'`, `'4pm'`, `'4:30 pm'`, a bare hour (`'9'` = 09:00) and `'24:00'`.
- `format_spoken('2026-09-24', 960, 1020)` -> `'Thursday 24 September at 16:00 to 17:00'`
  for TTS output.

## The rules, and the bug each one prevents

1. **Overlap is half-open.** `ranges_overlap(a_start, a_end, b_start, b_end)` is
   `a_start < b_end and b_start < a_end`. **Touching is not a conflict**: an event ending
   15:00 and one starting 15:00 do not collide. Treating them as a collision is a real bug
   that makes correct bookings get refused, so the comparison is strict on both sides.
2. **A move excludes the event being moved** from its own conflict check. Otherwise the
   event collides with itself and every correct move is refused — even a 15-minute nudge.
3. **The current occurrence is excluded from the busy set** when scanning for
   alternatives, for the same reason: an event already in the schedule must not block its
   own slot.
4. **"The requested time is free" and "here is the nearest alternative" are ONE code
   path.** The scan starts at the requested time, so when that moment is free the first
   proposal *is* the requested time.
5. **Proposals never leave the allowed window** (default 08:00–21:00) **and never run past
   the horizon** (default 365 days). A scan that checks only the START would happily
   propose a slot that overruns the window end, so the whole span is checked:
   `start >= window_start and end <= window_end`.
6. **A gap must be long enough for the WHOLE event.** A 30-minute gap cannot host a
   90-minute booking.
7. **ON CONFLICT the event is NOT written** and the result carries the proposals instead.
   Atomic refusal with alternatives: never a partial write, never a silent overwrite.
8. **Recurrence** is supported (`daily`/`weekly` with a mandatory `until` date, since an
   unbounded rule would expand forever) plus **per-date cancellations** of a recurring
   event via `cancelled_dates`.

## Defaults

| Default | Value | Constant |
| --- | --- | --- |
| Event duration | 60 minutes | `DEFAULT_DURATION_MINUTES` |
| Proposal granularity | 15 minutes | `DEFAULT_GRAIN_MINUTES` |
| Proposal window | 08:00–21:00 | `DEFAULT_WINDOW` |
| Horizon | 365 days | `DEFAULT_HORIZON_DAYS` |

These are the *only* values invented for a missing field. Everything else missing or
contradictory raises a named error — never a silent default.

## Canonical event

```
{id, title, date, start, end, duration, recurrence, cancelled_dates, notes, reminder_minutes}
```

- `recurrence` is `None` or `{"freq": "daily"|"weekly", "until": "YYYY-MM-DD"}`.
- `cancelled_dates` is a sorted, de-duplicated list of dates.
- `notes` defaults to `""`; `reminder_minutes` (minutes before the start) defaults to
  `None`, meaning no reminder.
- `id` is preserved when supplied, otherwise derived deterministically from the content
  (so the same event always gets the same id, and tests never depend on randomness).
  `add_event` disambiguates against existing ids.
- Unknown event fields are refused (`UnknownEventFieldError`): silently ignoring `titel`
  is how a typo becomes a silently-defaulted event.

## API

| Function | Purpose |
| --- | --- |
| `parse_local_date` / `parse_local_time` | validate and canonicalise input |
| `format_spoken` | TTS sentence fragment |
| `weekday_of` / `add_days` / `days_between` | date arithmetic (Monday = 0) |
| `normalize_event` | raw dict -> canonical event, or a named error |
| `ranges_overlap` | the half-open overlap rule |
| `expand_event(event, from_date, to_date)` | concrete occurrences, recurrence + cancellations |
| `schedule_instances(schedule, from, to)` | every occurrence in a schedule, ordered |
| `find_conflicts(instances, start, end, *, exclude_id=None, date=None)` | colliding instances |
| `propose_alternatives(schedule_or_instances, event, days=7, *, window, grain, exclude_id)` | ranked free slots, nearest first |
| `add_event` / `move_event` / `cancel_event` | mutations returning a result dict |
| `list_events(schedule, from_date, to_date)` | expanded, ordered occurrences |
| `due_reminders(schedule, now)` / `describe_reminder` / `mark_reminder_fired` | reminders |
| `validate_schedule` | problems list (never raises) |
| `empty_schedule` / `SCHEDULE_VERSION` | container helpers |

`find_conflicts` accepts the query range as minutes, `'HH:MM'` strings, an occurrence dict,
or a `(date, start, end)` triple. When no date is given it compares against each instance's
own day, so callers spanning several days should pass `date=` or pre-filter.

## Result contract

`add_event` / `move_event` return:

```
{ok, action, event, conflicts, proposals, error, ...}
```

- `ok=True`: the schedule dict was mutated in place (identity preserved); `conflicts` and
  `proposals` are empty.
- `ok=False`: the schedule is **byte-for-byte unchanged**; `conflicts` lists the clashing
  occurrences, `proposals` the ranked alternatives, `error` a human sentence.

`cancel_event(schedule, event_id)` drops the whole event; with `date=` it records a
per-date cancellation. Naming a date the event never occurs on raises
`NotAnOccurrenceError` rather than quietly recording a cancellation that does nothing.

## Named errors

`ScheduleError` is the base; all are also `ValueError`/`LookupError` where apt:
`InvalidDateError`, `InvalidTimeError`, `InvalidEventError`,
`UnknownEventFieldError`, `RecurrenceError`, `InvalidScheduleError`,
`EventNotFoundError`, `NotAnOccurrenceError`, `InvalidWindowError`, `InvalidGrainError`.

Invalid input raises; a *conflict* is not an error but an `ok=False` result carrying
alternatives, because the caller needs the proposals to offer the user.

## Scope boundary

The allowed window applies to **proposals**, not to `add_event`: a user who explicitly
books 07:00 gets 07:00 if it is free. Any window policy belongs to the caller, which can
pass `window=` to `propose_alternatives`.
