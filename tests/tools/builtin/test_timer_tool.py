"""Tests for the ``timer`` builtin (R-SCHED-1 countdown timers).

The countdown model in ``claire.schedule.timers`` was complete and tested while
nothing in ``src/`` ever imported it, so "Claire, set a timer for ten minutes"
had never worked.  These tests pin the tool that is the missing caller:

* registration: the tool is reachable through the registry with a valid schema,
  because a tool the router cannot see is the same bug again;
* ``set`` relays ``TimerManager.PERSISTENCE_NOTE``, so the owner is told timers
  do not survive a restart;
* a cancel that did not happen (unknown or ambiguous identifier) fails instead
  of reporting success;
* every action names its failures rather than silently doing nothing.

Every test drives time through a manager on an injected fake clock, so nothing
here sleeps and the ordering/remaining assertions are exact.
"""

from __future__ import annotations

import json
import threading

import pytest

from claire.tools.base import ToolContext
from claire.tools.builtin import TimerTool as TimerToolFromPackage
from claire.tools.builtin.timer import TimerTool, TimerToolError
from claire.tools.registry import BUILTIN_TOOLS, generate_tools_json_schema
from claire.tools.types import ToolExecutionResult
from claire.schedule.timers import TimerManager, set_default_manager

# The gate runs `pytest -m unit`; without this marker these tests are deselected,
# so they would pass while never running in CI.
pytestmark = pytest.mark.unit


class FakeClock:
    """A monotonic clock under test control (the same one the manager uses)."""

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


@pytest.fixture(autouse=True)
def _clean_default_manager():
    """No timer may leak between tests through the process-wide manager."""
    set_default_manager(None)
    yield
    set_default_manager(None)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def manager(clock: FakeClock) -> TimerManager:
    return TimerManager(clock=clock)


@pytest.fixture()
def tool(manager: TimerManager) -> TimerTool:
    return TimerTool(manager=manager)


@pytest.fixture()
def context() -> ToolContext:
    return ToolContext(
        db=None,
        cfg=None,
        system_prompt="",
        original_prompt="set a timer",
        redacted_text="set a timer",
        max_retries=0,
        user_print=lambda _message: None,
    )


def run(tool: TimerTool, context: ToolContext, **args) -> ToolExecutionResult:
    return tool.run(args, context)


def payload(result: ToolExecutionResult) -> dict:
    """The JSON object a success result carries in ``reply_text``."""
    assert result.success is True, result.error_message
    assert result.reply_text is not None
    return json.loads(result.reply_text)


# --- registration -----------------------------------------------------------


def test_timer_is_reachable_through_the_registry_with_a_valid_schema():
    """The router can only select what the registry actually holds."""
    assert "timer" in BUILTIN_TOOLS
    assert isinstance(BUILTIN_TOOLS["timer"], TimerTool)
    assert BUILTIN_TOOLS["timer"].name == "timer"

    schema = BUILTIN_TOOLS["timer"].inputSchema
    assert schema["type"] == "object"
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"] == [
        "set",
        "list",
        "cancel",
        "check",
    ]
    assert set(schema["properties"]) == {"action", "duration", "label", "timer"}
    assert schema["properties"]["duration"]["description"].strip()
    assert (
        "required for set"
        in schema["properties"]["duration"]["description"].lower()
    )


def test_the_registry_lists_timer_as_a_function_definition():
    """The OpenAI-compatible catalogue the model is given must include it."""
    definitions = {
        entry["function"]["name"]: entry["function"]
        for entry in generate_tools_json_schema()
    }
    assert "timer" in definitions
    assert definitions["timer"]["description"].strip()
    assert definitions["timer"]["parameters"]["required"] == ["action"]


def test_timer_tool_is_exported_from_the_builtin_package():
    assert TimerToolFromPackage is TimerTool


# --- set --------------------------------------------------------------------


def test_set_without_a_label_auto_numbers_and_confirms(tool, manager, context):
    result = run(tool, context, action="set", duration="25 minutes")
    body = payload(result)

    assert body["timer_number"] == 1
    assert body["label"] == "timer 1"
    assert body["seconds"] == 1500.0
    assert body["spoken"] == "Timer 1 set for 25 minutes."
    assert manager.active_count() == 1


def test_set_with_a_label_keeps_the_owners_word(tool, context):
    body = payload(run(tool, context, action="set", duration="90s", label="pasta"))

    assert body["label"] == "pasta"
    assert body["seconds"] == 90.0
    assert body["spoken"] == "Timer 1 set for 1 minute."


def test_set_relays_the_persistence_note(tool, context):
    """The owner must be told a timer does not survive a restart."""
    body = payload(run(tool, context, action="set", duration="10 minutes"))

    assert body["persistence_note"] == TimerManager.PERSISTENCE_NOTE
    assert TimerManager.PERSISTENCE_NOTE in body["message"]
    assert "do not survive a restart" in body["message"]


def test_set_accepts_a_bare_number_of_minutes(tool, context):
    body = payload(run(tool, context, action="set", duration=25))

    assert body["seconds"] == 1500.0


def test_set_without_a_duration_names_the_problem(tool, manager, context):
    result = run(tool, context, action="set")

    assert result.success is False
    assert result.reply_text is None
    assert "duration" in (result.error_message or "")
    assert manager.active_count() == 0


def test_set_with_a_blank_duration_names_the_problem(tool, context):
    result = run(tool, context, action="set", duration="   ")

    assert result.success is False
    assert "duration" in (result.error_message or "")


def test_set_with_an_unreadable_duration_fails_instead_of_guessing(tool, manager, context):
    result = run(tool, context, action="set", duration="10 fortnights")

    assert result.success is False
    assert "fortnights" in (result.error_message or "")
    assert manager.active_count() == 0


