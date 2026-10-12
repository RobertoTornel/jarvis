"""Tests for the ``schedule`` builtin (R-SCHED-1).

Everything here runs against an injected in-memory fake backend — no real
calendar files, no network, and no dependency on ``claire.schedule.calendar``
or ``claire.schedule.store`` existing yet (the tool imports them lazily).

The fake implements the same rules the real calendar does, so these tests
exercise the tool's orchestration end to end: the tool decides "free" by asking
whether the request is the FIRST proposal, and refuses on conflict without
writing.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.claire.tools.base import ToolContext
from src.claire.tools.builtin import ScheduleTool as ScheduleToolFromPackage
from src.claire.tools.builtin.schedule import (
    DEFAULT_DURATION_MINUTES,
    DEFAULT_GRANULARITY_MINUTES,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_WINDOW_END_MINUTES,
    DEFAULT_WINDOW_START_MINUTES,
    FreeSlot,
    ScheduleTool,
    ScheduleToolError,
    build_default_backend,
    format_clock,
    format_spoken,
    parse_date_text,
    parse_time_of_day,
    resolve_when,
)
from src.claire.tools.registry import BUILTIN_TOOLS
from src.claire.tools.types import ToolExecutionResult

# The gate runs `pytest -m unit`; without this marker these tests are
# deselected, so they would pass while never running in CI.
pytestmark = pytest.mark.unit

# A Monday, so weekday phrases land on known dates:
#   thursday        -> 2026-09-24
#   next thursday   -> 2026-10-01
#   next week       -> 2026-09-28
TODAY = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, 9, 0)
# Alias so the fake's `date=` keyword (the protocol's parameter name) does not
# shadow the `date` class inside the method body.
_Date = date


# --- Test doubles -------------------------------------------------------------

class FakeBackend:
    """In-memory backend implementing the tool's protocol.

    ``load`` hands out copies and ``save`` is the only thing that persists, so
    "the store was not mutated" is a real assertion rather than a tautology.
    The proposal scan is half-open — touching is not a conflict — which is what
    lets the move test prove the event is excluded from its own conflict check.
    """

    def __init__(self, events=None, proposals=None, due_items=None):
        self.events = [dict(event) for event in (events or [])]
        self.proposals = proposals
        self.due_items = [dict(item) for item in (due_items or [])]
        self.save_calls = 0
        self.add_calls = []
        self.move_calls = []
        self.cancel_calls = []
        self.propose_calls = []
        self.list_calls = []
        self.due_now = None
        self.fired = []
        self._next_id = 1

    # -- persistence ---------------------------------------------------------
    def load(self):
        return [dict(event) for event in self.events]

    def save(self, events):
        self.save_calls += 1
        self.events = [dict(event) for event in events]

    # -- scheduling logic ----------------------------------------------------
    def propose(
        self,
        events,
        date=None,
        start=None,
        duration_minutes=DEFAULT_DURATION_MINUTES,
        window_start=DEFAULT_WINDOW_START_MINUTES,
        window_end=DEFAULT_WINDOW_END_MINUTES,
        granularity_minutes=DEFAULT_GRANULARITY_MINUTES,
        horizon_days=DEFAULT_HORIZON_DAYS,
        exclude=None,
        **kwargs,
    ):
        self.propose_calls.append(
            {
                "date": date,
                "start": start,
                "duration_minutes": duration_minutes,
                "window_start": window_start,
                "window_end": window_end,
                "granularity_minutes": granularity_minutes,
                "horizon_days": horizon_days,
                "exclude": exclude,
            }
        )
        if self.proposals is not None:
            return list(self.proposals)
        day = _Date.fromisoformat(date)
        found = []
        for offset in range(3):
            candidate_date = (day + timedelta(days=offset)).isoformat()
            busy = [
                (event["start"], event["start"] + event["duration_minutes"])
                for event in events
                if event is not exclude and event.get("date") == candidate_date
            ]
            candidate = start if offset == 0 else window_start
            while candidate + duration_minutes <= window_end:
                # HALF-OPEN: touching is not a conflict, so `candidate < b_end`
                # and `b_start < candidate_end` — never `<=`.
                clashes = any(
                    candidate < b_end and b_start < candidate + duration_minutes
                    for b_start, b_end in busy
                )
                if not clashes:
                    found.append(FreeSlot(candidate_date, candidate, duration_minutes))
                    break
                candidate += granularity_minutes
        return found

    def add(self, events, **fields):
        event = {
            "id": f"ev{self._next_id}",
            "title": fields.get("title"),
            "date": fields.get("date"),
            "start": fields.get("start"),
            "duration_minutes": fields.get("duration_minutes"),
            "repeat": fields.get("repeat"),
            "until": fields.get("until"),
        }
        self._next_id += 1
        self.add_calls.append(dict(fields))
        events.append(event)
        return event

    def move(self, events, event_ref=None, event=None, date=None, start=None, duration_minutes=None):
        self.move_calls.append(
            {
                "event_ref": event_ref,
                "event": event,
                "date": date,
                "start": start,
                "duration_minutes": duration_minutes,
            }
        )
        target = event
        if target is None:
            target = next(
                (item for item in events if item.get("title") == event_ref or str(item.get("id")) == str(event_ref)),
                None,
            )
        if target is None:
            raise ScheduleToolError(f"no event matching {event_ref}")
        target["date"] = date
        target["start"] = start
        target["duration_minutes"] = duration_minutes
        return target

    def cancel(self, events, event_ref=None, event=None, date=None):
        self.cancel_calls.append({"event_ref": event_ref, "event": event, "date": date})
        target = event
        if target is None:
            target = next(
                (item for item in events if item.get("title") == event_ref or str(item.get("id")) == str(event_ref)),
                None,
            )
        if target is None:
            raise ScheduleToolError(f"no event matching {event_ref}")
        if target not in events:
            return target
        if date is None:
            events.remove(target)
        return target

    def list_events(self, events, start_date=None, days=None):
        self.list_calls.append({"start_date": start_date, "days": days})
        return list(events)

    def due(self, events, now=None):
        self.due_now = now
        return list(self.due_items)

    def mark_fired(self, events, reminders):
        marked = 0
        for reminder in reminders:
            key = reminder.get("key")
            if isinstance(key, str) and key.strip():
                self.fired.append(key)
                marked += 1
        return marked


def make_tool(backend=None, **kwargs):
    kwargs.setdefault("today_provider", lambda: TODAY)
    kwargs.setdefault("now_provider", lambda: NOW)
    return ScheduleTool(backend=backend, **kwargs)


def make_context():
    context = Mock(spec=ToolContext)
    context.user_print = Mock()
    return context


def spoken_context(prompt, redacted=None):
    """A real ToolContext carrying the owner's own words, as the voice path
    hands them to a tool (``original_prompt``; ``redacted_text`` is the
    redacted copy of the same utterance)."""
    return ToolContext(
        db=None,
        cfg=None,
        system_prompt="",
        original_prompt=prompt,
        redacted_text=prompt if redacted is None else redacted,
        max_retries=0,
        user_print=lambda _message: None,
    )


def run_tool(tool, args, context=None):
    return tool.run(args, make_context() if context is None else context)


def event(title, day, start, duration=DEFAULT_DURATION_MINUTES, **extra):
    payload = {"title": title, "date": day, "start": start, "duration_minutes": duration}
    payload.update(extra)
    return payload


# --- Registration -------------------------------------------------------------

class TestRegistration:
    def test_tool_is_discoverable_by_the_registry(self):
        """The router must be able to select 'schedule'."""
        assert "schedule" in BUILTIN_TOOLS
        assert isinstance(BUILTIN_TOOLS["schedule"], ScheduleTool)

    def test_tool_is_exported_from_the_builtin_package(self):
        assert ScheduleToolFromPackage is ScheduleTool

    def test_tool_is_constructible_and_resolves_its_backend_lazily(self):
        """Construction happens at import time, so it must not touch the store."""
        tool = ScheduleTool()
        assert tool.name == "schedule"
        assert tool._backend is None  # resolved on first run, not on registration

    def test_tool_metadata_describes_conflicts_and_alternatives(self):
        tool = ScheduleTool()
        description = tool.description.lower()
        assert "calendar" in description
        assert "alternative" in description or "free" in description
        assert tool.inputSchema["type"] == "object"
        assert tool.inputSchema["required"] == ["action"]

    def test_tool_exposes_the_tool_interface_surface(self):
        tool = ScheduleTool()
        assert isinstance(tool.name, str)
        assert isinstance(tool.inputSchema, dict)
        assert callable(tool.run)
        assert callable(tool.execute)


# --- Parameter schema ---------------------------------------------------------

class TestParameterSchema:
    def setup_method(self):
        self.schema = ScheduleTool().inputSchema

    def test_action_enum_covers_every_action(self):
        assert self.schema["properties"]["action"]["enum"] == [
            "add",
            "list",
            "move",
            "cancel",
            "due",
        ]

    def test_every_documented_parameter_is_present(self):
        properties = self.schema["properties"]
        for name in (
            "action",
            "title",
            "when",
            "time",
            "duration_minutes",
            "event_id",
            "date",
            "days",
            "repeat",
            "until",
        ):
            assert name in properties, f"{name} missing from the schema"

    def test_parameter_types_match_what_the_tool_reads(self):
        properties = self.schema["properties"]
        assert properties["duration_minutes"]["type"] == "integer"
        assert properties["days"]["type"] == "integer"
        assert properties["repeat"]["enum"] == ["daily", "weekly"]
        assert properties["time"]["type"] == "string"
        assert properties["when"]["type"] == "string"

    def test_when_parameter_documents_the_accepted_phrasings(self):
        description = self.schema["properties"]["when"]["description"]
        for phrasing in ("today", "tomorrow", "thursday", "next week", "2026-09-24", "+3"):
            assert phrasing in description


# --- Natural date and time resolution ----------------------------------------

class TestNaturalWhenResolution:
    @pytest.mark.parametrize(
        "phrase,expected",
        [
            ("today", "2026-09-21"),
            ("tomorrow", "2026-09-22"),
            ("thursday", "2026-09-24"),        # strictly future, never today
            ("next thursday", "2026-10-01"),   # the week after the nearest one
            ("next week", "2026-09-28"),
            ("2026-09-24", "2026-09-24"),
            ("+3", "2026-09-24"),
            ("24 September", "2026-09-24"),
            ("September 24", "2026-09-24"),
        ],
    )
    def test_date_phrases_resolve_deterministically(self, phrase, expected):
        assert parse_date_text(phrase, TODAY).isoformat() == expected

    def test_weekday_said_on_that_weekday_means_the_next_one(self):
        assert parse_date_text("thursday", date(2026, 9, 24)) == date(2026, 10, 1)

    def test_month_day_in_the_past_rolls_to_next_year(self):
        assert parse_date_text("24 September", date(2026, 10, 1)) == date(2027, 9, 24)

    @pytest.mark.parametrize(
        "phrase,expected",
        [
            ("16:00", 960),
            ("4pm", 960),
            ("4 pm", 960),
            ("9", 540),
            ("9pm", 1260),
            ("4:30pm", 990),
            ("noon", 720),
            ("midnight", 0),
            ("12am", 0),
            ("12pm", 720),
        ],
    )
    def test_time_phrases_resolve_deterministically(self, phrase, expected):
        assert parse_time_of_day(phrase) == expected

    @pytest.mark.parametrize(
        "phrase,expected_day,expected_start",
        [
            ("tomorrow at 4pm", "2026-09-22", 960),
            ("thursday 16:00", "2026-09-24", 960),
            ("at 5pm", "2026-09-21", 1020),
            ("thursday", "2026-09-24", None),
            ("2026-09-24", "2026-09-24", None),
        ],
    )
    def test_combined_phrases_resolve_date_and_time_together(
        self, phrase, expected_day, expected_start
    ):
        day, start = resolve_when(phrase, TODAY)
        assert day.isoformat() == expected_day
        assert start == expected_start

    def test_format_spoken_matches_the_spoken_convention(self):
        assert format_spoken("2026-09-24", 16 * 60) == "Thursday 24 September at 16:00"

    def test_format_spoken_includes_duration_when_known(self):
        assert format_spoken("2026-09-24", 16 * 60, 90) == (
            "Thursday 24 September at 16:00 for 1 hour 30 minutes"
        )

    def test_format_clock_is_24_hour(self):
        assert format_clock(0) == "00:00"
        assert format_clock(16 * 60 + 30) == "16:30"

    @pytest.mark.parametrize("phrase", ["whenever", "", "   "])
    def test_unparseable_date_raises_a_named_error(self, phrase):
        with pytest.raises(ScheduleToolError) as excinfo:
            parse_date_text(phrase, TODAY)
        assert str(excinfo.value)

    @pytest.mark.parametrize("phrase", ["25:00", "13pm", "half past four", "", "99"])
    def test_unparseable_time_raises_a_named_error(self, phrase):
        with pytest.raises(ScheduleToolError) as excinfo:
            parse_time_of_day(phrase)
        message = str(excinfo.value).lower()
        assert "time" in message or "hour" in message

    def test_impossible_calendar_date_raises_a_named_error(self):
        with pytest.raises(ScheduleToolError):
            parse_date_text("2026-02-30", TODAY)


# --- Happy paths against an injected fake store -------------------------------

class TestAddHappyPath:
    def test_add_writes_one_event_resolved_from_natural_phrasing(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "thursday", "time": "16:00"},
        )
        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert result.error_message is None
        assert "Dentist" in result.reply_text
        assert "Thursday 24 September at 16:00" in result.reply_text
        assert backend.save_calls == 1
        assert len(backend.events) == 1
        assert backend.events[0]["date"] == "2026-09-24"
        assert backend.events[0]["start"] == 960
        assert backend.events[0]["duration_minutes"] == DEFAULT_DURATION_MINUTES

    def test_add_defaults_are_the_documented_ones(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        run_tool(tool, {"action": "add", "title": "Dentist", "time": "4pm"})
        assert backend.events[0]["start"] == 960
        assert backend.events[0]["date"] == TODAY.isoformat()  # 'at 4pm' means today
        assert backend.events[0]["duration_minutes"] == DEFAULT_DURATION_MINUTES
        scan = backend.propose_calls[0]
        assert scan["duration_minutes"] == DEFAULT_DURATION_MINUTES
        assert scan["granularity_minutes"] == DEFAULT_GRANULARITY_MINUTES == 15
        assert scan["horizon_days"] == DEFAULT_HORIZON_DAYS == 365
        assert scan["window_start"] == DEFAULT_WINDOW_START_MINUTES
        assert scan["window_end"] == DEFAULT_WINDOW_END_MINUTES

    def test_add_accepts_an_explicit_duration(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {
                "action": "add",
                "title": "Workshop",
                "when": "2026-09-24",
                "time": "16:00",
                "duration_minutes": "90",
            },
        )
        assert result.success is True
        assert backend.events[0]["duration_minutes"] == 90
        assert "1 hour 30 minutes" in result.reply_text

    def test_first_booking_on_a_missing_calendar_file_is_not_an_error(self):
        """An empty store means an empty calendar, not a failure."""
        backend = FakeBackend(events=[])
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "add", "title": "First", "time": "9am"})
        assert result.success is True
        assert result.error_message is None
        assert backend.events[0]["title"] == "First"

    def test_add_of_a_repeating_event_carries_repeat_and_until(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {
                "action": "add",
                "title": "Standup",
                "time": "9",
                "repeat": "weekly",
                "until": "+30",
            },
        )
        assert result.success is True
        assert backend.events[0]["repeat"] == "weekly"
        assert backend.events[0]["until"] == (TODAY + timedelta(days=30)).isoformat()
        assert "repeats weekly" in result.reply_text


# --- Reminder creation from the owner's words --------------------------------

class TestAddReminderFromTheOwnersWords:
    """A voice reminder booked with no offset is silently dead: the calendar
    treats ``reminder_minutes=None`` as "no reminder", so the model forgetting
    the field used to lose the owner's "remind me at 6" without a word. The
    owner's own words are on the tool context, and they decide."""

    def test_plain_calendar_entry_keeps_no_offset(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "thursday", "time": "16:00"},
            context=spoken_context("book the dentist on thursday at 16:00"),
        )
        assert result.success is True, result.error_message
        assert backend.add_calls[0]["reminder_minutes"] is None
        assert "remind" not in result.reply_text.lower()

    def test_plain_entry_outside_the_voice_path_keeps_no_offset(self):
        # Default test context carries no owner utterance at all.
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool, {"action": "add", "title": "Dentist", "when": "thursday", "time": "16:00"}
        )
        assert result.success is True, result.error_message
        assert backend.add_calls[0]["reminder_minutes"] is None

    def test_reminder_request_without_an_offset_fires_at_the_start(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Call mum", "when": "thursday", "time": "18:00"},
            context=spoken_context("remind me at 6 to call mum"),
        )
        assert result.success is True, result.error_message
        assert backend.add_calls[0]["reminder_minutes"] == 0
        assert "when it starts" in result.reply_text
        assert "0 minutes" not in result.reply_text

    def test_reminder_wording_on_redacted_text_also_fires_at_the_start(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Water plants", "when": "thursday", "time": "17:00"},
            context=spoken_context("", redacted="reminder: water the plants at 5"),
        )
        assert result.success is True, result.error_message
        assert backend.add_calls[0]["reminder_minutes"] == 0
        assert "when it starts" in result.reply_text

    def test_an_explicit_offset_from_the_model_is_kept_verbatim(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {
                "action": "add",
                "title": "Call mum",
                "when": "thursday",
                "time": "18:00",
                "reminder_minutes": 10,
            },
            context=spoken_context("remind me in good time to call mum"),
        )
        assert result.success is True, result.error_message
        assert backend.add_calls[0]["reminder_minutes"] == 10
        assert "10 minutes before" in result.reply_text

    def test_an_explicit_zero_also_reads_naturally(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {
                "action": "add",
                "title": "Call mum",
                "when": "thursday",
                "time": "18:00",
                "reminder_minutes": 0,
            },
        )
        assert result.success is True, result.error_message
        assert backend.add_calls[0]["reminder_minutes"] == 0
        assert "when it starts" in result.reply_text
        assert "0 minutes" not in result.reply_text

    def test_schema_tells_the_model_when_to_set_the_offset(self):
        text = ScheduleTool().inputSchema["properties"]["reminder_minutes"][
            "description"
        ].lower()
        assert "remind" in text
        assert "0" in text
        assert "starts" in text


