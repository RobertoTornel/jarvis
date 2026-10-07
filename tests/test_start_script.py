"""The one-command start path must stay machine-independent.

``./start.sh`` is the first thing a fresh user runs, so it is the file most
likely to accumulate a hardcoded home directory, and ``deploy/claire.service``
is a *shipped* unit file, so a hardcoded checkout path there breaks every clone
that is not the author's.

These tests pin the properties that keep both portable:

* no ``/home/<name>``, ``/Users/<name>`` or Windows-drive literal anywhere in
  either file, and the current checkout path itself must not appear;
* the panel's port lives in the panel's source, not copied into the script;
* the unit file carries ``@CLAIRE_REPO_ROOT@`` and renders for *any* checkout;
* the phone bridge is not started a second time (the daemon starts it).

The regexes below are the machine-independent shape (``/home/<name>``), never
one real path, so this test file is not itself an offender.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
START_SH = ROOT / "start.sh"
UNIT_TEMPLATE = ROOT / "deploy" / "claire.service"
PANEL_SERVER = ROOT / "src" / "claire" / "panel" / "server.py"
README = ROOT / "README.md"

REPO_PLACEHOLDER = "@CLAIRE_REPO_ROOT@"

#: The shape of a hardcoded home directory: ``/home/<user>`` or ``/Users/<user>``.
HOME_LITERAL = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")
#: ``C:\...`` - this fork is Linux-only, so a drive letter is always a mistake.
WINDOWS_LITERAL = re.compile(r"[A-Za-z]:\\\\")


def test_start_script_exists_and_is_executable() -> None:
    assert START_SH.is_file(), "the single entry point is missing"
    assert START_SH.stat().st_mode & 0o111, "start.sh must be executable"


def test_start_script_parses_under_bash() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    proc = subprocess.run(
        [bash, "-n", str(START_SH)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("path", [START_SH, UNIT_TEMPLATE], ids=["start.sh", "claire.service"])
def test_no_machine_specific_values(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    match = HOME_LITERAL.search(text)
    assert match is None, f"{path.name} hardcodes a home directory: {match.group(0)}"
    assert not WINDOWS_LITERAL.search(text), f"{path.name} hardcodes a Windows path"
    assert str(ROOT) not in text, f"{path.name} hardcodes this checkout's path"
    assert str(Path.home()) not in text, f"{path.name} hardcodes this user's home"


def test_panel_default_port_is_not_duplicated_in_the_script() -> None:
    """The panel's port belongs to the panel's source, not to the starter."""
    source = PANEL_SERVER.read_text(encoding="utf-8")
    found = re.search(r"^DEFAULT_PORT\s*=\s*(\d+)\s*$", source, re.MULTILINE)
    assert found is not None, "the panel must define DEFAULT_PORT"
    assert found.group(1) not in START_SH.read_text(encoding="utf-8")


def test_start_script_does_not_start_the_phone_bridge_twice() -> None:
    """The daemon starts the bridge; a second instance faults on one GPU."""
    assert "-m claire.phone" not in START_SH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "token",
    ["stop", "restart", "status", "check", "--dry-run", "--install-service", "--no-panel"],
)
def test_start_script_exposes_the_documented_interface(token: str) -> None:
    assert token in START_SH.read_text(encoding="utf-8"), f"start.sh lost '{token}'"


def test_unit_template_uses_a_repo_root_placeholder() -> None:
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    assert REPO_PLACEHOLDER in text
    # systemd's %h is the user's home, not the checkout: %h/jarvis_cloud was the
    # old hardcoded assumption and must not come back.
    assert "%h/jarvis_cloud" not in text


def test_unit_template_renders_for_any_checkout(tmp_path: Path) -> None:
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    checkout = tmp_path / "somewhere" / "else" / "claire"
    rendered = text.replace(REPO_PLACEHOLDER, str(checkout))

    assert REPO_PLACEHOLDER not in rendered
    exec_lines = [ln for ln in rendered.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 1, "the unit must have exactly one ExecStart"
    assert exec_lines[0].endswith("-m claire")
    assert str(checkout) in exec_lines[0]


def test_readme_points_at_the_one_command_start() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "./start.sh" in readme
    # The template must not be installed by a bare copy any more.
    assert "cp deploy/claire.service" not in readme
