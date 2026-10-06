"""Persistence for Claire's local-first calendar (R-SCHED-1).

This module owns ONE thing: the schedule file on disk.  It deliberately
contains no calendaring logic — no overlap arithmetic, no alternative
proposals, no recurrence expansion.  That lives in
:mod:`claire.schedule.calendar`.  Keeping the two apart is what makes the
dangerous part (destroying the owner's calendar) auditable in one file.

Where the file lives
--------------------
The rest of Claire keeps the owner's data OUTSIDE the repository, in
``~/.local/share/claire`` (the SQLite database and the memory vault both land
there).  The schedule follows the same convention::

    ~/.local/share/claire/schedule/schedule.json

with an environment override so tests and alternative installs can redirect
it: ``$CLAIRE_SCHEDULE_PATH`` (same shape as ``$CLAIRE_VAULT_DIR`` in
:mod:`claire.memory.notes`).  A missing file is NORMAL — the first run of a
fresh install has no calendar yet — but the caller can always tell whether the
file was absent, because an absent file and an *empty* file are very different
things when the owner has been booking appointments for a year.

The file is pretty-printed JSON with the keys in insertion order and no
``sort_keys``, so the owner can open it in an editor, change a time by hand,
and hand it back.  Human-readable storage is a feature, not a fallback.

The two failures that would actually lose data
----------------------------------------------
1. **A truncated file.**  A crash in the middle of a write must never leave a
   half-written calendar behind, so writes go temp-file → ``fsync`` →
   ``os.replace``.  ``os.replace`` is atomic on POSIX: a reader sees either
   the whole old file or the whole new one, never a mixture.  We also take a
   backup of the previous good file before replacing it, because the most
   likely way to lose a calendar is not a crash — it is the owner (or a
   future bug) writing something wrong *successfully*.
2. **Mapping a damaged file onto "the calendar is empty".**  If the file is
   corrupt we raise a NAMED error that quotes the path and says what is wrong.
   Returning an empty schedule instead would look, to every caller above us,
   exactly like a calendar that happens to have no events — so the owner's
   real bookings would silently vanish from view, and the next save would
   overwrite them for real.  There is no code path here that turns an
   unreadable file into an empty one.

The version field is the forward-compatibility guard.  A file written by a
NEWER Claire than this one may contain fields (and rules) we do not
understand; loading it and saving it back would mangle unknown data, so we
refuse by name instead.  Older versions are accepted — forward migration is
the calendar module's business, and we can at least read them honestly.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Union

__all__ = [
    "BACKUP_SUFFIX_FMT",
    "DEFAULT_BACKUP_COUNT",
    "SCHEDULE_DIRNAME",
    "SCHEDULE_ENV_VAR",
    "SCHEDULE_FILENAME",
    "Schedule",
    "ScheduleBackupError",
    "ScheduleError",
    "ScheduleFormatError",
    "ScheduleMutationError",
    "ScheduleParseError",
    "ScheduleSerialisationError",
    "ScheduleStoreError",
    "ScheduleValidationError",
    "ScheduleVersionError",
    "ScheduleWriteError",
    "SaveResult",
    "empty_schedule",
    "load_schedule",
    "resolve_schedule_path",
    "save_schedule",
    "schedule_version",
    "update_schedule",
]

_PathLike = Union[str, os.PathLike]

#: Environment variable that redirects the schedule file outright.
#: Mirrors ``$CLAIRE_VAULT_DIR`` in :mod:`claire.memory.notes`.
SCHEDULE_ENV_VAR = "CLAIRE_SCHEDULE_PATH"

#: Directory under the Claire data root that holds the schedule file.
SCHEDULE_DIRNAME = "schedule"

#: The file itself.  ``.json`` because it is meant to be readable by hand.
SCHEDULE_FILENAME = "schedule.json"

#: How many previous versions to keep.  Three is enough to notice that a bad
#: edit happened two saves ago and still recover from it, without slowly
#: filling the disk with calendars.
DEFAULT_BACKUP_COUNT = 3

#: Backup files are ``schedule.json.bak1`` (newest) … ``.bakN`` (oldest).
BACKUP_SUFFIX_FMT = ".bak{}"

#: What this code understands when :mod:`claire.schedule.calendar` is not
#: importable.  The version is a STRING in this project (``"1"``), and the
#: comparison below is written to survive either spelling, so a hand-made file
#: with an integer version is not treated as alien.  See
#: :func:`schedule_version` for the live value.
_FALLBACK_VERSION = "1"

#: A schedule we can write when the calendar module is unavailable: it is
#: structurally valid, and having a shape to fall back to is what lets
#: ``load_schedule`` report an absent file without guessing at the calendar
#: module's internals.
_FALLBACK_EMPTY: Dict[str, Any] = {
    "version": _FALLBACK_VERSION,
    "events": [],
    "fired_reminders": [],
}

_CALENDAR_MODULE: Any = None
_CALENDAR_IMPORT_TRIED = False


# --------------------------------------------------------------------------
# Errors — every one of them NAMED, and every message says which file
# --------------------------------------------------------------------------

class ScheduleStoreError(Exception):
    """Base class for every failure in this module.

    Catch this to mean "the calendar on disk could not be read or written",
    without having to enumerate the reasons.
    """


class ScheduleParseError(ScheduleStoreError):
    """The file exists but is not readable JSON (truncated, edited badly…)."""


class ScheduleFormatError(ScheduleStoreError):
    """The file parsed as JSON but is not a schedule we can recognise."""


class ScheduleVersionError(ScheduleFormatError):
    """The file's version is NEWER than this code understands.

    Refusing is the whole point: silently loading -- and later re-saving --
    data whose rules we do not know is how an unknown field gets deleted.
    """

    def __init__(self, path: _PathLike, found: Any, supported: Any) -> None:
        self.path = Path(path)
        self.found = found
        self.supported = supported
        super().__init__(
            f"schedule file {self.path} was written by a newer version of "
            f"Claire (file version {found}, this code understands up to "
            f"{supported}); refusing to load it so unknown data is not "
            f"mangled — upgrade Claire, or edit the version field by hand "
            f"if you are sure the file is compatible"
        )


class ScheduleValidationError(ScheduleStoreError):
    """The calendar module reported problems with the schedule.

    Raised instead of writing, so a schedule that breaks Claire's rules never
    reaches the disk in the first place.
    """

    def __init__(self, problems: List[str], path: Optional[_PathLike] = None) -> None:
        self.problems = list(problems)
        self.path = Path(path) if path is not None else None
        where = f" for {self.path}" if self.path is not None else ""
        joined = "; ".join(self.problems) or "unspecified validation failure"
        super().__init__(f"schedule is not valid{where}: {joined}")


class ScheduleSerialisationError(ScheduleStoreError):
    """The schedule cannot be turned into JSON (a non-serialisable value)."""


class ScheduleWriteError(ScheduleStoreError):
    """The calendar could not be written; the previous file is untouched."""


class ScheduleBackupError(ScheduleStoreError):
    """The previous calendar could not be backed up, so nothing was written.

    We would rather refuse the save than replace a good file without a
    fallback: "keep a copy first" is exactly the guarantee that must not be
    quietly skipped.
    """


class ScheduleMutationError(ScheduleStoreError):
    """``update_schedule`` was given a mutator that cannot be used."""


# --------------------------------------------------------------------------
# Defensive, lazy access to the calendar module
# --------------------------------------------------------------------------
#
# ``calendar.py`` is a sibling module that owns the calendaring rules.  We
# never import it eagerly: if it is missing (a partial install, a test that
# exercises persistence alone) importing the store must still succeed — a
# persistence layer that cannot be imported cannot recover anything.

def _calendar() -> Any:
    """Return :mod:`claire.schedule.calendar`, or ``None`` if unavailable.

    Cached (including the failure), so a broken sibling module does not turn
    every call into an import attempt.
    """
    global _CALENDAR_MODULE, _CALENDAR_IMPORT_TRIED
    if _CALENDAR_IMPORT_TRIED:
        return _CALENDAR_MODULE
    _CALENDAR_IMPORT_TRIED = True
    try:
        import importlib

        _CALENDAR_MODULE = importlib.import_module("claire.schedule.calendar")
    except Exception:  # ImportError, SyntaxError in a half-written sibling…
        _CALENDAR_MODULE = None
    return _CALENDAR_MODULE


def reset_calendar_cache() -> None:
    """Forget the cached calendar module (used by tests and by late imports)."""
    global _CALENDAR_MODULE, _CALENDAR_IMPORT_TRIED
    _CALENDAR_MODULE = None
    _CALENDAR_IMPORT_TRIED = False


def _is_version_scalar(value: Any) -> bool:
    """True for the plain scalars we accept as a version identifier."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and bool(value.strip())