# --- list -------------------------------------------------------------------


def test_list_says_plainly_when_none_are_running(tool, context):
    body = payload(run(tool, context, action="list"))

    assert body["count"] == 0
    assert body["timers"] == []
    assert body["message"] == "No timers are running."


def test_list_is_ordered_soonest_deadline_first(tool, clock, context):
    run(tool, context, action="set", duration="10 minutes", label="long")
    run(tool, context, action="set", duration="2 minutes", label="short")

    body = payload(run(tool, context, action="list"))

    assert body["count"] == 2
    assert [row["label"] for row in body["timers"]] == ["short", "long"]
    assert [row["timer_number"] for row in body["timers"]] == [2, 1]
    assert body["timers"][0]["remaining_seconds"] == pytest.approx(120.0)
    assert "short" in body["message"]


def test_list_reports_the_remaining_span_from_the_managers_clock(tool, clock, context):
    run(tool, context, action="set", duration="10 minutes", label="tea")
    clock.advance(60)

    body = payload(run(tool, context, action="list"))

    row = body["timers"][0]
    assert row["remaining_seconds"] == pytest.approx(540.0)
    assert "9 minutes" in row["remaining"]


# --- cancel -----------------------------------------------------------------


def test_cancel_by_label_confirms_and_removes(tool, manager, context):
    run(tool, context, action="set", duration="10 minutes", label="pasta")
    assert manager.active_count() == 1

    body = payload(run(tool, context, action="cancel", timer="pasta"))

    assert body["timer_number"] == 1
    assert body["label"] == "pasta"
    assert body["seconds_left"] == pytest.approx(600.0)
    assert "Cancelled pasta" in body["message"]
    assert manager.active_count() == 0


def test_cancel_by_number_confirms_and_removes(tool, manager, context):
    run(tool, context, action="set", duration="10 minutes", label="pasta")

    body = payload(run(tool, context, action="cancel", timer=1))

    assert body["label"] == "pasta"
    assert manager.active_count() == 0


def test_cancel_reports_the_time_that_was_left(tool, clock, context):
    run(tool, context, action="set", duration="10 minutes", label="pasta")
    clock.advance(120)

    body = payload(run(tool, context, action="cancel", timer="pasta"))

    assert body["seconds_left"] == pytest.approx(480.0)
    assert "8 minutes" in body["message"]


def test_cancel_of_an_unknown_timer_fails_and_keeps_the_real_one(tool, manager, context):
    """No success body for a cancel that did not happen."""
    run(tool, context, action="set", duration="10 minutes", label="pasta")

    result = run(tool, context, action="cancel", timer="soup")

    assert result.success is False
    assert result.reply_text is None
    assert "soup" in (result.error_message or "")
    assert manager.active_count() == 1


def test_cancel_with_an_ambiguous_label_fails(tool, manager, context):
    """Two timers with one label must not silently cancel one of them."""
    run(tool, context, action="set", duration="10 minutes", label="tea")
    run(tool, context, action="set", duration="20 minutes", label="tea")

    result = run(tool, context, action="cancel", timer="tea")

    assert result.success is False
    assert "matches 2 timers" in (result.error_message or "")
    assert manager.active_count() == 2


def test_cancel_without_an_identifier_names_the_problem(tool, context):
    run(tool, context, action="set", duration="10 minutes", label="pasta")

    result = run(tool, context, action="cancel")

    assert result.success is False
    assert "timer" in (result.error_message or "").lower()


# --- check ------------------------------------------------------------------


def test_check_without_an_identifier_reports_the_soonest(tool, clock, context):
    run(tool, context, action="set", duration="10 minutes", label="long")
    run(tool, context, action="set", duration="2 minutes", label="short")

    body = payload(run(tool, context, action="check"))

    assert body["timer_number"] == 2
    assert body["label"] == "short"
    assert "short has" in body["spoken"]
    assert "2 minutes" in body["spoken"]


def test_check_by_label_reports_that_timer(tool, context):
    run(tool, context, action="set", duration="10 minutes", label="pasta")

    body = payload(run(tool, context, action="check", timer="pasta"))

    assert body["timer_number"] == 1
    assert body["spoken"] == "pasta has 10 minutes left"
    assert body["message"] == "Pasta has 10 minutes left."


def test_check_says_plainly_when_none_are_running(tool, context):
    body = payload(run(tool, context, action="check"))

    assert body["message"] == "No timers are running."


def test_check_of_an_unknown_timer_fails(tool, context):
    result = run(tool, context, action="check", timer="soup")

    assert result.success is False
    assert "soup" in (result.error_message or "")


# --- dispatch errors --------------------------------------------------------


def test_unknown_action_is_a_named_error(tool, context):
    result = run(tool, context, action="explode")

    assert result.success is False
    assert "not a timer action" in (result.error_message or "")
    assert "set, list, cancel, check" in (result.error_message or "")


def test_a_missing_action_is_a_named_error(tool, context):
    result = run(tool, context)

    assert result.success is False
    assert "action" in (result.error_message or "")


def test_a_non_object_argument_is_a_named_error(manager, context):
    tool = TimerTool(manager=manager)

    result = tool.run(["set"], context)  # type: ignore[arg-type]

    assert result.success is False
    assert "JSON object" in (result.error_message or "")


# --- the injection seam -----------------------------------------------------


def test_tool_uses_the_process_wide_manager_when_none_is_injected(manager):
    set_default_manager(manager)
    tool = TimerTool()

    assert tool._manager_or_default() is manager


def test_timer_tool_error_is_the_named_error_type():
    assert issubclass(TimerToolError, Exception)
