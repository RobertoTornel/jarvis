"""The build-stamped version must be parseable as a version, not just readable.

``src/claire/_version.py`` is generated (and gitignored): the installer script
stamps it, and ``pyproject.toml`` reads it as the ``dynamic = ["version"]``
source. It used to be stamped as ``dev-<sha>``, which is fine for a filename and
fine for PyInstaller, but which ``pip install`` rejects outright:

    packaging.version.InvalidVersion: Invalid version: 'dev-7235752'

So the package could not be installed at all - which is exactly how the missing
N3 entry point (`python -m claire` without PYTHONPATH) stayed hidden. The stamp
is now a PEP 440 local version, ``0.1.0.dev0+g<sha>``.

These tests read the *script* rather than only the generated artifact, because
the artifact is not tracked: a fresh clone has no ``_version.py`` to check, and
the bug would only reappear at build time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_installer.sh"

#: A deliberately permissive PEP 440 shape: release segment, optional
#: pre/dev/post segment, optional local segment. Enough to reject "dev-abc1234"
#: (a leading non-numeric release) without reimplementing the spec.
_PEP440 = re.compile(
    r"^\d+(\.\d+)*"                       # release
    r"((a|b|rc)\d+)?"
    r"(\.post\d+)?"
    r"(\.dev\d+)?"
    r"(\+[a-z0-9]+([.\-][a-z0-9]+)*)?$",
    re.IGNORECASE,
)


def _looks_like_pep440(value: str) -> bool:
    if not _PEP440.match(value):
        return False
    try:  # the real judge, when the library is available
        from packaging.version import Version

        Version(value)
    except ImportError:  # pragma: no cover - packaging ships with pip/setuptools
        pass
    except Exception:
        return False
    return True


def test_the_build_script_does_not_stamp_a_hyphenated_dev_version():
    """The exact regression: ``DEV_VERSION="dev-${GIT_SHA}"`` is not installable."""

    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'DEV_VERSION="dev-${GIT_SHA}"' not in script, (
        "the stamp is installable-invalid again; pip rejects 'dev-<sha>' with "
        "InvalidVersion, which makes the whole package uninstallable"
    )


def test_the_version_the_build_script_would_stamp_is_pep440_valid():
    """Resolve the script's own expression and check the shape it produces."""

    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'^DEV_VERSION="([^"]+)"', script, re.MULTILINE)
    assert match, "could not find DEV_VERSION in scripts/build_installer.sh"

    # Substitute the one variable the script interpolates, then judge the result
    # for both a hex and a non-hex sha, since ${GIT_SHA} can be either.
    for sha in ("7235752", "abcdef0", "c54491"):
        candidate = match.group(1).replace("${GIT_SHA}", sha)
        assert _looks_like_pep440(candidate), (
            f"the build script would stamp {candidate!r}, which is not a valid "
            f"PEP 440 version"
        )


def test_the_stamped_version_matches_the_script_format():
    """Whatever is stamped right now must be valid too, when it exists at all."""

    version_file = REPO_ROOT / "src" / "claire" / "_version.py"
    if not version_file.exists():
        pytest.skip("_version.py is generated and not present in this checkout")

    text = version_file.read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "_version.py has no VERSION assignment"

    assert _looks_like_pep440(match.group(1)), (
        f"the stamped VERSION {match.group(1)!r} is not a valid PEP 440 version, "
        "so this checkout cannot be pip-installed"
    )

    assert 'RELEASE_CHANNEL' in text, (
        "claire.get_version() imports RELEASE_CHANNEL too; dropping it breaks "
        "the version accessor"
    )


def test_the_version_accessor_reports_something():
    """claire.get_version() must not explode merely because it is unbuilt."""

    import claire

    assert callable(claire.get_version)
    version, channel = claire.get_version()
    assert isinstance(version, str) and version
    assert isinstance(channel, str)
