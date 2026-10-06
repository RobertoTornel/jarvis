"""Tests for the pure local-first calendar logic (R-SCHED-1).

Every rule in ``src/claire/schedule/calendar.py`` is pinned here, and several
tests exist specifically to catch the two classic bugs the rules guard against:
touching ranges being treated as a collision, and a MOVE colliding with the very
event being moved.

The module is pure, so nothing here touches the disk, a network, or a clock
beyond values constructed inside the test.
"""

import copy
from datetime import date, datetime

import pytest

from claire.schedule import calendar as cal

# The gate runs `pytest -m unit`; without this marker these tests are
# deselected, so they would pass while never running in CI.
pytestmark = pytest.mark.unit

# 2026-09-24 is a Thursday -- pinned so the spoken-format expectations are real.
THURSDAY = "2026-09-24"
FRIDAY = "2026-09-25"
DATE_FMT = "YYYY-MM-DD"


def instance(start, end, *, day=THURSDAY, event_id="busy-1", title="Busy"):
    """A concrete occurrence dict, the shape find_conflicts() consumes."""
    return {
        "event_id": event_id,
        "title": title,
        "date": day,
        "start": start,
        "end": end,
        "duration": end - start,
    }


# ---------------------------------------------------------------------------
# parse_local_date / parse_local_time
# ---------------------------------------------------------------------------


class TestParseLocalDate:
    def test_canonical_string_is_returned(self):
        assert cal.parse_local_date("2026-09-24") == "2026-09-24"

    def test_single_digit_month_and_day_are_padded(self):
        assert cal.parse_local_date("2026-9-4") == "2026-09-04"

    def test_accepts_date_object(self):
        assert cal.parse_local_date(date(2026, 9, 24)) == THURSDAY

    def test_accepts_datetime_object(self):
        assert cal.parse_local_date(datetime(2026, 9, 24, 16, 0)) == THURSDAY

    @pytest.mark.parametrize("bad", ["", "tomorrow", "24/09/2026", "2026-13-01", "2026-02-30"])
    def test_nonsense_raises_named_error(self, bad):
        with pytest.raises(cal.InvalidDateError):
            cal.parse_local_date(bad)

    @pytest.mark.parametrize("bad", [None, 20260924, 3.5])
    def test_non_string_raises_named_error(self, bad):
        with pytest.raises(cal.InvalidDateError):
            cal.parse_local_date(bad)


class TestParseLocalTime:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, 0),
            (960, 960),
            (1440, 1440),
            ("16:00", 960),
            ("9:05", 545),
            ("16", 960),
            ("9", 540),
            ("4pm", 960),
            ("4PM", 960),
            ("4:30pm", 990),
            ("12am", 0),
            ("12pm", 720),
            ("24:00", 1440),
        ],
    )
    def test_understood_forms(self, value, expected):
        assert cal.parse_local_time(value) == expected

    @pytest.mark.parametrize("bad", ["", "quarter past four", "25:00", "16:75", "13pm", "0pm"])
    def test_nonsense_raises_named_error(self, bad):
        with pytest.raises(cal.InvalidTimeError):
            cal.parse_local_time(bad)

    @pytest.mark.parametrize("bad", [None, 1.5, True])
    def test_wrong_type_raises_named_error(self, bad):
        with pytest.raises(cal.InvalidTimeError):
            cal.parse_local_time(bad)

    def test_beyond_midnight_raises_named_error(self):
        with pytest.raises(cal.InvalidTimeError):
            cal.parse_local_time(1441)


class TestDayArithmetic:
    def test_weekday_of_monday_is_zero(self):
        assert cal.weekday_of("2026-09-21") == 0

    def test_weekday_of_thursday_is_three(self):
        assert cal.weekday_of(THURSDAY) == 3

    def test_weekday_of_nonsense_raises(self):
        with pytest.raises(cal.InvalidDateError):
            cal.weekday_of("not-a-day")

    def test_add_days_crosses_month_and_year(self):
        assert cal.add_days("2026-09-30", 3) == "2026-10-03"
        assert cal.add_days("2026-12-31", 1) == "2027-01-01"

    def test_add_days_negative_goes_backwards(self):
        assert cal.add_days("2026-01-01", -1) == "2025-12-31"

    def test_add_days_rejects_non_int(self):
        with pytest.raises(cal.InvalidDateError):
            cal.add_days(THURSDAY, "soon")

    def test_days_between(self):
        assert cal.days_between(THURSDAY, FRIDAY) == 1
        assert cal.days_between(FRIDAY, THURSDAY) == -1
        assert cal.days_between(THURSDAY, THURSDAY) == 0


class TestFormatSpoken:
    def test_day_and_start(self):
        assert cal.format_spoken(THURSDAY, 16 * 60) == "Thursday 24 September at 16:00"

    def test_day_with_range(self):
        assert (
            cal.format_spoken(THURSDAY, "16:00", "17:00")
            == "Thursday 24 September at 16:00 to 17:00"
        )

    def test_day_only(self):
        assert cal.format_spoken(THURSDAY) == "Thursday 24 September"

    def test_friday(self):
        assert cal.format_spoken(FRIDAY, "9:05") == "Friday 25 September at 09:05"

    def test_end_without_start_raises(self):
        with pytest.raises(cal.InvalidTimeError):
            cal.format_spoken(THURSDAY, None, 17 * 60)

    def test_end_before_start_raises(self):
        with pytest.raises(cal.InvalidTimeError):
            cal.format_spoken(THURSDAY, "17:00", "16:00")


# ---------------------------------------------------------------------------
# normalize_event
# ---------------------------------------------------------------------------


