# schedule — local-first calendar tool (R-SCHED-1)

`ScheduleTool` exposes Claire's own calendar (`claire.schedule.calendar` +
`claire.schedule.store`) to the reply loop as one builtin named `schedule`. It
handles five actions — `add`, `list`, `move`, `cancel`, `due` — with all date
and time arithmetic resolved inside the tool, never by the model.

## Purpose

Give Claire a deterministic, offline calendar she can write to and read back in
speech: book an event, list what is coming up, move one, cancel one, and read
the reminders that are due now. The tool is the boundary between natural
phrasing ("tomorrow at 4pm", "next week") and the pure, timezone-free calendar
logic (dates as `YYYY-MM-DD`, times as minutes from local midnight).

## Rules the tool guarantees

* **Overlap is half-open.** `rangesOverlap(aStart, aEnd, bStart, bEnd)` is
  `aStart < bEnd and bStart < aEnd`; touching is not a conflict, so an event
  ending 15:00 and one starting 15:00 do not collide. The tool never re-derives
  overlap — it asks `calendar.propose_alternatives` and treats the *first*
  proposal as the answer, so the rule lives in exactly one place.
* **Free and alternative are one code path.** The scan starts at the requested
  time, so a free request returns as `proposals[0] == requested slot`. Equal
  means write; different means refuse and offer the proposals. There is no
  second availability query that could disagree with the list.
* **A move excludes the event being moved** from its own conflict check (the
  matched event travels as `exclude`, and its id as `exclude_id`), or the event
  collides with itself and every correct move is refused.
* **The current occurrence is excluded from the busy set** when scanning, so a
  recurring event does not block its own next slot.
* **Proposals stay inside the window** (`08:00`–`21:00` by default) and inside
  the horizon (`365` days). The whole span is checked, so a 60-minute event at
  20:45 is never proposed. Gaps must fit the whole event, not just its start.
