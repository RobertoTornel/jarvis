"""Tests for the authoritative Parakeet engine (NeMo and ONNX loaders).

No Parakeet weights exist on this machine and the tests must not touch the
network, so the happy paths inject fake ``nemo.collections.asr`` and
``onnxruntime`` modules through the single lazy-import hook the engine uses.
Nothing here is skipped: every backend-mediated path is exercised against a
fake, and every missing-file / missing-backend path runs for real.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from claire.listening.stt import parakeet
from claire.listening.stt.base import (
    SttBackendUnavailableError,
    SttModelMissingError,
    SttResult,
)

# The gate runs `pytest -m unit`. A test file without this marker is
# silently DESELECTED, so it can pass forever without ever running.
pytestmark = pytest.mark.unit

SAMPLE_RATE = 16_000


def _audio(seconds: float = 1.0, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    t = np.linspace(0, seconds, int(seconds * sample_rate), endpoint=False)
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeNemoModel:
    """Stands in for a restored NeMo ASR model."""

    def __init__(self, text: str = "hello from parakeet") -> None:
        self.text = text
        self.calls: list[tuple[list[np.ndarray], dict]] = []

    def transcribe(self, audio, batch_size=1):  # noqa: ANN001 - mirrors NeMo
        self.calls.append((list(audio), {"batch_size": batch_size}))
        return [self.text]


class FakeInput:
    def __init__(self, name: str, shape: list) -> None:
        self.name = name
        self.shape = shape


class FakeSession:
    """Stands in for onnxruntime.InferenceSession."""

    def __init__(self, path: str, token_ids=None, shape=None) -> None:
        self.path = path
        # Keep the caller's dtype: integer ids and float logits are decoded by
        # two different (correct) branches of the engine.
        if token_ids is None:
            self.token_ids = np.asarray([[0, 3, 4, 4, 5]], dtype=np.int64)
        else:
            self.token_ids = np.asarray(token_ids)
        self.shape = shape if shape is not None else [1, 80, 3000]
        self.feeds: list[tuple[str, np.ndarray]] = []

    def get_inputs(self):
        return [FakeInput("input_features", list(self.shape))]

    def run(self, output_names, feed):  # noqa: ANN001 - mirrors onnxruntime
        for key, value in feed.items():
            self.feeds.append((key, value))
        return [self.token_ids]


def _fake_nemo_module(model: FakeNemoModel, restore_calls: list):
    def restore_from(path, **kwargs):  # noqa: ANN001
        restore_calls.append((path, kwargs))
        return model

    return SimpleNamespace(
        models=SimpleNamespace(ASRModel=SimpleNamespace(restore_from=restore_from))
    )


def _fake_torch(cuda_available: bool = False):
    """Stand-in for torch: device construction + CUDA availability, no import."""
    return SimpleNamespace(
        device=lambda name: f"device({name})",
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
    )


def _install_import(monkeypatch, mapping: dict):
    """Patch the engine's only import hook; record what was asked for."""
    asked: list[str] = []

    def fake_import(dotted: str):
        asked.append(dotted)
        if dotted not in mapping:
            raise ModuleNotFoundError(f"No module named {dotted!r}")
        return mapping[dotted]

    monkeypatch.setattr(parakeet, "_import_module", fake_import)
    return asked


def _write_nemo(tmp_path):
    path = tmp_path / "parakeet-tdt-0.6b-v2.nemo"
    path.write_bytes(b"not a real checkpoint")
    return path


def _write_onnx_dir(tmp_path, name: str = "model.onnx", tokens: bool = True):
    directory = tmp_path / "parakeet_onnx"
    directory.mkdir()
    (directory / name).write_bytes(b"not a real graph")
    if tokens:
        (directory / "tokens.txt").write_text(
            "0 <blk>\n1 <pad>\n2 <s>\n3 \u2581hello\n4 \u2581there\n5 \u2581friend\n",
            encoding="utf-8",
        )
    return directory


# --------------------------------------------------------------------------
# kind inference
# --------------------------------------------------------------------------
def test_auto_infers_nemo_for_dot_nemo_file(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    model = FakeNemoModel()
    asked = _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(model, []), "torch": _fake_torch()})

    engine = parakeet.create_parakeet_engine(str(path))

    assert engine.identity().model_kind == "nemo"
    assert engine.name == parakeet.NEMO_ENGINE_NAME
    assert asked == ["nemo.collections.asr", "torch"]
    assert engine.raw_model is model


