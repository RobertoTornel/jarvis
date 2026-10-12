"""``intent_judge_skip_on_wake_word`` is the opt-in latency escape hatch.

The intent judge runs on every turn where a wake word was detected, and on this
machine that call measures about 3.2 s at p50 — the largest avoidable item on
the reply's critical path. The key lets a wake-word turn take the deterministic
path instead. It is opt-in because the judge is also what refuses speech that
merely mentions the name, so the default must stay exactly as it was, and a
hand-edited ``"false"`` must not switch it on.
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


def test_the_default_is_off():
    """The default preserves today's behaviour: the judge still runs."""
    assert get_default_config()["intent_judge_skip_on_wake_word"] is False


def test_settings_declares_the_field():
    named = {f.name for f in dataclasses.fields(Settings)}

    assert "intent_judge_skip_on_wake_word" in named


def test_an_absent_key_loads_as_off(tmp_path, monkeypatch):
    """A config written before this key existed must not change behaviour."""
    _write_config(tmp_path, monkeypatch, {})

    assert load_settings().intent_judge_skip_on_wake_word is False


def test_true_reaches_settings(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"intent_judge_skip_on_wake_word": True})

    assert load_settings().intent_judge_skip_on_wake_word is True


def test_the_string_false_does_not_switch_it_on(tmp_path, monkeypatch):
    """A hand-edited ``"false"`` must stay False, as `bool("false")` would not."""
    _write_config(
        tmp_path, monkeypatch, {"intent_judge_skip_on_wake_word": "false"}
    )

    assert load_settings().intent_judge_skip_on_wake_word is False


@pytest.mark.parametrize("written", ["true", "yes", "on", "1", True, 1, 7])
def test_the_truthy_values_switch_it_on(tmp_path, monkeypatch, written):
    """A non-zero integer is truthy, the same rule the sibling key uses."""
    _write_config(
        tmp_path, monkeypatch, {"intent_judge_skip_on_wake_word": written}
    )

    assert load_settings().intent_judge_skip_on_wake_word is True


@pytest.mark.parametrize("written", ["", "off", "no", "0", "maybe", 0])
def test_anything_else_is_off(tmp_path, monkeypatch, written):
    """An unrecognised value falls back to the safe default rather than raising."""
    _write_config(
        tmp_path, monkeypatch, {"intent_judge_skip_on_wake_word": written}
    )

    assert load_settings().intent_judge_skip_on_wake_word is False
