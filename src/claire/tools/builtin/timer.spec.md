# timer — countdown timers (R-SCHED-1)

`TimerTool` exposes Claire's countdown model
(`claire.schedule.timers.TimerManager`) to the reply loop as one builtin named
`timer`. It handles four actions — `set`, `list`, `cancel`, `check` — with every
duration parsed inside the tool (by the timer module), never by the model.

## Purpose

Make "Claire, set a timer for ten minutes" actually work. The countdown model
was complete and tested while nothing in `src/` ever imported it: the feature
had no caller, so it did not exist. This tool is that caller for the voice loop,
and `claire.schedule.timer_announcer` is the caller that fires the timers and
speaks them.

Timers are **not** calendar events. A timer has no start/end/repeat and no diary
entry: it is a deadline measured on the monotonic clock, and it deliberately
does not survive a restart (a timer that outlived the process would announce a
deadline that passed while Claire was off). The module docstring in
`claire/schedule/timers.py` explains the full rationale.

## Rules the tool guarantees

* **Durations are parsed, never guessed.** `set` hands the duration to
  `parse_duration`, which accepts `'90s'`, `'1h30m'`, `'25 minutes'`,
  `'1 hour 30 minutes'` and a bare number meaning minutes. A missing or
  unreadable duration raises `TimerToolError`; there is no default, because a
  misheard "twenty" that silently became ten minutes fires at a time the owner
  never asked for.
* **The confirmation speaks the manager's own words.** The duration words in
  "Timer 1 set for 25 minutes." come from the timer module's formatter, so the
  same span is never called two different things by two code paths.
* **The tool and the announcer share one manager.** Both use
  `default_manager()`, so a timer started here is a timer the announcer loop
  polls. Two managers would look like a working feature while behaving like a
  broken one.
* **Timers do not persist, and the result says so.** Every `set` result carries
  `persistence_note` (`TimerManager.PERSISTENCE_NOTE`, verbatim) and a
  human-readable `message` that includes it, so the model can tell the owner the
  timer will not survive a restart.
* **A cancel that did not happen is not reported as success.** An unknown or
  ambiguous label/number raises `TimerToolError` carrying the timer module's own
  message ("there is no timer called 'pasta' running"), which `run` turns into
  `success=False`. Silently answering "cancelled" for a timer that is still
  running is exactly how a voice assistant becomes untrustworthy.
* **Ordering is deterministic.** `list` returns active timers soonest deadline
  first, ties broken by timer number — the order `list_active()` already
  guarantees.
* **No silent no-ops anywhere.** An unknown action, a missing `set` duration and
  a missing `cancel` identifier are all named errors, never a cheerful empty
  answer.

## Arguments

| Parameter | Type | Notes |
| --- | --- | --- |
| `action` | string enum | `set`, `list`, `cancel`, `check` (required). |
| `duration` | string or number | For `set` (required): `'25 minutes'`, `'90s'`, `'1 hour 30 minutes'`, `'1.5h'`; a bare number means minutes. |
| `label` | string | Optional name to give the timer (`'pasta'`). Without one the manager auto-names it `timer 1`, `timer 2`, … |
| `timer` | string or integer | For `cancel` (required) and `check`: the label (`'pasta'`) or the number (`2`). Omit for `check` to hear the soonest timer. |

## Results

`run` returns `ToolExecutionResult`. Success results carry a JSON object in
`reply_text` (the model reads `reply_text` as the tool's answer), because the
dataclass has no room for extra fields:

* `set` → `message`, `spoken`, `persistence_note`, `timer_number`, `label`,
  `seconds`.
* `list` → `message`, `count`, `timers` (each with `timer_number`, `label`,
  `seconds`, `remaining_seconds`, `remaining`).
* `cancel` → `message`, `spoken`, `timer_number`, `label`, `seconds_left`.
* `check` → `message`, `spoken`, and `timer_number`/`label` when one resolved.

A failure result is `ToolExecutionResult(success=False, reply_text=None,
error_message=…)`, which the engine feeds back to the model as tool-error text.

## Errors

`TimerToolError` is raised for a missing or unknown action, a missing `set`
duration, a missing `cancel` identifier, and any identifier the manager cannot
resolve. The timer module's own named errors (`InvalidDurationError`,
`UnknownTimerError`, `AmbiguousTimerError`, `TimerError`) are converted to
`success=False` with their message intact; anything else is reported as a named
failure rather than escaping into the reply loop.

## Registration

`timer` is added to `BUILTIN_TOOLS` in `src/claire/tools/registry.py` and
re-exported from `src/claire/tools/builtin/__init__.py`; both edits are purely
additive and follow exactly how `schedule` is registered. Construction happens
at import time and touches no filesystem; the manager resolves lazily on first
use, so a manager installed after import is still the one the tool writes to.

## Tests

`tests/tools/builtin/test_timer_tool.py` covers registration and schema, `set`
(with and without a label, and the relayed `PERSISTENCE_NOTE`), `list` (empty
and populated, ordered), `cancel` (by label and by number, plus unknown and
ambiguous identifiers failing rather than reporting success), `check`, unknown
action and missing duration — all against an injected `TimerManager` on an
injected fake clock, so nothing sleeps.

`tests/test_timer_announcer.py` covers the loop that fires and speaks the
timers: spoken exactly once, the printed sentence, two timers both spoken, a
busy speech check consuming nothing, false/raising speech leaving the timer
pending for a later poll, a raising manager survived, prompt start/stop, the
daemon's builder/speak adapter with a fake engine, and the shared
`default_manager()` identity.
