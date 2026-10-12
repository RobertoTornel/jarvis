"""The follow-up window's knobs are configuration, and every one of them is pinned.

The owner's complaint was that Claire could only answer every other turn: the
renewal cap was a module constant and the live config had the window disabled.
``hot_window_max_follow_ups`` puts that cap in config (``0`` means unlimited),
``hot_window_seconds`` must stay inside the range the settings UI declares, and
``hot_window_require_directed`` is the opt-in strictness that stops background
television driving a turn. Defaults, coercion and clamping are all pinned here
because a silent regression re-opens the multi-turn conversation bug.
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


# ---------------------------------------------------------------------------
# hot_window_max_follow_ups
# ---------------------------------------------------------------------------

def test_the_default_follow_up_count_is_three():
    assert get_default_config()["hot_window_max_follow_ups"] == 3


def test_settings_declares_the_follow_up_field():
    named = {f.name for f in dataclasses.fields(Settings)}

    assert "hot_window_max_follow_ups" in named


def test_a_configured_follow_up_count_reaches_settings(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_max_follow_ups": 5})

    assert load_settings().hot_window_max_follow_ups == 5


def test_a_string_follow_up_count_is_coerced(tmp_path, monkeypatch):
    """config.json is hand-edited, so a quoted number is accepted."""
    _write_config(tmp_path, monkeypatch, {"hot_window_max_follow_ups": "4"})

    assert load_settings().hot_window_max_follow_ups == 4


def test_a_negative_follow_up_count_clamps_to_zero(tmp_path, monkeypatch):
    """0 means unlimited, so a negative value clamps to it rather than to 1."""
    _write_config(tmp_path, monkeypatch, {"hot_window_max_follow_ups": -7})

    assert load_settings().hot_window_max_follow_ups == 0


@pytest.mark.parametrize("value", ["", None, "lots", [], {}])
def test_garbage_follow_up_count_falls_back_to_the_default(tmp_path, monkeypatch, value):
    _write_config(tmp_path, monkeypatch, {"hot_window_max_follow_ups": value})

    assert load_settings().hot_window_max_follow_ups == 3


def test_zero_is_honoured_as_unlimited(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_max_follow_ups": 0})

    assert load_settings().hot_window_max_follow_ups == 0


# ---------------------------------------------------------------------------
# hot_window_seconds
# ---------------------------------------------------------------------------

def test_seconds_are_clamped_into_the_declared_range_from_below(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_seconds": 0.5})

    assert load_settings().hot_window_seconds == pytest.approx(1.0)


def test_seconds_are_clamped_into_the_declared_range_from_above(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_seconds": 120.0})

    assert load_settings().hot_window_seconds == pytest.approx(30.0)


def test_seconds_inside_the_range_are_untouched(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_seconds": 7.5})

    assert load_settings().hot_window_seconds == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# hot_window_require_directed
# ---------------------------------------------------------------------------

def test_strict_mode_defaults_to_off():
    assert get_default_config()["hot_window_require_directed"] is False


def test_settings_declares_the_strict_field():
    named = {f.name for f in dataclasses.fields(Settings)}

    assert "hot_window_require_directed" in named


def test_a_configured_strict_flag_reaches_settings(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_require_directed": True})

    assert load_settings().hot_window_require_directed is True


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True),
    ("false", False), ("False", False), ("0", False), ("", False),
    ("no", False), (None, False), ("maybe", False),
])
def test_strict_flag_strings_are_coerced(tmp_path, monkeypatch, value, expected):
    """A hand-edited config may quote the boolean; "false" must not become True."""
    _write_config(tmp_path, monkeypatch, {"hot_window_require_directed": value})

    assert load_settings().hot_window_require_directed is expected

# ---------------------------------------------------------------------------
# hot_window_grace_seconds
# ---------------------------------------------------------------------------

def test_the_default_grace_band_is_off():
    """0.0 means every follow-up is judged: strict mode stays strict."""
    assert get_default_config()["hot_window_grace_seconds"] == 0.0

def test_settings_declares_the_grace_field():
    named = {f.name for f in dataclasses.fields(Settings)}

    assert "hot_window_grace_seconds" in named

def test_the_grace_band_is_read_from_config(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_grace_seconds": 2.5})

    assert load_settings().hot_window_grace_seconds == 2.5

def test_a_quoted_number_is_accepted(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_grace_seconds": "2.5"})

    assert load_settings().hot_window_grace_seconds == 2.5

def test_a_negative_grace_band_is_clamped_to_zero(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hot_window_grace_seconds": -4.0})

    assert load_settings().hot_window_grace_seconds == 0.0

def test_a_grace_band_longer_than_the_window_is_clamped(tmp_path, monkeypatch):
    """A band longer than the window it lives in is meaningless."""
    _write_config(tmp_path, monkeypatch, {
        "hot_window_seconds": 3.0,
        "hot_window_grace_seconds": 30.0,
    })

    assert load_settings().hot_window_grace_seconds == 3.0

def test_a_non_numeric_grace_band_is_rejected(tmp_path, monkeypatch):
    """A typo must be loud, not silently treated as off."""
    _write_config(tmp_path, monkeypatch, {"hot_window_grace_seconds": "soon"})

    with pytest.raises(TypeError):
        load_settings()
