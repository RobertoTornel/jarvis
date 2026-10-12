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

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
START_SH = ROOT / "start.sh"
DEPLOY_DIR = ROOT / "deploy"
UNIT_TEMPLATE = DEPLOY_DIR / "claire.service"
PANEL_UNIT_TEMPLATE = DEPLOY_DIR / "claire-panel.service"
DESKTOP_UNIT_TEMPLATE = DEPLOY_DIR / "claire-desktop.service"
DESKTOP_AUTOSTART_TEMPLATE = DEPLOY_DIR / "claire-desktop.desktop"
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


@pytest.mark.parametrize(
    "path",
    [START_SH, UNIT_TEMPLATE, PANEL_UNIT_TEMPLATE, DESKTOP_UNIT_TEMPLATE, DESKTOP_AUTOSTART_TEMPLATE],
    ids=["start.sh", "claire.service", "claire-panel.service", "claire-desktop.service", "claire-desktop.desktop"],
)
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


# --------------------------------------------------------------------------- #
# Startup wiring: the daemon, the panel and the tray app must all come up by
# themselves, each through the mechanism that can actually reach it.
# --------------------------------------------------------------------------- #

def _exec_start(template: Path, checkout: Path) -> str:
    """Render ``template`` for a checkout and return its single ExecStart."""
    rendered = template.read_text(encoding="utf-8").replace(REPO_PLACEHOLDER, str(checkout))
    assert REPO_PLACEHOLDER not in rendered
    exec_lines = [ln for ln in rendered.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 1, f"{template.name} must have exactly one ExecStart"
    assert str(checkout) in exec_lines[0], f"{template.name} did not render the checkout path"
    return exec_lines[0]


def test_panel_unit_renders_and_runs_the_panel(tmp_path: Path) -> None:
    exec_start = _exec_start(PANEL_UNIT_TEMPLATE, tmp_path / "checkout")
    assert exec_start.endswith("-m claire.panel.server")


def test_tray_unit_renders_and_runs_the_tray_app(tmp_path: Path) -> None:
    exec_start = _exec_start(DESKTOP_UNIT_TEMPLATE, tmp_path / "checkout")
    assert exec_start.endswith("-m desktop_app")


def test_tray_unit_is_scoped_to_the_desktop_session() -> None:
    """The trap this pins: the tray needs a display, so it must not be enabled
    at ``default.target`` - with linger that is reached at boot, before any
    session exists, and the failures burn StartLimitBurst so the app is left
    permanently dead by the time the user logs in."""
    text = DESKTOP_UNIT_TEMPLATE.read_text(encoding="utf-8")
    install = text.split("[Install]", 1)[1]
    assert "WantedBy=graphical-session.target" in install
    assert "WantedBy=default.target" not in install


def test_tray_app_also_has_a_session_autostart_trigger() -> None:
    """Desktops that never activate graphical-session.target still need a way
    in, and it must go through systemd so the tray is supervised."""
    text = DESKTOP_AUTOSTART_TEMPLATE.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("#")
    assert "[Desktop Entry]" in text
    assert "Type=Application" in text
    exec_lines = [ln for ln in text.splitlines() if ln.startswith("Exec=")]
    assert len(exec_lines) == 1
    assert exec_lines[0].strip() == "Exec=systemctl --user start claire-desktop.service"


def test_start_script_manages_all_three_components() -> None:
    text = START_SH.read_text(encoding="utf-8")
    for token in ("claire-panel", "claire-desktop", "claire-desktop.desktop",
                  "--no-desktop", "panel_state", "desktop_state"):
        assert token in text, f"start.sh lost '{token}'"


def test_install_service_installs_and_enables_every_part() -> None:
    text = START_SH.read_text(encoding="utf-8")
    install = text.split("command_install_service()", 1)[1].split("\n}", 1)[0]
    for template_var in ("PANEL_UNIT_TEMPLATE", "DESKTOP_UNIT_TEMPLATE", "DESKTOP_AUTOSTART_TEMPLATE"):
        assert template_var in install, f"--install-service does not install {template_var}"
    assert "systemctl --user enable" in install, "installing without enabling is not startup wiring"

# --------------------------------------------------------------------------- #
# The panel's port probe. ``--install-service`` goes straight to
# ``start_panel`` without the preflight that resolves the interpreter, and
# ``panel_default`` resolves it inside a command substitution (a subshell), so
# the parent shell's ``$PYBIN`` could still be empty when ``port_open`` ran it.
# The probe then ran ``""`` and *every* check failed, including the one for a
# panel that was already serving. This test pins the behaviour end to end: a
# live listener on the panel's port must be seen, without the preflight.
# --------------------------------------------------------------------------- #

def _write_systemctl_stub(bin_dir: Path) -> None:
    """A ``systemctl`` that reports the units as installed and accepts commands.

    Keeps the test off the real user manager: nothing is started, enabled or
    written under the test's own ``HOME``.
    """
    stub = bin_dir / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *list-unit-files*) printf '%s\\n' claire.service claire-panel.service claire-desktop.service ;;\n"
        "  *is-active*) exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

def test_install_service_sees_a_panel_that_is_already_listening(tmp_path: Path) -> None:
    """The false failure this fixes: with the interpreter unresolved, a panel
    already bound to its port was reported as not coming up after the full wait."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_systemctl_stub(bin_dir)

        env = dict(os.environ)
        env.update({
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "CLAIRE_USER_UNIT_DIR": str(tmp_path / "units"),
            "CLAIRE_AUTOSTART_DIR": str(tmp_path / "autostart"),
            "CLAIRE_PANEL_HOST": "127.0.0.1",
            "CLAIRE_PANEL_PORT": str(port),
            "CLAIRE_PANEL_MODE": "systemd",
            "CLAIRE_START_WAIT": "5",
        })

        proc = subprocess.run(
            [str(START_SH), "--install-service", "--no-daemon", "--no-desktop"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        output = proc.stdout + proc.stderr

    assert "did not start listening" not in output, output
    assert f"already listening on 127.0.0.1:{port}" in output, output


def test_install_service_leaves_a_local_drop_in_alone(tmp_path: Path) -> None:
    """Local policy lives in claire.service.d and must survive a re-install.

    claire.service is rendered from deploy/claire.service by --install-service,
    so a hand-edited unit loses its edits. On this machine the ordering that
    waits for the LLM provider (`After=/Wants=omniroute.service`) was lost
    exactly that way once, and a drop-in is the supported place for it: the
    installer writes the unit, never this directory.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_systemctl_stub(bin_dir)

    unit_dir = tmp_path / "units"
    dropin_dir = unit_dir / "claire.service.d"
    dropin_dir.mkdir(parents=True)
    dropin = dropin_dir / "omniroute-ordering.conf"
    dropin.write_text("[Unit]\nAfter=omniroute.service\nWants=omniroute.service\n")

    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CLAIRE_USER_UNIT_DIR": str(unit_dir),
        "CLAIRE_AUTOSTART_DIR": str(tmp_path / "autostart"),
        "CLAIRE_START_WAIT": "5",
    })

    proc = subprocess.run(
        [str(START_SH), "--install-service", "--no-daemon", "--no-panel", "--no-desktop"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert dropin.exists(), "the installer removed a local drop-in directory"
    assert "omniroute.service" in dropin.read_text()
    assert (unit_dir / "claire.service").exists(), "the unit itself must still be installed"
    assert proc.returncode == 0, proc.stdout + proc.stderr