class TestListHappyPath:
    def test_list_shows_upcoming_events_in_order(self):
        backend = FakeBackend(
            events=[
                event("Standup", "2026-09-25", 9 * 60),
                event("Dentist", "2026-09-24", 16 * 60),
            ]
        )
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "list"})
        assert result.success is True
        assert "2 events" in result.reply_text
        assert "Dentist" in result.reply_text and "Standup" in result.reply_text
        assert result.reply_text.index("Dentist") < result.reply_text.index("Standup")
        assert backend.save_calls == 0

    def test_list_of_an_empty_calendar_says_so_without_an_error(self):
        backend = FakeBackend(events=[])
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "list"})
        assert result.success is True
        assert result.error_message is None
        assert "Nothing is scheduled" in result.reply_text

    def test_list_honours_the_days_window(self):
        backend = FakeBackend(
            events=[
                event("Soon", "2026-09-22", 9 * 60),
                event("Far", "2026-10-20", 9 * 60),
            ]
        )
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "list", "days": 7})
        assert "Soon" in result.reply_text
        assert "Far" not in result.reply_text

    def test_list_can_start_from_a_natural_day(self):
        backend = FakeBackend(events=[event("Later", "2026-09-28", 9 * 60)])
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "list", "when": "next week"})
        assert "Later" in result.reply_text