def _as_ordinal(value: Any) -> Optional[int]:
    """Return *value* as an integer ordinal, or ``None`` if it is not numeric.

    Versions in this project are strings (``"1"``) but an integer spelling is
    just as meaningful, so both are compared by value rather than by type.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _is_newer_version(found: Any, supported: Any) -> bool:
    """Is the file's version *found* newer than the *supported* one?

    The comparison is numeric whenever both sides are numeric strings (so
    ``"10"`` is correctly newer than ``"9"``, which plain string comparison
    gets wrong).  When the two are not comparable we refuse unless they are
    exactly equal: we cannot prove we understand a version we cannot even
    order, and guessing is how unknown data gets deleted.
    """
    if found == supported:
        return False
    found_ordinal = _as_ordinal(found)
    supported_ordinal = _as_ordinal(supported)
    if found_ordinal is not None and supported_ordinal is not None:
        return found_ordinal > supported_ordinal
    return True


def schedule_version() -> Any:
    """The file version this code understands.

    Prefers the calendar module's ``SCHEDULE_VERSION`` when it is available —
    the calendar owns the schema, we only police it — and falls back to the
    version baked into this module otherwise.
    """
    module = _calendar()
    version = getattr(module, "SCHEDULE_VERSION", None) if module is not None else None
    if _is_version_scalar(version):
        return version
    return _FALLBACK_VERSION


def empty_schedule() -> "Schedule":
    """Return an empty, valid schedule.

    Delegates to the calendar module's ``empty_schedule()`` when it is
    available, so "empty" always means whatever the schema says it means, and
    keeps a structurally valid local fallback for the case where the sibling
    module cannot be imported at all.
    """
    module = _calendar()
    factory = getattr(module, "empty_schedule", None) if module is not None else None
    if callable(factory):
        produced = factory()
        if isinstance(produced, dict):
            return Schedule(produced, existed=False, path=None)
    return Schedule(dict(_FALLBACK_EMPTY), existed=False, path=None)


def _validation_problems(schedule: Mapping[str, Any]) -> List[str]:
    """Return the calendar module's complaints about *schedule*.

    Handles both contracts: ``validate_schedule`` returning a list of problem
    strings, and it raising the calendar's named ``ScheduleError``.  When the
    module is unavailable we fall back to a structural check — never to "no
    problems", because accepting anything would let corrupt data be written
    back over a good file.
    """
    module = _calendar()
    validator = getattr(module, "validate_schedule", None) if module is not None else None
    if not callable(validator):
        return _structural_problems(schedule)

    calendar_error = getattr(module, "ScheduleError", None)
    try:
        reported = validator(schedule)
    except Exception as exc:  # noqa: BLE001 - re-raised as a named error below
        if isinstance(calendar_error, type) and isinstance(exc, calendar_error):
            return [str(exc)]
        raise
    if reported is None:
        return []
    if isinstance(reported, str):
        return [reported]
    try:
        return [str(item) for item in reported]
    except TypeError:
        return [f"validate_schedule returned something unusable: {reported!r}"]


def _structural_problems(schedule: Mapping[str, Any]) -> List[str]:
    """Structural-only validation used when the calendar module is absent.

    This is deliberately shallow.  Its job is to stop obviously broken data
    from being written and reported as valid; the real rules belong to the
    calendar module and are applied whenever it can be imported.
    """
    problems: List[str] = []
    version = schedule.get("version")
    if not _is_version_scalar(version):
        problems.append(f"missing or unusable 'version' (got {version!r})")
    events = schedule.get("events")
    if events is None:
        problems.append("missing 'events' list")
    elif not isinstance(events, list):
        problems.append(f"'events' must be a list, got {type(events).__name__}")
    else:
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                problems.append(
                    f"events[{index}] must be an object, got {type(event).__name__}"
                )
    fired = schedule.get("fired_reminders")
    if fired is not None and not isinstance(fired, list):
        problems.append(
            f"'fired_reminders' must be a list, got {type(fired).__name__}"
        )
    return problems


# --------------------------------------------------------------------------
# The schedule value itself
# --------------------------------------------------------------------------

class Schedule(dict):
    """The loaded schedule: a plain ``dict`` that remembers where it came from.

    Subclassing ``dict`` (rather than wrapping it) keeps every existing
    consumer working — ``schedule["events"]``, ``json.dump(schedule, …)``,
    ``schedule.items()`` — while adding the two facts the caller needs to
    avoid the silent-empty trap:

    ``schedule.existed``
        ``False`` when ``load_schedule`` found no file at all (a normal first
        run), ``True`` when a real file was read.  An empty calendar and an
        absent one look identical in the data and very different in meaning.
    ``schedule.path``
        The file it was loaded from (or would be saved to).
    """

    def __init__(
        self,
        *args: Any,
        existed: bool = True,
        path: Optional[_PathLike] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.existed = bool(existed)
        self.path = Path(path) if path is not None else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        origin = self.path if self.existed else f"{self.path} (absent)"
        return f"<Schedule events={len(self.get('events') or [])} from={origin}>"


class SaveResult(NamedTuple):
    """Where a save landed.

    ``backup`` is the path of the copy of the *previous* file, or ``None``
    when there was no previous file to back up (first save, or the target was
    created by this very call).  It is reported rather than merely created so
    that a caller can tell the owner how to recover.
    """

    path: Path
    backup: Optional[Path]


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

def resolve_schedule_path(path: Optional[_PathLike] = None) -> Path:
    """Return the schedule file path, expanding ``~``.

    Resolution order: explicit *path*, then ``$CLAIRE_SCHEDULE_PATH``, then
    ``~/.local/share/claire/schedule/schedule.json`` — the same data root the
    SQLite database and the memory vault use, so a backup of one directory
    captures everything Claire knows.

    The path is returned even when it does not exist yet: ``save_schedule``
    creates the parent directory on demand.
    """
    if path is not None:
        return Path(path).expanduser()
    env = os.environ.get(SCHEDULE_ENV_VAR)
    if env:
        return Path(env).expanduser()
    return (
        Path.home()
        / ".local"
        / "share"
        / "claire"
        / SCHEDULE_DIRNAME
        / SCHEDULE_FILENAME
    )


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_schedule(path: Optional[_PathLike] = None) -> Schedule:
    """Load the schedule from *path* (or the default location).

    A missing file is not an error — a fresh install has no calendar yet — and
    returns an empty schedule with ``.existed is False`` so the caller can say
    "you have no calendar yet" rather than "your calendar is empty".

    A file that exists but cannot be understood raises a named error quoting
    the path and the problem: :class:`ScheduleParseError` for unreadable or
    malformed JSON, :class:`ScheduleFormatError` for JSON that is not a
    schedule (including a missing/non-integer version), and
    :class:`ScheduleVersionError` for a file from a newer Claire.  We never
    return an empty schedule for a damaged one: the difference between "no
    bookings" and "I could not read your bookings" matters to a person
    deciding whether to trust the answer.
    """
    resolved = resolve_schedule_path(path)

    if not resolved.exists():
        empty = empty_schedule()
        empty.path = resolved
        empty.existed = False
        return empty

    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ScheduleParseError(
            f"schedule file {resolved} exists but could not be read: {exc}"
        ) from exc

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScheduleParseError(
            f"schedule file {resolved} is not valid UTF-8 ({exc}); it may be "
            f"a binary file or a partially written one"
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScheduleParseError(
            f"schedule file {resolved} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno}); the file may be "
            f"truncated or hand-edited incorrectly"
        ) from exc

    if not isinstance(data, dict):
        raise ScheduleFormatError(
            f"schedule file {resolved} must contain a JSON object at the top "
            f"level, found {type(data).__name__}"
        )

    version = data.get("version")
    if not _is_version_scalar(version):
        raise ScheduleFormatError(
            f"schedule file {resolved} has no usable 'version' field "
            f"(found {version!r}); refusing to guess at its layout"
        )

    supported = schedule_version()
    if _is_newer_version(version, supported):
        raise ScheduleVersionError(resolved, version, supported)

    # Shape checks only.  Semantic validation (does this event make sense, do
    # the times fit the day) belongs to the calendar module and to the write
    # path; refusing to *read* an oddly-shaped file would lock the owner out
    # of a calendar he could still repair by hand.
    events = data.get("events")
    if events is not None and not isinstance(events, list):
        raise ScheduleFormatError(
            f"schedule file {resolved} has an 'events' field that is not a "
            f"list (found {type(events).__name__})"
        )
    settings = data.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise ScheduleFormatError(
            f"schedule file {resolved} has a 'settings' field that is not an "
            f"object (found {type(settings).__name__})"
        )
    fired = data.get("fired_reminders")
    if fired is not None and not isinstance(fired, list):
        raise ScheduleFormatError(
            f"schedule file {resolved} has a 'fired_reminders' field that is "
            f"not a list (found {type(fired).__name__})"
        )

    return Schedule(data, existed=True, path=resolved)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _atomic_replace(src: _PathLike, dst: _PathLike) -> None:
    """``os.replace`` behind a seam.

    Kept as a module-level function so tests can make the final rename fail —
    the interesting crash point — and prove the old file survives it.
    """
    os.replace(src, dst)


def _backup_paths(path: Path, keep: int) -> List[Path]:
    return [path.with_name(path.name + BACKUP_SUFFIX_FMT.format(i)) for i in range(1, keep + 1)]


def _rotate_backups(path: Path, keep: int, leftovers: List[Path]) -> Optional[Path]:
    """Copy the current *path* to ``.bak1``, shifting older backups down.

    Called BEFORE the live file is replaced, so ``.bak1`` is always the last
    known-good calendar.  Returns the newest backup path, or ``None`` if there
    was nothing to back up.
    """
    if keep <= 0 or not path.exists():
        return None

    backups = _backup_paths(path, keep)

    # Shift the oldest to the right first: bak(k-1) -> bakk, and so on.  A
    # destination with no source is unlinked so a hand-made stray .bak3 does
    # not survive as a "backup" that rotation would never refresh.
    for index in range(keep - 1, 0, -1):
        source = backups[index - 1]
        destination = backups[index]
        if source.exists():
            _atomic_replace(source, destination)
        elif destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass

    newest = backups[0]
    # Copy via a temp file in the same directory and rename it into place, so
    # an interrupted backup cannot be mistaken for a usable one.
    tmp_path = _write_temp(path.parent, path.name, "bak", leftovers, source=path)
    try:
        _atomic_replace(tmp_path, newest)
    except BaseException:
        _unlink_quietly(tmp_path)
        raise
    return newest


def _unlink_quietly(target: _PathLike) -> None:
    try:
        Path(target).unlink()
    except OSError:
        pass


def _write_temp(
    directory: Path,
    base_name: str,
    tag: str,
    leftovers: List[Path],
    source: Optional[Path] = None,
    payload: Optional[bytes] = None,
) -> Path:
    """Create a temp file next to the target and fill it.

    Exactly one of *source* (copy a file) or *payload* (write bytes) is used.
    The path is registered in *leftovers* immediately, so a failure anywhere
    after creation still cleans it up: a stray ``.tmp`` in the data directory
    is litter, and in a directory the owner is meant to read it is confusing
    litter.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{base_name}.{tag}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    leftovers.append(tmp_path)
    try:
        if source is not None:
            with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
        else:
            with os.fdopen(fd, "wb") as dst:
                dst.write(payload if payload is not None else b"")
                dst.flush()
                os.fsync(dst.fileno())
    except BaseException:
        # fd is closed by fdopen's context manager; only the file must go.
        _unlink_quietly(tmp_path)
        raise
    return tmp_path


