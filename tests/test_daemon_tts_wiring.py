"""The daemon must hand its config to the TTS factory.

Regression, found by starting the real systemd service rather than by any test:
``daemon._initialize_tts_engine`` called ``create_tts_engine`` without the
``config`` argument. That argument is not decoration - the Fish branch resolves
its voice id, key file, latency and model through ``_fish_option``, which falls
back to ``getattr(config, attr, None)``. With ``config=None`` every one of those
lookups returned ``None``, so the engine was built with no voice id, and
``FishTTS.start()`` then refused:

    FishConfigError: Fish Audio voice id is not configured for key file '...';
    refusing to report speech that cannot be made (N10).

The refusal is right; the wiring was wrong. The practical effect was that Claire
had no voice at all when run as a daemon, while the Fish *client* worked
perfectly when called directly - which is exactly why every client-level test
passed. Nothing exercised the factory the daemon actually uses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from claire import daemon

pytestmark = pytest.mark.unit

VOICE = "2466933c79e14edebce3ca8224e497e0"


def _stub_cfg(**overrides):
    """A settings object with every attribute ``_initialize_tts_engine`` reads."""
    base = dict(
        tts_engine="fish",
        tts_enabled=True,
        tts_voice="",
        tts_rate=1.0,
        tts_chatterbox_device="cpu",
        tts_chatterbox_audio_prompt=None,
        tts_chatterbox_exaggeration=0.5,
        tts_chatterbox_cfg_weight=0.5,
        tts_piper_model_path=None,
        tts_piper_speaker=None,
        tts_piper_length_scale=1.0,
        tts_piper_noise_scale=0.667,
        tts_piper_noise_w=0.8,
        tts_piper_sentence_silence=0.2,
        fish_voice_id=VOICE,
        fish_key_file="/tmp/claire-test-fish-keys.txt",
        fish_latency="low",
        fish_model="s2.1-pro-free",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_fish_gets_its_voice_id_from_config_through_the_daemon():
    """The exact regression: config defaults must reach the built engine."""

    tts = daemon._initialize_tts_engine(_stub_cfg())

    assert tts is not None, "the Fish engine failed to build at all"
    assert tts.voice_id == VOICE, (
        "the daemon did not pass its config to create_tts_engine, so Fish was "
        "built with no voice id and would refuse to speak"
    )


def test_all_four_fish_options_survive_the_daemon_path():
    """Not just the voice id - every Fish default travels the same way."""

    cfg = _stub_cfg(
        fish_key_file="/tmp/other-keys.txt",
        fish_latency="normal",
        fish_model="s1",
    )
    tts = daemon._initialize_tts_engine(cfg)

    assert tts.voice_id == VOICE
    assert tts.key_file == "/tmp/other-keys.txt"
    assert tts.model == "s1"


def _key_file(tmp_path):
    """A real 0600 key file: FishKeyPool refuses a missing or world-readable one.

    Writing a real file keeps each test honest about *which* guard fires. With a
    non-existent path the pool fails first with FishKeyFileMissingError, and the
    voice-id refusal this test exists to pin would never be reached.
    """
    path = tmp_path / "fish-keys.txt"
    path.write_text("sk-test-not-a-real-key\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def test_the_built_engine_would_refuse_with_no_voice_id(tmp_path):
    """The guard this all depends on is real, and fires rather than faking speech.

    Pinning the failure side matters: if this ever stopped refusing, the wiring
    bug above would become silent instead of loud, and Claire would claim to have
    spoken in a voice she never used.
    """

    from claire.output.fish_client import FishConfigError

    tts = daemon._initialize_tts_engine(
        _stub_cfg(fish_voice_id="", fish_key_file=_key_file(tmp_path))
    )
    assert tts is not None

    with pytest.raises(FishConfigError) as excinfo:
        tts.start()

    assert "voice id is not configured" in str(excinfo.value)


def test_a_missing_voice_id_names_the_key_file_not_a_guess(tmp_path):
    """The diagnostics stay useful: the message says which key file was used."""

    from claire.output.fish_client import FishConfigError

    named = _key_file(tmp_path)
    tts = daemon._initialize_tts_engine(_stub_cfg(fish_voice_id="", fish_key_file=named))
    with pytest.raises(FishConfigError) as excinfo:
        tts.start()

    assert named in str(excinfo.value)


def test_the_daemon_call_site_passes_config():
    """A source-level guard, so deleting the argument fails here and not in production."""

    import inspect

    source = inspect.getsource(daemon._initialize_tts_engine)

    assert "config=cfg" in source, (
        "the daemon stopped passing config=cfg to create_tts_engine; Fish will "
        "silently lose its voice id, key file, latency and model"
    )