class TestMoveHappyPath:
    def test_move_excludes_the_event_being_moved_from_its_own_conflict_check(self):
        """Moving an event onto its own current slot must not report a clash."""
        backend = FakeBackend(events=[event("Dentist", "2026-09-24", 16 * 60)])
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "move", "event_id": "Dentist", "when": "2026-09-24", "time": "16:00"},
        )
        assert result.success is True
        assert "Moved 'Dentist'" in result.reply_text
        assert backend.save_calls == 1
        # The proposal scan must have been told to ignore the event being moved.
        excluded = backend.propose_calls[0]["exclude"]
        assert excluded is not None and excluded["title"] == "Dentist"

    def test_move_to_a_slot_that_only_touches_the_next_event_is_allowed(self):
        """Half-open ranges: 15:00-16:00 does not collide with 16:00-17:00."""
        backend = FakeBackend(
            events=[
                event("Dentist", "2026-09-24", 16 * 60),
                event("Standup", "2026-09-24", 14 * 60),
            ]
        )
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "move", "event_id": "Dentist", "when": "2026-09-24", "time": "15:00"},
        )
        assert result.success is True, result.reply_text or result.error_message
        assert "Thursday 24 September at 15:00" in result.reply_text
        assert backend.save_calls == 1
        moved = next(ev for ev in backend.events if ev["title"] == "Dentist")
        assert moved["start"] == 15 * 60

    def test_move_keeps_the_existing_duration_when_none_is_given(self):
        backend = FakeBackend(events=[event("Workshop", "2026-09-24", 10 * 60, duration=90)])
        tool = make_tool(backend)
        run_tool(
            tool,
            {"action": "move", "event_id": "Workshop", "when": "2026-09-24", "time": "11:00"},
        )
        assert backend.move_calls[0]["duration_minutes"] == 90

    def test_move_conflict_writes_nothing_and_offers_alternatives(self):
        backend = FakeBackend(
            events=[
                event("Dentist", "2026-09-24", 16 * 60),
                event("Meeting", "2026-09-24", 17 * 60),
            ]
        )
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "move", "event_id": "Dentist", "when": "2026-09-24", "time": "17:00"},
        )
        assert result.success is True
        assert "already taken" in result.reply_text
        assert "18:00" in result.reply_text          # first free slot, found by the scan
        assert backend.save_calls == 0               # atomic refusal: nothing written
        assert backend.move_calls == []
        assert next(ev for ev in backend.events if ev["title"] == "Dentist")["start"] == 16 * 60


