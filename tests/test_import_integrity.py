"""Every first-party import must name a module that actually exists.

This is the regression test for a fork leftover that shipped a broken tray app:
commit ``2f1bb6a`` ("drop dictation and non-Linux") deleted
``src/desktop_app/macos_tray.py``, but the composition root in
``src/desktop_app/app.py`` still imported it, unguarded::

    from desktop_app.macos_tray import install_macos_tray_event_guard

The call looked safe because the *guard* used to live inside that module - the
module checked the platform and did nothing on Linux - so deleting the module
deleted the guard along with it. The tray app then died with
``ModuleNotFoundError`` the moment startup reached the tray, which nobody saw
because a startup dialog about an unreachable LLM server used to block first.

A missing module is not something a type checker or a lint rule catches here
(the import is inside a function, and the name is built normally), so the
invariant is asserted directly: walk the tree, collect every absolute
first-party import, and require the target to exist on disk.

Relative imports (``from .sibling import x``) are skipped: they resolve inside
the same package, and a missing sibling is an ``ImportError`` the module's own
tests hit immediately.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The gate is `pytest tests/ -m unit`; a file without this marker is silently
# deselected, which is exactly how a fork leftover crash shipped unnoticed.
pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

FIRST_PARTY_ROOTS = ("claire", "desktop_app")


def _module_exists(module: str) -> bool:
    """True when ``module`` is a file or a package under ``src/``."""
    parts = module.split(".")
    return (
        SRC.joinpath(*parts).with_suffix(".py").is_file()
        or (SRC.joinpath(*parts) / "__init__.py").is_file()
    )


def _first_party_imports() -> list[tuple[Path, int, str]]:
    """(file, line, module) for every absolute first-party import in the tree."""
    found: list[tuple[Path, int, str]] = []
    paths = [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]
    paths += [p for p in (ROOT / "tests").rglob("*.py") if "__pycache__" not in p.parts]
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not this test's business
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                if module.split(".")[0] in FIRST_PARTY_ROOTS:
                    found.append((path, node.lineno, module))
    return found


def test_the_scan_is_not_vacuous() -> None:
    """A walk that finds nothing would make the assertion below meaningless."""
    found = _first_party_imports()

    assert len(found) > 100, f"only found {len(found)} first-party imports"
    assert any(module == "claire.config" for _, _, module in found)


def test_every_first_party_import_names_a_module_that_exists() -> None:
    missing = [
        f"{path.relative_to(ROOT)}:{line} imports {module!r}"
        for path, line, module in _first_party_imports()
        if not _module_exists(module)
    ]

    assert missing == [], "imports point at modules that do not exist:\n" + "\n".join(missing)


@pytest.mark.parametrize("module", ["claire", "desktop_app"])
def test_the_packages_themselves_exist(module: str) -> None:
    assert _module_exists(module)
