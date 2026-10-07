# Backup Spec — rotating verified snapshots (R-BACKUP-1)

This specification documents `src/claire/platform/backup.py`, the module that
makes a change revertible: it copies files into a slot, records what it copied,
and can prove later that the copy is still intact before rebuilding it.

Public surface (re-exported from `claire.platform`):

| Function | Contract |
|----------|----------|
| `snapshot(paths, label, root=None, slots=4)` | Copy files/dirs into a new slot; write a manifest; rotate. Returns `Snapshot`. |
| `verify(snapshot_id, root=None)` | Re-hash every payload file against the manifest. Returns `VerifyReport`. |
| `restore(snapshot_id, target, root=None, force=False)` | Rebuild the files under `target`. Returns `RestoreResult`. |
| `list_snapshots(root=None)` | Every slot, newest first, with metadata and live verification. |
| `load_snapshot(snapshot_id, root=None)` | Read one manifest as a `Snapshot`. |
| `snapshot_dir(snapshot_id, root=None)` | The slot directory on disk (for inspection). |
| `sha256_file(path)` | SHA-256 of a file's contents, read in 1 MiB chunks. |
| `default_root()` | `$CLAIRE_BACKUP_ROOT`, else `~/.local/share/claire/snapshots`. |

## On-disk layout

```
<root>/
  snap-000001/
    manifest.json          human-readable JSON (indent=2, sorted keys)
    payload/
      notes.txt            a snapshot of a single file
      memory/              a snapshot of a directory (tree preserved)
        facts.json
  .snap-ab12cd/            in-progress slot; never a valid snapshot
```

A slot directory is *only* created by renaming a hidden temporary directory
after its manifest is written, so a crash mid-snapshot leaves
`.snap-<random>/`, which never matches the `snap-NNNNNN` slot pattern and is
reaped by the next `snapshot()` call. A half-written slot can therefore never be
mistaken for a usable rollback point.

## Manifest

```json
{
  "version": 1,
  "snapshot_id": "snap-000001",
  "label": "before settings edit",
  "created_at": "2026-02-14T09:31:07+00:00",
  "created_at_epoch": 1771061467.412,
  "slots": 4,
  "total_bytes": 4096,
  "sources": ["/home/you/.config/claire/config.json"],
  "files": [
    {"path": "config.json", "size": 2048, "sha256": "…64 hex…",
     "source": "/home/you/.config/claire/config.json"}
  ]
}
```

Every file is recorded with its **payload-relative path, byte size and SHA-256**
plus the absolute location it came from. The path is what `restore()` rebuilds
under `target`; `source` is information for the owner, and is never written to.
A manifest whose `version` is not `1`, whose `files` entry is incomplete, or
whose `sha256` is not 64 lowercase hex characters is refused with
`ManifestError` naming the file.

## Naming inside the payload

A source file lands at `payload/<file name>`; a source directory lands at
`payload/<directory name>/…` with its tree preserved. Two sources that share a
name are disambiguated as `name-2`, `name-3`. Empty directories are not
recorded (the manifest records files), so `restore()` rebuilds files, not empty
directories.

## Rotation — four slots

`snapshot()` keeps at most `slots` (default `DEFAULT_SLOTS = 4`) slots. Slot
numbers increase monotonically (`snap-000001`, `snap-000002`, …); after creating
a new one, the **oldest** slot is deleted until the count is at most `slots`. So
the 5th snapshot exists, the 1st is gone, and the middle three survive. If the
oldest cannot be deleted, `RotationError` names the directory and the fix — the
new snapshot is kept and the excess is reported rather than hidden.

## Verification — contents, not sizes

`verify()` opens every payload file and compares **SHA-256 and size** against
the manifest, then scans the payload for files the manifest does not know about:

| Status | Meaning |
|--------|---------|
| `match` | hash and size both equal the manifest |
| `mismatch` | content differs (a same-length byte flip lands here — a size-only check would pass) |
| `missing` | the manifest records a file that is not in the payload |
| `extra` | the payload holds a file the manifest does not record |

`VerifyReport.ok` is true only when every verdict is `match` and there are no
notes (for example, a missing `payload/` directory). `VerifyReport.problems`
lists the payload-relative path of every file that is not an exact match, and
`describe()` renders a one-line human summary naming each problem file.

## Restore — safe, and refuses corruption

1. **Verify first.** If the snapshot does not verify, `restore()` raises
   `RestoreRefusedError` naming the snapshot id and every problem file, unless
   the caller passes `force=True`. Silently restoring corrupt data is worse than
   refusing; with `force=True` the restore proceeds and the `RestoreResult`
   reports `verified=False` and the problem paths.
2. **Stage completely.** Every file is copied into a staging directory created
   inside `target` (same filesystem, so the final move is a rename and never a
   cross-device copy) and re-hashed as it is staged. A payload file that changed
   between verification and staging aborts the restore before anything live is
   touched.
3. **Move into place atomically.** For each file, the existing live entry is
   moved aside into the rollback area, then the fully-staged file is moved onto
   the live path with `os.replace`. A live file is never truncated before its
   replacement is ready.
4. **Roll back on failure.** If any step fails, every live file that had already
   been replaced is moved back (`RestoreError.rolled_back` says whether that
   succeeded), the staging directory is removed, and the error names the cause.

`restore()` writes and replaces files; it does **not** delete live files that
the snapshot never recorded. A snapshot is a rollback point for what it
contains, not a wipe of what it does not.

Manifest paths are validated before use: a path that is absolute or contains
`..` raises `ManifestError` and nothing outside the payload or target is ever
touched. A crash *between* restores (process killed, no chance to roll back) is
not covered — the in-process rollback restores the live tree on any failure the
process can observe.

## Named errors (all carry an actionable fix)

| Error | Raised when | Fix in the message |
|-------|-------------|--------------------|
| `BackupRootError` | the snapshot root cannot be created/used | points at `CLAIRE_BACKUP_ROOT` |
| `InvalidSnapshotRequestError` | bad `paths`, `label` or `slots` | shows the accepted form |
| `EmptySnapshotError` | the request would copy no files | says it would protect nothing |
| `SourcePathNotFoundError` | a source does not exist | says to check the path |
| `UnsupportedSourceError` | a source (or a file in the tree) is a symlink/socket/FIFO/device | says to resolve or exclude it |
| `SnapshotNotFoundError` | unknown snapshot id | lists the available ids |
| `ManifestError` | manifest missing, not JSON, wrong version, incomplete entry, escaping path | says to delete the slot and take a fresh snapshot |
| `RotationError` | the oldest slot cannot be evicted | gives the `rm -rf` command |
| `RestoreRefusedError` | the snapshot does not verify and `force` was not set | names the snapshot and problem files, offers `force=True` |
| `RestoreTargetError` | target is not a directory, or not writable | says to choose another target |
| `RestoreError` | a restore failed part-way | says whether the live files were rolled back |

Symlinks and special files are **refused, not skipped**: a snapshot that quietly
omits a file would let the owner believe a rollback is complete when it is not.

## Tests

`tests/test_platform_backup.py` (unit, `tmp_path`) covers: snapshot-then-verify;
a one-byte corruption that keeps the file's length and must still fail
verification by name (the fail-proof against a size-only check); missing and
extra files; byte-for-byte restore compared by hash; refusal to restore a
corrupt snapshot by name and `force=True` proceeding; the four-slot rotation
boundary; and an interrupted restore that leaves the live files verifying
exactly as they did before.