class TestCancelHappyPath:
    def test_cancel_removes_the_event(self):
        backend = FakeBackend(events=[event("Dentist", "2026-09-24", 16 * 60)])
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "cancel", "event_id": "Dentist"})
        assert result.success is True
        assert "Cancelled 'Dentist'" in result.reply_text
        assert backend.save_calls == 1
        assert backend.events == []

    def test_cancel_by_id_works_too(self):
        backend = FakeBackend(events=[event("Dentist", "2026-09-24", 16 * 60, id="ev7")])
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "cancel", "event_id": "ev7"})
        assert result.success is True
        assert backend.events == []

    def test_cancelling_one_occurrence_leaves_the_series_alone(self):
        backend = FakeBackend(
            events=[event("Standup", "2026-09-24", 9 * 60, repeat="weekly", until="2026-12-24")]
        )
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "cancel", "event_id": "Standup", "date": "2026-10-01"},
        )
        assert result.success is True
        assert "occurrence" in result.reply_text
        assert "series is unchanged" in result.reply_text
        assert backend.cancel_calls[0]["date"] == "2026-10-01"
        assert backend.events  # the recurring event itself survives


# --- Conflict: no write, ranked alternatives ----------------------------------

class TestConflictOffersAlternatives:
    def test_conflicting_add_writes_nothing(self):
        backend = FakeBackend(events=[event("Standup", "2026-09-24", 16 * 60)])
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "2026-09-24", "time": "16:00"},
        )
        assert result.success is True
        assert "already taken" in result.reply_text
        assert "nothing" in result.reply_text.lower() or "haven't booked" in result.reply_text
        assert backend.save_calls == 0
        assert backend.add_calls == []
        assert [ev["title"] for ev in backend.events] == ["Standup"]

    def test_conflicting_add_carries_the_ranked_free_alternatives(self):
        backend = FakeBackend(events=[event("Standup", "2026-09-24", 16 * 60)])
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "2026-09-24", "time": "16:00"},
        )
        # 17:00 is free because the busy event ENDS at 17:00 (half-open).
        assert "Thursday 24 September at 17:00" in result.reply_text
        assert "Would you like" in result.reply_text

    def test_alternatives_are_spoken_soonest_first(self):
        backend = FakeBackend(
            events=[event("Standup", "2026-09-24", 16 * 60)],
            proposals=[
                FreeSlot("2026-09-25", 9 * 60, 60),
                FreeSlot("2026-09-24", 18 * 60, 60),
                FreeSlot("2026-09-24", 17 * 60, 60),
            ],
        )
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "2026-09-24", "time": "16:00"},
        )
        text = result.reply_text
        assert text.index("17:00") < text.index("18:00") < text.index("25 September")

    def test_a_free_requested_slot_is_not_a_conflict(self):
        """The scan starts at the requested time, so a free request IS the first proposal."""
        backend = FakeBackend(
            events=[event("Standup", "2026-09-24", 16 * 60)],
            proposals=[FreeSlot("2026-09-24", 15 * 60, 60)],
        )
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "2026-09-24", "time": "15:00"},
        )
        assert result.success is True
        assert backend.save_calls == 1
        assert "on Thursday 24 September at 15:00" in result.reply_text

    def test_an_empty_scan_reports_no_alternative_instead_of_booking(self):
        backend = FakeBackend(events=[event("Standup", "2026-09-24", 16 * 60)], proposals=[])
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "2026-09-24", "time": "16:00"},
        )
        assert result.success is True
        assert "couldn't find a free slot" in result.reply_text
        assert backend.save_calls == 0


