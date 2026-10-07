"""Retired config keys must not come back — and old config files must still load.

Seven keys were removed from the config surface because nothing read them:

* ``location_city`` — its only consumer was the removed ``getWeather`` tool.
* ``stop_command_fuzzy_ratio``, ``whisper_min_word_length`` — zero readers.
* ``evaluator_enabled`` / ``evaluator_nudge_max`` — the evaluator was retired.
* ``allowlist_bundles`` — a leftover from the removed macOS screen capture.
* ``active_profiles`` — never read by running code.

The owner's live ``config.json`` still contains ``location_city``, so removing a
key from the defaults must not break the load/merge/migrate path: an unknown key
in the file is simply ignored, never a crash.  These tests pin both halves.
"""

import dataclasses
import json

import pytest

from claire.config import Settings, get_default_config, load_settings

pytestmark = pytest.mark.unit

#: The keys retired from the schema.  None may reappear in the defaults or in
#: ``Settings``, and a config file that still sets them must still load.
REMOVED_KEYS = (
    "location_city",
    "stop_command_fuzzy_ratio",
    "whisper_min_word_length",
    "evaluator_enabled",
    "evaluator_nudge_max",
    "allowlist_bundles",
    "active_profiles",
)


def _write_config(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    return cfg_path


def test_defaults_do_not_declare_removed_keys():
    defaults = get_default_config()
    for key in REMOVED_KEYS:
        assert key not in defaults, f"{key} is back in get_default_config()"


def test_settings_has_no_removed_fields():
    field_names = {f.name for f in dataclasses.fields(Settings)}
    for key in REMOVED_KEYS:
        assert key not in field_names, f"{key} is back on Settings"


def test_load_tolerates_a_config_that_still_sets_removed_keys(tmp_path, monkeypatch):
    """A pre-cleanup config file loads cleanly; live keys keep their values."""
    _write_config(
        tmp_path,
        monkeypatch,
        {
            # The owner's real config still sets this one.
            "location_city": "Lisbon",
            "stop_command_fuzzy_ratio": 0.8,
            "whisper_min_word_length": 1,
            "evaluator_enabled": False,
            "evaluator_nudge_max": 1,
            "allowlist_bundles": ["com.apple.Terminal"],
            "active_profiles": ["developer"],
            # Live keys prove the rest of the merge/coercion path still runs.
            "agentic_max_turns": 3,
            "use_stdin": True,
        },
    )

    cfg = load_settings()

    assert isinstance(cfg, Settings)
    assert cfg.agentic_max_turns == 3
    assert cfg.use_stdin is True
    for key in REMOVED_KEYS:
        assert not hasattr(cfg, key), f"{key} was reintroduced as a Settings attribute"


def test_migration_leaves_removed_keys_inert(tmp_path, monkeypatch):
    """The migration/merge path does not resurrect a removed key or crash on it."""
    cfg_path = _write_config(
        tmp_path,
        monkeypatch,
        {"_config_version": 0, "location_city": "Lisbon"},
    )

    cfg = load_settings()

    assert isinstance(cfg, Settings)
    # The file may be rewritten by migrations (e.g. the version stamp), which is
    # fine; what matters is that an unknown key survives untouched and unread.
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "location_city" not in on_disk or on_disk["location_city"] == "Lisbon"
    assert all(key not in vars(cfg) for key in REMOVED_KEYS)