def test_auto_infers_onnx_for_directory(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"))
    asked = _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    engine = parakeet.create_parakeet_engine(str(directory))

    assert engine.identity().model_kind == "onnx"
    assert engine.name == parakeet.ONNX_ENGINE_NAME
    assert asked == ["onnxruntime"]
    assert engine.raw_model is session


def test_auto_infers_onnx_for_dot_onnx_file(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    onnx_file = directory / "model.onnx"
    session = FakeSession(str(onnx_file))
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    engine = parakeet.create_parakeet_engine(str(onnx_file))

    assert engine.identity().model_kind == "onnx"


def test_explicit_kind_overrides_suffix(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"))
    asked = _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    engine = parakeet.create_parakeet_engine(str(directory), kind="ONNX")

    assert engine.identity().model_kind == "onnx"
    assert asked == ["onnxruntime"]


def test_unknown_kind_is_rejected(tmp_path):
    path = _write_nemo(tmp_path)
    with pytest.raises(ValueError):
        parakeet.create_parakeet_engine(str(path), kind="magic")


def test_nemo_loader_does_not_import_onnxruntime(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    model = FakeNemoModel()
    asked = _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(model, []), "torch": _fake_torch()})

    parakeet.create_parakeet_engine(str(path))

    assert "onnxruntime" not in asked


# --------------------------------------------------------------------------
# missing model files
# --------------------------------------------------------------------------
def test_missing_nemo_path_names_the_path(tmp_path, monkeypatch):
    asked = _install_import(monkeypatch, {})
    missing = tmp_path / "does-not-exist.nemo"

    with pytest.raises(SttModelMissingError) as excinfo:
        parakeet.create_parakeet_engine(str(missing))

    assert str(missing) in str(excinfo.value)
    assert str(missing) == excinfo.value.path
    # A missing model must not reach for the backend at all.
    assert asked == []


def test_missing_model_hint_names_stock_model_and_finetune(tmp_path):
    missing = tmp_path / "nope" / "parakeet.nemo"

    with pytest.raises(SttModelMissingError) as excinfo:
        parakeet.create_parakeet_engine(str(missing))

    message = str(excinfo.value)
    assert "nvidia/parakeet-tdt-0.6b-v2" in message
    assert "finetuned Parakeet" in message


def test_missing_onnx_directory_path_names_the_path(tmp_path):
    missing = tmp_path / "absent-dir"

    with pytest.raises(SttModelMissingError) as excinfo:
        parakeet.create_parakeet_engine(str(missing), kind="onnx")

    assert str(missing) in str(excinfo.value)


def test_onnx_dir_without_graph_names_the_expected_file(tmp_path, monkeypatch):
    directory = tmp_path / "empty_export"
    directory.mkdir()
    (directory / "tokens.txt").write_text("0 <blk>\n1 \u2581hi\n", encoding="utf-8")
    _install_import(monkeypatch, {})

    with pytest.raises(SttModelMissingError) as excinfo:
        parakeet.create_parakeet_engine(str(directory))

    assert str(directory / "model.onnx") in str(excinfo.value)


def test_onnx_dir_without_tokens_names_tokens_file(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path, tokens=False)
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: FakeSession(p))},
    )

    with pytest.raises(SttModelMissingError) as excinfo:
        parakeet.create_parakeet_engine(str(directory))

    message = str(excinfo.value)
    assert "tokens.txt" in message
    assert str(directory) in message


def test_unsupported_auto_path_is_a_named_error(tmp_path, monkeypatch):
    _install_import(monkeypatch, {})
    stray = tmp_path / "weights.bin"
    stray.write_bytes(b"x")

    with pytest.raises(SttModelMissingError) as excinfo:
        parakeet.create_parakeet_engine(str(stray))

    assert str(stray) in str(excinfo.value)


