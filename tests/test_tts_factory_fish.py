"""Factory wiring for the Fish engine (``create_tts_engine``).

Two contracts are pinned here, both offline (no key file, no socket, no device):

* ``engine="fish"`` builds a :class:`~claire.output.fish_tts.FishTTS` out of the
  ``fish_*`` arguments, falling back to the attributes of the config object with
  :func:`getattr` defaults — so a config written before the Fish fields existed
  still produces a usable engine instead of an ``AttributeError``;
* an *unrecognised* engine name raises ``ValueError`` naming the supported
  engines. The old behaviour — quietly building PiperTTS — meant the user asked
  for one voice and silently heard another, and it is the failure this suite
  exists to prevent (rule N10).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import claire.output.tts as speech
from claire.output.fish_client import FishKeyFileMissingError
from claire.output.fish_tts import (
    DEFAULT_KEY_FILE,
    DEFAULT_LATENCY,
    DEFAULT_MODEL,
    FishTTS,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _fish(**overrides):
    """Build a Fish engine through the factory, offline and disabled."""
    kwargs = {"engine": "fish", "enabled": False}
    kwargs.update(overrides)
    return speech.create_tts_engine(**kwargs)


# --------------------------------------------------------------------------
# The Fish branch
# --------------------------------------------------------------------------

def test_fish_engine_builds_fish_tts(tmp_path):
    engine = _fish(fish_voice_id="v-test", fish_key_file=str(tmp_path / "keys.txt"))

    assert isinstance(engine, FishTTS)
    assert "fish" in type(engine).__name__.lower()
    assert engine.voice_id == "v-test"
    assert engine.key_file == str(tmp_path / "keys.txt")


@pytest.mark.parametrize("name", ["fish", "FISH", "Fish", " fish "])
def test_fish_engine_name_is_case_and_space_insensitive(name, tmp_path):
    engine = _fish(engine=name, fish_voice_id="v", fish_key_file=str(tmp_path / "k"))

    assert isinstance(engine, FishTTS)


def test_fish_config_attrs_are_passed_through(tmp_path):
    cfg = SimpleNamespace(
        fish_voice_id="voice-from-config",
        fish_key_file=str(tmp_path / "keys-from-config.txt"),
        fish_latency="normal",
        fish_model="s1-mini",
    )

    engine = _fish(config=cfg)

    assert engine.config is cfg
    assert engine.voice_id == "voice-from-config"
    assert engine.key_file == str(tmp_path / "keys-from-config.txt")
    assert engine.latency == "normal"
    assert engine.model == "s1-mini"


def test_explicit_fish_kwargs_win_over_config(tmp_path):
    cfg = SimpleNamespace(
        fish_voice_id="voice-from-config",
        fish_key_file=str(tmp_path / "config-keys.txt"),
        fish_latency="normal",
        fish_model="s1-mini",
    )

    engine = _fish(
        config=cfg,
        fish_voice_id="voice-explicit",
        fish_key_file=str(tmp_path / "explicit-keys.txt"),
        fish_latency="low",
        fish_model="s1-explicit",
    )

    assert engine.voice_id == "voice-explicit"
    assert engine.key_file == str(tmp_path / "explicit-keys.txt")
    assert engine.latency == "low"
    assert engine.model == "s1-explicit"


def test_missing_fish_config_attrs_fall_back_to_engine_defaults():
    # A config written before the Fish fields existed must not raise
    # AttributeError; the engine's own documented defaults apply.
    engine = _fish(config=SimpleNamespace())

    assert engine.voice_id is None
    assert engine.key_file == DEFAULT_KEY_FILE
    assert engine.latency == DEFAULT_LATENCY
    assert engine.model == DEFAULT_MODEL


def test_fish_engine_does_not_reuse_a_piper_voice_name():
    # ``voice`` carries a Piper/Chatterbox voice name. Feeding that to Fish would
    # be the same wrong-voice substitution that the unknown-engine error
    # refuses, so it is deliberately not used as the Fish voice id.
    engine = _fish(voice="en_GB-alan-medium")

    assert engine.voice_id is None


# --------------------------------------------------------------------------
# Unknown engine names: a named error, never a silent Piper
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["unknown", "pipers", "gpt-sovits", "", None])
def test_unknown_engine_raises_naming_the_supported_engines(name):
    with pytest.raises(ValueError) as excinfo:
        speech.create_tts_engine(engine=name, enabled=False)

    message = str(excinfo.value)
    assert repr(name) in message
    for supported in ("piper", "chatterbox", "fish"):
        assert supported in message


def test_unknown_engine_does_not_build_piper(monkeypatch):
    built = []

    class _SpyPiper:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(speech, "PiperTTS", _SpyPiper)

    with pytest.raises(ValueError):
        speech.create_tts_engine(engine="not-an-engine", enabled=False)

    assert built == []


def test_unknown_engine_does_not_build_any_engine(monkeypatch):
    built = []

    class _Spy:
        def __init__(self, *args, **kwargs):
            built.append(type(self).__name__)

    monkeypatch.setattr(speech, "PiperTTS", _Spy)
    monkeypatch.setattr(speech, "ChatterboxTTS", _Spy)

    with pytest.raises(ValueError):
        speech.create_tts_engine(engine="sovits", enabled=False)

    assert built == []


# --------------------------------------------------------------------------
# The existing names still work
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["piper", "PIPER", "Piper", " piper "])
def test_piper_still_builds_piper(name):
    engine = speech.create_tts_engine(engine=name, enabled=False)

    assert isinstance(engine, speech.PiperTTS)
    assert not isinstance(engine, FishTTS)


def test_piper_parameters_still_pass_through():
    engine = speech.create_tts_engine(
        engine="piper",
        enabled=False,
        piper_model_path="/path/to/model.onnx",
        piper_speaker=1,
    )

    assert engine.model_path == "/path/to/model.onnx"
    assert engine.speaker == 1


def test_chatterbox_still_builds_chatterbox():
    engine = speech.create_tts_engine(engine="chatterbox", enabled=False)

    assert isinstance(engine, speech.ChatterboxTTS)


def test_default_engine_is_still_piper():
    assert isinstance(speech.create_tts_engine(enabled=False), speech.PiperTTS)


# --------------------------------------------------------------------------
# Laziness and the startup-status contract
# --------------------------------------------------------------------------

def test_importing_tts_does_not_import_fish_or_touch_a_key_file():
    # Imported in a fresh interpreter: importing this module must stay cheap and
    # must not read a key file (the Fish client is built on first use).
    code = (
        "import sys, claire.output.tts as tts;"
        "print('fish_tts=' + str('claire.output.fish_tts' in sys.modules));"
        "print('fish_client=' + str('claire.output.fish_client' in sys.modules));"
        "print('eager=' + str('FishTTS' in tts.__dict__))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "fish_tts=False" in result.stdout
    assert "fish_client=False" in result.stdout
    assert "eager=False" in result.stdout


def test_fish_engine_is_reachable_as_a_module_attribute():
    # Probe-style consumers (the latency harness) check for the engine by name;
    # the attribute is served lazily, so this must not make the import eager.
    assert hasattr(speech, "FishTTS")
    assert speech.FishTTS is FishTTS


def test_unusable_fish_key_file_raises_a_named_error_not_silence(tmp_path):
    # The fish counterpart of tests/test_tts_startup_status.py: a missing
    # credential must fail loudly, naming the path, and must never be reported
    # as speech.
    missing = tmp_path / "no-such-fish-key-file.txt"
    engine = _fish(enabled=True, fish_voice_id="v", fish_key_file=str(missing))

    with pytest.raises(FishKeyFileMissingError) as excinfo:
        engine.start()

    message = str(excinfo.value)
    assert str(missing) in message
    assert not engine.is_speaking()
    assert engine.get_last_spoken_text() == ""
    engine.stop()
