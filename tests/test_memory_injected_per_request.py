"""Memory is a router re-read from disk on every request, never a chat message.

These tests pin the contract of ``claire.system_prompt.build_system_prompt``:

* a rendered router appears in the system prompt under a clear heading;
* the router is re-rendered on **every** assembly, so editing it changes the
  next prompt with no restart and memory is never stale;
* memory costs a **constant** number of bytes per turn — it is folded into the
  system prompt, not appended to the conversation history, so 20 turns cost 20
  constant sections rather than one growing blob (the C-2 evidence);
* a router that fails, is absent, renders nothing, or blows the size budget
  still yields a usable prompt with the failure visible **by name** — memory is
  never silently dropped;
* with no router passed, the legacy prompt is returned byte-for-byte.

The router is duck-typed on purpose: anything exposing
``render_for_prompt() -> str`` works, so these tests inject fakes and never
depend on ``claire.memory.router`` being written yet.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import claire.system_prompt as sp
from claire.memory import notes

# The gate runs `pytest -m unit`. A test file without this marker is
# silently DESELECTED, so it can pass forever without ever running.
pytestmark = pytest.mark.unit

LEGACY_PLAIN = sp._SYSTEM_PROMPT_TEMPLATE.format(name="Claire")
LEGACY_FRIDAY = sp._SYSTEM_PROMPT_TEMPLATE.format(name="Friday")


class FakeRouter:
    """Minimal duck-typed router: content in, rendered text out."""

    def __init__(self, content: str = "## Facts\n- Dentist on Wednesday 16:00") -> None:
        self.content = content
        self.calls = 0

    def render_for_prompt(self) -> str:
        self.calls += 1
        return self.content


class ExplodingRouter:
    def render_for_prompt(self) -> str:
        raise RuntimeError("vault is unreadable")


class FakeRouterModule:
    """Stands in for ``claire.memory.router`` at the lazy-import seam."""

    def __init__(self, router=None, error: Exception | None = None) -> None:
        self._router = router
        self._error = error
        self.load_calls = 0
        self.paths: list = []

    def load_router(self, path=None):
        self.load_calls += 1
        self.paths.append(path)
        if self._error is not None:
            raise self._error
        return self._router


@pytest.fixture
def patched_module(monkeypatch):
    """Install a fake router module at the lazy-import seam."""

    def _install(module):
        monkeypatch.setattr(sp, "_load_router_module", lambda: module)
        return module

    return _install


@pytest.fixture
def empty_vault(monkeypatch, tmp_path):
    """Point the default vault at an empty directory (no CLAIRE.md)."""
    monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# 1. The rendered memory text appears in the system prompt
# ---------------------------------------------------------------------------

class TestRenderedMemoryAppears:
    def test_rendered_memory_is_in_the_prompt_under_a_heading(self):
        router = FakeRouter("## Facts\n- Dentist on Wednesday 16:00")

        prompt = sp.build_system_prompt("Claire", router=router)

        assert "Dentist on Wednesday 16:00" in prompt
        assert sp.MEMORY_SECTION_HEADING in prompt
        assert prompt.startswith(LEGACY_PLAIN)
        assert prompt == f"{LEGACY_PLAIN}\n\n{sp.build_memory_section(router)}"

    def test_memory_router_keyword_is_equivalent_to_router(self):
        assert sp.build_system_prompt(
            "Claire", memory_router=FakeRouter("## Facts\n- box on Tuesdays")
        ) == sp.build_system_prompt("Claire", router=FakeRouter("## Facts\n- box on Tuesdays"))

    def test_explicit_router_wins_over_memory_router_without_duplication(self):
        prompt = sp.build_system_prompt(
            router=FakeRouter("FIRST"), memory_router=FakeRouter("SECOND")
        )

        assert "FIRST" in prompt
        assert "SECOND" not in prompt
        assert prompt.count(sp.MEMORY_SECTION_HEADING) == 1

    def test_assistant_name_still_applies_with_memory(self):
        prompt = sp.build_system_prompt("Friday", router=FakeRouter("## Facts\n- tea at four"))

        assert "named Friday" in prompt
        assert "named Claire" not in prompt
        assert "tea at four" in prompt


# ---------------------------------------------------------------------------
# 2. Editing the router changes the NEXT prompt, with no restart
# ---------------------------------------------------------------------------

class TestRereadFromDiskEveryAssembly:
    def test_editing_the_router_changes_the_next_prompt(self):
        router = FakeRouter("## Facts\n- Dentist on Wednesday 16:00")

        first = sp.build_system_prompt(router=router)
        router.content = "## Facts\n- Dentist moved to Thursday 09:00"
        second = sp.build_system_prompt(router=router)

        assert "Dentist on Wednesday 16:00" in first
        assert "Dentist moved to Thursday 09:00" not in first
        assert "Dentist moved to Thursday 09:00" in second
        assert "Dentist on Wednesday 16:00" not in second
        assert router.calls == 2, "the router must be re-rendered, not cached"

    def test_correcting_the_router_corrects_the_very_next_prompt(self):
        router = FakeRouter("## Facts\n- allergic to shellfish")
        assert "allergic to shellfish" in sp.build_system_prompt(router=router)

        router.content = "## Facts\n- shellfish allergy is an old mistake"
        assert "shellfish allergy is an old mistake" in sp.build_system_prompt(router=router)

    def test_default_vault_router_is_reread_on_each_assembly(
        self, patched_module, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))
        module = patched_module(
            FakeRouterModule(FakeRouter("## Facts\n- flat key is in the drawer"))
        )

        first = sp.build_system_prompt(load_default_router=True)
        second = sp.build_system_prompt(load_default_router=True)

        assert "flat key is in the drawer" in first
        assert "flat key is in the drawer" in second
        assert module.load_calls == 2, "the vault must be read per request"


# ---------------------------------------------------------------------------
# 3. No history growth: memory costs a constant per turn (C-2 evidence)
# ---------------------------------------------------------------------------

class TestNoHistoryGrowth:
    def test_twenty_assemblies_are_byte_identical_and_memory_is_reread(self):
        router = FakeRouter("## Facts\n" + "".join(f"- remembered item {i}\n" for i in range(20)))

        prompts = [sp.build_system_prompt(router=router) for _ in range(20)]

        assert len(set(prompts)) == 1, "same router must yield the same prompt bytes"
        assert router.calls == 20, "memory is re-rendered per request, never cached"
        assert all(p.count(sp.MEMORY_SECTION_HEADING) == 1 for p in prompts)

    def test_memory_bytes_are_constant_across_turns_and_never_a_message(self):
        section_router = FakeRouter(
            "## Facts\n" + "".join(f"- remembered item {i}\n" for i in range(20))
        )
        memory_bytes = len(sp.build_memory_section(section_router).encode("utf-8"))
        expected = len(LEGACY_PLAIN.encode("utf-8")) + 2 + memory_bytes

        history: list[dict] = []  # a real conversation history: turns only
        sizes = []
        for turn in range(20):
            system_prompt = sp.build_system_prompt(router=section_router)
            sizes.append(len(system_prompt.encode("utf-8")))
            history.append({"role": "user", "content": f"turn {turn}"})
            history.append({"role": "assistant", "content": "Very good."})

        assert isinstance(sp.build_system_prompt(router=section_router), str)
        assert set(sizes) == {expected}, "each turn's prompt is the base plus ONE constant section"
        assert sum(sizes) == 20 * expected, "memory is paid per turn, never accumulated"
        # and memory never leaks into the history as its own conversation message
        assert len(history) == 40
        assert all("## Facts" not in message["content"] for message in history)
        assert all(sp.MEMORY_SECTION_HEADING not in message["content"] for message in history)

    def test_memory_section_size_is_the_same_for_every_turn(self):
        router = FakeRouter("## Facts\n- one line")

        sections = {sp.build_memory_section(router) for _ in range(20)}

        assert len(sections) == 1


# ---------------------------------------------------------------------------
# 4. A failing or unavailable router still yields a usable, visible prompt
# ---------------------------------------------------------------------------

class TestFailuresAreVisible:
    def test_router_that_raises_still_yields_usable_prompt(self):
        prompt = sp.build_system_prompt("Claire", router=ExplodingRouter())

        assert LEGACY_PLAIN in prompt, "the persona prompt must survive a broken router"
        assert sp.MEMORY_SECTION_HEADING in prompt
        assert sp.MEMORY_UNAVAILABLE_MARKER in prompt
        assert "RuntimeError" in prompt
        assert "vault is unreadable" in prompt

    def test_object_without_render_for_prompt_is_named(self):
        prompt = sp.build_system_prompt(router=object())

        assert sp.MEMORY_UNAVAILABLE_MARKER in prompt
        assert "render_for_prompt" in prompt
        assert LEGACY_PLAIN in prompt

    def test_router_returning_none_is_named(self):
        class NoneRouter:
            def render_for_prompt(self):
                return None

        prompt = sp.build_system_prompt(router=NoneRouter())

        assert sp.MEMORY_UNAVAILABLE_MARKER in prompt
        assert "returned None" in prompt

    def test_router_rendering_nothing_is_not_silently_empty(self):
        prompt = sp.build_system_prompt(router=FakeRouter("   \n  "))

        assert sp.MEMORY_EMPTY_MARKER in prompt
        assert sp.MEMORY_SECTION_HEADING in prompt
        assert len(sp.build_memory_section(FakeRouter(""))) > len(sp.MEMORY_SECTION_HEADING)

    def test_missing_router_module_is_reported_not_swallowed(self, monkeypatch):
        def boom():
            raise ImportError("No module named 'claire.memory.router'")

        monkeypatch.setattr(sp, "_load_router_module", boom)

        prompt = sp.build_system_prompt(load_default_router=True)

        assert LEGACY_PLAIN in prompt
        assert sp.MEMORY_UNAVAILABLE_MARKER in prompt
        assert "ImportError" in prompt

    def test_load_router_returning_none_is_reported(self, patched_module):
        patched_module(FakeRouterModule(None))

        prompt = sp.build_system_prompt(load_default_router=True)

        assert sp.MEMORY_UNAVAILABLE_MARKER in prompt
        assert "no router" in prompt

    def test_load_failure_from_the_vault_is_visible(self, patched_module):
        patched_module(FakeRouterModule(error=notes.RouterNotFoundError("no CLAIRE.md")))

        prompt = sp.build_system_prompt(load_default_router=True)

        assert LEGACY_PLAIN in prompt
        assert sp.MEMORY_UNAVAILABLE_MARKER in prompt
        assert "RouterNotFoundError" in prompt

    def test_over_budget_router_is_loudly_named(self):
        huge = "## Facts\n" + "x" * (notes.ROUTER_BUDGET_BYTES + 500)

        prompt = sp.build_system_prompt(router=FakeRouter(huge))

        assert sp.MEMORY_OVER_BUDGET_MARKER in prompt
        # the real size is named, so rot is visible and self-correcting
        section = sp.build_memory_section(FakeRouter(huge))
        honest_size = len(f"{sp.MEMORY_SECTION_HEADING}\n\n{huge}".encode("utf-8"))
        assert honest_size > notes.ROUTER_BUDGET_BYTES
        assert str(honest_size) in section
        assert str(notes.ROUTER_BUDGET_BYTES) in section


# ---------------------------------------------------------------------------
# 5. The default-vault seam (paths reused from claire.memory.notes)
# ---------------------------------------------------------------------------

class TestDefaultVaultSeam:
    def test_path_helper_is_reused_from_notes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))

        assert sp.resolve_default_router_path() == notes.resolve_router_path()
        assert sp.resolve_default_router_path() == tmp_path / notes.ROUTER_FILENAME

    def test_budget_comes_from_notes(self):
        assert sp._memory_budget_bytes() == notes.ROUTER_BUDGET_BYTES == 6000

    def test_auto_load_reads_the_vault_when_a_router_file_exists(
        self, patched_module, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))
        (tmp_path / notes.ROUTER_FILENAME).write_text("---\ntitle: Claire\n---\n", encoding="utf-8")
        patched_module(FakeRouterModule(FakeRouter("## Facts\n- sister's birthday is in May")))

        prompt = sp.build_system_prompt(load_default_router=None)  # automatic

        assert "sister's birthday is in May" in prompt

    def test_auto_load_stays_legacy_when_no_router_file_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))

        assert sp.build_system_prompt(load_default_router=None) == LEGACY_PLAIN

    def test_auto_load_keeps_legacy_when_the_router_module_is_absent_and_no_file(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))

        def boom():
            raise ImportError("No module named 'claire.memory.router'")

        monkeypatch.setattr(sp, "_load_router_module", boom)

        # no vault file at all -> automatic mode adds nothing and imports nothing
        assert sp.build_system_prompt(load_default_router=None) == LEGACY_PLAIN

    def test_auto_load_with_a_file_but_no_module_reports_the_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))
        (tmp_path / notes.ROUTER_FILENAME).write_text("body", encoding="utf-8")

        def boom():
            raise ImportError("No module named 'claire.memory.router'")

        monkeypatch.setattr(sp, "_load_router_module", boom)

        prompt = sp.build_system_prompt(load_default_router=None)

        assert LEGACY_PLAIN in prompt
        assert sp.MEMORY_UNAVAILABLE_MARKER in prompt
        assert "ImportError" in prompt


# ---------------------------------------------------------------------------
# 6. No regression: no router passed -> the pre-existing prompt, byte-for-byte
# ---------------------------------------------------------------------------

class TestLegacyPromptUnchanged:
    def test_default_call_is_byte_identical_to_the_pre_memory_prompt(self):
        assert sp.build_system_prompt() == LEGACY_PLAIN

    def test_router_none_is_byte_identical_to_the_pre_memory_prompt(self):
        assert sp.build_system_prompt("Claire", router=None) == LEGACY_PLAIN
        assert sp.build_system_prompt("Claire", memory_router=None) == LEGACY_PLAIN
        assert sp.build_system_prompt(
            "Claire", router=None, memory_router=None, load_default_router=False
        ) == LEGACY_PLAIN

    def test_no_memory_section_or_marker_leaks_into_the_legacy_prompt(self):
        for prompt in (sp.build_system_prompt(), sp.build_system_prompt(router=None)):
            assert sp.MEMORY_SECTION_HEADING not in prompt
            for marker in (
                sp.MEMORY_UNAVAILABLE_MARKER,
                sp.MEMORY_EMPTY_MARKER,
                sp.MEMORY_OVER_BUDGET_MARKER,
            ):
                assert marker not in prompt

    def test_explicit_false_never_loads_the_default_vault(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAIRE_VAULT_DIR", str(tmp_path))
        (tmp_path / notes.ROUTER_FILENAME).write_text("body", encoding="utf-8")

        assert sp.build_system_prompt(load_default_router=False) == LEGACY_PLAIN
        assert sp.build_system_prompt(router=False) == LEGACY_PLAIN

    def test_custom_name_prompt_is_unchanged_without_a_router(self):
        assert sp.build_system_prompt("Friday") == LEGACY_FRIDAY
        assert "named Friday" in sp.build_system_prompt("Friday", router=None)

    def test_blank_name_behaviour_is_unchanged(self):
        assert sp.build_system_prompt("") == LEGACY_PLAIN
        assert sp.build_system_prompt("   ") == LEGACY_PLAIN
        assert sp.build_system_prompt(None) == LEGACY_PLAIN  # type: ignore[arg-type]

    def test_module_imports_when_claire_memory_router_is_absent(self):
        # the router import is lazy: a missing module must not break importing
        # claire.system_prompt (checked in a subprocess so the suite's module
        # identity is untouched)
        src = Path(__file__).resolve().parents[1] / "src"
        code = (
            "import sys\n"
            "sys.modules['claire.memory.router'] = None\n"
            "from claire.system_prompt import build_system_prompt, _SYSTEM_PROMPT_TEMPLATE\n"
            "assert build_system_prompt() == _SYSTEM_PROMPT_TEMPLATE.format(name='Claire')\n"
            "print('ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(src)},
        )

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().endswith("ok")
