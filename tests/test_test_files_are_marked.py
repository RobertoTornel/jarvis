"""Guard: every test in ``tests/`` is either visible to the unit gate or
deliberately classified as non-unit.

The gate is ``pytest tests/ -m unit``. pytest *deselects* any test whose
markers do not satisfy ``unit``, and deselection is silent: the gate prints a
green summary and the test never runs. That is not hypothetical. When this
guard was rewritten the gate collected 3833 of 5318 tests, i.e. **1485 tests
were deselected, of which only 25 were intentional** (``integration`` /
``e2e`` / ``performance``). The rest had simply never run.

The previous version of this guard looked at whole files by substring:

    if "pytest.mark.unit" in text or "pytestmark" in text:
        continue

so a file with exactly one marked test and a hundred unmarked ones passed.
``tests/test_voice_listener.py`` was exactly that: a single marker, 101 of its
104 tests deselected.

This version asks pytest itself. It runs ``pytest tests/ --collect-only -m ""``
in a subprocess with a small recording plugin, so it sees every collected test
and its own marker set, then fails on any test that is neither ``unit`` nor
deliberately ``integration`` / ``e2e`` / ``performance`` / ``eval``.

Cost: one collection-only subprocess (~4 s here, no test bodies executed), so
it is cheap enough to run inside the gate. There is no cache and no
file-by-file scoping to go stale — the source of truth is pytest's own
collection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

UNIT_MARKER = "unit"

#: Markers that make a deselection deliberate. A test carrying any one of these
#: is a legitimate non-unit test; every other deselected test is a defect.
NON_UNIT_MARKERS = frozenset({"integration", "e2e", "performance", "eval"})

#: Files (relative to ``tests/``) allowed to be invisible to the unit gate.
#:
#: This once held 56 upstream files (1117 tests) that the fork inherited
#: unmarked. They are all classified now: the genuinely external ones carry an
#: ``integration`` / ``e2e`` marker (so they are *deliberately* deselected) and
#: the rest carry a module-level ``pytestmark = pytest.mark.unit``.
#:
#: KEEP THIS EMPTY. Adding an entry re-hides tests instead of classifying them;
#: ``test_deselection_allowlist_stays_empty`` fails if anyone does.
PRE_EXISTING_UNMARKED: list[str] = []

#: Records node id + marker names for every collected item, then exits before
#: running anything. Written to a temporary directory, never into the repo.
_COLLECT_PLUGIN = '''\
import json
import os


def pytest_collection_finish(session):
    payload = [
        {"id": item.nodeid, "markers": sorted({m.name for m in item.iter_markers()})}
        for item in session.items
    ]
    with open(os.environ["CLAIRE_MARKER_DUMP"], "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
'''

_COLLECTION_CACHE: dict[str, list[dict]] = {}


def _collect_every_test(tmp_path_factory) -> list[dict]:
    """Collect ``tests/`` with ``-m ""`` and return one dict per test."""
    plugin_dir = tmp_path_factory.mktemp("marker_plugin")
    (plugin_dir / "claire_marker_dump.py").write_text(_COLLECT_PLUGIN, encoding="utf-8")
    dump_path = tmp_path_factory.mktemp("marker_dump") / "markers.json"

    env = dict(os.environ)
    env["CLAIRE_MARKER_DUMP"] = str(dump_path)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(plugin_dir), str(REPO_ROOT), env.get("PYTHONPATH", "")) if p
    )

    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(TESTS_DIR),
            "-m", "",                      # override addopts' `-m "not performance"`
            "--collect-only", "-q",
            "-p", "no:cacheprovider",
            "-p", "claire_marker_dump",
        ],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not dump_path.exists():
        pytest.fail(
            "the guard could not collect tests/ with `-m \"\"`, so it cannot tell "
            f"which tests the unit gate hides (pytest exited {proc.returncode}).\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}"
        )
    return json.loads(dump_path.read_text(encoding="utf-8"))


def _collection(tmp_path_factory) -> list[dict]:
    """Collected tests, computed once per session (the subprocess is ~4 s)."""
    if "tests" not in _COLLECTION_CACHE:
        _COLLECTION_CACHE["tests"] = _collect_every_test(tmp_path_factory)
    return _COLLECTION_CACHE["tests"]


def _unaccounted(collected: list[dict]) -> list[str]:
    """Node ids the unit gate deselects with no deliberate non-unit marker."""
    out = []
    for item in collected:
        markers = set(item["markers"])
        if UNIT_MARKER in markers or markers & NON_UNIT_MARKERS:
            continue
        out.append(item["id"])
    return out


def _describe(node_ids: list[str], limit_per_file: int = 3) -> str:
    by_file: dict[str, list[str]] = {}
    for node_id in node_ids:
        file_part, _, test_part = node_id.partition("::")
        by_file.setdefault(file_part, []).append(test_part)

    lines = []
    for file_part in sorted(by_file):
        names = by_file[file_part]
        lines.append(f"  {file_part}  ({len(names)} test(s))")
        for name in names[:limit_per_file]:
            lines.append(f"      {name}")
        if len(names) > limit_per_file:
            lines.append(f"      ... and {len(names) - limit_per_file} more")
    return "\n".join(lines)


def test_every_test_is_visible_to_the_unit_gate(tmp_path_factory):
    """No test may be deselected by `-m unit` unless it is marked non-unit.

    Compares pytest's own collection with the marker set on each item, so it
    catches *partial* marking — one marker in a file of a hundred unmarked
    tests — which source-text scanning cannot see.
    """
    collected = _collection(tmp_path_factory)
    assert len(collected) > 1000, (
        "the guard collected almost nothing, so it would pass vacuously; "
        f"got {len(collected)} tests"
    )

    unaccounted = sorted(_unaccounted(collected))
    allowed_files = set(PRE_EXISTING_UNMARKED)
    new = [t for t in unaccounted if t.partition("::")[0] not in allowed_files]

    assert not new, (
        f"{len(new)} test(s) would be DESELECTED by `pytest tests/ -m unit` and "
        "never run:\n"
        + _describe(new)
        + "\n\nFix: add `pytestmark = pytest.mark.unit` near the imports of each "
        "file above (that covers every test in it). If a specific test genuinely "
        "needs a live service, an audio device or minutes of runtime, mark that "
        "test with the honest `@pytest.mark.integration`, `@pytest.mark.e2e` or "
        "`@pytest.mark.performance` instead, so the deselection is deliberate."
    )


def test_deselection_allowlist_stays_empty():
    """The recorded deselection baseline must not grow.

    It is empty on purpose: every file it used to list is now either marked
    ``unit`` or honestly classified as integration/e2e/performance. A new entry
    would silently hide a whole file again.
    """
    assert PRE_EXISTING_UNMARKED == [], (
        "PRE_EXISTING_UNMARKED must stay empty — it is the old 'these files never "
        "run' allowlist. Mark the file `unit`, or mark its genuinely-external "
        "tests integration/e2e/performance. Entries present:\n  "
        + "\n  ".join(PRE_EXISTING_UNMARKED)
    )


def test_the_recorded_baseline_is_still_accurate(tmp_path_factory):
    """Keep the allowlist honest: no entry may be stale (already accounted for)."""
    collected = _collection(tmp_path_factory)
    unaccounted_files = {t.partition("::")[0] for t in _unaccounted(collected)}
    stale = sorted(set(PRE_EXISTING_UNMARKED) - unaccounted_files)
    assert not stale, (
        "These files are listed as unmarked but are now fully accounted for; "
        "remove them from PRE_EXISTING_UNMARKED:\n  " + "\n  ".join(stale)
    )