class TestNormalizeEvent:
    def test_canonical_shape(self):
        event = cal.normalize_event(
            {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        assert event["title"] == "Haircut"
        assert event["date"] == THURSDAY
        assert event["start"] == 960
        assert event["end"] == 1020
        assert event["duration"] == 60
        assert event["recurrence"] is None
        assert event["cancelled_dates"] == []
        assert event["notes"] == ""
        assert event["reminder_minutes"] is None
        assert isinstance(event["id"], str) and event["id"]

    def test_default_duration_is_one_hour(self):
        event = cal.normalize_event({"title": "Lunch", "date": THURSDAY, "start": "12:00"})
        assert event["duration"] == cal.DEFAULT_DURATION_MINUTES == 60
        assert event["end"] == 780

    def test_explicit_duration_is_used(self):
        event = cal.normalize_event(
            {"title": "Deep work", "date": THURSDAY, "start": "09:00", "duration": 90}
        )
        assert event["end"] == 9 * 60 + 90

    def test_start_derived_from_end_and_duration(self):
        event = cal.normalize_event(
            {"title": "Deep work", "date": THURSDAY, "end": "10:30", "duration": 90}
        )
        assert event["start"] == 9 * 60

    def test_id_and_notes_are_preserved(self):
        event = cal.normalize_event(
            {"id": "evt-1", "title": "Haircut", "date": THURSDAY, "start": 960, "notes": "ask for Sam"}
        )
        assert event["id"] == "evt-1"
        assert event["notes"] == "ask for Sam"

    def test_generated_id_is_deterministic(self):
        raw = {"title": "Haircut", "date": THURSDAY, "start": 960}
        assert cal.normalize_event(raw)["id"] == cal.normalize_event(raw)["id"]

    def test_normalize_is_idempotent(self):
        once = cal.normalize_event(
            {
                "title": "Standup",
                "date": THURSDAY,
                "start": "09:00",
                "end": "09:15",
                "recurrence": {"freq": "weekly", "until": "2026-10-15"},
                "cancelled_dates": ["2026-10-01"],
                "notes": "zoom",
            }
        )
        assert cal.normalize_event(once) == once

    def test_cancelled_dates_are_parsed_sorted_and_deduped(self):
        event = cal.normalize_event(
            {
                "title": "Standup",
                "date": THURSDAY,
                "start": "09:00",
                "cancelled_dates": ["2026-10-01", "2026-9-24", "2026-10-01"],
            }
        )
        assert event["cancelled_dates"] == ["2026-09-24", "2026-10-01"]

    @pytest.mark.parametrize(
        "raw",
        [
            {},
            {"title": "No date", "start": "09:00"},
            {"title": "", "date": THURSDAY, "start": "09:00"},
            {"title": "   ", "date": THURSDAY, "start": "09:00"},
            {"date": THURSDAY, "start": "09:00"},
            {"title": "No time", "date": THURSDAY},
            {"title": "Bad date", "date": "soon", "start": "09:00"},
            {"title": "Bad time", "date": THURSDAY, "start": "soon"},
            {"title": "No start", "date": THURSDAY, "duration": 60},
            {"title": "No start", "date": THURSDAY, "end": "10:00"},
            {"title": "Backwards", "date": THURSDAY, "start": "10:00", "end": "09:00"},
            {"title": "Zero", "date": THURSDAY, "start": "10:00", "end": "10:00"},
            {"title": "Contradiction", "date": THURSDAY, "start": "09:00", "end": "10:00", "duration": 90},
            {"title": "Bad duration", "date": THURSDAY, "start": "09:00", "duration": 0},
            {"title": "Negative reminder", "date": THURSDAY, "start": "09:00", "reminder_minutes": -5},
            {"title": "Bad notes", "date": THURSDAY, "start": "09:00", "notes": 7},
            {"title": "Bad cancelled", "date": THURSDAY, "start": "09:00", "cancelled_dates": "2026-10-01"},
        ],
    )
    def test_nonsense_raises_named_error(self, raw):
        with pytest.raises(cal.InvalidEventError):
            cal.normalize_event(raw)

    def test_non_dict_raises_named_error(self):
        with pytest.raises(cal.InvalidEventError):
            cal.normalize_event(["Haircut", THURSDAY])

    def test_unknown_field_is_refused_by_name(self):
        with pytest.raises(cal.UnknownEventFieldError):
            cal.normalize_event(
                {"title": "Haircut", "date": THURSDAY, "start": "16:00", "titel": "typo"}
            )

    def test_reminder_minutes_zero_is_allowed(self):
        event = cal.normalize_event(
            {"title": "Haircut", "date": THURSDAY, "start": "16:00", "reminder_minutes": 0}
        )
        assert event["reminder_minutes"] == 0


class TestRecurrenceNormalisation:
    def test_valid_rules(self):
        event = cal.normalize_event(
            {
                "title": "Standup",
                "date": THURSDAY,
                "start": "09:00",
                "recurrence": {"freq": "weekly", "until": "2026-10-15"},
            }
        )
        assert event["recurrence"] == {"freq": "weekly", "until": "2026-10-15"}

    @pytest.mark.parametrize(
        "recurrence",
        [
            "weekly",
            {"freq": "yearly", "until": "2026-10-15"},
            {"freq": "weekly"},
            {"freq": "weekly", "until": None},
            {"freq": "weekly", "until": "2026-09-01"},
            {"freq": "weekly", "until": "soon"},
            {"freq": "weekly", "until": "2026-10-15", "every": 2},
        ],
    )
    def test_bad_rules_raise_named_error(self, recurrence):
        with pytest.raises(cal.RecurrenceError):
            cal.normalize_event(
                {"title": "Standup", "date": THURSDAY, "start": "09:00", "recurrence": recurrence}
            )


# ---------------------------------------------------------------------------
# ranges_overlap -- half-open, touching is fine
# ---------------------------------------------------------------------------


class TestRangesOverlap:
    def test_touching_end_to_start_is_not_a_conflict(self):
        # 15:00-16:00 then 16:00-17:00: an event ending 15:00 and one starting
        # 15:00 do not collide. Treating these as a collision refuses correct
        # bookings, so this is the single most important assertion here.
        assert cal.ranges_overlap(900, 960, 960, 1020) is False
        assert cal.ranges_overlap(960, 1020, 900, 960) is False

    def test_one_minute_overlap_is_a_conflict(self):
        assert cal.ranges_overlap(900, 960, 959, 1020) is True
        assert cal.ranges_overlap(959, 1020, 900, 960) is True

    def test_identical_ranges_conflict(self):
        assert cal.ranges_overlap(960, 1020, 960, 1020) is True

    def test_contained_range_conflicts(self):
        assert cal.ranges_overlap(900, 1100, 960, 1020) is True
        assert cal.ranges_overlap(960, 1020, 900, 1100) is True

    def test_disjoint_ranges_do_not_conflict(self):
        assert cal.ranges_overlap(900, 960, 1020, 1080) is False


# ---------------------------------------------------------------------------
# expand_event
# ---------------------------------------------------------------------------


class TestExpandEvent:
    def test_single_event_inside_range(self):
        event = {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        occurrences = cal.expand_event(event, THURSDAY, THURSDAY)
        assert [o["date"] for o in occurrences] == [THURSDAY]
        assert occurrences[0]["start"] == 960
        assert occurrences[0]["end"] == 1020
        assert occurrences[0]["title"] == "Haircut"
        assert occurrences[0]["is_recurring"] is False

    def test_single_event_outside_range_is_empty(self):
        event = {"title": "Haircut", "date": THURSDAY, "start": "16:00"}
        assert cal.expand_event(event, FRIDAY, "2026-10-01") == []

    def test_daily_expansion(self):
        event = {
            "title": "Standup",
            "date": "2026-09-21",
            "start": "09:00",
            "recurrence": {"freq": "daily", "until": "2026-09-25"},
        }
        assert [o["date"] for o in cal.expand_event(event, "2026-09-21", "2026-09-30")] == [
            "2026-09-21",
            "2026-09-22",
            "2026-09-23",
            "2026-09-24",
            "2026-09-25",
        ]

    def test_daily_expansion_respects_window_start(self):
        event = {
            "title": "Standup",
            "date": "2026-09-21",
            "start": "09:00",
            "recurrence": {"freq": "daily", "until": "2026-09-25"},
        }
        assert [o["date"] for o in cal.expand_event(event, "2026-09-23", "2026-09-24")] == [
            "2026-09-23",
            "2026-09-24",
        ]

    def test_weekly_expansion_jumps_to_the_right_week(self):
        # Base is a Thursday; asking from a Friday must land on the NEXT Thursday
        # rather than walking day by day and drifting off the weekly cadence.
        event = {
            "title": "Standup",
            "date": THURSDAY,
            "start": "09:00",
            "recurrence": {"freq": "weekly", "until": "2026-10-15"},
        }
        assert [o["date"] for o in cal.expand_event(event, "2026-10-02", "2026-10-09")] == [
            "2026-10-08"
        ]

    def test_weekly_expansion_stops_at_until(self):
        event = {
            "title": "Standup",
            "date": THURSDAY,
            "start": "09:00",
            "recurrence": {"freq": "weekly", "until": "2026-10-08"},
        }
        assert [o["date"] for o in cal.expand_event(event, THURSDAY, "2026-12-31")] == [
            "2026-09-24",
            "2026-10-01",
            "2026-10-08",
        ]

    def test_cancelled_dates_are_skipped(self):
        event = {
            "title": "Standup",
            "date": "2026-09-21",
            "start": "09:00",
            "recurrence": {"freq": "daily", "until": "2026-09-25"},
            "cancelled_dates": ["2026-09-23"],
        }
        dates = [o["date"] for o in cal.expand_event(event, "2026-09-21", "2026-09-25")]
        assert "2026-09-23" not in dates
        assert dates == ["2026-09-21", "2026-09-22", "2026-09-24", "2026-09-25"]

    def test_reversed_range_is_empty(self):
        event = {"title": "Haircut", "date": THURSDAY, "start": "16:00"}
        assert cal.expand_event(event, FRIDAY, THURSDAY) == []

    def test_invalid_event_raises_named_error(self):
        with pytest.raises(cal.InvalidEventError):
            cal.expand_event({"title": "No date", "start": "16:00"}, THURSDAY, FRIDAY)


# ---------------------------------------------------------------------------
# find_conflicts
# ---------------------------------------------------------------------------


class TestFindConflicts:
    def test_touching_instances_are_not_conflicts(self):
        busy = [instance(900, 960)]
        assert cal.find_conflicts(busy, 960, 1020) == []

    def test_one_minute_earlier_is_a_conflict(self):
        busy = [instance(900, 960)]
        assert len(cal.find_conflicts(busy, 959, 1020)) == 1

    def test_exact_match_is_a_conflict(self):
        busy = [instance(900, 960)]
        assert len(cal.find_conflicts(busy, 900, 960)) == 1

    def test_exclude_id_drops_the_event_being_moved(self):
        busy = [instance(960, 1020, event_id="evt-a"), instance(960, 1020, event_id="evt-b")]
        found = cal.find_conflicts(busy, 960, 1020, exclude_id="evt-a")
        assert [item["event_id"] for item in found] == ["evt-b"]

    def test_date_filter_limits_matching_instances(self):
        busy = [instance(960, 1020, day=THURSDAY), instance(960, 1020, day=FRIDAY, event_id="evt-fri")]
        found = cal.find_conflicts(busy, 960, 1020, date=FRIDAY)
        assert [item["event_id"] for item in found] == ["evt-fri"]

    def test_occurrence_dict_can_be_the_query(self):
        busy = [instance(900, 960, event_id="evt-a")]
        query = instance(950, 1010, event_id="evt-b")
        assert len(cal.find_conflicts(busy, query)) == 1

    def test_triple_query(self):
        busy = [instance(900, 960)]
        assert len(cal.find_conflicts(busy, (THURSDAY, 930, 990))) == 1

    def test_hhmm_query(self):
        busy = [instance(900, 960)]
        assert len(cal.find_conflicts(busy, "15:30", "16:30")) == 1

    def test_backwards_query_raises_named_error(self):
        with pytest.raises(cal.InvalidEventError):
            cal.find_conflicts([], "16:00", "15:00")


# ---------------------------------------------------------------------------
# propose_alternatives
# ---------------------------------------------------------------------------


class TestProposeAlternatives:
    def test_requested_time_free_is_the_first_proposal(self):
        # "The requested time is free" and "nearest alternative" are one path:
        # the scan starts at the requested time.
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "10:00", "end": "11:00"})
        proposals = cal.propose_alternatives(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60}
        )
        assert proposals[0]["start"] == 960
        assert proposals[0]["is_requested"] is True
        assert proposals[0]["distance_minutes"] == 0
        assert "requested time is free" in proposals[0]["label"]

    def test_touching_busy_event_does_not_block_the_requested_time(self):
        # A busy event ending exactly when the requested event starts must not
        # push the booking later -- this is the touching-is-not-a-conflict rule
        # seen from the proposal path.
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "14:00", "end": "15:00"})
        proposals = cal.propose_alternatives(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "15:00", "duration": 60}
        )
        assert proposals[0]["start"] == 900
        assert proposals[0]["is_requested"] is True

    def test_busy_requested_time_gives_nearer_slots_in_order(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "15:30", "end": "16:30"})
        proposals = cal.propose_alternatives(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60}
        )
        assert proposals[0]["start"] == 16 * 60 + 30
        assert [p["distance_minutes"] for p in proposals] == sorted(
            p["distance_minutes"] for p in proposals
        )
        assert [p["rank"] for p in proposals] == list(range(1, len(proposals) + 1))
        assert all(p["is_requested"] is False for p in proposals)

    def test_proposals_never_overrun_the_window_end(self):
        # 20:30 + 60 minutes runs past the 21:00 window end. A scan that checks
        # only the START would happily propose it.
        schedule = cal.empty_schedule()
        proposals = cal.propose_alternatives(
            schedule,
            {"title": "Late call", "date": THURSDAY, "start": "20:30", "duration": 60},
            days=1,
        )
        assert all(p["start"] != 20 * 60 + 30 for p in proposals)
        assert proposals[0]["date"] == FRIDAY
        assert proposals[0]["start"] == cal.DEFAULT_WINDOW[0]

    def test_every_proposal_is_fully_inside_the_window(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "09:00", "end": "10:00"})
        proposals = cal.propose_alternatives(
            schedule,
            {"title": "Long session", "date": THURSDAY, "start": "09:00", "duration": 120},
            days=3,
        )
        assert proposals
        window_start, window_end = cal.DEFAULT_WINDOW
        for proposal in proposals:
            assert proposal["start"] >= window_start
            # The END, not just the start, must be inside the window.
            assert proposal["end"] <= window_end
            assert proposal["end"] - proposal["start"] == 120

    def test_custom_window_is_respected(self):
        proposals = cal.propose_alternatives(
            cal.empty_schedule(),
            {"title": "Standup", "date": THURSDAY, "start": "07:00", "duration": 60},
            days=0,
            window=("09:00", "12:00"),
            grain=30,
        )
        assert [(p["start"], p["end"]) for p in proposals] == [
            (9 * 60, 10 * 60),
            (9 * 60 + 30, 10 * 60 + 30),
            (10 * 60, 11 * 60),
            (10 * 60 + 30, 11 * 60 + 30),
            (11 * 60, 12 * 60),
        ]
        assert all(p["start"] >= 9 * 60 and p["end"] <= 12 * 60 for p in proposals)

    def test_gap_too_short_for_the_duration_is_not_proposed(self):
        # A 30-minute gap between 17:00 and 17:30 cannot host a 90-minute
        # booking, and the 60 minutes before 17:00 cannot either.
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "17:00", "end": "17:30"})
        proposals = cal.propose_alternatives(
            schedule,
            {"title": "Deep work", "date": THURSDAY, "start": "16:00", "duration": 90},
            days=0,
        )
        assert proposals[0]["start"] == 17 * 60 + 30
        assert proposals[0]["end"] == 19 * 60
        assert all(p["end"] <= 17 * 60 or p["start"] >= 17 * 60 + 30 for p in proposals)

    def test_grain_controls_spacing(self):
        proposals = cal.propose_alternatives(
            cal.empty_schedule(),
            {"title": "Standup", "date": THURSDAY, "start": "09:00", "duration": 60},
            days=0,
            grain=45,
        )
        starts = [p["start"] for p in proposals]
        assert starts[:3] == [9 * 60, 9 * 60 + 45, 9 * 60 + 90]

    def test_scan_does_not_go_beyond_the_requested_days(self):
        proposals = cal.propose_alternatives(
            cal.empty_schedule(),
            {"title": "Standup", "date": THURSDAY, "start": "09:00", "duration": 60},
            days=2,
            window=("09:00", "10:00"),
        )
        assert sorted({p["date"] for p in proposals}) == [THURSDAY, FRIDAY, "2026-09-26"]

    def test_horizon_caps_even_a_long_scan(self):
        proposals = cal.propose_alternatives(
            cal.empty_schedule(),
            {"title": "Standup", "date": THURSDAY, "start": "09:00", "duration": 60},
            days=1000,
            window=("09:00", "10:00"),
            horizon=2,
        )
        horizon_end = cal.add_days(THURSDAY, 2)
        assert sorted({p["date"] for p in proposals}) == [THURSDAY, FRIDAY, horizon_end]
        assert all(p["date"] <= horizon_end for p in proposals)

    def test_current_occurrence_is_excluded_from_the_busy_set(self):
        # Scanning for alternatives to an event that is already in the schedule
        # must ignore that very occurrence, or it blocks its own slot.
        schedule = cal.empty_schedule()
        result = cal.add_event(
            schedule, {"id": "evt-haircut", "title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60}
        )
        assert result["ok"] is True
        current = result["event"]
        proposals = cal.propose_alternatives(schedule, current, days=0)
        assert proposals[0]["start"] == 960
        assert proposals[0]["is_requested"] is True

    def test_explicit_exclude_id_is_honoured(self):
        schedule = cal.empty_schedule()
        cal.add_event(
            schedule, {"id": "evt-haircut", "title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60}
        )
        proposals = cal.propose_alternatives(
            schedule,
            {"id": "evt-other", "title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60},
            days=0,
            exclude_id="evt-haircut",
        )
        assert proposals[0]["start"] == 960

    def test_accepts_a_bare_instance_list(self):
        busy = [instance(960, 1020, event_id="evt-a")]
        proposals = cal.propose_alternatives(
            busy, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60}, days=0
        )
        # 16:00-17:00 is taken; the next free minute-aligned slot is 17:00,
        # because a slot may start exactly when the busy event ends.
        assert proposals[0]["start"] == 17 * 60

    def test_labels_are_human_readable(self):
        proposals = cal.propose_alternatives(
            cal.empty_schedule(),
            {"title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60},
            days=0,
        )
        assert "Thursday 24 September at 16:00 to 17:00" in proposals[0]["label"]

    def test_recurring_busy_events_block_their_future_dates(self):
        schedule = cal.empty_schedule()
        cal.add_event(
            schedule,
            {
                "title": "Standup",
                "date": THURSDAY,
                "start": "09:00",
                "duration": 60,
                "recurrence": {"freq": "daily", "until": "2026-09-30"},
            },
        )
        proposals = cal.propose_alternatives(
            schedule,
            {"title": "Long session", "date": THURSDAY, "start": "09:00", "duration": 60},
            days=2,
            window=("09:00", "10:00"),
        )
        assert proposals == []

    @pytest.mark.parametrize("bad_grain", [0, -15, "quarter"])
    def test_bad_grain_raises_named_error(self, bad_grain):
        with pytest.raises(cal.InvalidGrainError):
            cal.propose_alternatives(
                cal.empty_schedule(),
                {"title": "Haircut", "date": THURSDAY, "start": "16:00"},
                grain=bad_grain,
            )

    @pytest.mark.parametrize("bad_window", [("21:00", "08:00"), ("09:00",), "all-day"])
    def test_bad_window_raises_named_error(self, bad_window):
        with pytest.raises(cal.InvalidWindowError):
            cal.propose_alternatives(
                cal.empty_schedule(),
                {"title": "Haircut", "date": THURSDAY, "start": "16:00"},
                window=bad_window,
            )

    def test_bad_schedule_input_raises_named_error(self):
        with pytest.raises(cal.InvalidScheduleError):
            cal.propose_alternatives(
                "not-a-schedule", {"title": "Haircut", "date": THURSDAY, "start": "16:00"}
            )


# ---------------------------------------------------------------------------
# add_event
# ---------------------------------------------------------------------------


class TestAddEvent:
    def test_add_writes_the_canonical_event(self):
        schedule = cal.empty_schedule()
        result = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60}
        )
        assert result["ok"] is True
        assert result["conflicts"] == [] and result["proposals"] == []
        assert schedule["events"] == [result["event"]]
        assert schedule["events"][0]["duration"] == 60

    def test_touching_booking_is_accepted(self):
        # 15:00-16:00 then 16:00-17:00 is a fully correct day.
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "15:00", "end": "16:00"})
        result = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        assert result["ok"] is True
        assert len(schedule["events"]) == 2

    def test_overlapping_booking_is_refused_with_proposals(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "15:30", "end": "16:30"})
        before = copy.deepcopy(schedule)
        result = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        assert result["ok"] is False
        assert result["conflicts"]
        assert result["proposals"]
        assert result["error"] and "Haircut" in result["error"]
        # Atomic refusal: nothing was written, not even partially.
        assert schedule == before

    def test_overlap_one_minute_earlier_is_refused(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "15:00", "end": "16:00"})
        before = copy.deepcopy(schedule)
        result = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "15:59", "end": "16:59"}
        )
        assert result["ok"] is False
        assert schedule == before

    def test_invalid_event_raises_and_does_not_write(self):
        schedule = cal.empty_schedule()
        with pytest.raises(cal.InvalidEventError):
            cal.add_event(schedule, {"title": "No date", "start": "16:00"})
        assert schedule["events"] == []

    def test_ids_stay_unique_for_identical_events(self):
        schedule = cal.empty_schedule()
        first = cal.add_event(
            schedule,
            {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"},
        )
        # Same content on a different day so it is not a conflict, but the
        # content-derived id would collide.
        second = cal.add_event(
            schedule,
            {"title": "Haircut", "date": FRIDAY, "start": "16:00", "end": "17:00"},
        )
        assert first["event"]["id"] != second["event"]["id"]
        assert len({event["id"] for event in schedule["events"]}) == 2

    def test_recurring_event_conflicts_with_a_later_occurrence(self):
        schedule = cal.empty_schedule()
        cal.add_event(
            schedule,
            {
                "title": "Standup",
                "date": THURSDAY,
                "start": "09:00",
                "duration": 30,
                "recurrence": {"freq": "weekly", "until": "2026-10-15"},
            },
        )
        before = copy.deepcopy(schedule)
        result = cal.add_event(
            schedule, {"title": "Interview", "date": "2026-10-01", "start": "09:15", "duration": 30}
        )
        assert result["ok"] is False
        assert schedule == before

    def test_add_to_a_bad_schedule_raises_named_error(self):
        with pytest.raises(cal.InvalidScheduleError):
            cal.add_event({"events": "nope"}, {"title": "Haircut", "date": THURSDAY, "start": "16:00"})


# ---------------------------------------------------------------------------
# move_event
# ---------------------------------------------------------------------------


class TestMoveEvent:
    def test_small_shift_succeeds(self):
        # The event must be excluded from its own conflict check: without that,
        # a 15-minute nudge overlaps its own old slot and is refused.
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        before = copy.deepcopy(schedule)
        result = cal.move_event(schedule, added["event"]["id"], new_start="16:15")
        assert result["ok"] is True
        assert result["event"]["start"] == 16 * 60 + 15
        assert result["event"]["end"] == 17 * 60 + 15
        assert result["event"]["duration"] == 60
        assert schedule != before
        assert schedule["events"][0]["start"] == 16 * 60 + 15

    def test_move_fifteen_minutes_earlier_succeeds(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        result = cal.move_event(schedule, added["event"]["id"], new_start="15:45")
        assert result["ok"] is True
        assert result["event"]["start"] == 15 * 60 + 45

    def test_move_to_a_touching_slot_succeeds(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "14:00", "end": "15:00"})
        result = cal.move_event(schedule, added["event"]["id"], new_start="15:00")
        assert result["ok"] is True
        assert result["event"]["start"] == 15 * 60

    def test_move_to_a_new_day_succeeds(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        result = cal.move_event(schedule, added["event"]["id"], new_date=FRIDAY)
        assert result["ok"] is True
        assert result["event"]["date"] == FRIDAY
        assert result["event"]["start"] == 16 * 60

    def test_move_onto_another_event_is_refused_without_mutation(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "10:00", "end": "11:00"}
        )
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "14:00", "end": "15:00"})
        before = copy.deepcopy(schedule)
        result = cal.move_event(schedule, added["event"]["id"], new_start="14:30")
        assert result["ok"] is False
        assert [c["title"] for c in result["conflicts"]] == ["Call"]
        assert result["proposals"]
        assert result["previous"]["start"] == 10 * 60
        assert schedule == before

    def test_move_with_new_duration(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        result = cal.move_event(schedule, added["event"]["id"], new_start="16:00", new_duration=90)
        assert result["ok"] is True
        assert result["event"]["duration"] == 90
        assert result["event"]["end"] == 17 * 60 + 30

    def test_unknown_id_raises_named_error(self):
        schedule = cal.empty_schedule()
        with pytest.raises(cal.EventNotFoundError):
            cal.move_event(schedule, "evt-does-not-exist", new_start="10:00")

    def test_recurring_move_checks_every_occurrence(self):
        # Hand-built because add_event() would (correctly) refuse the collision
        # that this test needs: a weekly standup whose THURSDAY base slot is
        # free but whose 2026-10-08 occurrence is not.
        schedule = cal.empty_schedule()
        schedule["events"] = [
            cal.normalize_event(
                {
                    "id": "evt-standup",
                    "title": "Standup",
                    "date": THURSDAY,
                    "start": "09:00",
                    "duration": 30,
                    "recurrence": {"freq": "weekly", "until": "2026-10-15"},
                }
            ),
            cal.normalize_event(
                {
                    "id": "evt-interview",
                    "title": "Interview",
                    "date": "2026-10-08",
                    "start": "09:15",
                    "duration": 30,
                }
            ),
        ]
        before = copy.deepcopy(schedule)
        result = cal.move_event(schedule, "evt-standup", new_start="09:15")
        assert result["ok"] is False
        assert {c["date"] for c in result["conflicts"]} == {"2026-10-08"}
        assert schedule == before


# ---------------------------------------------------------------------------
# cancel_event
# ---------------------------------------------------------------------------


class TestCancelEvent:
    def test_cancel_removes_the_whole_event(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        result = cal.cancel_event(schedule, added["event"]["id"])
        assert result["ok"] is True
        assert result["cancelled"] == "event"
        assert schedule["events"] == []

    def test_cancel_one_occurrence_of_a_recurring_event(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule,
            {
                "title": "Standup",
                "date": "2026-09-21",
                "start": "09:00",
                "duration": 30,
                "recurrence": {"freq": "daily", "until": "2026-09-25"},
            },
        )
        result = cal.cancel_event(schedule, added["event"]["id"], date="2026-09-23")
        assert result["ok"] is True
        assert result["cancelled"] == "occurrence"
        assert schedule["events"][0]["cancelled_dates"] == ["2026-09-23"]
        dates = [o["date"] for o in cal.expand_event(schedule["events"][0], "2026-09-21", "2026-09-25")]
        assert dates == ["2026-09-21", "2026-09-22", "2026-09-24", "2026-09-25"]

    def test_the_freed_slot_can_then_be_booked(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule,
            {
                "title": "Standup",
                "date": "2026-09-21",
                "start": "09:00",
                "duration": 30,
                "recurrence": {"freq": "daily", "until": "2026-09-25"},
            },
        )
        cal.cancel_event(schedule, added["event"]["id"], date="2026-09-23")
        result = cal.add_event(
            schedule, {"title": "Dentist", "date": "2026-09-23", "start": "09:00", "duration": 30}
        )
        assert result["ok"] is True

    def test_cancelling_a_non_occurrence_raises_and_changes_nothing(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"}
        )
        before = copy.deepcopy(schedule)
        with pytest.raises(cal.NotAnOccurrenceError):
            cal.cancel_event(schedule, added["event"]["id"], date=FRIDAY)
        assert schedule == before

    def test_cancelling_a_skipped_weekday_raises(self):
        schedule = cal.empty_schedule()
        added = cal.add_event(
            schedule,
            {
                "title": "Standup",
                "date": THURSDAY,
                "start": "09:00",
                "duration": 30,
                "recurrence": {"freq": "weekly", "until": "2026-10-15"},
            },
        )
        with pytest.raises(cal.NotAnOccurrenceError):
            cal.cancel_event(schedule, added["event"]["id"], date=FRIDAY)

    def test_unknown_id_raises_named_error(self):
        with pytest.raises(cal.EventNotFoundError):
            cal.cancel_event(cal.empty_schedule(), "evt-nope")


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


class TestListEvents:
    def test_lists_everything_when_unbounded(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "15:00", "end": "16:00"})
        cal.add_event(schedule, {"title": "Haircut", "date": FRIDAY, "start": "09:00", "end": "10:00"})
        result = cal.list_events(schedule)
        assert result["ok"] is True
        assert result["count"] == 2
        assert [(o["date"], o["title"]) for o in result["occurrences"]] == [
            (THURSDAY, "Call"),
            (FRIDAY, "Haircut"),
        ]

    def test_expands_recurrence_and_orders_by_start(self):
        schedule = cal.empty_schedule()
        cal.add_event(
            schedule,
            {
                "title": "Standup",
                "date": "2026-09-21",
                "start": "09:00",
                "duration": 15,
                "recurrence": {"freq": "daily", "until": "2026-09-23"},
            },
        )
        cal.add_event(schedule, {"title": "Call", "date": "2026-09-22", "start": "08:00", "end": "08:30"})
        result = cal.list_events(schedule, "2026-09-21", "2026-09-23")
        assert [(o["date"], o["start"], o["title"]) for o in result["occurrences"]] == [
            ("2026-09-21", 540, "Standup"),
            ("2026-09-22", 480, "Call"),
            ("2026-09-22", 540, "Standup"),
            ("2026-09-23", 540, "Standup"),
        ]

    def test_range_filters_occurrences(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "15:00", "end": "16:00"})
        result = cal.list_events(schedule, FRIDAY, FRIDAY)
        assert result["count"] == 0

    def test_bad_range_raises_named_error(self):
        with pytest.raises(cal.InvalidDateError):
            cal.list_events(cal.empty_schedule(), "yesterday", FRIDAY)


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------