# --------------------------------------------------------------------------
# missing backends
# --------------------------------------------------------------------------
def test_missing_nemo_backend_raises_backend_unavailable(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    _install_import(monkeypatch, {})  # every import fails

    with pytest.raises(SttBackendUnavailableError) as excinfo:
        parakeet.create_parakeet_engine(str(path))

    assert excinfo.value.backend == "nemo"
    assert excinfo.value.package == "nemo_toolkit[asr]"
    assert "nemo_toolkit[asr]" in str(excinfo.value)


def test_missing_onnx_backend_raises_backend_unavailable(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    _install_import(monkeypatch, {})

    with pytest.raises(SttBackendUnavailableError) as excinfo:
        parakeet.create_parakeet_engine(str(directory))

    assert excinfo.value.backend == "onnxruntime"
    assert excinfo.value.package == "onnxruntime"
    assert "onnxruntime" in str(excinfo.value)


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------
def test_identity_reports_model_path_kind_and_engine(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    model = FakeNemoModel()
    _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(model, []), "torch": _fake_torch()})

    engine = parakeet.create_parakeet_engine(str(path))
    identity = engine.identity()

    assert identity.engine == parakeet.NEMO_ENGINE_NAME
    assert identity.model_path == str(path)
    assert identity.model_kind == "nemo"
    assert identity.detail["device"] == "cpu"
    assert str(path) in identity.describe()


def test_identity_reports_onnx_export_detail(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"))
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    identity = parakeet.create_parakeet_engine(str(directory)).identity()

    assert identity.model_path == str(directory)
    assert identity.detail["onnx_files"] == ["model.onnx"]
    assert identity.detail["tokens"] == "tokens.txt"
    assert identity.detail["vocab_size"] == 6


def test_identity_reports_requested_device_honestly(tmp_path, monkeypatch):
    from claire.listening.stt.base import SttBackendUnavailableError
    directory = _write_onnx_dir(tmp_path)

    # cuda with a CUDA-capable fake: works, and identity echoes the request.
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(
            InferenceSession=lambda p, **kw: FakeSession(p),
            get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
        )},
    )
    identity = parakeet.create_parakeet_engine(str(directory), device="cuda").identity()
    assert identity.detail["device"] == "cuda"

    # cuda without CUDA providers: honest named error, not silent CPU fallback.
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(
            InferenceSession=lambda p, **kw: FakeSession(p),
            get_available_providers=lambda: ["CPUExecutionProvider"],
        )},
    )
    with pytest.raises(SttBackendUnavailableError):
        parakeet.create_parakeet_engine(str(directory), device="cuda")


# --------------------------------------------------------------------------
# device enforcement (stt_device honesty)
# --------------------------------------------------------------------------
def test_nemo_restore_receives_cpu_map_location(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    model = FakeNemoModel()
    calls: list = []
    _install_import(monkeypatch, {
        "nemo.collections.asr": _fake_nemo_module(model, calls),
        "torch": _fake_torch(),
    })

    parakeet.create_parakeet_engine(str(path), device="cpu")

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert "map_location" in kwargs, "stt_device='cpu' must reach restore_from as map_location"
    assert kwargs["map_location"] == "device(cpu)"


def test_onnx_session_receives_cpu_only_providers(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    seen: list = []

    class RecordingSession(FakeSession):
        def __init__(self, path, **kwargs):
            super().__init__(path)
            seen.append(kwargs)

    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=RecordingSession)},
    )

    parakeet.create_parakeet_engine(str(directory), device="cpu")

    assert len(seen) == 1
    providers = seen[0].get("providers", [])
    assert providers, "stt_device='cpu' must reach InferenceSession as providers="
    assert all("CPU" in p.upper() for p in providers), f"cpu must not enable GPU providers: {providers}"


# --------------------------------------------------------------------------
# transcribe
# --------------------------------------------------------------------------
def test_transcribe_nemo_returns_fake_text(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    model = FakeNemoModel("the quick brown fox")
    _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(model, []), "torch": _fake_torch()})
    engine = parakeet.create_parakeet_engine(str(path))

    result = engine.transcribe(_audio(), SAMPLE_RATE)

    assert isinstance(result, SttResult)
    assert result.text == "the quick brown fox"
    assert result.language is None
    assert result.engine == parakeet.NEMO_ENGINE_NAME
    assert result.elapsed_sec >= 0.0
    # NeMo was handed float32 16 kHz mono audio.
    fed = model.calls[0][0][0]
    assert fed.dtype == np.float32
    assert fed.ndim == 1
    assert fed.shape[0] == SAMPLE_RATE


def test_transcribe_nemo_handles_hypothesis_objects(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)

    class Hypothesis:
        text = "object shaped transcript"

    model = FakeNemoModel()
    model.transcribe = lambda audio, batch_size=1: [Hypothesis()]  # noqa: ARG005
    _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(model, []), "torch": _fake_torch()})

    result = parakeet.create_parakeet_engine(str(path)).transcribe(_audio(0.3), SAMPLE_RATE)

    assert result.text == "object shaped transcript"


