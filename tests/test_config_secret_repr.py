"""``repr(Settings)`` must never carry an API key.

This is not a cosmetic property. The repr reaches journals, error messages and
pasted bug reports: a TTS factory error that interpolated a Settings object put
the live ``llm_api_key`` into this machine's log. These tests pin the redaction,
and pin that it stays precise enough to be useful.
"""

from dataclasses import fields

import pytest

from claire.config import SECRET_SETTING_FIELDS, Settings

pytestmark = pytest.mark.unit

# A value distinctive enough that any leak is unmistakable in a failure message.
FAKE_LLM_KEY = "sk-live-must-never-be-printed-0123456789"
FAKE_EMBEDDING_KEY = "emb-live-must-never-be-printed-9876543210"
FAKE_BRAVE_KEY = "brave-live-must-never-be-printed-abcdef"


def _settings(**overrides):
    """A Settings with every field filled with something harmless."""
    values = {}
    for field in fields(Settings):
        if field.name in SECRET_SETTING_FIELDS:
            values[field.name] = ""
        elif field.type in ("str", str):
            values[field.name] = ""
        elif field.type in ("bool", bool):
            values[field.name] = False
        elif field.type in ("int", int):
            values[field.name] = 0
        elif field.type in ("float", float):
            values[field.name] = 0.0
        else:
            # Optional/dict fields: None is accepted by every one of them.
            values[field.name] = None
    values["dialogue_memory_max_tokens"] = 1234
    values.update(overrides)
    return Settings(**values)


class TestSettingsReprHidesSecrets:
    def test_the_api_keys_do_not_appear_in_the_repr(self):
        cfg = _settings(
            llm_api_key=FAKE_LLM_KEY,
            embedding_api_key=FAKE_EMBEDDING_KEY,
            brave_search_api_key=FAKE_BRAVE_KEY,
        )

        rendered = repr(cfg)

        assert FAKE_LLM_KEY not in rendered
        assert FAKE_EMBEDDING_KEY not in rendered
        assert FAKE_BRAVE_KEY not in rendered
        assert rendered.count("<redacted>") == 3

    def test_str_is_the_same_repr_so_printing_it_is_also_safe(self):
        cfg = _settings(llm_api_key=FAKE_LLM_KEY)

        assert FAKE_LLM_KEY not in str(cfg)
        assert FAKE_LLM_KEY not in f"{cfg}"

    def test_an_error_message_that_includes_the_settings_does_not_leak(self):
        """The exact shape of the leak: an f-string with the object in it."""
        cfg = _settings(llm_api_key=FAKE_LLM_KEY)

        message = f"Unknown TTS engine {cfg!r}: refusing to substitute."

        assert FAKE_LLM_KEY not in message

    def test_an_unset_key_is_shown_as_empty_rather_than_redacted(self):
        """An empty key means "not configured", which is worth being able to see."""
        cfg = _settings(llm_api_key="")

        assert "llm_api_key=''" in repr(cfg)

    def test_the_rest_of_the_settings_are_still_readable(self):
        """Redaction must not cost debuggability of everything else."""
        cfg = _settings(llm_chat_model="cmd/deepseek/test-model")

        rendered = repr(cfg)

        assert "llm_chat_model='cmd/deepseek/test-model'" in rendered
        assert "db_path=" in rendered


class TestRedactionStaysPrecise:
    def test_a_token_count_is_not_treated_as_a_credential(self):
        """`dialogue_memory_max_tokens` is a number; hiding it would help nobody."""
        cfg = _settings(dialogue_memory_max_tokens=4321)

        assert "dialogue_memory_max_tokens=4321" in repr(cfg)

    def test_a_key_file_path_stays_visible(self):
        """The path is the first thing worth knowing when Fish fails."""
        cfg = _settings(fish_key_file="/home/someone/keys/fish-keys.txt")

        assert "/home/someone/keys/fish-keys.txt" in repr(cfg)

    def test_every_api_key_field_is_listed_for_redaction(self):
        """A new *_api_key field must be added to the redaction set, not missed."""
        api_key_fields = {f.name for f in fields(Settings) if f.name.endswith("_api_key")}

        assert api_key_fields
        assert api_key_fields <= SECRET_SETTING_FIELDS

    def test_no_field_is_redacted_that_does_not_exist(self):
        """A typo in the set would silently redact nothing."""
        known = {f.name for f in fields(Settings)}

        assert SECRET_SETTING_FIELDS <= known
