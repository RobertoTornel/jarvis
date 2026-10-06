"""Unit tests for :mod:`claire.platform.backup` (R-BACKUP-1).

The load-bearing test here is
``test_corrupting_one_byte_fails_verify_and_names_that_file``: it flips a single
byte *without changing the file's length*, so a verification that only compared
sizes would pass.  It asserts the opposite, and names the exact file -- that is
the fail-proof for "verify() hashes contents".

The gate is ``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no
unit marker, hence ``pytestmark = pytest.mark.unit`` below.  Do not remove it
and do not add this file to the guard's baseline list.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from claire.platform import backup
from claire.platform.backup import (
    DEFAULT_SLOTS,
    STATUS_EXTRA,
    STATUS_MISMATCH,
    STATUS_MISSING,
    EmptySnapshotError,
    InvalidSnapshotRequestError,
    ManifestError,
    RestoreError,
    RestoreRefusedError,
    SnapshotNotFoundError,
    SourcePathNotFoundError,
    UnsupportedSourceError,
    list_snapshots,
    load_snapshot,
    restore,
    sha256_file,
    snapshot,
    snapshot_dir,
    verify,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_live_tree(base: Path) -> Path:
    """A small live tree: two nested files plus an empty file."""
    live = base / "live"
    (live / "nested").mkdir(parents=True)
    (live / "notes.txt").write_text("first line\nsecond line\n", encoding="utf-8")
    (live / "nested" / "facts.json").write_text(
        json.dumps({"answer": 42}, indent=2), encoding="utf-8"
    )
    (live / "nested" / "empty.txt").write_text("", encoding="utf-8")
    return live


def _hashes(directory: Path) -> dict:
    """sha256 of every regular file under *directory*, keyed by relative path."""
    return {
        path.relative_to(directory).as_posix(): sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _snapshot_ids(root: Path) -> list:
    """The slot directories under *root* (hidden staging dirs are not slots)."""
    if not root.is_dir():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def _corrupt_one_byte(path: Path) -> None:
    """Flip one bit in *path* without changing its length."""
    data = bytearray(path.read_bytes())
    assert data, f"{path} is empty; pick a file with content to corrupt"
    data[0] ^= 0x01
    path.write_bytes(bytes(data))


# ---------------------------------------------------------------------------
# snapshot + manifest
# ---------------------------------------------------------------------------


def test_snapshot_then_verify_passes_and_manifest_is_readable_json(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"

    snap = snapshot(
        [live / "notes.txt", live / "nested"], label="before settings edit", root=root
    )

    assert snap.snapshot_id.startswith("snap-")
    assert snap.label == "before settings edit"
    assert sorted(record.path for record in snap.files) == [
        "nested/empty.txt",
        "nested/facts.json",
        "notes.txt",
    ]
    assert snap.total_bytes == sum(record.size for record in snap.files)
    # The manifest records the real SHA-256 of the contents, not a placeholder.
    notes = next(r for r in snap.files if r.path == "notes.txt")
    assert notes.sha256 == sha256_file(live / "notes.txt")

    report = verify(snap.snapshot_id, root=root)
    assert report.ok is True
    assert report.checked == 3
    assert len(report.matches) == 3

    # Metadata is human-readable JSON the owner can open in an editor.
    manifest_path = snapshot_dir(snap.snapshot_id, root=root) / "manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert '\n  "' in raw, "manifest should be indented for a human to read"
    manifest = json.loads(raw)
    assert manifest["version"] == backup.SNAPSHOT_VERSION
    assert manifest["snapshot_id"] == snap.snapshot_id
    assert manifest["label"] == "before settings edit"
    assert manifest["slots"] == DEFAULT_SLOTS
    assert [entry["path"] for entry in manifest["files"]] == [
        "nested/empty.txt",
        "nested/facts.json",
        "notes.txt",
    ]
    for entry in manifest["files"]:
        assert set(entry) == {"path", "size", "sha256", "source"}
        assert entry["size"] >= 0
        assert len(entry["sha256"]) == 64
        assert Path(entry["source"]).is_file()

    reloaded = load_snapshot(snap.snapshot_id, root=root)
    assert reloaded.files == snap.files
    assert reloaded.label == snap.label


def test_snapshot_refuses_an_empty_source_and_a_missing_source(tmp_path):
    live = tmp_path / "live"
    (live / "empty-dir").mkdir(parents=True)
    root = tmp_path / "snapshots"

    with pytest.raises(SourcePathNotFoundError) as missing:
        snapshot(live / "nope.txt", label="x", root=root)
    assert "does not exist" in str(missing.value)

    with pytest.raises(EmptySnapshotError) as empty:
        snapshot(live / "empty-dir", label="x", root=root)
    assert "no files" in str(empty.value)
    assert _snapshot_ids(root) == []


def test_snapshot_refuses_a_symlink_source(tmp_path):
    live = _make_live_tree(tmp_path)
    link = tmp_path / "link-to-notes"
    try:
        os.symlink(live / "notes.txt", link)
    except (OSError, NotImplementedError):  # pragma: no cover - exotic filesystem
        pytest.skip("this filesystem does not support symlinks")

    with pytest.raises(UnsupportedSourceError) as refused:
        snapshot(link, label="x", root=tmp_path / "snapshots")
    assert "symlink" in str(refused.value)


def test_bad_slots_or_label_or_empty_paths_are_named_errors(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"

    with pytest.raises(InvalidSnapshotRequestError):
        snapshot(live / "notes.txt", label="x", root=root, slots=0)
    with pytest.raises(InvalidSnapshotRequestError):
        snapshot(live / "notes.txt", label=None, root=root)
    with pytest.raises(InvalidSnapshotRequestError):
        snapshot([], label="x", root=root)


def test_a_snapshot_that_dies_mid_copy_leaves_no_usable_slot(tmp_path, monkeypatch):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"

    real_copy = backup._copy_and_hash
    calls = {"n": 0}

    def flaky_copy(source, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated disk failure")
        return real_copy(source, destination)

    monkeypatch.setattr(backup, "_copy_and_hash", flaky_copy)
    with pytest.raises(OSError):
        snapshot(live / "nested", label="x", root=root)

    assert _snapshot_ids(root) == [], "a failed snapshot must not become a slot"
    assert [entry.name for entry in root.iterdir()] == [], "staging must be cleaned up"


# ---------------------------------------------------------------------------
# verify - the fail-proof against a size-only check
# ---------------------------------------------------------------------------


def test_corrupting_one_byte_fails_verify_and_names_that_file(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    snap = snapshot(live / "nested", label="before edit", root=root)
    # A directory source lands in the payload under its own name.
    payload_file = (
        snapshot_dir(snap.snapshot_id, root=root) / "payload" / "nested" / "facts.json"
    )
    size_before = payload_file.stat().st_size
    _corrupt_one_byte(payload_file)

    # The whole point: the length is untouched, so a size-only check passes.
    assert payload_file.stat().st_size == size_before
    assert sha256_file(payload_file) != sha256_file(live / "nested" / "facts.json")

    report = verify(snap.snapshot_id, root=root)

    assert report.ok is False
    assert report.problems == ("nested/facts.json",)
    assert [verdict.path for verdict in report.mismatches] == ["nested/facts.json"]
    verdict = report.mismatches[0]
    assert verdict.status == STATUS_MISMATCH
    assert verdict.actual_size == verdict.expected_size  # size alone would not catch it
    assert verdict.actual_sha256 != verdict.expected_sha256
    # The failing file is named in the human-readable report, not just counted.
    assert "nested/facts.json" in report.describe()
    # The untouched files are still reported as fine.
    assert [verdict.path for verdict in report.matches] == ["nested/empty.txt"]


def test_missing_and_extra_files_are_both_reported(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    snap = snapshot(live / "nested", label="before edit", root=root)

    payload = snapshot_dir(snap.snapshot_id, root=root) / "payload" / "nested"
    (payload / "empty.txt").unlink()
    (payload / "stowaway.txt").write_text("not in the manifest\n", encoding="utf-8")

    report = verify(snap.snapshot_id, root=root)

    assert report.ok is False
    assert [verdict.path for verdict in report.missing] == ["nested/empty.txt"]
    assert report.missing[0].status == STATUS_MISSING
    assert [verdict.path for verdict in report.extra] == ["nested/stowaway.txt"]
    assert report.extra[0].status == STATUS_EXTRA
    assert set(report.problems) == {"nested/empty.txt", "nested/stowaway.txt"}
    described = report.describe()
    assert "nested/empty.txt" in described and "nested/stowaway.txt" in described


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_rebuilds_the_tree_byte_for_byte(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    snap = snapshot([live / "notes.txt", live / "nested"], label="before edit", root=root)
    expected = {record.path: record.sha256 for record in snap.files}

    # Wreck the live tree, and leave junk where one restored file must land.
    (live / "notes.txt").write_text("clobbered\n", encoding="utf-8")
    (live / "nested" / "facts.json").write_text("clobbered\n", encoding="utf-8")
    (live / "nested" / "empty.txt").unlink()

    target = tmp_path / "restored"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "facts.json").write_text("stale junk\n", encoding="utf-8")
    (target / "untouched.txt").write_text("not in the snapshot\n", encoding="utf-8")

    result = restore(snap.snapshot_id, target, root=root)

    assert result.verified is True
    assert result.forced is False
    assert sorted(path.relative_to(target).as_posix() for path in result.restored) == [
        "nested/empty.txt",
        "nested/facts.json",
        "notes.txt",
    ]
    restored_hashes = _hashes(target)
    assert "untouched.txt" not in expected
    # A file the snapshot never recorded is left alone: restore is a rollback of
    # what it holds, not a wipe of what it does not.
    assert "untouched.txt" in restored_hashes
    restored_hashes.pop("untouched.txt")
    assert restored_hashes == expected, "restored bytes must match the manifest"
    assert (target / "notes.txt").read_text(encoding="utf-8") == (
        "first line\nsecond line\n"
    )
    assert (target / "nested" / "facts.json").read_text(
        encoding="utf-8"
    ) == json.dumps({"answer": 42}, indent=2)
    assert (
        target / "untouched.txt"
    ).read_text(encoding="utf-8") == "not in the snapshot\n"


def test_restore_refuses_a_corrupt_snapshot_by_name_and_force_proceeds(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    snap = snapshot(
        [live / "nested" / "facts.json", live / "nested" / "empty.txt"],
        label="before edit",
        root=root,
    )

    payload_file = snapshot_dir(snap.snapshot_id, root=root) / "payload" / "facts.json"
    _corrupt_one_byte(payload_file)
    corrupted_bytes = payload_file.read_bytes()

    target = tmp_path / "never-created"
    with pytest.raises(RestoreRefusedError) as refused:
        restore(snap.snapshot_id, target, root=root)

    assert refused.value.snapshot_id == snap.snapshot_id
    assert refused.value.problems == ("facts.json",)
    message = str(refused.value)
    assert snap.snapshot_id in message, "the refusal names the snapshot"
    assert "facts.json" in message, "the refusal names the problem file"
    assert not target.exists(), "a refused restore touches nothing"

    forced = restore(snap.snapshot_id, target, root=root, force=True)

    assert forced.verified is False
    assert forced.forced is True
    assert forced.problems == ("facts.json",)
    assert (target / "facts.json").read_bytes() == corrupted_bytes
    assert (target / "empty.txt").exists()


def test_interrupted_restore_leaves_the_live_files_intact(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (live / name).write_text(f"original {name}\n", encoding="utf-8")
    root = tmp_path / "snapshots"
    snap = snapshot(live, label="before edit", root=root)

    # Change every live file, so a half-done restore is detectable.
    for name in ("a.txt", "b.txt", "c.txt"):
        (live / name).write_text(f"modified {name}\n", encoding="utf-8")
    before = _hashes(live)
    assert set(before) == {"a.txt", "b.txt", "c.txt"}

    real_place = backup._place_file
    calls = {"n": 0}

    def flaky_place(staged, live_path):
        calls["n"] += 1
        if calls["n"] == 3:  # die after two files are already in place
            raise OSError("simulated failure mid-restore")
        return real_place(staged, live_path)

    monkeypatch.setattr(backup, "_place_file", flaky_place)

    with pytest.raises(RestoreError) as failure:
        restore(snap.snapshot_id, live, root=root)

    assert calls["n"] == 3
    assert failure.value.rolled_back is True
    assert "rolled back" in str(failure.value)
    # The originals (the pre-restore live content) still verify exactly.
    assert _hashes(live) == before
    assert (live / "a.txt").read_text(encoding="utf-8") == "modified a.txt\n"
    # No staging litter is left behind in the live tree.
    assert [path.name for path in live.glob(".claire-restore-*")] == []


def test_restore_refuses_a_manifest_path_that_escapes_the_payload(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    snap = snapshot(live / "notes.txt", label="before edit", root=root)
    slot = snapshot_dir(snap.snapshot_id, root=root)

    outside = slot / "evil.txt"
    outside.write_text("outside the payload\n", encoding="utf-8")
    manifest_path = slot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "path": "../evil.txt",
            "size": outside.stat().st_size,
            "sha256": sha256_file(outside),
            "source": str(outside),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    target = tmp_path / "target"
    demonstrated = tmp_path / "demonstrated"
    demonstrated.write_text("untouched\n", encoding="utf-8")

    for force in (False, True):
        with pytest.raises(ManifestError):
            restore(snap.snapshot_id, target, root=root, force=force)
    assert not target.exists()
    assert demonstrated.read_text(encoding="utf-8") == "untouched\n"
    assert outside.read_text(encoding="utf-8") == "outside the payload\n"


# ---------------------------------------------------------------------------
# rotation and listing
# ---------------------------------------------------------------------------


def test_rotation_keeps_exactly_four_slots_and_evicts_the_oldest(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    tracked = live / "state.txt"
    root = tmp_path / "snapshots"

    created = []
    for generation in range(5):
        tracked.write_text(f"generation {generation}\n", encoding="utf-8")
        created.append(snapshot(tracked, label=f"gen {generation}", root=root))

    ids = [snap.snapshot_id for snap in created]
    assert ids == [
        "snap-000001",
        "snap-000002",
        "snap-000003",
        "snap-000004",
        "snap-000005",
    ]

    # The 5th exists, the 1st is gone, the middle three survive.
    assert _snapshot_ids(root) == [
        "snap-000002",
        "snap-000003",
        "snap-000004",
        "snap-000005",
    ]
    assert snapshot_dir("snap-000005", root=root).is_dir()
    with pytest.raises(SnapshotNotFoundError) as gone:
        snapshot_dir("snap-000001", root=root)
    assert "snap-000001" in str(gone.value)
    assert "snap-000005" in str(gone.value), "the error lists what is available"

    listed = list_snapshots(root=root)
    assert [info.snapshot_id for info in listed] == [
        "snap-000005",
        "snap-000004",
        "snap-000003",
        "snap-000002",
    ]
    assert [info.label for info in listed] == ["gen 4", "gen 3", "gen 2", "gen 1"]

    # The surviving newest slot still holds the newest content.
    target = tmp_path / "restored"
    restore("snap-000005", target, root=root)
    assert (target / "state.txt").read_text(encoding="utf-8") == "generation 4\n"


def test_list_snapshots_reports_metadata_and_live_verification(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    good = snapshot([live / "notes.txt", live / "nested"], label="good", root=root)
    bad = snapshot(live / "nested" / "facts.json", label="to be corrupted", root=root)

    payload_file = snapshot_dir(bad.snapshot_id, root=root) / "payload" / "facts.json"
    _corrupt_one_byte(payload_file)

    listed = list_snapshots(root=root)
    by_id = {info.snapshot_id: info for info in listed}

    assert by_id[good.snapshot_id].verifies is True
    assert by_id[good.snapshot_id].problems == ()
    assert by_id[good.snapshot_id].label == "good"
    assert by_id[good.snapshot_id].size_bytes == good.total_bytes == sum(
        record.size for record in good.files
    )
    # created_at is a real ISO timestamp the owner can read.
    assert datetime.fromisoformat(by_id[good.snapshot_id].created_at) is not None

    assert by_id[bad.snapshot_id].verifies is False
    assert by_id[bad.snapshot_id].problems == ("facts.json",)
    assert by_id[bad.snapshot_id].label == "to be corrupted"


def test_unknown_snapshot_id_is_a_named_error(tmp_path):
    root = tmp_path / "snapshots"
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    snapshot(tmp_path / "x.txt", label="x", root=root)

    with pytest.raises(SnapshotNotFoundError) as missing:
        verify("snap-000999", root=root)
    assert "snap-000999" in str(missing.value)
    assert "snap-000001" in str(missing.value), "the error lists what is available"


def test_a_slot_with_a_broken_manifest_is_listed_not_hidden(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    good = snapshot(live / "notes.txt", label="good", root=root)
    broken = snapshot(live / "nested" / "facts.json", label="broken", root=root)
    (snapshot_dir(broken.snapshot_id, root=root) / "manifest.json").write_text(
        "{not json at all", encoding="utf-8"
    )

    listed = {info.snapshot_id: info for info in list_snapshots(root=root)}

    assert listed[good.snapshot_id].verifies is True
    assert listed[broken.snapshot_id].verifies is False
    assert listed[broken.snapshot_id].label == ""
    assert listed[broken.snapshot_id].problems
    assert "manifest" in listed[broken.snapshot_id].problems[0]

    # And asking to use it fails by name, with the fix.
    with pytest.raises(ManifestError) as unusable:
        verify(broken.snapshot_id, root=root)
    assert "manifest" in str(unusable.value)
    assert "fresh" in str(unusable.value)


def test_restore_target_that_is_a_file_is_a_named_error(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    snap = snapshot(live / "notes.txt", label="x", root=root)

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("i am a file\n", encoding="utf-8")

    with pytest.raises(backup.RestoreTargetError) as refused:
        restore(snap.snapshot_id, blocker, root=root)
    assert "not a directory" in str(refused.value)
    assert blocker.read_text(encoding="utf-8") == "i am a file\n"


def test_default_root_honours_the_environment_variable(tmp_path, monkeypatch):
    live = _make_live_tree(tmp_path)
    override = tmp_path / "elsewhere"
    monkeypatch.setenv(backup.ROOT_ENV_VAR, str(override))

    assert backup.default_root() == override

    snap = snapshot(live / "notes.txt", label="via env")
    assert (override / snap.snapshot_id / "manifest.json").is_file()
    assert verify(snap.snapshot_id).ok is True


def test_list_snapshots_on_an_untouched_root_is_empty(tmp_path):
    assert list_snapshots(root=tmp_path / "never-used") == ()
    with pytest.raises(SnapshotNotFoundError):
        snapshot_dir("snap-000001", root=tmp_path / "never-used")


def test_restore_refuses_to_replace_a_live_directory_with_a_file(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    snap = snapshot(live / "notes.txt", label="x", root=root)

    target = tmp_path / "target"
    (target / "notes.txt").mkdir(parents=True)
    (target / "notes.txt" / "keep.txt").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(backup.RestoreTargetError) as refused:
        restore(snap.snapshot_id, target, root=root)

    assert "is a directory" in str(refused.value)
    assert (target / "notes.txt" / "keep.txt").read_text(encoding="utf-8") == "keep me\n"


def test_a_dead_staging_directory_is_reaped_but_a_live_one_is_left_alone(tmp_path):
    live = _make_live_tree(tmp_path)
    root = tmp_path / "snapshots"
    root.mkdir()

    stale = root / f"{backup.TEMP_DIR_PREFIX}deadbeef"
    (stale / "payload").mkdir(parents=True)
    fresh = root / f"{backup.TEMP_DIR_PREFIX}cafebabe"
    fresh.mkdir()
    old = time.time() - backup.STALE_TEMP_SECONDS - 60
    os.utime(stale, (old, old))

    snapshot(live / "notes.txt", label="x", root=root)

    assert not stale.exists(), "a killed snapshot's staging is reaped"
    assert fresh.exists(), "a staging directory young enough to be in use is not"
    assert _snapshot_ids(root) == ["snap-000001"]
