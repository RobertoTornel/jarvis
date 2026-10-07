"""Guard: a built distribution must not carry the development trees.

WHAT IS ACTUALLY DEPLOYED.  Claire is not run from site-packages.  The systemd
user unit sets ``PYTHONPATH=<checkout>/src`` and runs
``<checkout>/.venv/bin/python -m claire`` (``deploy/claire.service``, the
``Environment=`` and ``ExecStart=`` lines), and the virtualenv holds an
*editable* install whose ``.pth`` points back at the checkout.  ``tests/`` is a
neighbour there, not a dependency: nothing under ``src/claire`` imports
``tests`` or ``evals``.  So the running install is clean by construction.

What this file pins is the other half of the owner's request — that a *built*
distribution cannot start carrying the suites, and that the exclusion is stated
rather than left to where the directories happen to sit.

TWO FILE SETS, TWO MECHANISMS.

* A **wheel**'s file set is package discovery.  ``[tool.setuptools.packages.find]``
  in ``pyproject.toml`` names the runtime packages positively (``include``) and
  the development trees negatively (``exclude``).  The negative list is the
  load-bearing half: a stray ``__init__.py`` under ``src/``, or a rename that
  relaxes ``include``, is caught by it and not by the positive list.
* An **sdist**'s file set is not package discovery at all.  Distutils' sdist
  command adds ``tests/test*.py`` to *every* source distribution from a legacy
  default (``_add_defaults_optional()`` in setuptools'
  ``_distutils/command/sdist.py``), and ``packages.find.exclude`` does not
  remove them.  ``MANIFEST.in`` has to ``prune`` them, after the default runs.

Measured on this repository before the rules existed: the wheel was already
clean (0 development members), the sdist shipped 201 files under ``tests/``.
Reproduce with the commands in ``docs/subsystems/deployment.md``.

Every check here is deterministic and offline — configuration is parsed and
discovery is re-run; no build, no network.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"
_MANIFEST = _ROOT / "MANIFEST.in"
_SRC = _ROOT / "src"

#: Development trees that must never be part of a distribution.  Package
#: discovery only looks in ``src/``, so these sit outside it today; the exclusion
#: exists so that stays true if one is moved, renamed or given an ``__init__.py``.
_DEV_TREES = (
    "tests",
    "evals",
    "scripts",
    "examples",
    "docs",
    "deploy",
    "templates",
    "android",
)

#: The only package roots a distribution may contain.
_RUNTIME_ROOTS = ("claire", "desktop_app")

#: Files a distribution must keep, used to prove the sdist rules do not
#: over-prune the runtime.
_KEEPERS = ("LICENSE", "README.md", "pyproject.toml", "src/claire/__init__.py")


def _find_config() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"][
        "packages"
    ]["find"]


def _discover(where: Path, include, exclude) -> list[str]:
    from setuptools import find_packages

    return find_packages(where=str(where), include=list(include), exclude=list(exclude))


def _exclude() -> list[str]:
    """The declared exclusion, failing with a diagnostic rather than a KeyError."""
    find = _find_config()
    assert "exclude" in find, (
        "packages.find.exclude is missing.  The exclusion has to be stated: "
        "`include` alone does not survive a rename that broadens it, and a stray "
        "__init__.py under src/ would then make a development tree shippable."
    )
    exclude = list(find["exclude"])
    assert exclude, "packages.find.exclude must not be empty"
    return exclude


def _manifest_lines() -> list[str]:
    return [
        line.strip()
        for line in _MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _decoy_src(tmp_path: Path) -> Path:
    """A ``src/`` where the development trees look exactly like packages."""
    src = tmp_path / "src"
    for name in (*_RUNTIME_ROOTS, "tests", "evals", "scripts"):
        package = src / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    return src


def test_pyproject_states_the_exclusion_explicitly() -> None:
    find = _find_config()
    assert find["where"] == ["src"]
    assert list(find["include"]) == ["claire*", "desktop_app*"]
    exclude = _exclude()
    for tree in _DEV_TREES:
        assert any(fnmatch.fnmatchcase(tree, pattern) for pattern in exclude), (
            f"{tree}/ is not covered by packages.find.exclude={exclude}"
        )


def test_discovery_ships_only_the_runtime_packages() -> None:
    find = _find_config()
    # The *outcome* under the real configuration.  `include` alone happens to be
    # sufficient for this today; that the exclusion is also declared is asserted
    # by the tests above and below, not here.
    found = _discover(_SRC, find["include"], find.get("exclude", []))
    assert found, "discovery found no packages at all, so this check would be vacuous"
    assert "claire" in found and "desktop_app" in found
    for package in found:
        assert package.split(".")[0] in _RUNTIME_ROOTS, (
            f"{package} is not a runtime package, so a wheel would ship it"
        )


def test_no_development_tree_is_discoverable_as_a_package() -> None:
    find = _find_config()
    found = _discover(_SRC, find["include"], find.get("exclude", []))
    leaked = [p for p in found if p.split(".")[0] in _DEV_TREES]
    assert not leaked, f"a distribution would ship these development packages: {leaked}"

    for tree in _DEV_TREES:
        assert not (_SRC / tree).exists(), (
            f"src/{tree} exists, which puts a development tree inside the only "
            "directory package discovery scans"
        )


def test_a_stray_development_package_under_src_is_excluded(tmp_path: Path) -> None:
    """The exclusion is doing the work, not luck about where the trees sit."""
    src = _decoy_src(tmp_path)
    include, exclude = _find_config()["include"], _exclude()

    guarded = _discover(src, include, exclude)
    assert sorted(guarded) == ["claire", "desktop_app"]

    # Non-vacuity, first half: with no exclusion at all, discovery picks the
    # decoys up.  So the assertion above is the exclusion, not the fixture.
    unguarded = _discover(src, ["*"], [])
    assert {"tests", "evals", "scripts"} <= set(unguarded), (
        "the decoy packages must be discoverable without the exclusion, or this "
        "test proves nothing"
    )

    # Non-vacuity, second half: the exclusion still holds when a rename relaxes
    # `include` to a wildcard, which the positive list alone would not catch.
    relaxed = _discover(src, ["*"], exclude)
    assert {"tests", "evals", "scripts"}.isdisjoint(relaxed)
    assert {"claire", "desktop_app"} <= set(relaxed)


def test_manifest_prunes_every_development_tree() -> None:
    assert _MANIFEST.is_file(), (
        "MANIFEST.in is missing.  setuptools' legacy sdist default adds "
        "tests/test*.py to every source distribution and "
        "packages.find.exclude does not remove them, so without this file an "
        "sdist ships the top-level test suite."
    )
    lines = _manifest_lines()
    for tree in _DEV_TREES:
        assert f"prune {tree}" in lines, f"MANIFEST.in must `prune {tree}`"


def test_the_manifest_removes_the_files_the_sdist_default_adds() -> None:
    """Replay the sdist file list offline: legacy default first, then MANIFEST.in.

    This reproduces the exact leak the sdist had before MANIFEST.in existed --
    every ``tests/test*.py`` in the tree -- and shows the manifest removes all of
    them while leaving the runtime files alone.  No build is performed.
    """
    from setuptools._distutils.filelist import FileList

    legacy = sorted(
        str(path.relative_to(_ROOT)) for path in (_ROOT / "tests").glob("test*.py")
    )
    assert legacy, (
        "expected tests/test*.py in the tree -- these are the files the sdist "
        "default adds, so without them this check would be vacuous"
    )

    filelist = FileList()
    filelist.extend(legacy + list(_KEEPERS))
    assert len(filelist.files) > len(_KEEPERS), "the default must have added files"

    # setuptools warns about the trees that are not in this file list; pytest
    # captures those warnings and shows them only if this test fails.
    for line in _manifest_lines():
        filelist.process_template_line(line)

    leftovers = [f for f in filelist.files if f.split("/")[0] in _DEV_TREES]
    assert not leftovers, (
        f"MANIFEST.in still lets {len(leftovers)} development files into an "
        f"sdist, e.g. {leftovers[:3]}"
    )
    assert sorted(filelist.files) == sorted(_KEEPERS), (
        "the sdist rules must remove only development files"
    )


def test_the_development_suites_stay_runnable_from_a_checkout() -> None:
    """Exclusion is about what ships, never about what exists."""
    for tree in ("tests", "evals"):
        assert (_ROOT / tree).is_dir(), f"{tree}/ must stay in the repository"
    assert list((_ROOT / "tests").glob("test_*.py")), "tests/ must keep its suite"
    assert list((_ROOT / "evals").rglob("*.py")), "evals/ must keep its suite"

    pytest_ini = (_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "testpaths = tests" in pytest_ini, "the gate must still collect tests/"