def _preserve_mode(tmp_path: Path, target: Path) -> None:
    """Give the new file the target's permissions (or 0o644) — best effort."""
    try:
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        os.chmod(tmp_path, mode)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync, so the rename survives a power cut."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_schedule(
    schedule: Mapping[str, Any],
    path: Optional[_PathLike] = None,
    *,
    keep: int = DEFAULT_BACKUP_COUNT,
) -> SaveResult:
    """Write *schedule* to *path* atomically, backing up the previous file.

    The order matters and is the whole guarantee:

    1. Serialise first.  A schedule that cannot become JSON must fail before
       anything on disk is touched — including before backups are rotated, so
       a bad value cannot push a good backup out of the rotation.
    2. Back up the current file (``.bak1``, with older ones shifted right).
    3. Write a temp file in the same directory, ``fsync`` it, then
       ``os.replace`` it over the target.  A crash therefore leaves either the
       complete old file or the complete new one, and a reader never sees a
       truncated calendar.
    4. Unlink every temp file we made, on success and on failure alike.

    Returns a :class:`SaveResult` naming the file written and the backup
    path, so callers can tell the owner where to recover from.
    """
    if not isinstance(schedule, Mapping):
        raise ScheduleSerialisationError(
            f"refusing to save a schedule that is a {type(schedule).__name__}, "
            f"not a mapping"
        )
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
        raise ScheduleStoreError(f"'keep' must be a non-negative integer, got {keep!r}")

    resolved = resolve_schedule_path(path)

    try:
        # ensure_ascii=False keeps the owner's accents readable in the file;
        # no sort_keys, so key order is preserved exactly as the caller built
        # it (a stable order that survives round-tripping).
        text = json.dumps(dict(schedule), indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ScheduleSerialisationError(
            f"schedule for {resolved} is not JSON-serialisable: {exc}"
        ) from exc

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScheduleWriteError(
            f"could not create the schedule directory {resolved.parent}: {exc}"
        ) from exc

    leftovers: List[Path] = []
    backup: Optional[Path] = None
    try:
        try:
            backup = _rotate_backups(resolved, keep, leftovers)
        except OSError as exc:
            raise ScheduleBackupError(
                f"could not back up the existing schedule {resolved} before "
                f"replacing it: {exc}; nothing was written"
            ) from exc
        except ScheduleStoreError:
            raise
        except BaseException as exc:  # a patched-out os.replace in tests, say
            raise ScheduleBackupError(
                f"could not back up the existing schedule {resolved}: {exc!r}; "
                f"nothing was written"
            ) from exc

        try:
            tmp_path = _write_temp(
                resolved.parent,
                resolved.name,
                "write",
                leftovers,
                payload=(text + "\n").encode("utf-8"),
            )
            _preserve_mode(tmp_path, resolved)
            _atomic_replace(tmp_path, resolved)
        except ScheduleStoreError:
            raise
        except BaseException as exc:
            raise ScheduleWriteError(
                f"could not write the schedule file {resolved}: {exc!r}; the "
                f"previous file is intact"
            ) from exc

        _fsync_directory(resolved.parent)
    finally:
        # Success or failure: no temp file of ours survives this call.
        for leftover in leftovers:
            _unlink_quietly(leftover)

    return SaveResult(path=resolved, backup=backup)


# --------------------------------------------------------------------------
# Load → mutate → validate → save, in one place
# --------------------------------------------------------------------------

def update_schedule(
    mutator: Callable[[Schedule], Any],
    path: Optional[_PathLike] = None,
    *,
    keep: int = DEFAULT_BACKUP_COUNT,
) -> Any:
    """Apply *mutator* to the schedule and save it, all four steps at once.

    Callers cannot forget a step, which is the point: a mutation that is saved
    without validation writes a broken calendar, and a mutation that is valid
    but never saved is a lie told to the owner.  So:

    * the current schedule is loaded (an absent file yields an empty one);
    * ``mutator(schedule)`` runs.  It may mutate in place (return ``None``) or
      return a replacement mapping — the replacement is what gets saved;
    * the result is validated with the calendar module's rules.  If there are
      problems we raise :class:`ScheduleValidationError` naming them and write
      NOTHING, leaving the previous calendar exactly as it was;
    * only then is it saved atomically.

    Returns the mutator's own result when it returned something (so a caller
    can return, say, a status message, and still have the write happen); when
    the mutator returned ``None``, returns the updated schedule.
    """
    if not callable(mutator):
        raise ScheduleMutationError(
            f"update_schedule needs a callable mutator, got {type(mutator).__name__}"
        )

    current = load_schedule(path)
    result = mutator(current)

    if result is None:
        updated: Any = current
        returned: Any = current
    elif isinstance(result, Mapping):
        updated = result
        returned = result
    else:
        # A non-mapping result is not a schedule: keep the in-place mutation
        # (if any) as the thing to save, and hand the value back untouched.
        updated = current
        returned = result

    if not isinstance(updated, Mapping):
        raise ScheduleMutationError(
            f"the mutator produced a {type(updated).__name__}, not a schedule mapping"
        )

    problems = _validation_problems(updated)
    if problems:
        raise ScheduleValidationError(problems, path=resolve_schedule_path(path))

    save_schedule(updated, path, keep=keep)
    return returned