def test_transcribe_onnx_decodes_token_ids(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"), token_ids=[[0, 3, 4, 4, 5]])
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )
    engine = parakeet.create_parakeet_engine(str(directory))

    result = engine.transcribe(_audio(), SAMPLE_RATE)

    assert result.text == "hello there friend"
    assert result.engine == parakeet.ONNX_ENGINE_NAME
    assert result.language is None


def test_transcribe_onnx_builds_mel_features_for_whisper_layout(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"), shape=[1, 80, 3000])
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    parakeet.create_parakeet_engine(str(directory)).transcribe(_audio(0.5), SAMPLE_RATE)

    name, features = session.feeds[0]
    assert name == "input_features"
    # (batch, n_mels, frames) — a Whisper-style export.
    assert features.shape == (1, 80, 3000)
    assert features.dtype == np.float32


def test_transcribe_onnx_builds_features_for_trailing_frames_layout(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"), shape=[1, -1, 80])
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    parakeet.create_parakeet_engine(str(directory)).transcribe(_audio(0.5), SAMPLE_RATE)

    _, features = session.feeds[0]
    # (batch, frames, n_mels) — a Parakeet-style export.
    assert features.shape[0] == 1
    assert features.shape[2] == 80
    assert features.shape[1] > 0


def test_transcribe_onnx_argmaxes_logits(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    logits = np.zeros((1, 3, 6), dtype=np.float32)
    for frame, token in enumerate([3, 4, 5]):
        logits[0, frame, token] = 5.0
    session = FakeSession(str(directory / "model.onnx"), token_ids=logits)
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    result = parakeet.create_parakeet_engine(str(directory)).transcribe(_audio(0.5), SAMPLE_RATE)

    assert result.text == "hello there friend"


def test_transcribe_resamples_non_16khz_input(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"), shape=[1, 80, -1])
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )
    engine = parakeet.create_parakeet_engine(str(directory))

    result = engine.transcribe(_audio(1.0, sample_rate=48_000), 48_000)

    assert result.text == "hello there friend"
    # 1s downsampled to 16 kHz lands at roughly 98 mel frames, not 298 at 48 kHz.
    assert 80 <= session.feeds[0][1].shape[2] <= 120


def test_transcribe_stereo_input_is_averaged(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"), shape=[1, 80, -1])
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )
    stereo = np.stack([_audio(0.3), _audio(0.3)], axis=1)  # (samples, 2)

    parakeet.create_parakeet_engine(str(directory)).transcribe(stereo, SAMPLE_RATE)

    assert session.feeds[0][1].shape[0] == 1
    assert session.feeds[0][1].shape[1] == 80


def test_empty_audio_is_not_an_empty_success(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: FakeSession(p))},
    )
    engine = parakeet.create_parakeet_engine(str(directory))

    with pytest.raises(parakeet.SttDecodeError):
        engine.transcribe(np.zeros(0, dtype=np.float32), SAMPLE_RATE)


def test_failing_nemo_transcribe_raises_named_decode_error(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    model = FakeNemoModel()

    def boom(audio, batch_size=1):  # noqa: ANN001, ARG001
        raise RuntimeError("cuda exploded")

    model.transcribe = boom
    _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(model, []), "torch": _fake_torch()})
    engine = parakeet.create_parakeet_engine(str(path))

    with pytest.raises(parakeet.SttDecodeError) as excinfo:
        engine.transcribe(_audio(0.3), SAMPLE_RATE)

    assert "cuda exploded" in str(excinfo.value)


def test_failing_onnx_session_raises_named_decode_error(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)

    class BoomSession(FakeSession):
        def run(self, output_names, feed):  # noqa: ANN001, ARG002
            raise RuntimeError("graph refused")

    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: BoomSession(p))},
    )
    engine = parakeet.create_parakeet_engine(str(directory))

    with pytest.raises(parakeet.SttDecodeError) as excinfo:
        engine.transcribe(_audio(0.3), SAMPLE_RATE)

    assert "graph refused" in str(excinfo.value)


# --------------------------------------------------------------------------
# laziness and the shared contract
# --------------------------------------------------------------------------
def test_heavy_backends_are_imported_lazily(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    asked = _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(FakeNemoModel(), []), "torch": _fake_torch()})

    # Importing the module has not reached for a backend...
    assert asked == []
    # ...and neither has merely constructing nothing: the call does it.
    parakeet.create_parakeet_engine(str(path))
    assert asked == ["nemo.collections.asr", "torch"]


