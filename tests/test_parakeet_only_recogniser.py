"""Parakeet is the only speech recogniser; Whisper is opt-in and lazy.

Decision D4 gives Claire **one** authoritative recogniser: Parakeet. Whisper
still exists in the tree for anyone who deliberately sets
``stt_engine='faster_whisper'``, but it must never be a fallback and must never
cost a Parakeet-configured process anything — not an import, not a download, not
memory.

These tests pin the invariants that separate "Whisper exists in the tree" from
"Whisper is loaded":

* importing the listener (and the daemon that drives it) pulls in no Whisper
  backend at all;
* a Parakeet-configured listener never calls the Whisper loader at startup;
* the engine factory routes Parakeet away from the Whisper engine, and refuses
  an unknown token instead of quietly picking one;
* the streaming sherpa path — the frozen provisional wake path — is not a
  hidden second Whisper.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_in_clean_interpreter(code: str) -> str:
    """Run ``code`` in a fresh interpreter and return its stdout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_importing_the_listener_and_daemon_imports_no_whisper_backend():
    """The eager module-level Whisper imports are gone.

    Before this change ``claire.listening.listener`` imported faster-whisper (or
    mlx-whisper) at import time and ``claire.daemon`` imported faster-whisper
    outright — so a Parakeet process paid for Whisper before doing anything.
    """
    out = _run_in_clean_interpreter(textwrap.dedent(
        """
        import sys
        import claire.listening.listener  # noqa: F401
        import claire.daemon  # noqa: F401
        print('faster_whisper' in sys.modules)
        print('mlx_whisper' in sys.modules)
        """
    ))
    assert out.split() == ["False", "False"]


def test_the_streaming_sherpa_path_imports_no_whisper_backend():
    """The retained streaming recogniser is sherpa-onnx, not a hidden Whisper."""
    out = _run_in_clean_interpreter(textwrap.dedent(
        """
        import sys
        import claire.listening.stt.sherpa_streaming  # noqa: F401
        print('faster_whisper' in sys.modules)
        print('mlx_whisper' in sys.modules)
        """
    ))
    assert out.split() == ["False", "False"]


def _engine_cfg(**overrides):
    settings = dict(
        stt_engine="parakeet",
        stt_model_path="/tmp/does-not-matter.nemo",
        stt_model_kind="auto",
        stt_device="cpu",
    )
    settings.update(overrides)
    return SimpleNamespace(**settings)


def test_engine_factory_routes_parakeet_away_from_the_whisper_engine(monkeypatch):
    from claire.listening import stt as stt_pkg
    from claire.listening.stt import parakeet as parakeet_mod
    from claire.listening.stt import whisper_engine as whisper_mod

    sentinel = object()
    seen: list[dict] = []

    def fake_parakeet(**kwargs):
        seen.append(kwargs)
        return sentinel

    monkeypatch.setattr(parakeet_mod, "create_parakeet_engine", fake_parakeet)
    monkeypatch.setattr(
        whisper_mod,
        "create_whisper_engine",
        lambda _cfg: pytest.fail("Parakeet must never build the Whisper engine"),
    )

    assert stt_pkg.create_stt_engine(_engine_cfg()) is sentinel
    assert seen == [
        {"model_path": "/tmp/does-not-matter.nemo", "kind": "auto", "device": "cpu"}
    ]


def test_engine_factory_builds_whisper_only_when_stt_engine_names_it(monkeypatch):
    from claire.listening import stt as stt_pkg
    from claire.listening.stt import parakeet as parakeet_mod
    from claire.listening.stt import whisper_engine as whisper_mod

    sentinel = object()
    monkeypatch.setattr(
        parakeet_mod,
        "create_parakeet_engine",
        lambda **_kw: pytest.fail("Whisper opt-in must not build Parakeet"),
    )
    monkeypatch.setattr(whisper_mod, "create_whisper_engine", lambda _cfg: sentinel)

    assert stt_pkg.create_stt_engine(_engine_cfg(stt_engine="faster_whisper")) is sentinel
    # A bare "whisper" is a documented spelling of the same opt-in.
    assert stt_pkg.create_stt_engine(_engine_cfg(stt_engine="whisper")) is sentinel


def test_engine_factory_refuses_an_unknown_token_instead_of_falling_back():
    from claire.listening.stt import create_stt_engine
    from claire.listening.stt.base import SttError

    with pytest.raises(SttError, match="unknown stt_engine"):
        create_stt_engine(_engine_cfg(stt_engine="whisperr"))


