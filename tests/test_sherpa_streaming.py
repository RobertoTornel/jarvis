"""Sherpa streaming recogniser: always-on provisional listener (D3/D4).

Two layers:

* ``unit`` — fake ``sherpa_onnx`` backend via monkeypatch; covers the named-
  failure contract, file selection, and stream-state semantics with no model
  files and no network.
* ``integration`` — the genuine streaming zipformer bundle (read-only,
  outside the repo) plus real speech from its ``test_wavs/``. No network:
  local files and local inference only. Skipped when the fixture is absent.
"""

from pathlib import Path
import sys
import types
import wave

import numpy as np
import pytest

from claire.listening.stt.base import (
    SttBackendUnavailableError,
    SttDecodeError,
    SttModelMissingError,
    StreamingSttEngine,
)
from claire.listening.stt.sherpa_streaming import SherpaStreamingEngine

REAL_MODEL_DIR = Path(
    "/home/mana/Documents/deepseek/Claire/models/"
    "sherpa-onnx-streaming-zipformer-en-20M-2023-02-17"
)
needs_real_model = pytest.mark.skipif(
    not (REAL_MODEL_DIR / "tokens.txt").is_file(),
    reason=f"real streaming fixture absent at {REAL_MODEL_DIR}",
)


# ---------------------------------------------------------------------------
# Fake sherpa_onnx backend (unit layer: no model, no network)
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self):
        self.frames = []
        self.closed = False
        # Frames already consumed by decode_stream; is_ready() compares against
        # this, mirroring the real binding's "undecoded feature frames" count.
        self.decoded = 0

    def accept_waveform(self, sample_rate: int, samples: np.ndarray) -> None:
        self.frames.append(np.asarray(samples, dtype=np.float32))