def test_module_import_pulls_no_heavy_backend():
    """Importing the module must not drag in a heavy backend.

    Checked in a subprocess on purpose.  ``sys.modules`` is process-global, so
    asserting on it in-process makes this test a report on every *other* test
    that ran first rather than on the module itself - and it duly went red
    intermittently once threaded tests entered the suite, while passing alone
    and while an import hook recorded no heavy import at all.  A fresh
    interpreter asks the question that was actually meant.
    """
    code = (
        "import sys\n"
        "from claire.listening.stt import parakeet  # noqa: F401\n"
        "heavy = [m for m in (\"nemo\", \"nemo.collections.asr\", \"onnxruntime\", \"torch\")\n"
        "         if m in sys.modules]\n"
        "print(\",\".join(heavy))\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, (
        "importing claire.listening.stt.parakeet pulled in a heavy backend in a "
        f"fresh interpreter: {result.stdout.strip() or result.stderr.strip()}"
    )


def test_engine_satisfies_stt_engine_protocol(tmp_path, monkeypatch):
    path = _write_nemo(tmp_path)
    _install_import(monkeypatch, {"nemo.collections.asr": _fake_nemo_module(FakeNemoModel(), []), "torch": _fake_torch()})

    engine = parakeet.create_parakeet_engine(str(path))

    assert isinstance(engine, parakeet.SttEngine)
    assert isinstance(engine.name, str)


def test_greedy_decode_keeps_every_token_when_blanks_absent(tmp_path, monkeypatch):
    directory = _write_onnx_dir(tmp_path)
    session = FakeSession(str(directory / "model.onnx"), token_ids=[[3, 4, 5]])
    _install_import(
        monkeypatch,
        {"onnxruntime": SimpleNamespace(InferenceSession=lambda p, **kw: session)},
    )

    result = parakeet.create_parakeet_engine(str(directory)).transcribe(_audio(0.3), SAMPLE_RATE)

    assert result.text == "hello there friend"


# ---------------------------------------------------------------------------
# One engine, two callers: the decode must be serialised
# ---------------------------------------------------------------------------
#
# NeMo's transcription path is not re-entrant: it restores module gradients from
# a snapshot the next call clears, so overlapping calls race. Reproduced on this
# machine against the real model - 3 of 18 concurrent transcribes failed with
#   ValueError: Cannot unfreeze partially without first freezing the module
#   with `freeze()`
# which the voice loop surfaced as "Voice transcript degraded". The voice loop
# and the phone walkie-talkie route share this engine on purpose, so the race is
# reachable whenever a phone upload lands while the microphone is decoding.

class _OverlapDetectingEngine(parakeet._ParakeetEngineBase):
    """A fake decode that fails if two callers are ever inside it at once."""

    name = "fake"

    def __init__(self) -> None:
        super().__init__("fake.nemo")
        self.inside = 0
        self.max_inside = 0
        self.calls = 0
        self.guard = threading.Lock()

    def _transcribe_samples(self, samples):  # type: ignore[override]
        with self.guard:
            self.inside += 1
            self.calls += 1
            self.max_inside = max(self.max_inside, self.inside)
        time.sleep(0.02)  # a decode takes ~1 s; this only needs to overlap
        with self.guard:
            self.inside -= 1
        return "ok"


def test_concurrent_transcribes_do_not_overlap() -> None:
    engine = _OverlapDetectingEngine()
    audio = np.zeros(16000, dtype=np.float32)

    results: list[str] = []
    errors: list[BaseException] = []

    def caller() -> None:
        for _ in range(3):
            try:
                results.append(engine.transcribe(audio, 16000).text)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert engine.max_inside == 1, (
        "two decodes ran on one engine at once, which is the race that made "
        "Claire report a degraded transcript"
    )
    assert len(results) == 9


def test_a_failing_decode_still_releases_the_lock() -> None:
    class _Failing(parakeet._ParakeetEngineBase):
        name = "failing"

        def __init__(self) -> None:
            super().__init__("failing.nemo")
            self.calls = 0

        def _transcribe_samples(self, samples):  # type: ignore[override]
            self.calls += 1
            raise RuntimeError("decode exploded")

    engine = _Failing()
    audio = np.zeros(16000, dtype=np.float32)

    for _ in range(2):
        # The base shell lets a decode's own error through: the concrete engines
        # raise SttDecodeError from _transcribe_samples, and wrapping it twice
        # would only hide which layer spoke.
        with pytest.raises(RuntimeError, match="decode exploded"):
            engine.transcribe(audio, 16000)

    assert engine.calls == 2, "the second call must not deadlock on the lock"
