"""Tests for scripts/check_stt_model.py.

These run without any real speech model: the engine factory is either exercised
against a missing path (the named-error path) or replaced with a fake.
"""

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "check_stt_model.py"
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))


def _load_module():
    spec = importlib.util.spec_from_file_location("check_stt_model_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _load_module()


def _settings(model_path: str = "", kind: str = "auto") -> SimpleNamespace:
    return SimpleNamespace(
        stt_engine="parakeet",
        stt_model_path=model_path,
        stt_model_kind=kind,
        stt_device="cpu",
    )


def _collected(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _write_wav(path: Path, channels: int = 1, seconds: float = 0.1) -> Path:
    sample_rate = 16000
    frames = int(sample_rate * seconds)
    t = np.arange(frames, dtype=np.float32) / sample_rate
    tone = (0.25 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype("<i2")
    data = np.repeat(tone, channels).astype("<i2").tobytes() if channels > 1 else tone.tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(data)
    return path


class _FakeEngine:
    def __init__(self, text: str = "hello world", name: str = "fake") -> None:
        self.name = name
        self.text = text
        self.seen: list[tuple[np.ndarray, int]] = []

    def identity(self):
        from claire.listening.stt import SttIdentity

        return SttIdentity(
            engine=self.name,
            model_path="/tmp/model.nemo",
            model_kind="nemo",
            detail={"device": "cpu", "fake": True},
        )

    def transcribe(self, audio, sample_rate):
        from claire.listening.stt import SttResult

        self.seen.append((audio, sample_rate))
        return SttResult(
            text=self.text, language="en", engine=self.name, elapsed_sec=0.01
        )


def _install_fake_engine(monkeypatch, engine) -> None:
    import claire.listening.stt as stt_pkg

    monkeypatch.setattr(stt_pkg, "create_stt_engine", lambda cfg: engine)


def test_missing_model_exits_nonzero_and_names_path(mod, monkeypatch, capsys, tmp_path):
    missing = tmp_path / "not-there.nemo"
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(missing)))

    rc = mod.main(["--model-path", str(missing)])

    output = _collected(capsys)
    assert rc != 0
    assert str(missing) in output
    assert "Traceback" not in output


def test_missing_model_via_patched_sys_argv(mod, monkeypatch, capsys, tmp_path):
    missing = tmp_path / "also-missing.nemo"
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(missing)))
    monkeypatch.setattr(
        sys, "argv", ["check_stt_model.py", "--model-path", str(missing)]
    )

    rc = mod.main()

    output = _collected(capsys)
    assert rc != 0
    assert str(missing) in output
    assert "Traceback" not in output


def test_default_model_path_comes_from_settings(mod, monkeypatch, capsys, tmp_path):
    missing = tmp_path / "from-settings.nemo"
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(missing)))

    rc = mod.main([])

    output = _collected(capsys)
    assert rc != 0
    assert str(missing) in output


def test_unset_model_path_is_a_named_error(mod, monkeypatch, capsys):
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(""))

    rc = mod.main([])

    output = _collected(capsys)
    assert rc != 0
    assert "stt_model_path" in output
    assert "Traceback" not in output


def test_fake_engine_gives_clean_zero_exit(mod, monkeypatch, capsys, tmp_path):
    model_dir = tmp_path / "onnx-export"
    model_dir.mkdir()
    engine = _FakeEngine()
    _install_fake_engine(monkeypatch, engine)
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(model_dir), "onnx"))

    rc = mod.main(["--model-path", str(model_dir), "--kind", "onnx"])

    output = _collected(capsys)
    assert rc == 0
    assert "fake" in output
    assert "onnx" in output
    assert str(model_dir) in output
    assert "device=cpu" in output


def test_wav_is_transcribed_as_mono_float32(mod, monkeypatch, capsys, tmp_path):
    model_dir = tmp_path / "model.nemo"
    model_dir.write_bytes(b"not a real model")
    wav = _write_wav(tmp_path / "turn.wav", channels=2)
    engine = _FakeEngine(text="turn the kitchen light on")
    _install_fake_engine(monkeypatch, engine)
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(model_dir), "nemo"))

    rc = mod.main(["--model-path", str(model_dir), "--wav", str(wav)])

    output = _collected(capsys)
    assert rc == 0
    assert "turn the kitchen light on" in output
    assert engine.seen, "engine was never asked to transcribe"
    audio, sample_rate = engine.seen[0]
    assert sample_rate == 16000
    assert audio.ndim == 1
    assert audio.dtype == np.float32
    assert audio.shape[0] == 1600  # 0.1 s of stereo downmixed to one channel
    assert float(np.max(np.abs(audio))) <= 1.0


def test_empty_transcript_is_not_reported_as_success(
    mod, monkeypatch, capsys, tmp_path
):
    model_dir = tmp_path / "model.nemo"
    model_dir.write_bytes(b"not a real model")
    wav = _write_wav(tmp_path / "quiet.wav")
    _install_fake_engine(monkeypatch, _FakeEngine(text="   "))
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(model_dir), "nemo"))

    rc = mod.main(["--model-path", str(model_dir), "--wav", str(wav)])

    output = _collected(capsys)
    assert rc != 0
    assert "empty" in output.lower()
    assert "Traceback" not in output


def test_missing_wav_is_a_named_error(mod, monkeypatch, capsys, tmp_path):
    model_dir = tmp_path / "model.nemo"
    model_dir.write_bytes(b"not a real model")
    missing_wav = tmp_path / "no-such.wav"
    _install_fake_engine(monkeypatch, _FakeEngine())
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(model_dir), "nemo"))

    rc = mod.main(["--model-path", str(model_dir), "--wav", str(missing_wav)])

    output = _collected(capsys)
    assert rc != 0
    assert str(missing_wav) in output
    assert "Traceback" not in output


def test_engine_failure_is_named_not_a_traceback(mod, monkeypatch, capsys, tmp_path):
    model_dir = tmp_path / "model.nemo"
    model_dir.write_bytes(b"not a real model")
    import claire.listening.stt as stt_pkg

    def _boom(cfg):
        raise stt_pkg.SttBackendUnavailableError("parakeet", "nemo-toolkit")

    monkeypatch.setattr(stt_pkg, "create_stt_engine", _boom)
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(model_dir), "nemo"))

    rc = mod.main(["--model-path", str(model_dir)])

    output = _collected(capsys)
    assert rc != 0
    assert "nemo-toolkit" in output
    assert str(model_dir) in output
    assert "Traceback" not in output


def test_bad_kind_is_rejected_without_traceback(mod, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(mod, "_load_settings", lambda: _settings(str(tmp_path)))

    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--kind", "sorcery"])

    assert excinfo.value.code != 0
    assert "Traceback" not in _collected(capsys)


def test_import_has_no_side_effects(capsys):
    module = _load_module()

    assert _collected(capsys) == ""
    assert callable(module.main)
    assert "if __name__" in _SCRIPT.read_text()
