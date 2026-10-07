"""
Regression tests for the most-reported desktop crash (#503 and friends).

When the PortAudio shared library is missing (common on Linux),
``import sounddevice`` raises **OSError**, not ImportError. Modules that
treat audio as optional must survive that, otherwise importing them kills
the whole app at first launch.

These tests import the modules in a subprocess where the dependency
raises at import time, and assert the modules still load with the
feature disabled (behaviour: app keeps running without audio).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`

ROOT = Path(__file__).resolve().parent.parent

SCENARIO = r"""
import sys

sys.path.insert(0, r"{src}")

# Simulate a broken optional dependency raising at import time.
class _Raiser:
    def find_spec(self, name, path=None, target=None):
        if name == "{dep}":
            raise {error}
        return None

sys.modules.pop("{dep}", None)
sys.meta_path.insert(0, _Raiser())

import {module} as m
assert getattr(m, "{attr}") is None, "{attr} should be None when {dep} is broken"

{extra}
print("OK")
"""


def _run(module: str, dep: str, error: str, attr: str, extra: str = "") -> subprocess.CompletedProcess:
    code = SCENARIO.format(src=str(ROOT / "src"), module=module, dep=dep, error=error, attr=attr, extra=extra)
    # The subprocess inherits the Windows console codepage (cp1252) by
    # default, which cannot encode the emoji in listener.py's graceful
    # PortAudio-failure message — force UTF-8 so the module actually reaches
    # the "loads with the feature disabled" assertion instead of dying on print.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        encoding="utf-8", timeout=60, env=env,
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


def test_listener_imports_without_portaudio():
    """Voice listener module must load (audio disabled) when PortAudio is absent."""
    result = _run(
        "claire.listening.listener",
        dep="sounddevice",
        error='OSError("PortAudio library not found")',
        attr="sd",
    )
    _assert_ok(result)
