"""The speech gate's absolute level floor is configuration, and it is load-bearing.

``noise_gate_margin`` compares an utterance against the room's *own* noise floor,
which is the right test in a loud room and no test at all in a quiet one: with a
floor of ~0.00008 the bar is smaller than the converter's dither. Measured on a
real microphone, dither at 0.0002-0.0015 cleared it and came back from the
recogniser as "Yeah.", "Okay." and "Uh." - 22 such utterances in seven minutes.

``noise_gate_min_rms`` is therefore not a convenience knob: it is the only thing
standing between a quiet room and a stream of hallucinated transcripts, so the
default, the file override and the coercion path are all pinned here.
"""

import dataclasses
import json

import pytest

from claire.config import Settings, get_default_config, load_settings


def _write_config(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    return cfg_path


def test_the_default_floor_is_declared_and_sits_above_measured_dither():
    defaults = get_default_config()

    assert defaults["noise_gate_min_rms"] == pytest.approx(0.002)
    # The loudest hallucination measured in a quiet room was 0.00151, so a
    # default at or below that would have stopped catching the real thing.
    assert defaults["noise_gate_min_rms"] > 0.00151


def test_settings_declares_the_field():
    named = {f.name for f in dataclasses.fields(Settings)}

    assert "noise_gate_min_rms" in named


def test_a_configured_floor_reaches_settings(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"noise_gate_min_rms": 0.004})

    assert load_settings().noise_gate_min_rms == pytest.approx(0.004)


def test_zero_is_honoured_as_disabled_not_replaced_by_the_default(tmp_path, monkeypatch):
    """0.0 means "no absolute floor", which is how a quieter mic opts out."""
    _write_config(tmp_path, monkeypatch, {"noise_gate_min_rms": 0.0})

    assert load_settings().noise_gate_min_rms == 0.0


@pytest.mark.parametrize("value", ["", None, "loud", [], {}, True])
def test_a_junk_value_falls_back_to_the_default(tmp_path, monkeypatch, value):
    """A microphone setting is never guessed at from a value nobody meant."""
    _write_config(tmp_path, monkeypatch, {"noise_gate_min_rms": value})

    assert load_settings().noise_gate_min_rms == pytest.approx(0.002)


def test_a_negative_floor_is_clamped_to_disabled(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"noise_gate_min_rms": -0.5})

    assert load_settings().noise_gate_min_rms == 0.0


def test_the_floor_is_overridable_by_a_string_number(tmp_path, monkeypatch):
    """config.json is hand-edited, so a quoted number is accepted."""
    _write_config(tmp_path, monkeypatch, {"noise_gate_min_rms": "0.003"})

    assert load_settings().noise_gate_min_rms == pytest.approx(0.003)