class TestProposalNormalization:
    def test_tuples_and_strings_from_a_scan_are_understood(self):
        backend = FakeBackend(
            events=[event("Standup", "2026-09-24", 16 * 60)],
            proposals=[("2026-09-24", 17 * 60), "2026-09-25 09:00"],
        )
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "2026-09-24", "time": "16:00"},
        )
        assert "17:00" in result.reply_text
        assert "Friday 25 September at 09:00" in result.reply_text

    def test_an_unreadable_slot_raises_instead_of_being_ignored(self):
        backend = FakeBackend(
            events=[event("Standup", "2026-09-24", 16 * 60)],
            proposals=[{"nonsense": True}],
        )
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "add", "title": "Dentist", "when": "2026-09-24", "time": "16:00"},
        )
        assert result.success is False
        assert "cannot read" in result.error_message
        assert backend.save_calls == 0


# --- Reminders ----------------------------------------------------------------

class TestDueReminders:
    def test_due_reminders_are_surfaced(self):
        backend = FakeBackend(
            due_items=[{"title": "Take pills", "date": "2026-09-21", "start": 9 * 60}]
        )
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "due"})
        assert result.success is True
        assert "1 reminder due now" in result.reply_text
        assert "Take pills" in result.reply_text
        assert "Monday 21 September at 09:00" in result.reply_text
        assert backend.due_now == NOW

    def test_reminders_alias_is_accepted(self):
        tool = make_tool(FakeBackend(due_items=[{"title": "Call mum"}]))
        result = run_tool(tool, {"action": "reminders"})
        assert result.success is True
        assert "Call mum" in result.reply_text

    def test_nothing_due_is_not_an_error(self):
        tool = make_tool(FakeBackend(due_items=[]))
        result = run_tool(tool, {"action": "due"})
        assert result.success is True
        assert result.error_message is None
        assert "Nothing is due" in result.reply_text

    def test_due_reminders_do_not_write_anything(self):
        backend = FakeBackend(due_items=[{"title": "Take pills"}])
        tool = make_tool(backend)
        run_tool(tool, {"action": "due"})
        assert backend.save_calls == 0


