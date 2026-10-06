"""Parakeet v2 — the *authoritative* speech recogniser.

Claire's always-on listener is a streaming zipformer whose output is
provisional (:mod:`claire.listening.stt.sherpa_streaming`). The text a turn
acts on and remembers comes from here: Parakeet, run once per clipped
utterance (decision D3/D4, see ``stt.spec.md``).

Two on-disk shapes are supported, because a Parakeet model is shipped either
way and both must be a configuration change rather than a code change:

``.nemo``
    A NeMo checkpoint. Loaded through ``nemo.collections.asr`` with
    ``ASRModel.restore_from``. ``nemo_toolkit[asr]`` is imported lazily, inside
    the loader, so a machine that only ever runs the ONNX export never pays for
    it (and never fails on its absence at import time).

directory of ``.onnx``
    A Whisper/Parakeet ONNX export — one or more ``.onnx`` files plus a
    tokens/vocab file. Loaded through ``onnxruntime.InferenceSession``, also
    imported lazily.

Nothing heavy (torch, nemo, onnxruntime) is imported at module import time;
``import claire.listening.stt.parakeet`` only needs numpy.

Every failure is named, per :mod:`claire.listening.stt.base`: a missing model
path raises :class:`SttModelMissingError` naming the path that was looked for
and how to obtain it, and a missing backend raises
:class:`SttBackendUnavailableError`. A missing file never degrades into an
empty transcript, which would be indistinguishable from "the user said
nothing".
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .base import (
    SttBackendUnavailableError,
    SttDecodeError,
    SttEngine,
    SttIdentity,
    SttModelMissingError,
    SttResult,
)

#: Where an operator gets a model when the configured path is missing. Names
#: both the stock model and the owner's finetune, because either is a valid
#: choice and the error should not force a look at the source to find out.
_MODEL_HINT = (
    "Fetch the stock model (nvidia/parakeet-tdt-0.6b-v2) or point "
    "stt_model_path at the owner's finetuned Parakeet .nemo / ONNX export "
    "directory."
)

#: Backend package names, used verbatim in SttBackendUnavailableError.
_NEMO_BACKEND = "nemo"
_NEMO_PACKAGE = "nemo_toolkit[asr]"
_ONNX_BACKEND = "onnxruntime"
_ONNX_PACKAGE = "onnxruntime"

#: Filenames a tokens/vocab table is allowed to have, in preference order.
_TOKEN_FILENAMES = ("tokens.txt", "vocab.txt", "tokens.json", "vocab.json")

#: Parakeet/NeMo models are trained at 16 kHz; so are the ONNX exports.
_TARGET_SAMPLE_RATE = 16_000

NEMO_ENGINE_NAME = "parakeet_nemo"
ONNX_ENGINE_NAME = "parakeet_onnx"


def _import_module(dotted: str) -> Any:
    """Import a dotted module name.

    Isolated behind a function so the loaders stay lazily importing (the call
    happens inside the loader, never at module import) and so tests can supply
    a fake backend without installing torch or onnxruntime.
    """
    return importlib.import_module(dotted)


def _resample(audio: np.ndarray, sample_rate: int, target: int) -> np.ndarray:
    """Linear-resample mono audio to ``target`` Hz.

    Cheap and dependency-free: the authoritative engine's job is correctness of
    *text*, and the capture path already aims for 16 kHz, so a high-order
    resampler would be unjustified weight here.
    """
    if sample_rate == target or audio.size == 0:
        return audio
    duration = audio.shape[0] / float(sample_rate)
    target_len = max(1, int(round(duration * target)))
    src_t = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    dst_t = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(dst_t, src_t, audio).astype(np.float32, copy=False)


def _as_mono_float32(audio: np.ndarray) -> np.ndarray:
    """Coerce whatever the caller passes into the 1-D float32 mono contract."""
    arr = np.asarray(audio)
    if arr.ndim > 1:
        # (channels, samples) or (samples, channels): average to mono along the
        # smaller axis, which is the channel axis in both layouts.
        axis = 0 if arr.shape[0] < arr.shape[1] else 1
        arr = arr.mean(axis=axis)
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32, copy=False)
    return np.ascontiguousarray(arr.reshape(-1))


def _validate_audio(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    arr = _as_mono_float32(audio)
    if arr.size == 0:
        # Never return an empty transcript for an empty buffer: that is a
        # capture bug, not silence.
        raise SttDecodeError("Parakeet received an empty audio buffer")
    if sample_rate <= 0:
        raise SttDecodeError(f"Parakeet received an invalid sample rate {sample_rate!r}")
    return arr


def _require_exists(path: Path) -> None:
    if not path.exists():
        raise SttModelMissingError(str(path), _MODEL_HINT)


def _infer_kind(model_path: str) -> str:
    """Decide the loader from the path shape when ``kind='auto'``."""
    path = Path(model_path)
    if path.suffix.lower() == ".nemo":
        return "nemo"
    if path.is_dir():
        return "onnx"
    if path.suffix.lower() == ".onnx":
        return "onnx"
    raise SttModelMissingError(
        str(path),
        "Parakeet paths must be a .nemo file or a directory of .onnx exports. "
        + _MODEL_HINT,
    )


class _ParakeetEngineBase:
    """Shared bookkeeping: name, path, monophonic-16 kHz transcription shell."""

    name = "parakeet"
    model_kind = "unknown"

    def __init__(self, model_path: str, detail: dict[str, Any] | None = None) -> None:
        self.model_path = str(model_path)
        self.raw_model: Any = None
        self._detail: dict[str, Any] = dict(detail or {})

    # -- contract ---------------------------------------------------------
    def identity(self) -> SttIdentity:
        detail = dict(self._detail)
        detail.setdefault("loaded", self.raw_model is not None)
        return SttIdentity(
            engine=self.name,
            model_path=self.model_path,
            model_kind=self.model_kind,
            detail=detail,
        )

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> SttResult:
        samples = _validate_audio(audio, sample_rate)
        samples = _resample(samples, sample_rate, _TARGET_SAMPLE_RATE)
        started = time.perf_counter()
        text = self._transcribe_samples(samples)
        elapsed = time.perf_counter() - started
        if not isinstance(text, str):
            raise SttDecodeError(
                f"{self.name} produced a {type(text).__name__}, not a transcript"
            )
        return SttResult(
            text=text,
            language=None,
            engine=self.name,
            elapsed_sec=elapsed,
        )

    # -- backend specific -------------------------------------------------
    def _transcribe_samples(self, samples: np.ndarray) -> str:  # pragma: no cover
        raise NotImplementedError


class ParakeetNemoEngine(_ParakeetEngineBase):
    """Parakeet loaded from a NeMo ``.nemo`` checkpoint."""

    name = NEMO_ENGINE_NAME
    model_kind = "nemo"

    def __init__(self, model_path: str, model: Any, detail: dict[str, Any] | None = None) -> None:
        super().__init__(model_path, detail)
        self.raw_model = model

    def _transcribe_samples(self, samples: np.ndarray) -> str:
        model = self.raw_model
        if model is None:  # pragma: no cover - constructor always sets it
            raise SttDecodeError("NeMo Parakeet model was never loaded")
        try:
            output = model.transcribe([samples], batch_size=1)
        except TypeError:
            # Older NeMo builds do not accept batch_size.
            output = model.transcribe([samples])
        except Exception as exc:  # noqa: BLE001 - re-raised as a named error
            raise SttDecodeError(f"NeMo Parakeet transcription failed: {exc}") from exc
        return _first_transcript(output)


def _first_transcript(output: Any) -> str:
    """Pull a string out of whatever shape NeMo's ``transcribe`` returns.

    NeMo returns ``list[str]`` for some versions, ``list[Hypothesis]`` for
    others, and a bare ``str``/``Hypothesis`` when handed a single item.
    """
    if output is None:
        raise SttDecodeError("NeMo Parakeet returned no transcript")
    item = output
    if isinstance(item, (list, tuple)):
        if len(item) == 0:
            raise SttDecodeError("NeMo Parakeet returned an empty result list")
        item = item[0]
    text = getattr(item, "text", item)
    if isinstance(text, (list, tuple)):
        if len(text) == 0:
            raise SttDecodeError("NeMo Parakeet returned an empty transcript tuple")
        text = text[0]
    if not isinstance(text, str):
        text = str(text)
    return text


def _load_tokens(tokens_path: Path) -> dict[int, str]:
    """Read a token table, supporting ``id token``, ``token`` and JSON forms."""
    raw = tokens_path.read_text(encoding="utf-8")
    if tokens_path.suffix.lower() == ".json":
        import json

        data = json.loads(raw)
        table: dict[int, str] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                try:
                    table[int(key)] = str(value)
                except (TypeError, ValueError):
                    continue
        elif isinstance(data, list):
            table = {i: str(v) for i, v in enumerate(data)}
        if not table:
            raise SttDecodeError(f"Token table {tokens_path} is empty")
        return table

    table = {}
    for index, line in enumerate(raw.splitlines()):
        if not line or line.startswith("#"):
            continue
        parts = line.rstrip("\n").split(maxsplit=1)
        if len(parts) == 2 and parts[0].lstrip("-").isdigit():
            table[int(parts[0])] = parts[1]
        else:
            table[len(table)] = parts[0] if parts else ""
    if not table:
        raise SttDecodeError(f"Token table {tokens_path} is empty")
    return table


def _pick_token_file(directory: Path) -> Path | None:
    for filename in _TOKEN_FILENAMES:
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


class ParakeetOnnxEngine(_ParakeetEngineBase):
    """Parakeet (or a Parakeet/Whisper-style export) loaded via onnxruntime."""

    name = ONNX_ENGINE_NAME
    model_kind = "onnx"

    def __init__(
        self,
        model_path: str,
        sessions: Sequence[Any],
        token_ids: dict[int, str],
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(model_path, detail)
        self._sessions = list(sessions)
        self._tokens = dict(token_ids)
        # The primary graph is what identity()/debugging surfaces.
        self.raw_model = self._sessions[0] if self._sessions else None

    def _transcribe_samples(self, samples: np.ndarray) -> str:
        session = self.raw_model
        if session is None:  # pragma: no cover - factory always supplies one
            raise SttDecodeError("ONNX Parakeet session was never created")
        inputs = session.get_inputs()
        if not inputs:
            raise SttDecodeError("ONNX Parakeet graph exposes no inputs")
        spec = inputs[0]
        features = self._features(samples, spec)
        try:
            outputs = session.run(None, {spec.name: features})
        except Exception as exc:  # noqa: BLE001 - re-raised as a named error
            raise SttDecodeError(f"ONNX Parakeet inference failed: {exc}") from exc
        ids = _token_ids_from_outputs(outputs, len(self._tokens))
        if ids is None:
            raise SttDecodeError("ONNX Parakeet produced no token ids")
        return _decode_ids(ids, self._tokens)

    def _features(self, samples: np.ndarray, spec: Any) -> np.ndarray:
        """Shape log-mel features the way this particular export expects.

        Exports disagree on layout and on frame count: a Whisper export wants
        ``(1, n_mels, frames)``, a Parakeet export ``(1, frames, n_mels)``. The
        graph's own declared input shape decides, and a fixed frame count is
        honoured (padded/truncated) when the export declares one.

        A mel axis is only recognisable by its size (64/80/128); when that
        leaves the layout ambiguous the input's *name* is the tie-breaker
        (Whisper exports their mel input as ``input_features``/``mel``), and an
        entirely dynamic shape falls back to the Parakeet layout that this
        engine exists to serve.
        """
        shape = list(getattr(spec, "shape", []) or [])
        name = str(getattr(spec, "name", "") or "").lower()
        mel_sizes = (64, 80, 128)
        n_mels = 80
        trailing_mels = True  # (batch, frames, n_mels) — the Parakeet default

        if len(shape) == 3:
            first, second = shape[1], shape[2]
            first_is_mel = isinstance(first, int) and first in mel_sizes
            second_is_mel = isinstance(second, int) and second in mel_sizes
            if first_is_mel and not second_is_mel:
                n_mels, trailing_mels = first, False
            elif second_is_mel and not first_is_mel:
                n_mels, trailing_mels = second, True
            elif first_is_mel and second_is_mel:
                # Both look like a mel count: trust the Whisper-ish input name
                # if it says so, otherwise keep mels last.
                trailing_mels = "feature" not in name and "mel" not in name
                n_mels = second if trailing_mels else first
            elif "feature" in name or "mel" in name:
                trailing_mels = False

        mel = _log_mel(samples, n_mels)
        frames_axis = 1 if trailing_mels else 2
        declared_frames = shape[frames_axis] if len(shape) == 3 else None
        if isinstance(declared_frames, int) and declared_frames > 0:
            fixed_frames = declared_frames
            if mel.shape[0] < fixed_frames:
                pad = np.zeros((fixed_frames - mel.shape[0], n_mels), dtype=np.float32)
                mel = np.concatenate([mel, pad], axis=0)
            else:
                mel = mel[:fixed_frames]

        if trailing_mels:
            return mel[np.newaxis, :, :].astype(np.float32)
        return np.transpose(mel, (1, 0))[np.newaxis, :, :].astype(np.float32)


def _hann_window(size: int) -> np.ndarray:
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(size) / size)


def _mel_filterbank(n_mels: int, fft_size: int, sample_rate: int) -> np.ndarray:
    """Slaney-style triangular mel filterbank, computed without librosa."""
    def hz_to_mel(freq: np.ndarray | float) -> np.ndarray | float:
        return 2595.0 * np.log10(1.0 + np.asarray(freq) / 700.0)

    def mel_to_hz(mel: np.ndarray) -> np.ndarray:
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    n_bins = fft_size // 2 + 1
    mel_points = np.linspace(hz_to_mel(0.0), hz_to_mel(sample_rate / 2.0), n_mels + 2)
    hz_points = mel_to_hz(mel_points)
    bins = np.floor((fft_size + 1) * hz_points / sample_rate).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    bank = np.zeros((n_mels, n_bins), dtype=np.float32)
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        if center == left:
            center = min(left + 1, n_bins - 1)
        if right == center:
            right = min(center + 1, n_bins - 1)
        for k in range(left, center):
            bank[m - 1, k] = (k - left) / max(1, center - left)
        for k in range(center, right):
            bank[m - 1, k] = (right - k) / max(1, right - center)
    return bank


def _log_mel(samples: np.ndarray, n_mels: int = 80, fft_size: int = 512,
             hop_length: int = 160, win_length: int = 400) -> np.ndarray:
    """Small dependency-free log-mel front end, used only by the ONNX path."""
    if samples.size < win_length:
        samples = np.pad(samples, (0, win_length - samples.size))
    frames = 1 + (samples.size - win_length) // hop_length
    frames = max(1, frames)
    window = _hann_window(win_length)
    strides = (samples.strides[0] * hop_length, samples.strides[0])
    framed = np.lib.stride_tricks.as_strided(
        samples, shape=(frames, win_length), strides=strides, writeable=False
    )
    spectrum = np.fft.rfft(framed * window, n=fft_size, axis=1)
    power = (np.abs(spectrum) ** 2).astype(np.float32)
    bank = _mel_filterbank(n_mels, fft_size, _TARGET_SAMPLE_RATE)
    mel = power @ bank.T
    return np.log(np.maximum(mel, 1e-10)).astype(np.float32)


def _token_ids_from_outputs(outputs: Sequence[Any], vocab_size: int) -> np.ndarray | None:
    """Find the token-id tensor in an export's output list.

    Exports return either integer ids directly or float logits to argmax over.
    """
    for output in outputs or ():
        arr = np.asarray(output)
        if arr.size == 0:
            continue
        if np.issubdtype(arr.dtype, np.integer):
            return arr.reshape(-1)
        if arr.ndim >= 2 and arr.shape[-1] >= 2:
            return np.argmax(arr, axis=-1).reshape(-1)
        if arr.ndim == 1 and vocab_size and arr.shape[0] == vocab_size:
            return np.argmax(arr).reshape(1)
        if np.issubdtype(arr.dtype, np.floating):
            return np.argmax(arr).reshape(1)
    return None


def _decode_ids(ids: np.ndarray, tokens: dict[int, str]) -> str:
    """Greedy decode: drop blanks and repeats, then map ids to pieces."""
    pieces: list[str] = []
    previous: int | None = None
    for raw_id in ids.tolist():
        token_id = int(raw_id)
        if token_id == previous:
            continue
        previous = token_id
        if token_id == 0 and 0 not in tokens:
            continue
        piece = tokens.get(token_id)
        if piece is None:
            continue
        piece = piece.replace("\u2581", " ").replace("\u0120", " ")
        if piece in ("<blk>", "<blank>", "<pad>", "<s>", "</s>", "<unk>"):
            continue
        pieces.append(piece)
    return "".join(pieces).strip()


def _load_nemo(model_path: str) -> ParakeetNemoEngine:
    try:
        nemo_asr = _import_module("nemo.collections.asr")
    except ImportError as exc:
        raise SttBackendUnavailableError(_NEMO_BACKEND, _NEMO_PACKAGE, exc) from exc

    asr_model = getattr(getattr(nemo_asr, "models", nemo_asr), "ASRModel", None)
    if asr_model is None or not hasattr(asr_model, "restore_from"):
        raise SttBackendUnavailableError(
            _NEMO_BACKEND,
            _NEMO_PACKAGE,
            AttributeError("nemo.collections.asr.models.ASRModel.restore_from is missing"),
        )
    try:
        model = asr_model.restore_from(model_path)
    except FileNotFoundError as exc:
        raise SttModelMissingError(model_path, _MODEL_HINT) from exc
    except Exception as exc:  # noqa: BLE001 - a broken checkpoint is not a missing file
        raise SttDecodeError(f"Could not restore NeMo Parakeet from {model_path!r}: {exc}") from exc

    return ParakeetNemoEngine(
        model_path,
        model,
        detail={"backend": _NEMO_BACKEND, "package": _NEMO_PACKAGE},
    )


def _load_onnx(model_path: str) -> ParakeetOnnxEngine:
    root = Path(model_path)
    if root.is_file():
        search_dir = root.parent
        onnx_files = [root]
    else:
        search_dir = root
        onnx_files = sorted(search_dir.glob("*.onnx"))
    if not onnx_files:
        raise SttModelMissingError(
            str(search_dir / "model.onnx"),
            "No .onnx export found in the Parakeet model directory. " + _MODEL_HINT,
        )

    tokens_path = _pick_token_file(search_dir)
    if tokens_path is None:
        raise SttModelMissingError(
            str(search_dir / _TOKEN_FILENAMES[0]),
            "The ONNX Parakeet export needs a tokens/vocab file "
            f"({' or '.join(_TOKEN_FILENAMES)}). " + _MODEL_HINT,
        )

    try:
        onnxruntime = _import_module("onnxruntime")
    except ImportError as exc:
        raise SttBackendUnavailableError(_ONNX_BACKEND, _ONNX_PACKAGE, exc) from exc

    sessions = []
    for onnx_file in onnx_files:
        try:
            sessions.append(onnxruntime.InferenceSession(str(onnx_file)))
        except Exception as exc:  # noqa: BLE001 - a corrupt graph is not a missing file
            raise SttDecodeError(f"Could not load ONNX graph {onnx_file}: {exc}") from exc
    token_ids = _load_tokens(tokens_path)

    return ParakeetOnnxEngine(
        model_path,
        sessions,
        token_ids,
        detail={
            "backend": _ONNX_BACKEND,
            "package": _ONNX_PACKAGE,
            "onnx_files": [p.name for p in onnx_files],
            "tokens": tokens_path.name,
            "vocab_size": len(token_ids),
        },
    )


def create_parakeet_engine(
    model_path: str,
    kind: str = "auto",
    device: str = "cpu",
) -> SttEngine:
    """Build the authoritative Parakeet engine for ``model_path``.

    ``kind`` is ``'auto'`` (infer from the path), ``'nemo'`` or ``'onnx'``.
    ``device`` is reported in :meth:`identity`; CPU execution is what this
    engine is configured for, and a non-CPU value is recorded honestly rather
    than silently ignored.
    """
    if not isinstance(model_path, str) or not model_path:
        raise SttModelMissingError(str(model_path), _MODEL_HINT)

    requested = (kind or "auto").strip().lower()
    if requested not in ("auto", "nemo", "onnx"):
        raise ValueError(f"unknown Parakeet model kind {kind!r}; expected 'auto', 'nemo' or 'onnx'")

    path = Path(model_path).expanduser()
    _require_exists(path)

    resolved = _infer_kind(str(path)) if requested == "auto" else requested
    if resolved == "nemo" and path.suffix.lower() != ".nemo":
        if not path.is_file():
            raise SttModelMissingError(
                str(path), f"Expected a .nemo checkpoint file. {_MODEL_HINT}"
            )

    engine = _load_nemo(str(path)) if resolved == "nemo" else _load_onnx(str(path))
    engine._detail["device"] = device
    engine._detail["kind_requested"] = requested
    return engine
