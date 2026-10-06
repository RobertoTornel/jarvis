"""An unusable TTS engine must disable speech, never kill the daemon.

The factory (``src/claire/output/tts.py``) raises for an unrecognised
``tts_engine`` name instead of substituting Piper, and ``config.py`` no longer
coerces a bad value. That makes the daemon's TTS construction a failure point:
it must be reported clearly and speech output must go unavailable, while Claire
herself keeps running. These tests follow the contract already established in
``tests/test_tts_startup_status.py`` ("a failed TTS init disables runtime speech
output") — no real model, key file or audio device is touched.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

BAD_ENGINE = "sovits"
BAD_ENGINE_ERROR = (
    "Unknown TTS engine 'sovits': supported engines are 'piper', 'chatterbox', "
    "'fish'. Refusing to substitute a different engine for 'sovits'."
)
SUPPORTED_ENGINES = ("piper", "chatterbox", "fish")


def _config(**overrides):
    """A config that asks for speech output, with overrides applied."""
    from tests.conftest import MockConfig

    cfg = MockConfig(db_path=":memory:")
    cfg.tts_enabled = True
    cfg.tts_engine = "piper"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# ---------------------------------------------------------------------------
# The init seam
# ---------------------------------------------------------------------------

def test_unknown_engine_name_is_reported_and_yields_no_engine(capsys):
    """The real factory refuses 'sovits'; the daemon reports why, not silence."""
    from claire import daemon

    engine = daemon._initialize_tts_engine(_config(tts_engine=BAD_ENGINE))

    assert engine is None, "an unknown engine must not be substituted or invented"
    output = capsys.readouterr().out
    lower = output.lower()
    assert BAD_ENGINE in output, "the message must name the offending value"
    for name in SUPPORTED_ENGINES:
        assert name in output, f"the message must name the supported engine {name!r}"
    assert "unavailable" in lower
    # Honest degradation: no success line, no hidden substitution.
    assert "TTS engine started" not in output
    assert "traceback" not in lower


def test_init_failure_prints_the_reason_instead_of_swallowing_it(monkeypatch, capsys):
    """A non-ValueError init failure (missing key file) is surfaced too."""
    from claire import daemon

    reason = "Fish API key file missing: /home/user/.claire/fish.key"

    def _factory(**_kwargs):
        raise RuntimeError(reason)

    monkeypatch.setattr(daemon, "create_tts_engine", _factory)

    assert daemon._initialize_tts_engine(_config(tts_engine="fish")) is None
    output = capsys.readouterr().out
    assert reason in output, "the failure reason must be visible, not swallowed"
    assert "fish" in output
    for name in SUPPORTED_ENGINES:
        assert name in output


def test_valid_engine_still_constructs_normally(monkeypatch, capsys):
    """The guard must not disturb the working path."""
    from claire import daemon

    engine = MagicMock()
    engine.enabled = True
    seen = {}

    def _factory(**kwargs):
        seen.update(kwargs)
        return engine

    monkeypatch.setattr(daemon, "create_tts_engine", _factory)

    assert daemon._initialize_tts_engine(_config(tts_engine="piper")) is engine
    assert seen["engine"] == "piper"
    output = capsys.readouterr().out
    assert "Initializing TTS engine (piper)" in output
    assert "unavailable" not in output.lower()
    assert "❌" not in output

    daemon._start_tts_engine(engine)
    assert "TTS engine started" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Downstream tolerance of an absent engine
# ---------------------------------------------------------------------------

def test_start_tts_engine_tolerates_an_absent_engine(capsys):
    """None must be a spoken-about state, not an AttributeError."""
    from claire import daemon

    daemon._start_tts_engine(None)  # must not raise

    output = capsys.readouterr().out
    assert "speech output unavailable" in output.lower()
    assert "TTS engine started" not in output


def test_unavailable_engine_reference_never_claims_to_speak():
    """What the face-widget poller and the listener receive is inert and honest."""
    from claire import daemon

    engine = daemon._UnavailableTTS()

    assert engine.enabled is False
    assert engine.is_speaking() is False
    assert engine.speak("a reply that cannot be spoken") is False
    # The listener's control surface must exist and be harmless.
    engine.start()
    engine.interrupt()
    engine.stop()


# ---------------------------------------------------------------------------
# The startup path itself
# ---------------------------------------------------------------------------

def _run_smoke_startup(monkeypatch, tts_factory, engine_name="piper"):
    """Run ``daemon.main(smoke_test=True)`` with heavy dependencies stubbed.

    Mirrors the established harness in ``tests/test_desktop_app.py`` so the real
    startup path runs, but no model, database, key file or audio device is used.
    Returns what the voice listener received and what the poller saw.
    """
    from claire import daemon

    recorded = {}
    mock_db = MagicMock()

    monkeypatch.setattr(
        daemon, "load_settings", lambda: _config(tts_engine=engine_name),
    )
    monkeypatch.setattr(daemon, "Database", MagicMock(return_value=mock_db))
    monkeypatch.setattr(daemon, "initialize_mcp_tools", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(daemon, "DialogueMemory", MagicMock())
    monkeypatch.setattr(
        daemon, "get_location_context", lambda *a, **k: "Location: Test",
    )
    monkeypatch.setattr(daemon, "create_tts_engine", tts_factory)

    def _voice_listener(db, cfg, tts, memory, **kwargs):
        recorded["listener_tts"] = tts
        recorded["polled_engine"] = daemon.get_tts_engine()
        return MagicMock(spec=threading.Thread)

    monkeypatch.setattr(daemon, "VoiceListener", _voice_listener)
    monkeypatch.setattr(
        "claire.memory.graph.GraphMemoryStore", MagicMock(), raising=False,
    )

    try:
        daemon.main(smoke_test=True)
    finally:
        daemon._stop_voice_feedback_worker()
    return recorded


def test_daemon_startup_survives_an_unknown_engine(monkeypatch, capsys):
    """The whole point: a bad tts_engine must not take the daemon down."""
    from claire import daemon

    def _factory(**_kwargs):
        raise ValueError(BAD_ENGINE_ERROR)

    recorded = _run_smoke_startup(monkeypatch, _factory, engine_name=BAD_ENGINE)
    output = capsys.readouterr().out
    lower = output.lower()

    # Claire came up, said why speech is off, and did not pretend to be speaking.
    assert "SMOKE_TEST_INIT_OK" in output, f"startup did not complete:\n{output}"
    assert BAD_ENGINE in output
    assert "speech output unavailable" in lower
    assert "TTS engine started" not in output

    # The face-widget polling reference tolerates the absence.
    polled = recorded["polled_engine"]
    assert polled is not None, "the poller must never receive None to crash on"
    assert polled.enabled is False
    assert polled.is_speaking() is False
    assert polled.speak("cannot be spoken") is False
    polled.interrupt()

    # The voice listener receives the same inert, non-speaking engine.
    assert recorded["listener_tts"] is polled
    assert recorded["listener_tts"].enabled is False


def test_daemon_startup_still_starts_a_valid_engine(monkeypatch, capsys):
    """A valid engine reaches the listener and reports itself as started."""
    from claire import daemon

    engine = MagicMock()
    engine.enabled = True

    recorded = _run_smoke_startup(monkeypatch, lambda **_kwargs: engine)
    output = capsys.readouterr().out

    assert "SMOKE_TEST_INIT_OK" in output
    assert "TTS engine started" in output
    assert "speech output unavailable" not in output.lower()
    assert recorded["listener_tts"] is engine
    assert recorded["polled_engine"] is engine
    engine.start.assert_called_once()