def test_a_bare_or_blank_engine_setting_is_parakeet(monkeypatch):
    from claire.listening import stt as stt_pkg
    from claire.listening.stt import parakeet as parakeet_mod

    sentinel = object()
    monkeypatch.setattr(parakeet_mod, "create_parakeet_engine", lambda **_kw: sentinel)

    assert stt_pkg.create_stt_engine(_engine_cfg(stt_engine="")) is sentinel
    cfg = _engine_cfg()
    del cfg.stt_engine
    assert stt_pkg.create_stt_engine(cfg) is sentinel


def _parakeet_listener(monkeypatch, **cfg_overrides):
    import claire.listening.listener as capture
    from claire.config import load_settings
    from claire.listening.listener import VoiceListener
    from dataclasses import replace

    cfg = replace(load_settings(), voice_debug=False, **cfg_overrides)
    monkeypatch.setattr(capture, "create_intent_judge", lambda _cfg: None)
    monkeypatch.setattr(VoiceListener, "_start_llm_warmup", lambda _self: [])
    monkeypatch.setattr("desktop_app.face_widget.get_claire_state", lambda: MagicMock())
    return VoiceListener(MagicMock(), cfg, None, None)


def test_configured_engine_predicate_is_parakeet_unless_whisper_is_named(monkeypatch):
    obj = _parakeet_listener(monkeypatch)
    assert obj._configured_engine() == "parakeet"
    assert obj._configures_whisper() is False

    obj.cfg = SimpleNamespace(stt_engine="faster_whisper")
    assert obj._configures_whisper() is True

    # An undescribed config object must not select Whisper by accident.
    obj.cfg = SimpleNamespace()
    assert obj._configured_engine() == "parakeet"


def test_parakeet_startup_never_calls_the_whisper_loader(monkeypatch, capsys):
    """``run()`` on a Parakeet config must not load or construct Whisper.

    The startup gate is exercised directly — ``_initialise_whisper_backend`` is
    the only Whisper load path left, and ``run()`` must not reach it.
    """
    import claire.listening.listener as capture
    from claire.listening.listener import VoiceListener
    from test_voice_listener import _create_mock_config, _mock_input_devices

    monkeypatch.setattr(
        VoiceListener,
        "_initialise_whisper_backend",
        lambda _self: pytest.fail("Parakeet must never initialise Whisper"),
    )
    monkeypatch.setattr(
        capture,
        "_load_faster_whisper_model",
        lambda *a, **kw: pytest.fail("Parakeet must never load a Whisper model"),
    )
    monkeypatch.setattr(
        capture,
        "WhisperModel",
        lambda *a, **kw: pytest.fail("Parakeet must never construct a Whisper model"),
    )
    monkeypatch.setattr(capture, "MLX_WHISPER_AVAILABLE", False)

    sd = MagicMock()
    _mock_input_devices(sd, [{"name": "Test Mic", "max_input_channels": 1}])
    sd.InputStream.side_effect = Exception("Stop test here")
    monkeypatch.setattr(capture, "sd", sd)

    obj = VoiceListener(
        MagicMock(), _create_mock_config(stt_engine="parakeet"), None, MagicMock()
    )
    obj.run()

    # Prove the gate was actually reached (so the test cannot pass vacuously by
    # returning early) and that it took the Parakeet branch.
    out = capsys.readouterr().out
    assert "Recogniser: parakeet" in out


def test_parakeet_decode_never_calls_the_whisper_transcribe_path(monkeypatch):
    import numpy as np
    from claire.listening.stt.base import SttResult

    obj = _parakeet_listener(monkeypatch)

    class Engine:
        name = "parakeet_nemo"

        def transcribe(self, audio, sample_rate):
            return SttResult(text="Claire hello", language="en", engine=self.name)

    monkeypatch.setattr("claire.listening.stt.create_stt_engine", lambda _cfg: Engine())
    obj._transcribe_audio = lambda audio: pytest.fail(
        "Parakeet must never fall back to the Whisper decode path"
    )

    text, _language, _events, source, reason = obj._decode_utterance(
        np.zeros(1600, dtype=np.float32)
    )
    assert text == "Claire hello"
    assert source == "parakeet_nemo"
    assert reason is None
