"""Platform-level safety services for Claire.

Currently one service: :mod:`claire.platform.backup` - rotating, verified
snapshots that make a change revertible (R-BACKUP-1).  Import the functions
from here rather than reaching into the submodule::

    from claire.platform import snapshot, verify, restore, list_snapshots

The contract (four rotating slots, SHA-256 verification, refuse-to-restore
corrupt data, crash-safe restore) is documented in ``backup.spec.md`` next to
:mod:`claire.platform.backup`.

Sibling submodules in this package are imported directly by their own callers
(``from claire.platform import keys``); this ``__init__`` deliberately re-exports
only the backup API, so importing it does not drag in heavier dependencies that
a sibling module may need.
"""

from claire.platform.backup import (
    DEFAULT_SLOTS,
    MANIFEST_NAME,
    PAYLOAD_DIRNAME,
    ROOT_ENV_VAR,
    SNAPSHOT_VERSION,
    STATUS_EXTRA,
    STATUS_MATCH,
    STATUS_MISMATCH,
    STATUS_MISSING,
    BackupError,
    BackupRootError,
    EmptySnapshotError,
    FileRecord,
    FileVerdict,
    InvalidSnapshotRequestError,
    ManifestError,
    RestoreError,
    RestoreRefusedError,
    RestoreResult,
    RestoreTargetError,
    RotationError,
    Snapshot,
    SnapshotInfo,
    SnapshotNotFoundError,
    SourcePathError,
    SourcePathNotFoundError,
    UnsupportedSourceError,
    VerifyReport,
    build_manifest,
    default_root,
    list_snapshots,
    load_snapshot,
    restore,
    sha256_file,
    snapshot,
    snapshot_dir,
    verify,
)

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
