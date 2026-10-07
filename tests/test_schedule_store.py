"""Unit tests for :mod:`claire.schedule.store` — the schedule file on disk.

The persistence layer is the only place where a bug can destroy the owner's
calendar, so these tests are about the destructive paths, not the happy path:
a corrupt file must never read as "empty", a failed write must never truncate,
a save must never skip its backup, and a write must never happen before
validation.  Every test uses ``tmp_path`` and the ``$CLAIRE_SCHEDULE_PATH``
override, so the owner's real calendar under ``~/.local/share/claire`` is never
read or written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from claire.schedule import store

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

@pytest.fixture()
def schedule_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated schedule path, redirected through the env override."""
    data_dir = tmp_path / "claire-data"
    target = data_dir / "schedule" / "schedule.json"
    monkeypatch.setenv(store.SCHEDULE_ENV_VAR, str(target))
    # Guard: the default resolution must not point anywhere real in a test.
    assert store.resolve_schedule_path() == target
    assert store.resolve_schedule_path() != Path.home() / ".local" / "share" / "claire"
    assert not target.exists()
    return target


def _event(title: str, day: str = "2026-09-24", start: int = 16 * 60, end: int = 17 * 60) -> Dict[str, Any]:
    """A minimal event that the calendar module accepts (no invented fields)."""
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "date": day,
        "start": start,
        "end": end,
    }


def _schedule(*events: Dict[str, Any]) -> Dict[str, Any]:
    """A valid schedule containing *events*."""
    schedule = dict(store.empty_schedule())
    schedule["events"] = list(events)
    return schedule


def _temps(directory: Path) -> List[Path]:
    """Every temp file this module could have left behind in *directory*."""
    return [item for item in directory.iterdir() if item.name.endswith(".tmp")]


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------

def test_round_trip_save_then_load_preserves_the_calendar(schedule_path: Path) -> None:
    original = _schedule(
        _event("Dentist"),
        {
            "id": "standup",
            "title": "Standup",
            "date": "2026-09-25",
            "start": 9 * 60,
            "end": 9 * 60 + 15,
            "recurrence": {"freq": "weekly", "until": "2026-12-18"},
            "cancelled_dates": ["2026-10-02"],
            "notes": "Zoom, not the office",
        },
    )

    result = store.save_schedule(original, schedule_path)
    assert result.path == schedule_path
    assert result.backup is None  # nothing to back up on the very first write
    assert schedule_path.exists()

    loaded = store.load_schedule(schedule_path)
    assert loaded.existed is True
    assert loaded.path == schedule_path
    assert dict(loaded) == original
    # The recurrence and the per-date cancellation survive the trip intact:
    # losing cancelled_dates would silently resurrect a cancelled occurrence.
    assert loaded["events"][1]["recurrence"] == {"freq": "weekly", "until": "2026-12-18"}
    assert loaded["events"][1]["cancelled_dates"] == ["2026-10-02"]


def test_load_of_absent_file_is_empty_and_reported_absent(schedule_path: Path) -> None:
    loaded = store.load_schedule(schedule_path)

    assert loaded.existed is False
    assert loaded.path == schedule_path
    assert isinstance(loaded, dict)
    assert loaded == store.empty_schedule()
    # Reading must not create the file: an absent calendar stays absent until
    # something actually books an event.
    assert not schedule_path.exists()
    assert not schedule_path.parent.exists()


def test_env_override_is_used_when_no_path_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere" / "calendar.json"
    monkeypatch.setenv(store.SCHEDULE_ENV_VAR, str(target))

    assert store.resolve_schedule_path() == target
    store.save_schedule(_schedule(), None)
    assert target.exists()
    assert store.load_schedule()["events"] == []


# --------------------------------------------------------------------------
# A corrupt file is an error, never an empty calendar
# --------------------------------------------------------------------------