# --- Named errors for missing or invalid input --------------------------------

class TestNamedErrors:
    def test_missing_arguments_are_an_error_result(self):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, None)
        assert result.success is False
        assert result.reply_text is None
        assert "action" in result.error_message

    def test_missing_action_is_an_error_result(self):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, {})
        assert result.success is False
        assert "action" in result.error_message

    def test_unknown_action_lists_the_valid_ones(self):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, {"action": "teleport"})
        assert result.success is False
        assert "add, list, move, cancel, due" in result.error_message

    def test_add_without_a_title_is_an_error_result(self):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, {"action": "add", "time": "16:00"})
        assert result.success is False
        assert "title" in result.error_message

    def test_add_without_a_time_is_a_named_error_not_a_default(self):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, {"action": "add", "title": "Dentist", "when": "thursday"})
        assert result.success is False
        assert "needs a time" in result.error_message
        assert "guess" in result.error_message

    @pytest.mark.parametrize(
        "args,expected",
        [
            ({"action": "add", "title": "Past", "when": "2026-09-20", "time": "10:00"}, "past"),
            ({"action": "add", "title": "Late", "when": "2026-09-24", "time": "22:00"}, "window"),
            ({"action": "add", "title": "Early", "when": "2026-09-24", "time": "03:00"}, "window"),
            ({"action": "add", "title": "Overrun", "when": "2026-09-24", "time": "20:30", "duration_minutes": 90}, "window"),
            ({"action": "add", "title": "Far", "when": "+400", "time": "10:00"}, "horizon"),
            ({"action": "add", "title": "Bad", "time": "16:00", "duration_minutes": "soon"}, "duration"),
        ],
    )
    def test_out_of_range_requests_are_refused_with_an_explanation(self, args, expected):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, args)
        assert result.success is False
        assert expected in result.error_message

    def test_repeating_event_without_until_is_refused(self):
        tool = make_tool(FakeBackend())
        result = run_tool(
            tool, {"action": "add", "title": "Standup", "time": "9", "repeat": "weekly"}
        )
        assert result.success is False
        assert "until" in result.error_message

    def test_unknown_repeat_rule_is_refused(self):
        tool = make_tool(FakeBackend())
        result = run_tool(
            tool,
            {"action": "add", "title": "Standup", "time": "9", "repeat": "hourly", "until": "+10"},
        )
        assert result.success is False
        assert "daily" in result.error_message

    def test_move_of_an_unknown_event_is_refused(self):
        tool = make_tool(FakeBackend(events=[]))
        result = run_tool(
            tool, {"action": "move", "event_id": "Ghost", "when": "2026-09-24", "time": "10:00"}
        )
        assert result.success is False
        assert "couldn't find" in result.error_message

    def test_cancel_of_an_unknown_event_is_refused(self):
        tool = make_tool(FakeBackend(events=[]))
        result = run_tool(tool, {"action": "cancel", "event_id": "Ghost"})
        assert result.success is False
        assert "couldn't find" in result.error_message

    def test_move_without_an_event_reference_is_refused(self):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, {"action": "move", "when": "2026-09-24", "time": "10:00"})
        assert result.success is False
        assert "event_id" in result.error_message


