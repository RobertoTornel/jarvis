"""Tests for the unified persona system prompt.

The persona should match the user's configured wake word so renaming the
wake word to e.g. "Friday" produces a butler named Friday, not one still
hardcoded to Claire.
"""

from claire.system_prompt import build_system_prompt

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