class TestReminders:
    def build(self):
        schedule = cal.empty_schedule()
        cal.add_event(
            schedule,
            {
                "title": "Haircut",
                "date": THURSDAY,
                "start": "16:00",
                "end": "17:00",
                "reminder_minutes": 15,
            },
        )
        return schedule

    def test_due_when_the_reminder_moment_arrives(self):
        due = cal.due_reminders(self.build(), "2026-09-24 15:45")
        assert len(due) == 1
        reminder = due[0]
        assert reminder["title"] == "Haircut"
        assert reminder["date"] == THURSDAY
        assert reminder["minutes_until_start"] == 15
        assert reminder["key"] == f"{reminder['event_id']}|{THURSDAY}"

    def test_not_due_before_the_reminder_moment(self):
        assert cal.due_reminders(self.build(), "2026-09-24 15:44") == []

    def test_not_due_once_the_event_has_started(self):
        assert cal.due_reminders(self.build(), "2026-09-24 16:00") == []

    def test_accepts_a_datetime(self):
        due = cal.due_reminders(self.build(), datetime(2026, 9, 24, 15, 50))
        assert len(due) == 1
        assert due[0]["minutes_until_start"] == 10

    def test_events_without_a_reminder_are_ignored(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00"})
        assert cal.due_reminders(schedule, "2026-09-24 15:45") == []

    def test_marking_fired_stops_the_reminder_repeating(self):
        schedule = self.build()
        due = cal.due_reminders(schedule, "2026-09-24 15:45")
        cal.mark_reminder_fired(schedule, due[0]["key"])
        assert cal.due_reminders(schedule, "2026-09-24 15:50") == []

    def test_recurring_occurrence_reminder(self):
        schedule = cal.empty_schedule()
        cal.add_event(
            schedule,
            {
                "title": "Standup",
                "date": "2026-09-21",
                "start": "09:00",
                "duration": 15,
                "recurrence": {"freq": "daily", "until": "2026-09-25"},
                "reminder_minutes": 10,
            },
        )
        due = cal.due_reminders(schedule, "2026-09-23 08:55")
        assert [d["date"] for d in due] == ["2026-09-23"]

    def test_describe_reminder(self):
        due = cal.due_reminders(self.build(), "2026-09-24 15:45")
        assert (
            cal.describe_reminder(due[0])
            == "Reminder: Haircut, Thursday 24 September at 16:00, in 15 minutes."
        )

    def test_describe_reminder_with_now(self):
        due = cal.due_reminders(self.build(), "2026-09-24 15:45")
        assert cal.describe_reminder(due[0], "2026-09-24 15:59").endswith("in 1 minute.")

    def test_describe_reminder_rejects_nonsense(self):
        with pytest.raises(cal.InvalidEventError):
            cal.describe_reminder({"title": "Haircut"})


# ---------------------------------------------------------------------------
# validate_schedule
# ---------------------------------------------------------------------------


class TestValidateSchedule:
    def test_empty_schedule_is_valid(self):
        assert cal.validate_schedule(cal.empty_schedule()) == []

    def test_a_booked_schedule_is_valid(self):
        schedule = cal.empty_schedule()
        cal.add_event(schedule, {"title": "Haircut", "date": THURSDAY, "start": "16:00", "end": "17:00"})
        cal.add_event(schedule, {"title": "Call", "date": THURSDAY, "start": "15:00", "end": "16:00"})
        assert cal.validate_schedule(schedule) == []

    def test_not_a_dict(self):
        assert cal.validate_schedule(["events"])

    def test_reports_version_and_bad_events(self):
        problems = cal.validate_schedule({"version": "99", "events": [{"title": "No date"}]})
        assert any("version" in problem for problem in problems)
        assert any("events[0]" in problem for problem in problems)

    def test_reports_duplicate_ids(self):
        schedule = {
            "version": cal.SCHEDULE_VERSION,
            "events": [
                {"id": "a", "title": "One", "date": THURSDAY, "start": "09:00", "duration": 30},
                {"id": "a", "title": "Two", "date": THURSDAY, "start": "11:00", "duration": 30},
            ],
        }
        assert any("duplicate event id" in problem for problem in cal.validate_schedule(schedule))

    def test_reports_overlaps(self):
        schedule = {
            "version": cal.SCHEDULE_VERSION,
            "events": [
                {"id": "a", "title": "One", "date": THURSDAY, "start": "09:00", "duration": 60},
                {"id": "b", "title": "Two", "date": THURSDAY, "start": "09:30", "duration": 60},
            ],
        }
        assert any("overlap" in problem for problem in cal.validate_schedule(schedule))

    def test_touching_events_are_not_reported(self):
        schedule = {
            "version": cal.SCHEDULE_VERSION,
            "events": [
                {"id": "a", "title": "One", "date": THURSDAY, "start": "09:00", "end": "10:00"},
                {"id": "b", "title": "Two", "date": THURSDAY, "start": "10:00", "end": "11:00"},
            ],
        }
        assert cal.validate_schedule(schedule) == []

    def test_recurring_overlap_is_reported_on_the_colliding_date(self):
        schedule = {
            "version": cal.SCHEDULE_VERSION,
            "events": [
                {
                    "id": "a",
                    "title": "Standup",
                    "date": THURSDAY,
                    "start": "09:00",
                    "duration": 30,
                    "recurrence": {"freq": "weekly", "until": "2026-10-15"},
                },
                {"id": "b", "title": "One-off", "date": "2026-10-01", "start": "09:00", "duration": 30},
            ],
        }
        problems = cal.validate_schedule(schedule)
        assert len(problems) == 1 and "2026-10-01" in problems[0]

    def test_garbage_never_raises(self):
        for junk in [None, 42, [], {}, {"events": None}, {"version": cal.SCHEDULE_VERSION}]:
            assert isinstance(cal.validate_schedule(junk), list)


# ---------------------------------------------------------------------------
# THE HAIRCUT SCENARIO, end to end
# ---------------------------------------------------------------------------


class TestHaircutScenario:
    def build_day(self):
        schedule = cal.empty_schedule()
        booked = cal.add_event(
            schedule,
            {"title": "Client call", "date": THURSDAY, "start": "15:30", "end": "16:30"},
        )
        assert booked["ok"] is True
        return schedule

    def test_haircut_into_a_clashing_day_returns_ranked_free_slots(self):
        schedule = self.build_day()
        before = copy.deepcopy(schedule)

        result = cal.add_event(
            schedule,
            {"title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60},
        )

        # Refused, with alternatives, and the day is untouched.
        assert result["ok"] is False
        assert [c["title"] for c in result["conflicts"]] == ["Client call"]
        assert schedule == before

        proposals = result["proposals"]
        assert proposals, "a clashing booking must come back with alternatives"
        # 15:30-16:30 is busy, so the first free slot is 16:30 (touching is free).
        assert proposals[0]["date"] == THURSDAY
        assert proposals[0]["start"] == 16 * 60 + 30
        assert proposals[0]["end"] == 17 * 60 + 30
        assert proposals[0]["rank"] == 1
        assert proposals[0]["is_requested"] is False

        # Ranked from the requested time outwards, every slot the full hour,
        # every slot inside the window.
        distances = [p["distance_minutes"] for p in proposals]
        assert distances == sorted(distances)
        assert distances[0] == 30
        assert all(p["duration"] == 60 for p in proposals)
        assert all(p["end"] <= cal.DEFAULT_WINDOW[1] for p in proposals)
        assert all(isinstance(p["label"], str) and p["label"] for p in proposals)
        assert "Thursday 24 September" in proposals[0]["label"]

    def test_the_same_booking_succeeds_once_the_clash_is_moved(self):
        schedule = self.build_day()
        clashing = schedule["events"][0]["id"]
        moved = cal.move_event(schedule, clashing, new_start="13:00", new_duration=60)
        assert moved["ok"] is True

        result = cal.add_event(
            schedule,
            {"title": "Haircut", "date": THURSDAY, "start": "16:00", "duration": 60},
        )
        assert result["ok"] is True
        listing = cal.list_events(schedule, THURSDAY, THURSDAY)
        assert [(o["start"], o["title"]) for o in listing["occurrences"]] == [
            (13 * 60, "Client call"),
            (16 * 60, "Haircut"),
        ]

    def test_a_free_haircut_slot_is_proposed_as_itself(self):
        schedule = self.build_day()
        proposals = cal.propose_alternatives(
            schedule,
            {"title": "Haircut", "date": THURSDAY, "start": "17:00", "duration": 60},
        )
        assert proposals[0]["is_requested"] is True
        assert proposals[0]["start"] == 17 * 60
        assert proposals[0]["distance_minutes"] == 0

    def test_the_whole_flow_survives_a_reload(self):
        # Persistence hands back plain dicts; the pure logic must round-trip.
        schedule = self.build_day()
        reloaded = copy.deepcopy(schedule)
        assert cal.validate_schedule(reloaded) == []
        result = cal.add_event(
            reloaded, {"title": "Haircut", "date": THURSDAY, "start": "17:00", "duration": 60}
        )
        assert result["ok"] is True
        assert len(cal.list_events(reloaded)["occurrences"]) == 2
