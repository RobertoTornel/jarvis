"""One recogniser spelling, understood the same way by all three layers.

`listening.spec.md` and `create_stt_engine()` both accept `faster_whisper`,
`faster-whisper` and `whisper` as names for the opt-in Whisper family, and the
settings loader is the layer that sits in front of both. It used to keep only the
first spelling and silently rewrite the other two to `parakeet`, so a config that
said `whisper` ran Parakeet — a different model listening to the owner, with no
signal anywhere.

These pin the three layers to one answer per token, and pin the fallback for a
token nobody recognises, which `docs/subsystems/configuration.md` documents as
the contract for string enums here.
"""

from __future__ import annotations

import json

import pytest

from claire.config import load_settings
from claire.listening.stt import PARAKEET_ENGINE_NAMES, WHISPER_ENGINE_NAMES
from claire.listening.listener import VoiceListener

# pytestmark makes this whole file visible to the unit gate.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`


def _settings_for(tmp_path, monkeypatch, token: str):
    """Load a real config file naming ``token``, with no ambient override."""
    monkeypatch.delenv("CLAIRE_TUNE_ENABLED", raising=False)
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"stt_engine": token}))
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_file))
    return load_settings()


class TestTheDocumentedSpellingsSurviveTheLoader:
    @pytest.mark.parametrize("token", sorted(WHISPER_ENGINE_NAMES))
    def test_every_whisper_spelling_reaches_the_listener_as_whisper(
        self, tmp_path, monkeypatch, token
    ):
        cfg = _settings_for(tmp_path, monkeypatch, token)

        assert cfg.stt_engine == "faster_whisper", (
            f"{token!r} is a documented Whisper token; the loader must not "
            "hand the listener a different model than the one configured"
        )
        assert VoiceListener._configured_engine(
            type("L", (), {"cfg": cfg, "_WHISPER_ENGINE_NAMES": WHISPER_ENGINE_NAMES})()
        ) == "whisper"

    @pytest.mark.parametrize("token", sorted(PARAKEET_ENGINE_NAMES))
    def test_every_parakeet_spelling_stays_parakeet(self, tmp_path, monkeypatch, token):
        cfg = _settings_for(tmp_path, monkeypatch, token)

        assert cfg.stt_engine == "parakeet"

    def test_an_underscore_and_a_hyphen_mean_the_same_thing(self, tmp_path, monkeypatch):
        underscored = _settings_for(tmp_path, monkeypatch, "faster_whisper")
        monkeypatch.undo()
        monkeypatch.delenv("CLAIRE_TUNE_ENABLED", raising=False)

        dashed = _settings_for(tmp_path, monkeypatch, "faster-whisper")

        assert dashed.stt_engine == underscored.stt_engine

    def test_case_and_whitespace_are_ignored(self, tmp_path, monkeypatch):
        """The loader has always stripped and lowered; keep that true."""
        cfg = _settings_for(tmp_path, monkeypatch, "  Faster-Whisper  ")

        assert cfg.stt_engine == "faster_whisper"


class TestTheUnrecognisedTokenContract:
    def test_junk_falls_back_to_parakeet(self, tmp_path, monkeypatch):
        """Documented behaviour: an unrecognised string enum takes the safe
        default rather than reaching the factory."""
        cfg = _settings_for(tmp_path, monkeypatch, "fast_whisper")

        assert cfg.stt_engine == "parakeet"

    def test_the_factory_still_refuses_a_token_the_loader_never_allows(self):
        """The factory raises by name for a token it does not know; the loader's
        fallback is what keeps that unreachable on the daemon path. Both are
        deliberate, so this records the pair rather than one of them."""
        from claire.listening.stt import SttError, create_stt_engine

        with pytest.raises(SttError) as excinfo:
            create_stt_engine(type("C", (), {"stt_engine": "fast_whisper"})())

        assert "fast_whisper" in str(excinfo.value)
        assert "parakeet" in str(excinfo.value)