class _FakeRecognizer:
    """Minimal stand-in: concatenates fed markers into a fake transcript."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.streams = []
        self.decode_calls = 0
        type(self).instances.append(self)

    @classmethod
    def from_transducer(cls, **kwargs):
        return cls(**kwargs)

    def create_stream(self):
        stream = _FakeStream()
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream):
        self.decode_calls += 1
        stream.decoded = len(stream.frames)

    def get_result(self, stream):
        # Mirrors the real binding: there is no result until decode_stream has
        # consumed frames, and get_result() on an unready stream safely returns
        # "" (verified against the real library — only decode_stream aborts).
        decoded = stream.frames[: stream.decoded]
        return " ".join(f"w{len(f)}" for f in decoded if f.any())

    def is_ready(self, stream):
        # Permissive default so the older tests keep their original semantics:
        # ready as soon as anything undecoded is buffered. It must subtract
        # `decoded`, or the engine's drain loop would never terminate — an
        # earlier version of this fake returned len(frames) > 0 and hung.
        return (len(stream.frames) - stream.decoded) > 0

    def is_endpoint(self, stream):
        return False


class _WindowedFakeRecognizer(_FakeRecognizer):
    """Models the real binding's readiness window honestly.

    sherpa-onnx refuses to decode until a full feature window is buffered, and
    calling decode_stream() before that calls LOG(FATAL) in features.cc, which
    aborts the entire process. This fake raises instead of aborting, so a
    regressed readiness guard fails a test instead of killing the test run.
    """

    WINDOW = 39  # measured: ~780 ms of 20 ms frames at 16 kHz

    def is_ready(self, stream):
        return (len(stream.frames) - stream.decoded) >= self.WINDOW

    def decode_stream(self, stream):
        if not self.is_ready(stream):
            raise AssertionError(
                "decode_stream() called before is_ready() — the real sherpa-onnx "
                "aborts the process here (features.cc LOG(FATAL))"
            )
        super().decode_stream(stream)


def _install_fake_sherpa(monkeypatch, recognizer_cls=_FakeRecognizer):
    module = types.ModuleType("sherpa_onnx")
    module.OnlineRecognizer = recognizer_cls
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    return module


def _model_dir(tmp_path: Path, *, int8: bool = True, tokens: bool = True) -> Path:
    d = tmp_path / "model"
    d.mkdir()
    for prefix in ("encoder-epoch-99-avg-1", "decoder-epoch-99-avg-1", "joiner-epoch-99-avg-1"):
        (d / f"{prefix}.onnx").write_bytes(b"fake")
        if int8:
            (d / f"{prefix}.int8.onnx").write_bytes(b"fake-int8")
    if tokens:
        (d / "tokens.txt").write_text("<blk> 0\n")
    return d


def _speech(n: int = 1600, freq: float = 220.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / 16000.0
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# Unit: named failures
# ---------------------------------------------------------------------------

pytestmark_unit = pytest.mark.unit


@pytest.mark.unit
def test_missing_dir_raises_with_path(tmp_path):
    missing = tmp_path / "no-such-model"
    with pytest.raises(SttModelMissingError) as exc:
        SherpaStreamingEngine(str(missing))
    assert str(missing) in str(exc.value)


@pytest.mark.unit
def test_empty_model_dir_raises():
    with pytest.raises(SttModelMissingError):
        SherpaStreamingEngine("")


@pytest.mark.unit
def test_missing_tokens_names_tokens_file(tmp_path):
    d = _model_dir(tmp_path, tokens=False)
    with pytest.raises(SttModelMissingError) as exc:
        SherpaStreamingEngine(str(d))
    assert "tokens.txt" in str(exc.value)


@pytest.mark.unit
def test_missing_component_files_raise(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "tokens.txt").write_text("<blk> 0\n")
    with pytest.raises(SttModelMissingError) as exc:
        SherpaStreamingEngine(str(d))
    assert str(d) in str(exc.value)


@pytest.mark.unit
def test_missing_backend_raises_named_error(tmp_path, monkeypatch):
    d = _model_dir(tmp_path)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)  # `import` -> ImportError
    with pytest.raises(SttBackendUnavailableError) as exc:
        SherpaStreamingEngine(str(d))
    assert "sherpa-onnx" in str(exc.value)


# ---------------------------------------------------------------------------
# Unit: construction, file choice, interface semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prefers_int8_variants(tmp_path, monkeypatch):
    _install_fake_sherpa(monkeypatch)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path, int8=True)))
    identity = engine.identity()
    assert identity.detail["encoder"].endswith(".int8.onnx")
    assert identity.detail["decoder"].endswith(".int8.onnx")
    assert identity.detail["joiner"].endswith(".int8.onnx")


@pytest.mark.unit
def test_falls_back_to_plain_onnx(tmp_path, monkeypatch):
    _install_fake_sherpa(monkeypatch)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path, int8=False)))
    identity = engine.identity()
    assert identity.detail["encoder"].endswith(".onnx")
    assert not identity.detail["encoder"].endswith(".int8.onnx")


@pytest.mark.unit
def test_implements_streaming_protocol(tmp_path, monkeypatch):
    _install_fake_sherpa(monkeypatch)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    assert isinstance(engine, StreamingSttEngine)
    assert engine.name == "sherpa_streaming"


@pytest.mark.unit
def test_identity_reports_model_path(tmp_path, monkeypatch):
    _install_fake_sherpa(monkeypatch)
    d = _model_dir(tmp_path)
    identity = SherpaStreamingEngine(str(d)).identity()
    assert identity.engine == "sherpa_streaming"
    assert identity.model_path == str(d)
    assert identity.model_kind == "transducer"


@pytest.mark.unit
def test_accept_rejects_stereo(tmp_path, monkeypatch):
    _install_fake_sherpa(monkeypatch)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    with pytest.raises(SttDecodeError):
        engine.accept_waveform(np.zeros((1600, 2), dtype=np.float32), 16000)


@pytest.mark.unit
def test_partial_finalize_reset_semantics(tmp_path, monkeypatch):
    _install_fake_sherpa(monkeypatch)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    assert engine.partial_text() == ""
    engine.accept_waveform(_speech(), 16000)
    assert engine.partial_text() != ""
    text = engine.finalize()
    assert text != ""
    # finalize resets: a fresh segment starts empty.
    assert engine.partial_text() == ""
    engine.accept_waveform(_speech(), 16000)
    engine.reset()
    assert engine.partial_text() == ""


@pytest.mark.unit
def test_off_rate_input_is_resampled(tmp_path, monkeypatch):
    _install_fake_sherpa(monkeypatch)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    engine.accept_waveform(_speech(n=800), 8000)  # half-rate frame
    assert engine.partial_text() != ""


@pytest.mark.unit
def test_finalize_never_masks_missing_model(tmp_path, monkeypatch):
    """A wrong implementation that swallows the missing-model error and
    returns '' must fail this test: silent empty success is forbidden."""
    with pytest.raises(SttModelMissingError):
        SherpaStreamingEngine(str(tmp_path / "absent"))


# ---------------------------------------------------------------------------
# Integration: real fixture, real speech, local inference only
# ---------------------------------------------------------------------------


def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        assert w.getsampwidth() == 2, "fixture must be 16-bit PCM"
        raw = w.readframes(w.getnframes())
        rate = w.getframerate()
    return (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, rate)


def _feed(engine: SherpaStreamingEngine, pcm: np.ndarray, rate: int, frame: int = 8000) -> None:
    for i in range(0, len(pcm), frame):
        engine.accept_waveform(pcm[i : i + frame], rate)


@pytest.mark.integration
@needs_real_model
def test_real_0_wav_transcribes_recognisable_words():
    """OBSERVED 2026-10-06: 'THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE THE SQUA'
    (reference trans.txt: 'AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP
    HERE AND THERE THE SQUALID QUARTER OF THE BROTHELS')."""
    engine = SherpaStreamingEngine(str(REAL_MODEL_DIR))
    pcm, rate = _load_wav(REAL_MODEL_DIR / "test_wavs" / "0.wav")
    _feed(engine, pcm, rate)
    text = engine.finalize()
    assert text.strip(), "real speech must not decode to empty"
    assert "YELLOW" in text
    assert "LAMPS" in text


@pytest.mark.integration
@needs_real_model
def test_real_1_wav_transcribes_recognisable_words():
    """OBSERVED 2026-10-06: 'AS A DIRECT CONSEQUENCE OF THE SIN WHICH MAN THUS
    PUNISHED HAD GIVEN HER A LOVELY CHILD WHOSE PLACE WAS ON THAT SAME
    DISHONORED BOSOM TO CONNECT HER PARENT FOR'."""
    engine = SherpaStreamingEngine(str(REAL_MODEL_DIR))
    pcm, rate = _load_wav(REAL_MODEL_DIR / "test_wavs" / "1.wav")
    _feed(engine, pcm, rate)
    text = engine.finalize()
    assert text.strip(), "real speech must not decode to empty"
    assert "LOVELY CHILD" in text


@pytest.mark.integration
@needs_real_model
def test_real_8k_wav_transcribes():
    """OBSERVED 2026-10-06: 8kHz clip decodes weakly as 'S AFFECTED'
    (reference: 'YET THESE THOUGHTS AFFECTED HESTER PRYNNE ...') — the 16k
    model on upsampled 8k audio keeps the content word but drops the rest."""
    engine = SherpaStreamingEngine(str(REAL_MODEL_DIR))
    pcm, rate = _load_wav(REAL_MODEL_DIR / "test_wavs" / "8k.wav")
    assert rate == 8000
    _feed(engine, pcm, rate)
    text = engine.finalize()
    assert text.strip(), "real speech must not decode to empty"
    assert "AFFECTED" in text


@pytest.mark.integration
@needs_real_model
def test_real_reset_clears_state():
    engine = SherpaStreamingEngine(str(REAL_MODEL_DIR))
    pcm, rate = _load_wav(REAL_MODEL_DIR / "test_wavs" / "0.wav")
    _feed(engine, pcm, rate)
    assert engine.partial_text().strip(), "speech must produce a partial"
    engine.reset()
    assert engine.partial_text() == ""


@pytest.mark.integration
@needs_real_model
def test_real_silence_then_speech_yields_text():
    engine = SherpaStreamingEngine(str(REAL_MODEL_DIR))
    engine.accept_waveform(np.zeros(16000, dtype=np.float32), 16000)  # 1 s silence
    pcm, rate = _load_wav(REAL_MODEL_DIR / "test_wavs" / "0.wav")
    _feed(engine, pcm, rate)
    assert "YELLOW" in engine.finalize()


@pytest.mark.integration
@needs_real_model
def test_real_identity_reports_fixture_path():
    identity = SherpaStreamingEngine(str(REAL_MODEL_DIR)).identity()
    assert identity.model_path == str(REAL_MODEL_DIR)
    assert identity.model_kind == "transducer"


# ---------------------------------------------------------------------------
# Readiness guard: the process-abort regression
#
# OBSERVED against the real library: decode_stream()/get_result() on a stream
# holding fewer than the feature window (39 frames, ~780 ms at 20 ms/frame)
# makes sherpa-onnx call LOG(FATAL) in features.cc ("0 + 39 > n") and abort the
# whole process. There is no exception to catch, so an always-on listener that
# decoded each 20 ms frame on arrival would die inside the first second of the
# first utterance. These tests pin the guard that prevents it.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_partial_text_survives_before_feature_window(tmp_path, monkeypatch):
    """Asking for text 20 ms into a segment must not decode early."""
    _install_fake_sherpa(monkeypatch, _WindowedFakeRecognizer)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    engine.accept_waveform(_speech(320), 16000)  # exactly one 20 ms frame
    assert engine.partial_text() == ""
    assert engine._recognizer.decode_calls == 0


@pytest.mark.unit
def test_finalize_survives_before_feature_window(tmp_path, monkeypatch):
    """A segment shorter than the window finalizes to "" instead of aborting."""
    _install_fake_sherpa(monkeypatch, _WindowedFakeRecognizer)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    engine.accept_waveform(_speech(320), 16000)
    assert engine.finalize() == ""
    assert engine._recognizer.decode_calls == 0


@pytest.mark.unit
def test_polling_partial_text_every_frame_never_decodes_early(tmp_path, monkeypatch):
    """The listener's real pattern: poll for wake text after every frame.

    The windowed fake raises AssertionError on an early decode, so this test
    fails loudly if the readiness guard is ever removed.
    """
    _install_fake_sherpa(monkeypatch, _WindowedFakeRecognizer)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    seen = []
    for _ in range(120):  # 2.4 s of 20 ms frames
        engine.accept_waveform(_speech(320), 16000)
        seen.append(engine.partial_text())
    assert engine._recognizer.decode_calls > 0, "should have decoded once ready"
    assert any(t for t in seen), "text should appear once the window is filled"


@pytest.mark.unit
def test_never_fed_stream_needs_no_decode(tmp_path, monkeypatch):
    """A stream that received nothing must not be decoded at all."""
    _install_fake_sherpa(monkeypatch, _WindowedFakeRecognizer)
    engine = SherpaStreamingEngine(str(_model_dir(tmp_path)))
    assert engine.partial_text() == ""
    assert engine.finalize() == ""
    assert engine._recognizer.decode_calls == 0


@pytest.mark.integration
@needs_real_model
def test_real_polling_every_20ms_frame_does_not_abort():
    """The real-model version of the listener loop.

    Before the readiness guard this aborted the interpreter (exit 255) partway
    through the utterance rather than failing an assertion.
    """
    engine = SherpaStreamingEngine(str(REAL_MODEL_DIR))
    pcm, rate = _load_wav(REAL_MODEL_DIR / "test_wavs" / "0.wav")
    last = ""
    for i in range(0, len(pcm), 320):
        engine.accept_waveform(pcm[i : i + 320], rate)
        last = engine.partial_text() or last
    text = engine.finalize()
    assert text.strip(), "polling during capture must still yield a transcript"
    assert "YELLOW" in text