# --- Backend seam -------------------------------------------------------------

class TestBackendSeam:
    def test_a_missing_calendar_file_loads_as_an_empty_calendar(self):
        """First booking: a missing file must not read as an error to the user."""
        from src.claire.tools.builtin.schedule import _ModuleBackend, _unwrap_events

        def raising_load():
            raise FileNotFoundError("schedule.json")

        backend = _ModuleBackend(
            SimpleNamespace(), SimpleNamespace(load_schedule=raising_load)
        )
        assert _unwrap_events(backend.load()) == []

        backend = _ModuleBackend(
            SimpleNamespace(), SimpleNamespace(load_schedule=lambda: None)
        )
        assert _unwrap_events(backend.load()) == []

        # When the store has an explicit empty schedule, that one is used.
        empty = {"events": [], "fired_reminders": []}
        store = SimpleNamespace(load_schedule=lambda: None, empty_schedule=lambda: empty)
        assert _ModuleBackend(SimpleNamespace(), store).load() == empty

    def test_an_unavailable_calendar_module_raises_a_named_error(self, monkeypatch):
        import src.claire.tools.builtin.schedule as schedule_module

        def exploding_import(name, package=None):
            raise ImportError(f"no module named {name}")

        monkeypatch.setattr(schedule_module.importlib, "import_module", exploding_import)
        with pytest.raises(ScheduleToolError) as excinfo:
            build_default_backend()
        assert "not available" in str(excinfo.value)

    def test_the_backend_is_used_for_every_read_and_write(self):
        backend = FakeBackend(events=[event("Dentist", "2026-09-24", 16 * 60)])
        tool = make_tool(backend)
        run_tool(tool, {"action": "list"})
        run_tool(tool, {"action": "add", "title": "Coffee", "when": "2026-09-25", "time": "10:00"})
        assert backend.list_calls
        assert backend.save_calls == 1


class TestReminderCommit:
    def test_reported_reminders_are_committed_so_they_are_spoken_once(self):
        backend = FakeBackend(
            due_items=[{"key": "ev1|2026-09-21", "title": "Take pills", "date": "2026-09-21", "start": 9 * 60}]
        )
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "due"})
        assert result.success is True
        assert backend.fired == ["ev1|2026-09-21"]
        assert backend.save_calls == 1

    def test_a_reminder_without_a_key_is_still_reported(self):
        backend = FakeBackend(due_items=[{"title": "Take pills"}])
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "due"})
        assert result.success is True
        assert "Take pills" in result.reply_text
        assert backend.fired == []
        assert backend.save_calls == 0

    def test_minutes_until_start_is_spoken(self):
        backend = FakeBackend(
            due_items=[
                {
                    "key": "ev1|2026-09-21",
                    "title": "Standup",
                    "date": "2026-09-21",
                    "start": 9 * 60,
                    "minutes_until_start": 1,
                }
            ]
        )
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "due"})
        assert "in 1 minute" in result.reply_text


class TestPerDateCancellationGuard:
    def test_a_date_on_a_one_off_event_is_refused_not_silently_ignored(self):
        backend = FakeBackend(events=[event("Dentist", "2026-09-24", 16 * 60)])
        tool = make_tool(backend)
        result = run_tool(
            tool,
            {"action": "cancel", "event_id": "Dentist", "date": "2026-09-24"},
        )
        assert result.success is False
        assert "not a repeating event" in result.error_message
        assert backend.cancel_calls == []
        assert backend.save_calls == 0
        assert [ev["title"] for ev in backend.events] == ["Dentist"]


