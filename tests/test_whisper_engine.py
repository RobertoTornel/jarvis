"""The faster-whisper SttEngine: real reuse, named failures, no silent empty text.

Every test here fakes the two seams the engine loads through — the
faster-whisper model class (``claire.listening.listener.WhisperModel``) and
model preparation (``claire.listening.model_download.prepare_faster_whisper_model``)
— so no real model is read and the network is never touched. Everything else
(the loader, the decode lock, the segment filter) is the project's real code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from claire.listening.model_download import ModelDownloadError
from claire.listening.stt import whisper_engine as we
from claire.listening.stt.base import (
    SttBackendUnavailableError,
    SttDecodeError,
    SttEngine,
    SttError,
    SttModelMissingError,
    SttResult,
)

pytestmark = pytest.mark.unit

SAMPLE_RATE = 16000
REPO_ROOT = Path(__file__).resolve().parents[1]


def make_cfg(**overrides):
    settings = dict(
        whisper_model="small",
        whisper_backend="faster-whisper",
        whisper_device="cpu",
        whisper_compute_type="int8",
        whisper_vad=False,
        whisper_min_confidence=0.3,
        whisper_no_speech_threshold=0.5,
        whisper_min_audio_duration=0.3,
        sample_rate=SAMPLE_RATE,
    )
    settings.update(overrides)
    return SimpleNamespace(**settings)


def utterance(seconds: float = 1.0, value: float = 0.01) -> np.ndarray:
    return np.full(int(seconds * SAMPLE_RATE), value, dtype=np.float32)


class FakeSegment:
    def __init__(self, text: str, avg_logprob: float = 0.0, no_speech_prob: float = 0.0):
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob


class FakeModel:
    """Stands in for ``faster_whisper.WhisperModel`` and records its calls."""

    def __init__(self, path, device="cpu", compute_type="int8", transcribe=None):
        self.path = path
        self.device = device
        self.compute_type = compute_type
        self.model = SimpleNamespace(device=device)
        self.calls = []
        self._transcribe = transcribe

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio": np.asarray(audio).copy(), "kwargs": dict(kwargs)})
        if self._transcribe is not None:
            return self._transcribe(audio, **kwargs)
        return iter([FakeSegment("hello Claire", avg_logprob=-0.1)]), SimpleNamespace(language="en")

    @property
    def audio_sizes(self):
        return [call["audio"].size for call in self.calls]


def default_reply(segments=None):
    def _transcribe(audio, **kwargs):
        return iter(segments if segments is not None else []), SimpleNamespace(language="en")

    return _transcribe


def install_fake_backend(monkeypatch, tmp_path, transcribe=None):
    """Patch model preparation and the model class; return the created models."""
    model_dir = tmp_path / "whisper-small"
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (model_dir / name).write_bytes(b"synthetic")

    monkeypatch.setattr(
        "claire.listening.model_download.prepare_faster_whisper_model",
        lambda name: str(model_dir),
    )

    created: list[FakeModel] = []

    def factory(path, *, device, compute_type, cpu_threads=0, **kwargs):
        model = FakeModel(
            path, device=device, compute_type=compute_type, transcribe=transcribe
        )
        created.append(model)
        return model

    from claire.listening import listener

    monkeypatch.setattr(listener, "WhisperModel", factory)
    monkeypatch.setattr(listener, "FASTER_WHISPER_AVAILABLE", True, raising=False)
    return created


def mark_backend_unavailable(monkeypatch):
    from claire.listening import listener

    monkeypatch.setattr(listener, "WhisperModel", None)
    monkeypatch.setattr(listener, "FASTER_WHISPER_AVAILABLE", False, raising=False)


# --------------------------------------------------------------------------- #
# successful transcription
# --------------------------------------------------------------------------- #


def test_transcribe_returns_text_language_and_low_confidence_events(monkeypatch, tmp_path):
    created = install_fake_backend(
        monkeypatch,
        tmp_path,
        transcribe=default_reply(
            [
                FakeSegment("hello Claire", avg_logprob=-0.1),
                FakeSegment("maybe not", avg_logprob=-0.95),
            ]
        ),
    )
    engine = we.create_whisper_engine(make_cfg())

    result = engine.transcribe(utterance(), SAMPLE_RATE)

    assert isinstance(result, SttResult)
    # The low-confidence segment is dropped from the text by the shared filter...
    assert result.text == "hello Claire"
    assert result.language == "en"
    assert result.engine == "faster_whisper"
    assert result.elapsed_sec >= 0.0
    # ...but is reported as an event, with the listener's own event type.
    assert len(result.low_confidence_events) == 1
    event = result.low_confidence_events[0]
    assert event.transcript == "maybe not"
    assert event.reason == "low_confidence"
    assert event.confidence == pytest.approx(0.05, abs=1e-6)
    assert len(created) == 1
    assert created[0].path.endswith("whisper-small")


def test_transcribe_reports_no_events_when_every_segment_is_confident(monkeypatch, tmp_path):
    install_fake_backend(
        monkeypatch, tmp_path, transcribe=default_reply([FakeSegment("all good", -0.05)])
    )
    result = we.create_whisper_engine(make_cfg()).transcribe(utterance(), SAMPLE_RATE)

    assert result.text == "all good"
    assert result.low_confidence_events == ()


def test_language_is_none_when_the_backend_reports_none(monkeypatch, tmp_path):
    install_fake_backend(
        monkeypatch,
        tmp_path,
        transcribe=lambda audio, **kwargs: (
            iter([FakeSegment("bonjour", -0.05)]),
            SimpleNamespace(),
        ),
    )
    result = we.create_whisper_engine(make_cfg()).transcribe(utterance(), SAMPLE_RATE)

    assert result.text == "bonjour"
    assert result.language is None


def test_audio_is_resampled_to_the_configured_rate(monkeypatch, tmp_path):
    created = install_fake_backend(monkeypatch, tmp_path, transcribe=default_reply(
        [FakeSegment("resampled", -0.05)]
    ))
    engine = we.create_whisper_engine(make_cfg())

    # 0.5s captured at 8 kHz is 0.5s at the configured 16 kHz.
    engine.transcribe(np.full(8000, 0.01, dtype=np.float32), 8000)

    assert created[0].audio_sizes == [SAMPLE_RATE]


def test_whisper_vad_configuration_reaches_the_decode_call(monkeypatch, tmp_path):
    created = install_fake_backend(monkeypatch, tmp_path, transcribe=default_reply(
        [FakeSegment("vad on", -0.05)]
    ))
    engine = we.create_whisper_engine(make_cfg(whisper_vad=True))

    engine.transcribe(utterance(), SAMPLE_RATE)

    assert created[0].calls[0]["kwargs"]["vad_filter"] is True


def test_vad_is_left_at_the_production_default_when_disabled(monkeypatch, tmp_path):
    created = install_fake_backend(monkeypatch, tmp_path, transcribe=default_reply(
        [FakeSegment("vad off", -0.05)]
    ))
    we.create_whisper_engine(make_cfg(whisper_vad=False)).transcribe(utterance(), SAMPLE_RATE)

    assert created[0].calls[0]["kwargs"]["vad_filter"] is False


def test_cuda_decode_failure_recovers_to_cpu_through_the_shared_loader(monkeypatch, tmp_path):
    model_dir = tmp_path / "whisper-small"
    model_dir.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (model_dir / name).write_bytes(b"synthetic")
    monkeypatch.setattr(
        "claire.listening.model_download.prepare_faster_whisper_model",
        lambda name: str(model_dir),
    )

    def gpu_transcribe(audio, **kwargs):
        raise RuntimeError("CUDA error: no kernel image is available")

    gpu = FakeModel("small", device="cuda", transcribe=gpu_transcribe)
    cpu = FakeModel("small", device="cpu", transcribe=default_reply([FakeSegment("recovered", -0.05)]))
    created: list[FakeModel] = []

    def factory(path, *, device, compute_type, cpu_threads=0, **kwargs):
        model = gpu if device == "cuda" else cpu
        created.append(model)
        return model

    from claire.listening import listener

    monkeypatch.setattr(listener, "WhisperModel", factory)
    monkeypatch.setattr(listener, "FASTER_WHISPER_AVAILABLE", True, raising=False)

    engine = we.create_whisper_engine(make_cfg(whisper_device="cuda"))
    result = engine.transcribe(utterance(), SAMPLE_RATE)

    assert result.text == "recovered"
    assert created == [gpu, cpu]
    # The engine adopted the recovered model instead of keeping the broken GPU one.
    assert engine.identity().detail["device"] == "cpu"


def test_concurrent_transcriptions_are_serialised_on_the_shared_model(monkeypatch, tmp_path):
    """One WhisperModel must not be driven concurrently (listener's lock)."""
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()
    release = threading.Event()

    def slow_transcribe(audio, **kwargs):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        release.wait(timeout=5)
        with guard:
            state["active"] -= 1
        return iter([FakeSegment("serialised", -0.05)]), SimpleNamespace(language="en")

    created = install_fake_backend(monkeypatch, tmp_path, transcribe=slow_transcribe)
    engine = we.create_whisper_engine(make_cfg())
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(timeout=5)
        results.append(engine.transcribe(utterance(), SAMPLE_RATE).text)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    # Let the first decode enter the model, then release it.
    for _ in range(200):
        if state["peak"] >= 1:
            break
        time.sleep(0.005)
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert results == ["serialised", "serialised"]
    assert state["peak"] == 1
    assert len(created) == 1


# --------------------------------------------------------------------------- #
# named failures (never an empty transcript)
# --------------------------------------------------------------------------- #


def test_unavailable_backend_raises_named_error(monkeypatch):
    mark_backend_unavailable(monkeypatch)
    engine = we.create_whisper_engine(make_cfg())
    created_before = engine.identity().detail["loaded"]

    with pytest.raises(SttBackendUnavailableError) as excinfo:
        engine.transcribe(utterance(), SAMPLE_RATE)

    assert created_before is False
    message = str(excinfo.value)
    assert "faster-whisper" in message
    assert "pip install faster-whisper" in message
    assert excinfo.value.backend == "faster-whisper"
    assert excinfo.value.package == "faster-whisper"


def test_missing_model_raises_named_error_naming_the_model_and_the_fix(monkeypatch):
    def prepare(name):
        raise ModelDownloadError("rate_limit", "HTTP 429 from the Hub")

    monkeypatch.setattr(
        "claire.listening.model_download.prepare_faster_whisper_model", prepare
    )
    engine = we.create_whisper_engine(make_cfg())

    with pytest.raises(SttModelMissingError) as excinfo:
        engine.transcribe(utterance(), SAMPLE_RATE)

    assert excinfo.value.path == "small"
    assert "small" in str(excinfo.value)
    assert "prepare_faster_whisper_model" in str(excinfo.value)
    assert "pip install faster-whisper" in str(excinfo.value)


def test_unusable_model_files_raise_a_named_error_naming_the_model(monkeypatch, tmp_path):
    def factory(path, **kwargs):
        raise FileNotFoundError(f"unable to open file '{path}/model.bin'")

    monkeypatch.setattr(
        "claire.listening.model_download.prepare_faster_whisper_model",
        lambda name: str(tmp_path / "absent"),
    )
    from claire.listening import listener

    monkeypatch.setattr(listener, "WhisperModel", factory)
    monkeypatch.setattr(listener, "FASTER_WHISPER_AVAILABLE", True, raising=False)

    with pytest.raises(SttModelMissingError) as excinfo:
        we.create_whisper_engine(make_cfg()).transcribe(utterance(), SAMPLE_RATE)

    assert excinfo.value.path == "small"
    assert "model.bin" in str(excinfo.value)


def test_decode_failure_raises_a_named_error_instead_of_empty_text(monkeypatch, tmp_path):
    def explode(audio, **kwargs):
        raise RuntimeError("decoder exploded on int8")

    install_fake_backend(monkeypatch, tmp_path, transcribe=explode)
    engine = we.create_whisper_engine(make_cfg())

    with pytest.raises(SttDecodeError) as excinfo:
        engine.transcribe(utterance(), SAMPLE_RATE)

    message = str(excinfo.value)
    assert "decoder exploded on int8" in message
    assert "small" in message


def test_all_segments_rejected_raises_instead_of_returning_empty_text(monkeypatch, tmp_path):
    """A no-speech/hallucination rejection never becomes a silent "" result.

    The no-speech gate drops the segment without raising a low-confidence
    event (upstream policy), so the only honest outcome is a named error.
    """
    install_fake_backend(
        monkeypatch,
        tmp_path,
        transcribe=default_reply([FakeSegment("MBC 뉴스", -0.05, no_speech_prob=0.9)]),
    )
    engine = we.create_whisper_engine(make_cfg())

    with pytest.raises(SttDecodeError) as excinfo:
        engine.transcribe(utterance(), SAMPLE_RATE)

    message = str(excinfo.value)
    assert "no transcript" in message
    assert "low_confidence_events=0" in message
    assert "empty" in message


@pytest.mark.parametrize(
    "audio, sample_rate, expected",
    [
        (None, SAMPLE_RATE, "no audio"),
        (np.zeros(0, dtype=np.float32), SAMPLE_RATE, "empty audio buffer"),
        (np.array([np.nan] * SAMPLE_RATE, dtype=np.float32), SAMPLE_RATE, "non-finite"),
        (utterance(), 0, "invalid sample_rate"),
        (utterance(), "sixteen thousand", "invalid sample_rate"),
    ],
)
def test_unusable_audio_raises_a_named_decode_error(monkeypatch, tmp_path, audio, sample_rate, expected):
    created = install_fake_backend(monkeypatch, tmp_path)
    engine = we.create_whisper_engine(make_cfg())

    with pytest.raises(SttDecodeError) as excinfo:
        engine.transcribe(audio, sample_rate)

    assert expected in str(excinfo.value)
    # A rejected buffer never reaches the model.
    assert created == []


def test_short_audio_raises_a_named_decode_error_naming_the_setting(monkeypatch, tmp_path):
    created = install_fake_backend(monkeypatch, tmp_path)
    engine = we.create_whisper_engine(make_cfg())

    with pytest.raises(SttDecodeError) as excinfo:
        engine.transcribe(utterance(seconds=0.1), SAMPLE_RATE)

    message = str(excinfo.value)
    assert "too short" in message
    assert "whisper_min_audio_duration" in message
    assert created == []


def test_unknown_backend_configuration_is_rejected_at_construction():
    with pytest.raises(SttError) as excinfo:
        we.create_whisper_engine(make_cfg(whisper_backend="sherpa"))

    assert "unknown whisper_backend" in str(excinfo.value)
    assert "faster-whisper" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# identity, laziness and wiring
# --------------------------------------------------------------------------- #


def test_identity_names_the_model_and_the_backend_before_and_after_loading(monkeypatch, tmp_path):
    install_fake_backend(monkeypatch, tmp_path, transcribe=default_reply(
        [FakeSegment("hi", -0.05)]
    ))
    engine = we.create_whisper_engine(make_cfg(whisper_model="medium"))

    before = engine.identity()
    assert before.engine == "faster_whisper"
    assert before.model_kind == "faster-whisper"
    assert before.model_path == "medium"
    assert before.detail["backend"] == "faster-whisper"
    assert before.detail["loaded"] is False
    assert before.detail["configured_backend"] == "faster-whisper"
    assert before.detail["device"] == "cpu"
    assert before.detail["vad_filter"] is False
    assert before.detail["sample_rate"] == SAMPLE_RATE
    assert "faster-whisper" in before.describe()

    engine.transcribe(utterance(), SAMPLE_RATE)

    after = engine.identity()
    assert after.detail["loaded"] is True
    assert after.detail["compute_type"] == "int8"
    assert after == before.__class__(
        engine=after.engine,
        model_path=after.model_path,
        model_kind=after.model_kind,
        detail=after.detail,
    )


def test_identity_reports_a_configured_mlx_backend_honestly(monkeypatch, tmp_path):
    install_fake_backend(monkeypatch, tmp_path)
    engine = we.create_whisper_engine(make_cfg(whisper_backend="mlx"))

    detail = engine.identity().detail
    assert detail["backend"] == "faster-whisper"
    assert detail["configured_backend"] == "mlx"


def test_model_is_not_loaded_until_the_first_transcription(monkeypatch, tmp_path):
    created = install_fake_backend(monkeypatch, tmp_path)
    engine = we.create_whisper_engine(make_cfg())

    assert created == []
    engine.identity()
    assert created == []

    engine.transcribe(utterance(), SAMPLE_RATE)

    assert len(created) == 1

    # The model is loaded once and reused, not per utterance.
    engine.transcribe(utterance(), SAMPLE_RATE)
    assert len(created) == 1


def test_factory_returns_an_stt_engine_and_the_package_factory_routes_to_it(monkeypatch, tmp_path):
    install_fake_backend(monkeypatch, tmp_path)
    engine = we.create_whisper_engine(make_cfg())

    assert isinstance(engine, SttEngine)
    assert engine.name == "faster_whisper"

    from claire.listening.stt import create_stt_engine

    routed = create_stt_engine(make_cfg(stt_engine="faster_whisper"))
    assert isinstance(routed, we.WhisperSttEngine)


def test_importing_the_engine_module_does_not_import_the_audio_stack():
    """Importing this module must stay cheap: the listener loads on first use."""
    code = (
        "import sys\n"
        "import claire.listening.stt.whisper_engine\n"
        "print('listener' if 'claire.listening.listener' in sys.modules else 'clean')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("clean")
