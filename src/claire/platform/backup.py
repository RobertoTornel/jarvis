"""Rotating, verified snapshots - roll Claire back to before a change (R-BACKUP-1).

A *snapshot* is a directory containing a copy of the files the caller asked
for (``payload/``) plus a human-readable ``manifest.json`` recording, for every
copied file, its payload-relative path, byte size, original location and
SHA-256 digest.  Snapshots live under a root directory (``CLAIRE_BACKUP_ROOT``,
default ``~/.local/share/claire/snapshots``) and the four most recent are kept;
taking a fifth evicts the oldest.

Why the shape of this module:

* **Verification hashes contents.**  :func:`verify` re-reads every payload file
  and compares SHA-256, not just size.  A same-length byte flip (the corruption
  a size check cannot see) is reported against the exact file by name.
* **Restore refuses corrupt data by name.**  :func:`restore` verifies first and
  raises :class:`RestoreRefusedError` naming the snapshot and the problem files
  unless the caller passes ``force=True``.  Silently restoring corrupt data is
  worse than refusing.
* **Restore never truncates a live file.**  Every file is staged completely in
  a temporary directory inside the target (so the final rename stays on one
  filesystem), then moved into place with :func:`_place_file` (an atomic
  ``os.replace``).  Each live file is moved aside before its replacement lands
  and is moved back if a later step fails, so an interrupted restore leaves the
  live trees exactly as they were.

Symlinks and special files (sockets, FIFOs, devices) are *refused by name*, not
skipped: a snapshot that quietly omits a file would make the owner believe a
rollback is complete when it is not.

Every failure is a named exception carrying an actionable fix; nothing here is
a silent no-op.  See ``backup.spec.md`` next to this file for the full contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "SNAPSHOT_VERSION",
    "DEFAULT_SLOTS",
    "MANIFEST_NAME",
    "PAYLOAD_DIRNAME",
    "ROOT_ENV_VAR",
    "STATUS_MATCH",
    "STATUS_MISMATCH",
    "STATUS_MISSING",
    "STATUS_EXTRA",
    "BackupError",
    "BackupRootError",
    "InvalidSnapshotRequestError",
    "EmptySnapshotError",
    "SourcePathError",
    "SourcePathNotFoundError",
    "UnsupportedSourceError",
    "SnapshotNotFoundError",
    "ManifestError",
    "RotationError",
    "RestoreRefusedError",
    "RestoreTargetError",
    "RestoreError",
    "FileRecord",
    "Snapshot",
    "SnapshotInfo",
    "FileVerdict",
    "VerifyReport",
    "RestoreResult",
    "default_root",
    "sha256_file",
    "build_manifest",
    "snapshot",
    "load_snapshot",
    "snapshot_dir",
    "verify",
    "restore",
    "list_snapshots",
]

#: Manifest schema version.  A manifest with any other value is refused rather
#: than half-understood.
SNAPSHOT_VERSION = 1

#: How many snapshots are kept before the oldest is evicted.
DEFAULT_SLOTS = 4

MANIFEST_NAME = "manifest.json"
PAYLOAD_DIRNAME = "payload"
SNAPSHOT_DIR_PREFIX = "snap-"
SNAPSHOT_INDEX_WIDTH = 6
#: Prefix of the in-progress snapshot directory.  It starts with a dot so it is
#: hidden, and it never matches :data:`_SNAPSHOT_DIR_RE`, so a crash halfway
#: through a snapshot can never leave something that looks like a usable slot.
TEMP_DIR_PREFIX = f".{SNAPSHOT_DIR_PREFIX}"
#: Prefix of the temporary staging directory created inside a restore target.
RESTORE_TEMP_PREFIX = ".claire-restore-"
#: Staging directories older than this are leftovers from a killed snapshot and
#: are safe to remove; newer ones may belong to a running snapshot.
STALE_TEMP_SECONDS = 3600
#: Environment variable that overrides where snapshots are kept.
ROOT_ENV_VAR = "CLAIRE_BACKUP_ROOT"
#: Read size for hashing/copying, 1 MiB.
HASH_CHUNK_BYTES = 1024 * 1024

STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_MISSING = "missing"
STATUS_EXTRA = "extra"

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SNAPSHOT_DIR_RE = re.compile(
    rf"\A{SNAPSHOT_DIR_PREFIX}(\d{{{SNAPSHOT_INDEX_WIDTH}}})\Z"
)

PathLike = Union[str, os.PathLike]
PathInput = Union[PathLike, Iterable[PathLike]]


# ---------------------------------------------------------------------------
# Errors - every one names the thing that failed and the fix.
# ---------------------------------------------------------------------------


class BackupError(Exception):
    """Base class for every snapshot failure raised by this module."""


class BackupRootError(BackupError):
    """The snapshot root cannot be created or used."""


class InvalidSnapshotRequestError(BackupError):
    """The caller asked for something that cannot be snapshotted as stated."""


class EmptySnapshotError(BackupError):
    """The request would produce a snapshot that protects nothing."""


class SourcePathError(BackupError):
    """A path the caller asked to snapshot is unusable."""


class SourcePathNotFoundError(SourcePathError):
    """A path the caller asked to snapshot does not exist."""


class UnsupportedSourceError(SourcePathError):
    """A source is a symlink or a special file this module will not snapshot."""


class SnapshotNotFoundError(BackupError):
    """No snapshot with that id exists under the root."""


class ManifestError(BackupError):
    """A manifest is unreadable, unsupported, or hostile."""


class RotationError(BackupError):
    """The oldest snapshot could not be evicted."""


class RestoreRefusedError(BackupError):
    """Restore refused because the snapshot does not verify.

    Carries :attr:`snapshot_id`, the :attr:`problems` that made it fail and the
    full :attr:`report` so a caller can show the owner exactly which files are
    wrong.  ``force=True`` on :func:`restore` bypasses the refusal.
    """

    def __init__(
        self,
        snapshot_id: str,
        problems: Sequence[str],
        message: Optional[str] = None,
        report: Optional["VerifyReport"] = None,
    ) -> None:
        self.snapshot_id = snapshot_id
        self.problems = tuple(problems)
        self.report = report
        if message is None:
            message = (
                f"refusing to restore snapshot {snapshot_id}: verification failed for "
                f"{len(self.problems)} file(s) ({', '.join(self.problems)}); "
                f"restoring corrupt data is worse than refusing - pass force=True to "
                f"restore it anyway"
            )
        super().__init__(message)


class RestoreTargetError(BackupError):
    """The restore target is unusable (not a directory, or not writable)."""


class RestoreError(BackupError):
    """A restore failed part-way through.

    :attr:`rolled_back` is ``True`` when every live file that had already been
    replaced was put back, so the caller can say honestly whether the live tree
    is intact.
    """

    def __init__(self, message: str, *, rolled_back: bool = True) -> None:
        self.rolled_back = rolled_back
        super().__init__(message)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRecord:
    """One file inside a snapshot payload, as recorded in the manifest."""

    path: str
    size: int
    sha256: str
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "FileRecord":
        if not isinstance(data, dict):
            raise ManifestError(
                f"manifest file entry is not a JSON object: {data!r}; "
                f"delete the snapshot and take a fresh one"
            )
        try:
            path = str(data["path"])
            size = int(data["size"])
            digest = str(data["sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(
                f"manifest file entry is incomplete: {data!r}; every entry needs "
                f"the keys path, size and sha256"
            ) from exc
        if not _SHA256_RE.match(digest):
            raise ManifestError(
                f"manifest file entry {path!r} has an invalid sha256 {digest!r}; "
                f"expected 64 lowercase hex characters"
            )
        return cls(
            path=path,
            size=size,
            sha256=digest,
            source=str(data.get("source") or ""),
        )


def build_manifest(
    snapshot_id: str,
    label: str,
    created_at: str,
    created_at_epoch: float,
    slots: int,
    files: Sequence[FileRecord],
    sources: Sequence[str],
) -> Dict[str, Any]:
    """The manifest payload, exactly as it is written to ``manifest.json``.

    Kept as a plain JSON-safe dict so the owner can read the file by hand.
    """
    return {
        "version": SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id,
        "label": label,
        "created_at": created_at,
        "created_at_epoch": round(float(created_at_epoch), 3),
        "slots": slots,
        "total_bytes": sum(record.size for record in files),
        "sources": list(sources),
        "files": [record.to_dict() for record in files],
    }


@dataclass(frozen=True)
class Snapshot:
    """A snapshot that exists on disk."""

    snapshot_id: str
    label: str
    created_at: str
    created_at_epoch: float
    directory: Path
    files: Tuple[FileRecord, ...]
    sources: Tuple[str, ...]
    total_bytes: int
    slots: int
    version: int = SNAPSHOT_VERSION

    @property
    def payload_dir(self) -> Path:
        return self.directory / PAYLOAD_DIRNAME

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_NAME

    def to_manifest(self) -> Dict[str, Any]:
        return build_manifest(
            snapshot_id=self.snapshot_id,
            label=self.label,
            created_at=self.created_at,
            created_at_epoch=self.created_at_epoch,
            slots=self.slots,
            files=self.files,
            sources=self.sources,
        )

    @classmethod
    def from_manifest(cls, data: Any, directory: Path) -> "Snapshot":
        if not isinstance(data, dict):
            raise ManifestError(
                f"manifest at {directory / MANIFEST_NAME} is not a JSON object; "
                f"delete {directory} and take a fresh snapshot"
            )
        version = data.get("version")
        if version != SNAPSHOT_VERSION:
            raise ManifestError(
                f"manifest at {directory / MANIFEST_NAME} has version {version!r}, "
                f"expected {SNAPSHOT_VERSION}; it was written by a different Claire - "
                f"delete {directory} and take a fresh snapshot"
            )
        raw_files = data.get("files")
        if not isinstance(raw_files, list):
            raise ManifestError(
                f"manifest at {directory / MANIFEST_NAME} has no 'files' list; "
                f"delete {directory} and take a fresh snapshot"
            )
        files = tuple(FileRecord.from_dict(item) for item in raw_files)
        raw_sources = data.get("sources")
        sources = (
            tuple(str(item) for item in raw_sources)
            if isinstance(raw_sources, list)
            else ()
        )
        try:
            epoch = float(data.get("created_at_epoch", 0.0))
        except (TypeError, ValueError):
            epoch = 0.0
        return cls(
            snapshot_id=str(data.get("snapshot_id") or directory.name),
            label=str(data.get("label") or ""),
            created_at=str(data.get("created_at") or ""),
            created_at_epoch=epoch,
            directory=directory,
            files=files,
            sources=sources,
            total_bytes=int(
                data.get("total_bytes", sum(record.size for record in files))
            ),
            slots=int(data.get("slots", DEFAULT_SLOTS)),
            version=SNAPSHOT_VERSION,
        )


@dataclass(frozen=True)
class SnapshotInfo:
    """The row :func:`list_snapshots` reports for one slot."""

    snapshot_id: str
    label: str
    created_at: Optional[str]
    size_bytes: Optional[int]
    verifies: bool
    problems: Tuple[str, ...]
    directory: Path

    @property
    def created_at_epoch(self) -> float:
        if not self.created_at:
            return 0.0
        try:
            return datetime.fromisoformat(self.created_at).timestamp()
        except ValueError:
            return 0.0


@dataclass(frozen=True)
class FileVerdict:
    """What verification found for one file."""

    path: str
    status: str
    expected_sha256: Optional[str] = None
    actual_sha256: Optional[str] = None
    expected_size: Optional[int] = None
    actual_size: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_MATCH

    def detail(self) -> str:
        if self.status == STATUS_MATCH:
            return f"sha256 {self.actual_sha256} matches"
        if self.status == STATUS_MISMATCH:
            return (
                f"content changed (sha256 {self.actual_sha256} != manifest "
                f"{self.expected_sha256}, size {self.actual_size} vs "
                f"{self.expected_size})"
            )
        if self.status == STATUS_MISSING:
            return "missing from the snapshot payload"
        return "extra file that the manifest does not record"


@dataclass(frozen=True)
class VerifyReport:
    """The result of re-hashing a snapshot against its manifest."""

    snapshot_id: str
    label: str
    checked: int
    verdicts: Tuple[FileVerdict, ...]
    notes: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.notes and all(verdict.ok for verdict in self.verdicts)

    @property
    def matches(self) -> Tuple[FileVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == STATUS_MATCH)

    @property
    def mismatches(self) -> Tuple[FileVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == STATUS_MISMATCH)

    @property
    def missing(self) -> Tuple[FileVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == STATUS_MISSING)

    @property
    def extra(self) -> Tuple[FileVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == STATUS_EXTRA)

    @property
    def problems(self) -> Tuple[str, ...]:
        """Payload-relative paths of every file that is not an exact match."""
        return tuple(v.path for v in self.verdicts if not v.ok)

    def describe(self) -> str:
        headline = (
            f"snapshot {self.snapshot_id} ({self.label!r}): "
            f"{self.checked} file(s) checked"
        )
        if self.ok:
            return f"{headline} - all match the manifest"
        counts = []
        if self.mismatches:
            counts.append(f"{len(self.mismatches)} changed")
        if self.missing:
            counts.append(f"{len(self.missing)} missing")
        if self.extra:
            counts.append(f"{len(self.extra)} extra")
        details = "; ".join(
            f"{verdict.path} ({verdict.detail()})"
            for verdict in self.verdicts
            if not verdict.ok
        )
        notes = f" [{' ; '.join(self.notes)}]" if self.notes else ""
        return (
            f"{headline} - FAILED verification: {', '.join(counts) or 'unusable'}"
            f"{notes}; {details}"
        )

    def __str__(self) -> str:
        return self.describe()


@dataclass(frozen=True)
class RestoreResult:
    """What a successful restore wrote."""

    snapshot_id: str
    target: Path
    restored: Tuple[Path, ...]
    verified: bool
    forced: bool
    problems: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def default_root() -> Path:
    """Where snapshots live: ``$CLAIRE_BACKUP_ROOT`` or the user data dir."""
    override = os.environ.get(ROOT_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "claire" / "snapshots"


def _resolve_root(root: Optional[PathLike] = None, *, create: bool = False) -> Path:
    path = default_root() if root is None else Path(root).expanduser()
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupRootError(
                f"cannot create the snapshot root {path}: {exc}; set {ROOT_ENV_VAR} "
                f"to a writable directory, e.g. {ROOT_ENV_VAR}=/home/you/.claire-backups"
            ) from exc
        if not path.is_dir():
            raise BackupRootError(
                f"snapshot root {path} is not a directory; set {ROOT_ENV_VAR} to a "
                f"writable directory"
            )
    return path


def sha256_file(path: PathLike) -> str:
    """SHA-256 of a file's *contents*, read in chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_and_hash(source: Path, destination: Path) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    try:
        shutil.copystat(source, destination, follow_symlinks=False)
    except OSError:
        # Mode/times are a nicety; contents are the contract.
        pass
    return size, digest.hexdigest()


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON next to *path* and rename it into place.

    ``indent=2`` and ``sort_keys=True`` keep the manifest readable for the
    owner; a crash mid-write leaves the previous file untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        _place_file(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _place_file(staged: Path, live: Path) -> None:
    """Atomically move a fully-staged file onto its live path.

    Kept as a module-level seam (the same convention as
    :func:`claire.schedule.store._atomic_replace`) so tests can make the
    replacement fail after earlier files have already been placed - the crash
    point that proves the rollback restores the live files.
    """
    os.replace(staged, live)


def _move_aside(live: Path, aside: Path) -> None:
    """Move an existing live entry out of the way (atomically, never a copy)."""
    os.replace(live, aside)


def _safe_relative(rel: str) -> PurePosixPath:
    """A manifest path that is guaranteed to stay inside the payload."""
    if "\\" in rel:
        raise ManifestError(
            f"manifest path {rel!r} contains a backslash; manifest paths must be "
            f"POSIX-relative"
        )
    path = PurePosixPath(rel)
    if path.is_absolute() or not path.parts or any(part == ".." for part in path.parts):
        raise ManifestError(
            f"manifest path {rel!r} escapes the snapshot payload; refusing to touch "
            f"anything outside it - delete the snapshot and take a fresh one"
        )
    return path


def _payload_path(rel: str) -> Path:
    return Path(*_safe_relative(rel).parts)


def _hash_payload_file(path: Path) -> str:
    """SHA-256 of a payload file, with an unreadable file named as an error."""
    try:
        return sha256_file(path)
    except OSError as exc:
        raise BackupError(
            f"cannot read the snapshot payload file {path}: {exc}; check its "
            f"permissions, or delete the snapshot and take a fresh one"
        ) from exc


def _snapshot_dirs(root: Path) -> List[Tuple[Path, int]]:
    """Every complete-looking snapshot directory under *root*, oldest first."""
    if not root.is_dir():
        return []
    found: List[Tuple[Path, int]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        match = _SNAPSHOT_DIR_RE.match(entry.name)
        if match:
            found.append((entry, int(match.group(1))))
    found.sort(key=lambda item: item[1])
    return found


def _snapshot_ids(root: Path) -> List[str]:
    return [directory.name for directory, _ in _snapshot_dirs(root)]


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


def _normalize_sources(paths: PathInput) -> List[Path]:
    if isinstance(paths, (str, os.PathLike)):
        raw: List[Any] = [paths]
    else:
        try:
            raw = list(paths)
        except TypeError as exc:
            raise InvalidSnapshotRequestError(
                f"paths must be a path or an iterable of paths, got "
                f"{type(paths).__name__}; call snapshot(['/path/to/file'], label='...')"
            ) from exc
    if not raw:
        raise InvalidSnapshotRequestError(
            "paths is empty; pass at least one file or directory to snapshot, "
            "otherwise there is nothing to roll back to"
        )

    sources: List[Path] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, (str, os.PathLike)):
            raise InvalidSnapshotRequestError(
                f"path entries must be str or Path, got {type(item).__name__} "
                f"({item!r})"
            )
        candidate = Path(item).expanduser()
        # Check for a symlink BEFORE resolving: resolve() would quietly replace
        # the link with its target and the caller would never learn that the
        # rollback point holds a different tree than the one they named.
        if candidate.is_symlink():
            raise UnsupportedSourceError(
                f"refusing to snapshot the symlink {candidate}: a rollback point "
                f"must hold real contents, not a link; resolve it first, e.g. "
                f"Path({str(candidate)!r}).resolve()"
            )
        try:
            candidate = candidate.resolve(strict=False)
        except OSError:
            candidate = candidate.absolute()
        if not candidate.exists():
            raise SourcePathNotFoundError(
                f"snapshot source does not exist: {candidate}; check the path, or "
                f"create it before snapshotting"
            )
        if candidate in seen:
            continue
        seen.add(candidate)
        sources.append(candidate)
    return sources


def _iter_tree(source: Path) -> List[Tuple[Path, str]]:
    """Every regular file under *source*, as (absolute path, relative path)."""
    found: List[Tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            candidate = base / name
            if candidate.is_symlink():
                raise UnsupportedSourceError(
                    f"refusing to snapshot {candidate}: it is a symlink, and a "
                    f"snapshot must not silently omit or duplicate a tree; resolve "
                    f"it or exclude that directory"
                )
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            candidate = base / name
            if candidate.is_symlink():
                raise UnsupportedSourceError(
                    f"refusing to snapshot the symlink {candidate}: resolve it and "
                    f"snapshot the target, or exclude it"
                )
            if not candidate.is_file():
                raise UnsupportedSourceError(
                    f"refusing to snapshot {candidate}: it is not a regular file "
                    f"(socket, FIFO or device); exclude it from the snapshot"
                )
            found.append((candidate, candidate.relative_to(source).as_posix()))
    found.sort(key=lambda item: item[1])
    return found


def _copy_one(source_file: Path, rel_path: str, payload_dir: Path) -> FileRecord:
    destination = payload_dir / _payload_path(rel_path)
    size, digest = _copy_and_hash(source_file, destination)
    return FileRecord(
        path=rel_path, size=size, sha256=digest, source=str(source_file)
    )


def snapshot(
    paths: PathInput,
    label: str,
    root: Optional[PathLike] = None,
    slots: int = DEFAULT_SLOTS,
) -> Snapshot:
    """Copy *paths* into a new verified snapshot slot and return it.

    *paths* is one path or an iterable of paths; each may be a regular file or a
    directory (copied recursively).  A directory lands in the payload under its
    own name (``payload/<dirname>/...``); two sources with the same name are
    disambiguated as ``name-2``, ``name-3``.  Taking a snapshot beyond *slots*
    (default :data:`DEFAULT_SLOTS`, four) evicts the oldest slot.

    The snapshot is built in a hidden temporary directory and renamed into place
    only once the manifest is written, so a crash cannot leave a half-snapshot
    that looks usable.
    """
    root_path = _resolve_root(root, create=True)

    if not isinstance(label, str):
        raise InvalidSnapshotRequestError(
            f"label must be a string, got {type(label).__name__} ({label!r}); "
            f"pass something the owner will recognise, e.g. label='before settings edit'"
        )
    label = label.strip()

    if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
        raise InvalidSnapshotRequestError(
            f"slots must be a positive integer, got {slots!r}; the default is "
            f"{DEFAULT_SLOTS}"
        )

    sources = _normalize_sources(paths)

    # Reap leftovers from a snapshot that died before its rename.  These can
    # never be valid slots (TEMP_DIR_PREFIX does not match the slot pattern),
    # and only old ones are touched so a concurrent snapshot in another process
    # is never deleted from under itself.
    reap_before = time.time() - STALE_TEMP_SECONDS
    for entry in root_path.iterdir():
        if not entry.name.startswith(TEMP_DIR_PREFIX):
            continue
        try:
            if entry.stat().st_mtime > reap_before:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)

    staging = Path(
        tempfile.mkdtemp(prefix=TEMP_DIR_PREFIX, dir=str(root_path))
    )
    renamed = False
    try:
        payload_dir = staging / PAYLOAD_DIRNAME
        payload_dir.mkdir(parents=True, exist_ok=True)

        records: List[FileRecord] = []
        used_names: Dict[str, int] = {}
        for source in sources:
            base_name = source.name or "root"
            used_names[base_name] = used_names.get(base_name, 0) + 1
            occurrence = used_names[base_name]
            root_key = (
                base_name if occurrence == 1 else f"{base_name}-{occurrence}"
            )
            if source.is_dir():
                for source_file, rel in _iter_tree(source):
                    records.append(
                        _copy_one(source_file, f"{root_key}/{rel}", payload_dir)
                    )
            else:
                records.append(_copy_one(source, root_key, payload_dir))
        records.sort(key=lambda record: record.path)

        if not records:
            raise EmptySnapshotError(
                f"snapshot({[str(s) for s in sources]!r}, ...) would contain no "
                f"files, so it would protect nothing; pass a file or a non-empty "
                f"directory"
            )

        created_epoch = datetime.now(timezone.utc).timestamp()
        created_at = datetime.fromtimestamp(created_epoch, timezone.utc).isoformat(
            timespec="seconds"
        )

        # Allocate a slot name; another process could take it between the scan
        # and the rename, so retry with the next index if the rename collides.
        existing_slots = _snapshot_dirs(root_path)
        index = (existing_slots[-1][1] + 1) if existing_slots else 1
        final_dir = root_path / f"{SNAPSHOT_DIR_PREFIX}{index:0{SNAPSHOT_INDEX_WIDTH}d}"
        for _ in range(50):
            manifest = build_manifest(
                snapshot_id=final_dir.name,
                label=label,
                created_at=created_at,
                created_at_epoch=created_epoch,
                slots=slots,
                files=records,
                sources=[str(source) for source in sources],
            )
            _write_json_atomic(staging / MANIFEST_NAME, manifest)
            try:
                os.rename(staging, final_dir)
            except OSError:
                index += 1
                final_dir = (
                    root_path / f"{SNAPSHOT_DIR_PREFIX}{index:0{SNAPSHOT_INDEX_WIDTH}d}"
                )
                continue
            renamed = True
            break
        else:
            raise BackupRootError(
                f"could not allocate a snapshot slot under {root_path} after 50 "
                f"attempts; remove stale directories there or point {ROOT_ENV_VAR} "
                f"at a different directory"
            )

        _rotate(root_path, slots)
        return load_snapshot(final_dir.name, root=root_path)
    finally:
        if not renamed:
            shutil.rmtree(staging, ignore_errors=True)


def _rotate(root: Path, slots: int) -> List[str]:
    """Delete the oldest snapshots until at most *slots* remain."""
    entries = _snapshot_dirs(root)
    evicted: List[str] = []
    while len(entries) > slots:
        directory, _ = entries.pop(0)
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise RotationError(
                f"cannot evict the oldest snapshot {directory}: {exc}; remove it by "
                f"hand (rm -rf {directory}) or free disk space"
            ) from exc
        evicted.append(directory.name)
    return evicted


# ---------------------------------------------------------------------------
# load / list
# ---------------------------------------------------------------------------


def load_snapshot(snapshot_id: str, root: Optional[PathLike] = None) -> Snapshot:
    """Read the manifest of an existing snapshot."""
    directory = snapshot_dir(snapshot_id, root=root)
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ManifestError(
            f"snapshot {snapshot_id} has no {MANIFEST_NAME} at {manifest_path}; the "
            f"snapshot is incomplete - delete {directory} and take a fresh one"
        )
    try:
        text = manifest_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise ManifestError(
            f"manifest for snapshot {snapshot_id} is not readable JSON "
            f"({manifest_path}): {exc}; delete {directory} and take a fresh snapshot"
        ) from exc
    return Snapshot.from_manifest(data, directory)


def snapshot_dir(snapshot_id: str, root: Optional[PathLike] = None) -> Path:
    """The directory of an existing snapshot, or a named error."""
    root_path = _resolve_root(root)
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_DIR_RE.match(snapshot_id or ""):
        raise SnapshotNotFoundError(
            f"unknown snapshot id {snapshot_id!r}; ids look like "
            f"{SNAPSHOT_DIR_PREFIX}000001 and the ones under {root_path} are: "
            f"{', '.join(_snapshot_ids(root_path)) or 'none'}"
        )
    directory = root_path / snapshot_id
    if not directory.is_dir():
        raise SnapshotNotFoundError(
            f"snapshot {snapshot_id} not found under {root_path}; available "
            f"snapshots: {', '.join(_snapshot_ids(root_path)) or 'none'}"
        )
    return directory


def list_snapshots(root: Optional[PathLike] = None) -> Tuple[SnapshotInfo, ...]:
    """Every slot under *root*, newest first, with its live verification state.

    Each row carries the id, label, creation time, payload byte size and whether
    the snapshot still verifies (contents are re-hashed, so this reports the
    truth rather than trusting the manifest).  A slot whose manifest cannot be
    read is still listed, with ``verifies=False`` and the reason in
    ``problems``, instead of being hidden.
    """
    root_path = _resolve_root(root)
    infos: List[SnapshotInfo] = []
    for directory, _ in _snapshot_dirs(root_path):
        snapshot_id = directory.name
        try:
            loaded = load_snapshot(snapshot_id, root=root_path)
            report = verify(snapshot_id, root=root_path)
        except ManifestError as exc:
            infos.append(
                SnapshotInfo(
                    snapshot_id=snapshot_id,
                    label="",
                    created_at=None,
                    size_bytes=None,
                    verifies=False,
                    problems=(str(exc),),
                    directory=directory,
                )
            )
            continue
        infos.append(
            SnapshotInfo(
                snapshot_id=snapshot_id,
                label=loaded.label,
                created_at=loaded.created_at or None,
                size_bytes=loaded.total_bytes,
                verifies=report.ok,
                problems=tuple(report.problems) + tuple(report.notes),
                directory=directory,
            )
        )
    infos.sort(key=lambda info: info.snapshot_id, reverse=True)
    return tuple(infos)


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


def verify(snapshot_id: str, root: Optional[PathLike] = None) -> VerifyReport:
    """Re-hash a snapshot's payload against its manifest.

    Every recorded file is compared by SHA-256 *and* size; a file whose content
    changed but whose length did not is still a mismatch, and is named.  Files
    the manifest records that are gone are reported as ``missing``; files in the
    payload that the manifest does not record are reported as ``extra``.
    """
    loaded = load_snapshot(snapshot_id, root=root)
    payload_dir = loaded.payload_dir
    verdicts: List[FileVerdict] = []
    notes: List[str] = []

    if not payload_dir.is_dir():
        notes.append(f"payload directory is missing ({payload_dir})")

    on_disk: set = set()
    if payload_dir.is_dir():
        for path in sorted(payload_dir.rglob("*")):
            candidate_rel = path.relative_to(payload_dir).as_posix()
            if path.is_symlink():
                on_disk.add(candidate_rel)
            elif path.is_file():
                on_disk.add(candidate_rel)

    known: set = set()
    for record in loaded.files:
        _safe_relative(record.path)
        known.add(record.path)
        candidate = payload_dir / _payload_path(record.path)
        if candidate.is_symlink() or not candidate.is_file():
            verdicts.append(
                FileVerdict(
                    path=record.path,
                    status=STATUS_MISSING,
                    expected_sha256=record.sha256,
                    expected_size=record.size,
                )
            )
            continue
        actual_size = candidate.stat().st_size
        actual_sha = _hash_payload_file(candidate)
        status = (
            STATUS_MATCH
            if actual_sha == record.sha256 and actual_size == record.size
            else STATUS_MISMATCH
        )
        verdicts.append(
            FileVerdict(
                path=record.path,
                status=status,
                expected_sha256=record.sha256,
                actual_sha256=actual_sha,
                expected_size=record.size,
                actual_size=actual_size,
            )
        )

    for rel in sorted(on_disk - known):
        candidate = payload_dir / _payload_path(rel)
        actual_size = None
        actual_sha = None
        if candidate.is_file() and not candidate.is_symlink():
            actual_size = candidate.stat().st_size
            actual_sha = _hash_payload_file(candidate)
        verdicts.append(
            FileVerdict(
                path=rel,
                status=STATUS_EXTRA,
                actual_sha256=actual_sha,
                actual_size=actual_size,
            )
        )

    return VerifyReport(
        snapshot_id=loaded.snapshot_id,
        label=loaded.label,
        checked=len(loaded.files),
        verdicts=tuple(verdicts),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------


def _rollback(saved: List[Tuple[Path, Optional[Path]]]) -> bool:
    """Put every live file that had been replaced back.  ``True`` if intact."""
    intact = True
    for live, previous in reversed(saved):
        try:
            if previous is not None:
                if previous.exists() or previous.is_symlink():
                    os.replace(previous, live)
                else:
                    intact = False
            elif live.exists() or live.is_symlink():
                live.unlink()
        except OSError:
            intact = False
    return intact


def restore(
    snapshot_id: str,
    target: PathInput,
    root: Optional[PathLike] = None,
    force: bool = False,
) -> RestoreResult:
    """Rebuild a snapshot's files under *target*.

    The snapshot is verified first: a snapshot that does not verify raises
    :class:`RestoreRefusedError` naming it and the problem files, *unless*
    ``force=True`` - restoring corrupt data silently is worse than refusing.

    Safety: every file is written to a staging directory inside *target* (so the
    final move stays on one filesystem) and only then moved onto its live path
    with an atomic rename.  Existing live files are moved aside first and moved
    back if any later step fails; a live file is never truncated before its
    replacement is complete.
    """
    loaded = load_snapshot(snapshot_id, root=root)
    report = verify(loaded.snapshot_id, root=root)
    if not report.ok and not force:
        raise RestoreRefusedError(
            loaded.snapshot_id,
            report.problems,
            report=report,
            message=(
                f"refusing to restore snapshot {loaded.snapshot_id}: verification "
                f"failed ({len(report.problems)} problem file(s): "
                f"{', '.join(report.problems) or 'payload unusable'}); restoring "
                f"corrupt data is worse than refusing - pass force=True to restore "
                f"it anyway"
            ),
        )

    target_path = Path(target).expanduser()
    if target_path.exists() and not target_path.is_dir():
        raise RestoreTargetError(
            f"restore target {target_path} is not a directory; pass a directory to "
            f"restore the snapshot tree into"
        )
    try:
        target_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestoreTargetError(
            f"cannot create restore target {target_path}: {exc}; check permissions "
            f"or choose another target"
        ) from exc

    try:
        staging = Path(
            tempfile.mkdtemp(prefix=RESTORE_TEMP_PREFIX, dir=str(target_path))
        )
    except OSError as exc:
        raise RestoreTargetError(
            f"cannot create a staging directory inside {target_path}: {exc}; check "
            f"permissions or choose another target"
        ) from exc

    staged_root = staging / "staged"
    rollback_root = staging / "rollback"
    saved: List[Tuple[Path, Optional[Path]]] = []
    restored: List[Path] = []

    try:
        # Phase 1: copy and hash everything.  The live tree is untouched.
        for record in loaded.files:
            relative = _safe_relative(record.path)
            payload_file = loaded.payload_dir / Path(*relative.parts)
            if payload_file.is_symlink() or not payload_file.is_file():
                raise RestoreError(
                    f"snapshot {loaded.snapshot_id} has no payload file for "
                    f"{record.path} (looked in {payload_file}); the snapshot is "
                    f"incomplete - take a fresh one",
                    rolled_back=True,
                )
            staged_file = staged_root / Path(*relative.parts)
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload_file, staged_file)
            actual = sha256_file(staged_file)
            if actual != record.sha256:
                if not force:
                    raise RestoreError(
                        f"payload file {record.path} changed between verification "
                        f"and staging (sha256 {actual} != manifest "
                        f"{record.sha256}); nothing was written - re-run verify and "
                        f"take a fresh snapshot, or pass force=True",
                        rolled_back=True,
                    )

        # Phase 2: move each fully-staged file into place, keeping the file it
        # replaces until the replacement has landed.
        for record in loaded.files:
            relative = _safe_relative(record.path)
            staged_file = staged_root / Path(*relative.parts)
            live_file = target_path / Path(*relative.parts)
            if live_file.is_dir():
                raise RestoreTargetError(
                    f"restore target {live_file} is a directory, but the snapshot "
                    f"records a file there; move that directory aside (or choose "
                    f"another target) so the restore cannot delete it"
                )
            live_file.parent.mkdir(parents=True, exist_ok=True)
            previous: Optional[Path] = None
            if live_file.exists() or live_file.is_symlink():
                previous = rollback_root / Path(*relative.parts)
                previous.parent.mkdir(parents=True, exist_ok=True)
                _move_aside(live_file, previous)
            saved.append((live_file, previous))
            _place_file(staged_file, live_file)
            restored.append(live_file)
    except BaseException as exc:
        rollback_ok = _rollback(saved)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, BackupError) and not isinstance(exc, RestoreError):
            # Already a named error (target unusable, manifest hostile): keep the
            # name the caller can act on.
            raise
        if not saved:
            detail = "no live file had been replaced yet"
        elif rollback_ok:
            detail = "the live files already replaced were rolled back"
        else:
            detail = (
                "ROLLBACK INCOMPLETE - some live files may not be back in place; "
                "check them against the snapshot"
            )
        raise RestoreError(
            f"restore of snapshot {loaded.snapshot_id} into {target_path} failed "
            f"part-way through: {exc}; {detail}",
            rolled_back=rollback_ok,
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return RestoreResult(
        snapshot_id=loaded.snapshot_id,
        target=target_path,
        restored=tuple(restored),
        verified=report.ok,
        forced=bool(force),
        problems=tuple(report.problems),
    )
