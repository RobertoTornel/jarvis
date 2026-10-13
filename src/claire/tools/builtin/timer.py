"""Countdown timer tool — the ``timer`` builtin.

The countdown model itself lives in :mod:`claire.schedule.timers` and is
complete and tested: ``TimerManager`` starts, cancels, lists, reports and fires
timers, all against an injectable monotonic clock.  What was missing was the
caller — nothing in ``src/`` ever imported it, so "Claire, set a timer for ten
minutes" had never worked even though every hard part was written.  A feature
with no caller does not exist.

This tool is that caller for the voice loop: one action per thing the owner can
say, with every duration parsed by the timer module (never by the model) and
every failure answered with a named explanation rather than a silent no-op.
The announcer loop in :mod:`claire.schedule.timer_announcer` is the other
caller, and it polls the *same* manager — see :func:`default_manager` — so a
timer the tool starts is a timer the loop will actually fire.

Two behaviours worth stating out loud:

* **A timer that is not running is not silently treated as running.**  A cancel
  for an unknown or ambiguous identifier raises :class:`TimerToolError` with the
  timer module's own message ("there is no timer called 'pasta' running"), so
  the model hears that nothing was cancelled.  Reporting success for a cancel
  that did not happen is exactly how a voice assistant becomes untrustworthy.
* **Timers do not survive a restart**, and the ``set`` result says so in a
  ``persistence_note`` field taken verbatim from
  ``TimerManager.PERSISTENCE_NOTE`` — the sentence the module docstring
  designates for the voice loop to read back.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...schedule.timers import (
    Timer,
    TimerError,
    TimerManager,
    _humanise,
    default_manager,
)
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

__all__ = ["TimerTool", "TimerToolError"]


class TimerToolError(Exception):
    """Named error for every timer failure the caller can fix.

    Raised for a missing or unknown action, a missing duration for ``set`` and
    an identifier that names no timer (or names several).  Each message explains
    the problem and usually the accepted form, because ``run`` passes it to the
    model as tool-error text.
    """


def _duration_words(seconds: float) -> str:
    """The timer module's own spoken form of a span, e.g. ``'25 minutes'``.

    Deliberately ``_humanise`` rather than a second formatter here: the
    confirmation must say "25 minutes" exactly the way the countdown and the
    module's own sentences do, or the same timer gets two different names
    depending on which sentence mentions it.
    """
    return _humanise(seconds)


def _sentence(text: str) -> str:
    """Capitalise the first letter and end with a full stop, without touching
    the rest of the string (``str.capitalize`` would lowercase a label)."""
    if not text:
        return text
    return text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")


class TimerTool(Tool):
    """Set, list, cancel and check countdown timers.

    Args:
        manager: The :class:`TimerManager` holding the live timers.  Defaults to
            the process-wide :func:`default_manager`, resolved on first use so
            the registry can build this tool at import time and still see a
            manager the daemon installs later.  Passing one explicitly is the
            injection seam the tests use.
    """

    def __init__(self, manager: Optional[TimerManager] = None) -> None:
        # Resolved lazily by ``_manager_or_default``: the registry builds every
        # builtin at import time, and a tool that captured the manager then
        # would not see one installed afterwards — which is the silent split
        # between the tool and the announcer this feature exists to avoid.
        self._manager = manager

    def _manager_or_default(self) -> TimerManager:
        manager = self._manager
        return manager if manager is not None else default_manager()

    # -- Tool interface ------------------------------------------------------
    @property
    def name(self) -> str:
        return "timer"

    @property
    def description(self) -> str:
        return (
            "Claire's own countdown timers. Use it when the user says 'set a "
            "timer for 25 minutes', 'what timers are running', 'how long is "
            "left', or 'cancel the pasta timer'. Durations are plain phrases "
            "('25 minutes', '90s', '1 hour 30 minutes') that Claire parses "
            "herself. Timers only exist while Claire is running and do not "
            "survive a restart; tell the user that when you set one."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "list", "cancel", "check"],
                    "description": (
                        "'set' starts a countdown timer, 'list' shows every "
                        "running timer, 'cancel' stops one, 'check' says how "
                        "much time is left on one."
                    ),
                },
                "duration": {
                    "type": ["string", "number"],
                    "description": (
                        "How long the timer should run, for example '25 minutes', "
                        "'90s' or '1 hour 30 minutes'. A bare number — '25' or a "
                        "JSON number 25 — means 25 minutes, because that is the "
                        "timer module's documented rule for a number with no "
                        "unit. Required for set (there is no default: a misheard "
                        "duration must be refused, not guessed)."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Optional name for the timer, for example 'pasta'. When "
                        "omitted the timer is auto-named 'timer 1', 'timer 2', "
                        "... Give a label when the user names one."
                    ),
                },
                "timer": {
                    "type": ["string", "integer"],
                    "description": (
                        "Which timer to cancel or check: its label ('pasta') or "
                        "its number (2). Omit for check to hear the soonest "
                        "timer; cancel always needs one."
                    ),
                },
            },
            "required": ["action"],
        }

    # -- dispatch ------------------------------------------------------------
    def run(
        self, args: Optional[Dict[str, Any]], context: ToolContext
    ) -> ToolExecutionResult:
        """Dispatch one action, converting named errors into tool-error results."""
        try:
            if not isinstance(args, dict):
                raise TimerToolError(
                    "timer needs a JSON object with an 'action' "
                    "(one of set, list, cancel, check)."
                )
            action = str(args.get("action") or "").strip().lower()
            if not action:
                raise TimerToolError(
                    "timer needs an 'action': one of set, list, cancel, check."
                )
            if action == "set":
                return self._action_set(args)
            if action == "list":
                return self._action_list(args)
            if action == "cancel":
                return self._action_cancel(args)
            if action == "check":
                return self._action_check(args)
            raise TimerToolError(
                f"'{action}' is not a timer action. Use one of: set, list, "
                "cancel, check."
            )
        except TimerToolError as exc:
            # The engine feeds error_message to the model as tool-error text, so
            # the message is written as an explanation the model can act on.
            return ToolExecutionResult(
                success=False, reply_text=None, error_message=str(exc)
            )
        except TimerError as exc:
            # A named timer-module failure (unknown/ambiguous timer, unreadable
            # duration). Its message is already written to be heard, so it is
            # passed through rather than reworded.
            return ToolExecutionResult(
                success=False, reply_text=None, error_message=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - a tool must answer, not crash the turn
            return ToolExecutionResult(
                success=False,
                reply_text=None,
                error_message=(
                    f"the timer failed ({type(exc).__name__}: {exc})."
                ),
            )

    # -- actions -------------------------------------------------------------
    def _action_set(self, args: Dict[str, Any]) -> ToolExecutionResult:
        duration = args.get("duration")
        if duration is None or (isinstance(duration, str) and not duration.strip()):
            raise TimerToolError(
                "timer set needs a 'duration', for example '25 minutes'."
            )
        label = args.get("label")
        if label is not None and not isinstance(label, str):
            raise TimerToolError("a timer label must be text, for example 'pasta'.")
        if isinstance(duration, bool):
            raise TimerToolError(
                f"{duration!r} is not a duration, for example '25 minutes'."
            )
        if isinstance(duration, (int, float)):
            # A JSON number 25 is the same request as the string '25': minutes.
            # parse_duration's numeric branch would read 25 as 25 *seconds*,
            # while its documented rule — and its string branch — says a bare
            # number means minutes.  The tool makes the two agree rather than
            # letting the model's choice of JSON type change how long the timer
            # runs.
            duration = str(duration)
        timer = self._manager_or_default().start(
            label=label.strip() if isinstance(label, str) and label.strip() else None,
            seconds=duration,
        )
        confirmation = f"Timer {timer.number} set for {_duration_words(timer.seconds)}."
        note = TimerManager.PERSISTENCE_NOTE
        return self._result(
            {
                "action": "set",
                # The model says this; the persistence note is attached to it so
                # the owner is told the timer will not survive a restart.
                "message": f"{confirmation} {note}",
                "spoken": confirmation,
                "persistence_note": note,
                "timer_number": timer.number,
                "label": timer.label,
                "seconds": timer.seconds,
            }
        )

    def _action_list(self, args: Dict[str, Any]) -> ToolExecutionResult:
        manager = self._manager_or_default()
        timers = manager.list_active()
        if not timers:
            return self._result(
                {
                    "action": "list",
                    "message": "No timers are running.",
                    "count": 0,
                    "timers": [],
                }
            )
        # Both the span and the sentence come back through the manager, never
        # through a clock this tool guessed: a timer started against an injected
        # clock would report nonsense against the real one.
        rows: List[Dict[str, Any]] = []
        parts: List[str] = []
        for timer in timers:
            remaining = manager.remaining(timer.number)
            spoken = manager.format_spoken(timer.number)
            rows.append(
                {
                    "timer_number": timer.number,
                    "label": timer.label,
                    "seconds": timer.seconds,
                    "remaining_seconds": remaining,
                    "remaining": spoken,
                }
            )
            left = "{} left".format(
                _duration_words(remaining if remaining is not None else 0.0)
            )
            if timer.label == f"timer {timer.number}":
                # The manager's sentence already names the timer ("timer 1 has 2
                # seconds left"); repeating the auto label beside the number
                # would read "timer 1 (timer 1)".
                parts.append(f"timer {timer.number}: {left}")
            else:
                parts.append(f"{timer.label} (timer {timer.number}): {left}")
        count = len(rows)
        return self._result(
            {
                "action": "list",
                "message": (
                    f"{count} timer{'s' if count != 1 else ''} running: "
                    + "; ".join(parts)
                    + "."
                ),
                "count": count,
                "timers": rows,
            }
        )

    def _action_cancel(self, args: Dict[str, Any]) -> ToolExecutionResult:
        identifier = args.get("timer")
        if identifier is None or (
            isinstance(identifier, str) and not identifier.strip()
        ):
            raise TimerToolError(
                "timer cancel needs a 'timer': the label or number of the timer "
                "to stop."
            )
        manager = self._manager_or_default()
        try:
            # Read the remaining span first: cancel() removes the timer, and the
            # manager refuses to report on a timer that no longer exists.
            remaining = manager.remaining(identifier)
            timer = manager.cancel(identifier)
        except TimerError as exc:
            # The module's own explanation ("there is no timer called 'pasta'
            # running") is the answer; a cancel that did not happen must not
            # come back as success.
            raise TimerToolError(str(exc)) from exc
        left = _duration_words(remaining if remaining is not None else 0.0)
        # "with 1 minute left" / "with no time left": no verb to disagree with
        # the number, unlike "1 minute were left".
        confirmation = (
            f"Cancelled {timer.label} (timer {timer.number}) "
            f"with {left} left."
        )
        return self._result(
            {
                "action": "cancel",
                "message": confirmation,
                "spoken": confirmation,
                "timer_number": timer.number,
                "label": timer.label,
                "seconds_left": remaining,
            }
        )

    def _action_check(self, args: Dict[str, Any]) -> ToolExecutionResult:
        identifier = args.get("timer")
        if (
            identifier is not None
            and isinstance(identifier, str)
            and not identifier.strip()
        ):
            identifier = None
        manager = self._manager_or_default()
        try:
            # No identifier means the soonest timer — the manager's own
            # documented "how long is left?" answer, including its "no timers
            # are running" sentence.
            spoken = manager.format_spoken(identifier)
        except TimerError as exc:
            raise TimerToolError(str(exc)) from exc
        if identifier is None and spoken == "no timers are running":
            return self._result(
                {
                    "action": "check",
                    "message": "No timers are running.",
                    "spoken": spoken,
                }
            )
        if identifier is not None:
            timer = self._identify(manager, identifier)
        else:
            timer = self._soonest(manager)
        payload: Dict[str, Any] = {
            "action": "check",
            "message": _sentence(spoken),
            "spoken": spoken,
        }
        if timer is not None:
            payload["timer_number"] = timer.number
            payload["label"] = timer.label
        return self._result(payload)

    @staticmethod
    def _identify(manager: TimerManager, identifier: object) -> Optional[Timer]:
        """The active timer a label or number names, or ``None``.

        Only used to fill in the result's number/label: the spoken answer and
        every failure already came from the manager, so a miss here is not an
        error.
        """
        text = str(identifier).strip()
        timers = manager.list_active()
        if text.isdigit():
            for timer in timers:
                if timer.number == int(text):
                    return timer
        wanted = text.casefold()
        for timer in timers:
            if timer.label.casefold() == wanted:
                return timer
        return None

    @staticmethod
    def _soonest(manager: TimerManager) -> Optional[Timer]:
        timers = manager.list_active()
        return timers[0] if timers else None

    # -- result shaping ------------------------------------------------------
    @staticmethod
    def _result(payload: Dict[str, Any]) -> ToolExecutionResult:
        """A success result whose ``reply_text`` is the structured payload.

        ``ToolExecutionResult`` has no room for extra fields, and the model reads
        ``reply_text`` as the tool's answer, so the fields the voice loop needs
        (``message``, ``persistence_note``, the timer's number/label/seconds) are
        serialised as one JSON object instead of being flattened into prose that
        the model would then have to re-parse.
        """
        return ToolExecutionResult(
            success=True,
            reply_text=json.dumps(payload, ensure_ascii=False),
        )