# --- Integration with the delivered calendar module ---------------------------
# The calendar and store are injected with an in-memory store stand-in, so the
# real overlap/scan/recurrence logic runs through the tool without any file on
# disk. Skipped rather than failed if those modules are not present.

class TestRealCalendarWiring:
    def setup_method(self):
        self.calendar = pytest.importorskip("claire.schedule.calendar")
        self.store = pytest.importorskip("claire.schedule.store")
        from src.claire.tools.builtin.schedule import _ModuleBackend

        self.schedule = self.store.empty_schedule()
        self.saved = []
        backend = _ModuleBackend(
            self.calendar,
            SimpleNamespace(
                load_schedule=lambda *args, **kwargs: self.schedule,
                save_schedule=lambda schedule, *args, **kwargs: self.saved.append(schedule),
                empty_schedule=self.store.empty_schedule,
            ),
        )
        self.tool = ScheduleTool(
            backend=backend,
            today_provider=lambda: date(2026, 9, 24),
            now_provider=lambda: datetime(2026, 9, 24, 15, 50),
        )

    def test_add_then_conflict_uses_the_real_scan_and_writes_nothing(self):
        first = run_tool(
            self.tool,
            {"action": "add", "title": "Dentist", "when": "today", "time": "16:00"},
        )
        assert first.success is True, first.error_message
        assert len(self.saved) == 1
        second = run_tool(
            self.tool,
            {"action": "add", "title": "Standup", "when": "today", "time": "16:00"},
        )
        assert second.success is True
        assert "already taken" in second.reply_text
        assert "17:00" in second.reply_text
        assert len(self.saved) == 1  # atomic refusal: the store was not written
        assert [ev["title"] for ev in self.schedule["events"]] == ["Dentist"]

    def test_touching_ranges_do_not_collide_in_the_real_scan(self):
        run_tool(self.tool, {"action": "add", "title": "Standup", "when": "today", "time": "16:00"})
        result = run_tool(
            self.tool,
            {"action": "add", "title": "Dentist", "when": "today", "time": "15:00"},
        )
        assert result.success is True
        assert "Added 'Dentist'" in result.reply_text
        assert len(self.saved) == 2

    def test_move_onto_its_own_slot_is_allowed_by_the_real_calendar(self):
        run_tool(self.tool, {"action": "add", "title": "Dentist", "when": "today", "time": "16:00"})
        result = run_tool(
            self.tool,
            {"action": "move", "event_id": "Dentist", "when": "today", "time": "16:00"},
        )
        assert result.success is True, result.error_message
        assert "Moved 'Dentist'" in result.reply_text

    def test_list_expands_a_weekly_recurrence(self):
        run_tool(
            self.tool,
            {
                "action": "add",
                "title": "Weekly",
                "when": "today",
                "time": "10:00",
                "repeat": "weekly",
                "until": "2026-10-15",
            },
        )
        result = run_tool(self.tool, {"action": "list", "when": "today", "days": 21})
        assert result.reply_text.count("Weekly") == 3

    def test_due_reminder_is_spoken_once_and_committed(self):
        run_tool(
            self.tool,
            {
                "action": "add",
                "title": "Dentist",
                "when": "today",
                "time": "16:00",
                "reminder_minutes": 15,
            },
        )
        before = len(self.saved)
        first = run_tool(self.tool, {"action": "due"})
        assert "Dentist" in first.reply_text
        assert "in 10 minutes" in first.reply_text
        assert len(self.saved) == before + 1
        second = run_tool(self.tool, {"action": "due"})
        assert "Nothing is due" in second.reply_text

    def test_per_date_cancel_of_a_recurrence_removes_only_that_day(self):
        run_tool(
            self.tool,
            {
                "action": "add",
                "title": "Weekly",
                "when": "today",
                "time": "10:00",
                "repeat": "weekly",
                "until": "2026-10-15",
            },
        )
        result = run_tool(
            self.tool,
            {"action": "cancel", "event_id": "Weekly", "date": "2026-10-01"},
        )
        assert result.success is True, result.error_message
        assert "occurrence" in result.reply_text
        listing = run_tool(self.tool, {"action": "list", "when": "today", "days": 21})
        assert "1 October" not in listing.reply_text
        assert listing.reply_text.count("Weekly") == 2


class TestNumericTimeArguments:
    """A model that sends `time` as a JSON number must not be silently ignored."""

    def test_a_bare_hour_number_is_read_as_that_hour(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "add", "title": "Coffee", "time": 9})
        assert result.success is True
        assert backend.events[0]["start"] == 9 * 60

    def test_a_minute_count_beyond_the_hour_range_is_minutes_from_midnight(self):
        backend = FakeBackend()
        tool = make_tool(backend)
        result = run_tool(tool, {"action": "add", "title": "Coffee", "time": 960})
        assert result.success is True
        assert backend.events[0]["start"] == 16 * 60

    def test_a_nonsense_time_type_is_a_named_error(self):
        tool = make_tool(FakeBackend())
        result = run_tool(tool, {"action": "add", "title": "Coffee", "time": ["4pm"]})
        assert result.success is False
        assert "time of day" in result.error_message