def test_corrupt_json_raises_named_error_naming_the_path(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text('{"version": "1", "events": [', encoding="utf-8")
    before = schedule_path.read_bytes()

    with pytest.raises(store.ScheduleParseError) as caught:
        loaded = store.load_schedule(schedule_path)
        pytest.fail(f"corrupt file must not load, got {loaded!r}")

    assert str(schedule_path) in str(caught.value)
    assert "not valid JSON" in str(caught.value)
    assert isinstance(caught.value, store.ScheduleStoreError)
    # A damaged file is left exactly as it was: recovery is a human decision.
    assert schedule_path.read_bytes() == before


def test_truncated_file_is_not_an_empty_calendar(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text(
        json.dumps(_schedule(_event("Dentist"))) [: -1], encoding="utf-8"
    )

    with pytest.raises(store.ScheduleParseError):
        store.load_schedule(schedule_path)


def test_empty_file_is_not_an_empty_calendar(schedule_path: Path) -> None:
    # The classic crash-mid-write artefact.  Reading it as "no events" would
    # hide a calendar that is really just unreadable.
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text("", encoding="utf-8")

    with pytest.raises(store.ScheduleParseError) as caught:
        store.load_schedule(schedule_path)
    assert str(schedule_path) in str(caught.value)


def test_non_object_json_is_refused_by_name(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text('["not", "a", "schedule"]', encoding="utf-8")

    with pytest.raises(store.ScheduleFormatError) as caught:
        store.load_schedule(schedule_path)
    assert str(schedule_path) in str(caught.value)
    assert "JSON object" in str(caught.value)


def test_file_without_a_version_is_refused_by_name(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text('{"events": []}', encoding="utf-8")

    with pytest.raises(store.ScheduleFormatError) as caught:
        store.load_schedule(schedule_path)
    assert str(schedule_path) in str(caught.value)
    assert "version" in str(caught.value)


def test_events_of_the_wrong_shape_are_refused(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text(
        json.dumps({"version": store.schedule_version(), "events": "yesterday"}),
        encoding="utf-8",
    )

    with pytest.raises(store.ScheduleFormatError) as caught:
        store.load_schedule(schedule_path)
    assert str(schedule_path) in str(caught.value)
    assert "events" in str(caught.value)


def test_non_utf8_file_is_refused_by_name(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_bytes(b"\xff\xfe\x00\x01CALENDAR")

    with pytest.raises(store.ScheduleParseError) as caught:
        store.load_schedule(schedule_path)
    assert str(schedule_path) in str(caught.value)


# --------------------------------------------------------------------------
# Version policing
# --------------------------------------------------------------------------

def test_newer_version_is_refused_by_name(schedule_path: Path) -> None:
    supported = store.schedule_version()
    newer = str(int(str(supported)) + 1)
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text(
        json.dumps({"version": newer, "events": [_event("From the future")]}),
        encoding="utf-8",
    )
    before = schedule_path.read_bytes()

    with pytest.raises(store.ScheduleVersionError) as caught:
        loaded = store.load_schedule(schedule_path)
        pytest.fail(f"a newer version must not load, got {loaded!r}")

    assert isinstance(caught.value, store.ScheduleFormatError)
    assert caught.value.found == newer
    assert caught.value.supported == supported
    assert str(schedule_path) in str(caught.value)
    # Refusing means refusing to touch it: unknown fields must survive.
    assert schedule_path.read_bytes() == before


def test_uncomparable_version_is_refused_rather_than_guessed(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text(
        json.dumps({"version": "two", "events": []}), encoding="utf-8"
    )

    with pytest.raises(store.ScheduleVersionError):
        store.load_schedule(schedule_path)


def test_older_version_is_still_readable(schedule_path: Path) -> None:
    # Reading an old file is not the dangerous direction; mangling a newer one
    # is.  Migration is the calendar module's business.
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text(
        json.dumps({"version": "0", "events": []}), encoding="utf-8"
    )

    loaded = store.load_schedule(schedule_path)
    assert loaded["version"] == "0"
    assert loaded.existed is True


def test_double_digit_versions_compare_numerically(
    schedule_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "10" > "9" numerically but not lexicographically; getting this wrong in
    # the other direction would accept a file from a newer Claire.
    monkeypatch.setattr(store, "schedule_version", lambda: "9")
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text(
        json.dumps({"version": "10", "events": []}), encoding="utf-8"
    )

    with pytest.raises(store.ScheduleVersionError) as caught:
        store.load_schedule(schedule_path)
    assert caught.value.found == "10"


# --------------------------------------------------------------------------
# Atomicity
# --------------------------------------------------------------------------

def test_replace_failure_leaves_the_original_intact_and_no_temp_file(
    schedule_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)
    original_bytes = schedule_path.read_bytes()

    real_replace = store._atomic_replace

    def failing_replace(src: Any, dst: Any) -> None:
        # Let the backup rotation happen; fail exactly where a crash would
        # hurt most — the rename onto the live calendar.
        if Path(dst) == schedule_path:
            raise OSError("simulated crash during rename")
        real_replace(src, dst)

    monkeypatch.setattr(store, "_atomic_replace", failing_replace)

    with pytest.raises(store.ScheduleWriteError) as caught:
        store.save_schedule(_schedule(_event("Yoga")), schedule_path)

    assert str(schedule_path) in str(caught.value)
    # The old calendar is byte-for-byte what it was, and no half-written temp
    # file is left lying next to it.
    assert schedule_path.read_bytes() == original_bytes
    assert _temps(schedule_path.parent) == []
    assert json.loads(schedule_path.read_text(encoding="utf-8"))["events"][0]["title"] == "Dentist"


def test_backup_failure_refuses_the_save_without_touching_the_file(
    schedule_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)
    original_bytes = schedule_path.read_bytes()

    def always_fails(src: Any, dst: Any) -> None:
        raise OSError("backup storage unavailable")

    monkeypatch.setattr(store, "_atomic_replace", always_fails)

    with pytest.raises(store.ScheduleBackupError) as caught:
        store.save_schedule(_schedule(_event("Yoga")), schedule_path)

    assert str(schedule_path) in str(caught.value)
    assert "nothing was written" in str(caught.value)
    assert schedule_path.read_bytes() == original_bytes
    assert _temps(schedule_path.parent) == []
    assert not (schedule_path.parent / (schedule_path.name + ".bak1")).exists()


def test_a_successful_save_leaves_no_temp_files(schedule_path: Path) -> None:
    for index in range(4):
        store.save_schedule(_schedule(_event(f"Event {index}")), schedule_path)

    assert _temps(schedule_path.parent) == []


def test_serialisation_failure_writes_nothing_and_does_not_rotate_backups(
    schedule_path: Path,
) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)
    original_bytes = schedule_path.read_bytes()

    bad: Dict[str, Any] = dict(_schedule(_event("Yoga")))
    bad["events"][0]["title"] = {"a", "set", "is", "not", "json"}

    with pytest.raises(store.ScheduleSerialisationError):
        store.save_schedule(bad, schedule_path)

    assert schedule_path.read_bytes() == original_bytes
    assert _temps(schedule_path.parent) == []
    # A bad value must not push a good backup out of the rotation either.
    assert not (schedule_path.parent / (schedule_path.name + ".bak1")).exists()


def test_saving_something_that_is_not_a_mapping_is_refused(schedule_path: Path) -> None:
    with pytest.raises(store.ScheduleSerialisationError):
        store.save_schedule(["not", "a", "schedule"], schedule_path)  # type: ignore[arg-type]
    assert not schedule_path.exists()


# --------------------------------------------------------------------------
# Backups and rotation
# --------------------------------------------------------------------------

def test_backup_is_reported_and_keeps_only_the_configured_number(
    schedule_path: Path,
) -> None:
    backups = [schedule_path.with_name(schedule_path.name + f".bak{i}") for i in (1, 2, 3, 4)]

    store.save_schedule(_schedule(_event("One")), schedule_path)
    previous_bytes = schedule_path.read_bytes()  # content of save #1

    result = store.save_schedule(_schedule(_event("Two")), schedule_path)
    assert result.backup == backups[0]  # the path is reported to the caller
    assert backups[0].read_bytes() == previous_bytes

    for index in range(3, 6):
        store.save_schedule(_schedule(_event(f"Event {index}")), schedule_path)

    assert backups[0].exists() and backups[1].exists() and backups[2].exists()
    assert not backups[3].exists()  # keep N = 3, so .bak4 is never created
    # .bak1 is the immediately previous calendar, .bak2 the one before it.
    assert json.loads(backups[0].read_text(encoding="utf-8"))["events"][0]["title"] == "Event 4"
    assert json.loads(backups[1].read_text(encoding="utf-8"))["events"][0]["title"] == "Event 3"
    assert json.loads(backups[2].read_text(encoding="utf-8"))["events"][0]["title"] == "Two"


def test_backup_count_can_be_chosen_per_save(schedule_path: Path) -> None:
    store.save_schedule(_schedule(_event("One")), schedule_path)
    store.save_schedule(_schedule(_event("Two")), schedule_path, keep=1)
    store.save_schedule(_schedule(_event("Three")), schedule_path, keep=1)

    assert (schedule_path.parent / (schedule_path.name + ".bak1")).exists()
    assert not (schedule_path.parent / (schedule_path.name + ".bak2")).exists()


def test_no_backups_when_keep_is_zero(schedule_path: Path) -> None:
    store.save_schedule(_schedule(_event("One")), schedule_path)
    result = store.save_schedule(_schedule(_event("Two")), schedule_path, keep=0)

    assert result.backup is None
    assert not (schedule_path.parent / (schedule_path.name + ".bak1")).exists()


def test_negative_keep_is_refused(schedule_path: Path) -> None:
    with pytest.raises(store.ScheduleStoreError):
        store.save_schedule(_schedule(), schedule_path, keep=-1)


def test_a_stray_higher_backup_is_not_kept_as_a_phantom(schedule_path: Path) -> None:
    schedule_path.parent.mkdir(parents=True)
    stray = schedule_path.parent / (schedule_path.name + ".bak3")
    stray.write_text("a hand-made file, not a real backup", encoding="utf-8")

    store.save_schedule(_schedule(_event("One")), schedule_path)
    store.save_schedule(_schedule(_event("Two")), schedule_path)

    # .bak1 holds the real previous calendar; the phantom is gone rather than
    # surviving as a "backup" rotation would never refresh.
    assert json.loads(
        (schedule_path.parent / (schedule_path.name + ".bak1")).read_text(encoding="utf-8")
    )["events"][0]["title"] == "One"
    assert not stray.exists()


# --------------------------------------------------------------------------
# The file is meant to be read (and edited) by a human
# --------------------------------------------------------------------------

def test_written_file_is_pretty_printed_json_with_a_stable_key_order(
    schedule_path: Path,
) -> None:
    # Deliberately unusual top-level order: it must survive, not be sorted.
    schedule = {
        "version": store.schedule_version(),
        "fired_reminders": [],
        "events": [_event("Dentist")],
    }

    store.save_schedule(schedule, schedule_path)
    text = schedule_path.read_text(encoding="utf-8")

    assert json.loads(text) == schedule
    assert text.endswith("\n")
    assert '\n  "version"' in text  # indent=2, not one long line
    assert '"events"' in text
    order = [text.index('"version"'), text.index('"fired_reminders"'), text.index('"events"')]
    assert order == sorted(order)

    # Saving the same schedule twice produces the same bytes: the file is not
    # shuffled on every write, so a diff shows only real changes.
    first = schedule_path.read_bytes()
    store.save_schedule(store.load_schedule(schedule_path), schedule_path)
    assert schedule_path.read_bytes() == first


def test_non_ascii_titles_stay_readable_in_the_file(schedule_path: Path) -> None:
    event = _event("Cita con José")
    store.save_schedule(_schedule(event), schedule_path)

    text = schedule_path.read_text(encoding="utf-8")
    assert "José" in text  # not \u00e9 escaped
    assert store.load_schedule(schedule_path)["events"][0]["title"] == "Cita con José"


# --------------------------------------------------------------------------
# update_schedule: load, mutate, validate, save — and never a partial step
# --------------------------------------------------------------------------

def test_update_schedule_saves_the_mutation_and_returns_the_mutator_result(
    schedule_path: Path,
) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)

    def book(schedule: store.Schedule) -> str:
        schedule["events"].append(_event("Yoga", day="2026-09-25"))
        return "booked Yoga"

    returned = store.update_schedule(book, schedule_path)

    assert returned == "booked Yoga"
    titles = [event["title"] for event in store.load_schedule(schedule_path)["events"]]
    assert titles == ["Dentist", "Yoga"]


def test_update_schedule_accepts_a_replacement_mapping(schedule_path: Path) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)

    def rename(schedule: store.Schedule) -> Dict[str, Any]:
        replacement = dict(schedule)
        replacement["events"] = [_event("Optician", day="2026-10-01")]
        return replacement

    returned = store.update_schedule(rename, schedule_path)

    assert returned["events"][0]["title"] == "Optician"
    assert store.load_schedule(schedule_path)["events"][0]["title"] == "Optician"


def test_update_schedule_creates_the_file_from_nothing(schedule_path: Path) -> None:
    def book(schedule: store.Schedule) -> None:
        assert schedule.existed is False
        schedule["events"].append(_event("Dentist"))

    store.update_schedule(book, schedule_path)

    loaded = store.load_schedule(schedule_path)
    assert loaded.existed is True
    assert loaded["events"][0]["title"] == "Dentist"


def test_validation_failure_names_the_problems_and_writes_nothing(
    schedule_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)
    before = schedule_path.read_bytes()
    before_files = sorted(item.name for item in schedule_path.parent.iterdir())

    problems = ["events[0]: event requires a non-empty 'title'", "events[1]: time outside the day"]
    monkeypatch.setattr(store, "_validation_problems", lambda schedule: list(problems))

    with pytest.raises(store.ScheduleValidationError) as caught:
        store.update_schedule(lambda schedule: schedule["events"].clear(), schedule_path)

    assert caught.value.problems == problems
    assert caught.value.path == schedule_path
    for problem in problems:
        assert problem in str(caught.value)

    # The calendar on disk is untouched by the refused update.
    assert schedule_path.read_bytes() == before
    assert sorted(item.name for item in schedule_path.parent.iterdir()) == before_files


def test_the_real_validator_also_blocks_a_bad_write(schedule_path: Path) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)
    before = schedule_path.read_bytes()

    def add_nonsense(schedule: store.Schedule) -> None:
        # The calendar module rejects this: an event with no title.
        schedule["events"].append({"id": "bad", "date": "2026-09-25", "start": 600, "end": 660})

    with pytest.raises(store.ScheduleValidationError) as caught:
        store.update_schedule(add_nonsense, schedule_path)

    assert any("title" in problem for problem in caught.value.problems)
    assert schedule_path.read_bytes() == before


def test_a_raising_mutator_leaves_the_calendar_alone(schedule_path: Path) -> None:
    store.save_schedule(_schedule(_event("Dentist")), schedule_path)
    before = schedule_path.read_bytes()

    def explode(schedule: store.Schedule) -> None:
        raise RuntimeError("the tool handler failed halfway")

    with pytest.raises(RuntimeError):
        store.update_schedule(explode, schedule_path)

    assert schedule_path.read_bytes() == before
    assert _temps(schedule_path.parent) == []


def test_update_schedule_needs_a_callable(schedule_path: Path) -> None:
    with pytest.raises(store.ScheduleMutationError):
        store.update_schedule("not callable", schedule_path)  # type: ignore[arg-type]


def test_update_schedule_uses_the_environment_path_when_none_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "data" / "schedule.json"
    monkeypatch.setenv(store.SCHEDULE_ENV_VAR, str(target))

    store.update_schedule(lambda schedule: schedule["events"].append(_event("Dentist")))

    assert target.exists()
    assert store.load_schedule(target)["events"][0]["title"] == "Dentist"


# --------------------------------------------------------------------------
# The default location follows the rest of Claire's data root
# --------------------------------------------------------------------------

def test_default_path_lives_under_the_claire_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(store.SCHEDULE_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert store.resolve_schedule_path() == (
        tmp_path / ".local" / "share" / "claire" / "schedule" / "schedule.json"
    )


def test_explicit_path_beats_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(store.SCHEDULE_ENV_VAR, str(tmp_path / "from-env.json"))
    explicit = tmp_path / "explicit.json"

    assert store.resolve_schedule_path(explicit) == explicit
    store.save_schedule(_schedule(), explicit)
    assert explicit.exists()
    assert not (tmp_path / "from-env.json").exists()


def test_tilde_in_a_path_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert store.resolve_schedule_path("~/calendar.json") == tmp_path / "calendar.json"


def test_saving_reuses_the_previous_file_permissions(schedule_path: Path) -> None:
    store.save_schedule(_schedule(_event("One")), schedule_path)
    os.chmod(schedule_path, 0o640)

    store.save_schedule(_schedule(_event("Two")), schedule_path)

    assert (schedule_path.stat().st_mode & 0o777) == 0o640
