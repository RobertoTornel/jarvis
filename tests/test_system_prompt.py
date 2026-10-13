"""Tests for the unified persona system prompt.

The persona should match the user's configured wake word so renaming the
wake word to e.g. "Friday" produces a butler named Friday, not one still
hardcoded to Claire.
"""

from claire.system_prompt import build_memory_section, build_system_prompt

import pytest
# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`


class TestBuildSystemPrompt:
    def test_default_name_is_claire(self):
        prompt = build_system_prompt()
        assert "named Claire" in prompt

    def test_custom_name_replaces_claire(self):
        prompt = build_system_prompt("Friday")
        assert "named Friday" in prompt
        assert "named Claire" not in prompt

    def test_lowercase_wake_word_is_capitalised(self):
        prompt = build_system_prompt("friday".capitalize())
        assert "named Friday" in prompt

    def test_blank_name_falls_back_to_claire(self):
        assert "named Claire" in build_system_prompt("")
        assert "named Claire" in build_system_prompt("   ")
        assert "named Claire" in build_system_prompt(None)  # type: ignore[arg-type]


class _OversizedRouter:
    """A router whose rendered text blows the byte budget on purpose."""

    def render_for_prompt(self) -> str:
        facts = "\n".join(f"- fact {i}: " + "detail " * 20 for i in range(400))
        return f"# Claire's always-loaded memory\n\n{facts}"


class TestMemorySectionDescribesTheRouterNotItsStorage:
    """The memory section must stay true on the vault and the store backends.

    The always-loaded router is ``<vault>/CLAIRE.md`` on the vault backend and
    a row in ``memory_documents`` on the store backend.  Wording that names a
    file, a ``notes/`` directory or a disk is false on one of them, so the
    rendered section names the router and its promise instead.
    """

    def _assert_medium_neutral(self, section: str) -> None:
        for untruth in ("CLAIRE.md", "notes/", "disk", "file"):
            assert untruth not in section, (untruth, section)

    def test_unavailable_router_section_names_no_file_or_disk(self):
        section = build_memory_section(None)
        assert "[memory-unavailable]" in section
        # The promise itself must survive the rewording, or the fix is a delete.
        assert "re-read on every request" in section
        assert "next reply with no restart" in section
        self._assert_medium_neutral(section)

    def test_over_budget_marker_sends_detail_to_notes_not_files(self):
        section = build_memory_section(_OversizedRouter())
        assert "[memory-over-budget]" in section
        # The remedy must survive too: detail moves out, pointers stay here.
        assert "Move detail into notes" in section
        self._assert_medium_neutral(section)
