"""Nutrition coaching after a logged meal is a configuration knob, and it is
pinned.

Evidence for the default: on the live configuration (2026-10-13) the coaching
call took 16.6-18.0 s and came back with empty content on two calls out of
three, because the model's reasoning consumed its whole 1024-token budget
before the answer began. That is the second model call of two, and the meal is
already committed to the database before it runs, so paying 16-18 s per meal for
text that usually never arrives is the wrong default. The capability stays
available behind the key because it is a product choice, not a broken feature.
"""

import dataclasses
import json

import pytest

from claire.config import Settings, get_default_config, load_settings

pytestmark = pytest.mark.unit


def _write_config(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    return cfg_path


def test_coaching_is_off_by_default():
    """The measured cost of leaving it on is 16-18 s per meal log."""
    assert get_default_config()["nutrition_coaching_enabled"] is False


def test_settings_declares_the_field():
    named = {f.name for f in dataclasses.fields(Settings)}

    assert "nutrition_coaching_enabled" in named


def test_coaching_can_be_turned_on(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"nutrition_coaching_enabled": True})

    assert load_settings().nutrition_coaching_enabled is True


def test_quoted_false_is_not_read_as_true(tmp_path, monkeypatch):
    """"false" must not become True the way bool("false") would."""
    _write_config(tmp_path, monkeypatch, {"nutrition_coaching_enabled": "false"})

    assert load_settings().nutrition_coaching_enabled is False


def test_quoted_true_is_read_as_true(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"nutrition_coaching_enabled": "true"})

    assert load_settings().nutrition_coaching_enabled is True


def test_a_missing_key_falls_back_to_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {})

    assert load_settings().nutrition_coaching_enabled is False