* **On conflict nothing is written.** The result carries the ranked free
  alternatives phrased for speech ("… is already taken, so I haven't booked
  anything. The nearest free times are …"). Refusal is atomic: no partial write
  and no silent overwrite. The calendar's own refusal (`{"ok": False, …}`) is
  honoured the same way, so a race cannot book a clashing slot.
* **Explicit requests are validated too.** A booking in the past, past the
  horizon, or outside the daily window is refused with a named error before
  anything is written.
* **Reminders are spoken once.** `due` reports the due reminders, then commits
  each one's key through the calendar so the next turn says "Nothing is due"
  instead of repeating itself. A reminder without a key is still reported.
* **A per-date cancellation of a one-off event is refused**, not silently
  ignored: the calendar records the date against a series, so pretending to
  cancel a single occurrence of a non-repeating event would leave it in the
  calendar while telling the user it was removed.
* **Defaults:** duration 60 minutes, proposal granularity 15 minutes, horizon
  365 days, window 08:00–21:00, list window 7 days, scan 7 days / 4 proposals.

## Arguments

| Parameter | Type | Notes |
| --- | --- | --- |
| `action` | string enum | `add`, `list`, `move`, `cancel`, `due` (required). |
| `title` | string | Required for `add`. |
| `when` | string | Day phrase: `today`, `tomorrow`, `thursday`, `next thursday`, `next week`, `2026-09-24`, `+3`, `24 September`, or a phrase carrying a time such as `tomorrow at 4pm`. For `list` it is the first day shown. |
| `time` | string | `16:00`, `4pm`, `4:30pm`, `9`, `noon`, `midnight`. Required for `add`/`move` unless `when` already carries one. A bare hour is read on the 24-hour clock (`9` = 09:00). A JSON number is read the same way when it can be an hour, and as minutes-from-midnight otherwise (`9` = 09:00, `960` = 16:00) — it is never silently ignored. |
| `duration_minutes` | integer | Default 60. `move` keeps the event's existing duration when omitted. |
| `event_id` | string | Event id or title, for `move`/`cancel`. |
| `date` | string | For `cancel`: the single `YYYY-MM-DD` occurrence of a repeating event to remove. |
| `days` | integer | For `list`: how many days ahead (default 7). |
| `repeat` | string enum | `daily` or `weekly`. |
| `until` | string | `YYYY-MM-DD` end date; required when `repeat` is given. |
| `reminder_minutes` | integer | For `add`: minutes before the event to remind, spoken when due. Set it for reminder requests (the owner's own words are read from the tool context); `0` means at the time the event starts; leave it out for a plain calendar entry with no reminder. |

Action aliases accepted: `create`/`book`/`schedule` for add, `agenda`/
`upcoming`/`whats_on` for list, `reschedule`/`shift` for move, `delete`/
`remove`/`drop` for cancel, `reminders`/`reminders_due` for due.

## Date and time resolution

Deterministic, no timezone library, no LLM arithmetic:

* `today` → today; `tomorrow` → today + 1; `next week` → today + 7; `+N` →
  today + N days; `YYYY-MM-DD` → that day (invalid dates raise).
* A weekday name means the next occurrence **strictly after today**, so
  `thursday` said on a Thursday means the following Thursday rather than
  silently booking today. `next thursday` is deliberately one week after that.
* `24 September` / `September 24` resolve to this year when still ahead,
  otherwise next year; a date that does not exist (30 February) raises.
* Clock forms: `HH:MM`, `H(am|pm)`, `H:MM(am|pm)`, bare `H` (24-hour, 0–23),
  `noon`, `midnight`. `12am` is 00:00 and `12pm` is 12:00.
* `format_spoken(date, start, duration)` produces the TTS convention
  `Thursday 24 September at 16:00` (plus `for 1 hour 30 minutes` when a
  duration is given); `format_clock` always renders 24-hour `HH:MM`.

## Errors

Every missing or invalid input raises the named `ScheduleToolError` with a
message that explains itself — an unreadable date, a missing time, a
non-positive duration, an unknown action, an event that cannot be found, a
past/out-of-window/past-horizon request, or an unavailable backend. `run`
converts it to `ToolExecutionResult(success=False, reply_text=None,
error_message=...)`, which the engine feeds back to the model as tool-error
text. No silent defaults, no invented times.

The calendar's and store's own named errors (`ScheduleError`,
`ScheduleStoreError` and their subclasses: a damaged file, an unknown event, a
date that is not an occurrence, an invalid window) are converted the same way
instead of escaping into the reply loop; anything else is re-raised.

A conflict is **not** an error: it returns `success=True` with a refusal and
the ranked alternatives, because the model must offer those to the user.

## Backend seam

The tool talks to an injected backend; the production adapter
`_ModuleBackend` is built lazily by `build_default_backend()` by importing
`claire.schedule.calendar` and `claire.schedule.store` (falling back to
`src.claire`, then to this module's own prefix), so this module imports and
unit-tests whether or not those modules exist yet.

The adapter knows no scheduling policy — only plumbing. The tool speaks
`(date, start, duration_minutes)` plus an event reference; the calendar speaks
a schedule document and canonical event dicts, and the mapping is:

| Tool protocol | Calendar / store call |
| --- | --- |
| `load()` | `store.load_schedule()` — a missing file is an empty schedule |
| `save(schedule)` | `store.save_schedule(schedule)` |
| `add(…)` | `calendar.add_event(schedule, raw)` with `title`, `date`, `start`, `duration`, `recurrence`, `reminder_minutes` |
| `move(…)` | `calendar.move_event(schedule, event_id, new_date, new_start, new_duration)` |
| `cancel(…)` | `calendar.cancel_event(schedule, event_id, date)` |
| `propose(…)` | `calendar.propose_alternatives(schedule, event, days, window, grain, exclude_id, horizon, limit)` |
| `list_events(…)` | `calendar.list_events(schedule, from_date, to_date)["occurrences"]` |
| `due(…)` | `calendar.due_reminders(schedule, now)` |
| `mark_fired(…)` | `calendar.mark_reminder_fired(schedule, key)` per reminder |

Event references (an id *or* a title) are resolved to the id an event dict
carries, since `move_event`/`cancel_event` take ids while users say titles.
`_invoke` passes the argument names a signature actually accepts, and
`_normalize_proposals` accepts dataclass-like objects, dicts, `(date, start)`
tuples and `"YYYY-MM-DD HH:MM"` strings, so a shape change fails loudly rather
than silently.

A missing calendar file is an empty calendar, not an error: `None` and
`FileNotFoundError` from `load_schedule` both mean "no events", so a first
booking is never answered with a filesystem complaint.

## Registration

`schedule` is added to `BUILTIN_TOOLS` in `src/claire/tools/registry.py` and
re-exported from `src/claire/tools/builtin/__init__.py`; both edits are purely
additive. Construction happens at import time and performs no I/O — the
backend resolves on first `run`.

## Tests

`tests/tools/builtin/test_schedule_tool.py` covers registration and schema,
every natural date/time phrasing, the `add`/`list`/`move`/`cancel`/`due` happy
paths against an injected in-memory fake, half-open touching behaviour,
self-exclusion on move, conflict refusal with ranked alternatives and an
unchanged store (no write), proposal-shape normalization, reminder commit,
per-date cancellation guards, and the named errors for missing/invalid input.

A second block drives the tool's adapter against the **delivered** calendar
module with an in-memory store stand-in — real overlap, scan, recurrence,
reminder and cancellation logic — and skips itself if those modules are absent.
No real files are read or written, and there is no network access anywhere.
