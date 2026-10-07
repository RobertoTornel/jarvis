"""Always-on streaming recogniser (sherpa-onnx zipformer transducer).

Decision D3/D4 gives Claire two recognisers with different jobs. This module
is the **streaming** one: it runs continuously on microphone audio and its
output is **provisional by definition** — used for wake-word matching and
pre-wake context only. It must never become the text a turn acts on; the
authoritative transcript comes from the Parakeet engine and is carried by
:class:`claire.listening.transcript.TurnTranscript`.

Failure policy (contract in :mod:`.base`): a missing model directory, a
missing component file, or an unimportable ``sherpa_onnx`` package raises a
*named* error (:class:`SttModelMissingError` /
:class:`SttBackendUnavailableError`) that names the exact path and the fix.
Those paths never degrade into an empty transcript — an empty string from
:meth:`finalize` means "this segment contained no speech", never "the model
was missing".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .base import (
    SttBackendUnavailableError,
    SttDecodeError,
    SttIdentity,
    SttModelMissingError,
)

#: Where the streaming zipformer model comes from. Kept as a hint string so
#: the missing-model error is actionable without reading this file.
_MODEL_HINT = (
    "Download a streaming zipformer bundle (encoder/decoder/joiner .onnx "
    "+ tokens.txt) and point streaming_stt_model_dir at it."
)

#: Endpoint rules: an utterance is declared finished after this much trailing
#: silence, so the wake loop can finalize the segment on pauses.
_RULE1_MIN_TRAILING_SILENCE = 1.2
_RULE2_MIN_TRAILING_SILENCE = 0.6
_RULE3_MIN_UTTERANCE_LENGTH = 2.0
#: Silence appended by finalize() so a segment ending on speech still reaches an
#: endpoint and its final words are decoded.
_TAIL_PADDING_SEC = 0.5


def _pick_variant(model_dir: Path, prefix: str) -> Path:
    """Return the ``<prefix>*.onnx`` file to load.

    Prefers the quantized ``.int8.onnx`` variant (faster on CPU) and falls
    back to the plain ``.onnx``. Raises :class:`SttModelMissingError` naming
    what was looked for when neither exists.
    """
    int8 = sorted(model_dir.glob(f"{prefix}*.int8.onnx"))
    if int8:
        return int8[0]
    plain = sorted(
        p for p in model_dir.glob(f"{prefix}*.onnx") if not p.name.endswith(".int8.onnx")
    )
    if plain:
        return plain[0]
    raise SttModelMissingError(str(model_dir / f"{prefix}*.onnx"), _MODEL_HINT)


class SherpaStreamingEngine:
    """Always-on streaming transducer behind the :class:`StreamingSttEngine` protocol.

    Usage from the wake loop::

        engine = SherpaStreamingEngine(model_dir)
        engine.accept_waveform(frame, 16000)
        if "hey claire" in engine.partial_text().lower():
            ...
        text = engine.finalize()  # flush endpoint, then reset for next segment
    """

    name = "sherpa_streaming"

    def __init__(self, model_dir: str, sample_rate: int = 16000, num_threads: int = 2) -> None:
        cleaned = (model_dir or "").strip()
        if not cleaned:
            raise SttModelMissingError(
                cleaned or "<empty streaming_stt_model_dir>",
                "Set streaming_stt_model_dir to a directory holding "
                "encoder/decoder/joiner .onnx files and tokens.txt. " + _MODEL_HINT,
            )
        resolved = Path(cleaned).expanduser()
        if not resolved.is_dir():
            raise SttModelMissingError(str(resolved), _MODEL_HINT)

        tokens = resolved / "tokens.txt"
        if not tokens.is_file():
            raise SttModelMissingError(str(tokens), _MODEL_HINT)
        encoder = _pick_variant(resolved, "encoder")
        decoder = _pick_variant(resolved, "decoder")
        joiner = _pick_variant(resolved, "joiner")

        try:
            import sherpa_onnx
        except ImportError as exc:
            raise SttBackendUnavailableError("sherpa-onnx", "sherpa-onnx", exc) from exc

        self._sample_rate = int(sample_rate)
        self._num_threads = int(num_threads)
        try:
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(tokens),
                encoder=str(encoder),
                decoder=str(decoder),
                joiner=str(joiner),
                num_threads=self._num_threads,
                sample_rate=self._sample_rate,  # int: the binding rejects float here
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=_RULE1_MIN_TRAILING_SILENCE,
                rule2_min_trailing_silence=_RULE2_MIN_TRAILING_SILENCE,
                rule3_min_utterance_length=_RULE3_MIN_UTTERANCE_LENGTH,
            )
        except SttModelMissingError:
            raise
        except Exception as exc:
            raise SttDecodeError(f"sherpa streaming recognizer failed to load: {exc}") from exc

        self._model_dir = str(resolved)
        self._files: dict[str, str] = {
            "tokens": str(tokens),
            "encoder": str(encoder),
            "decoder": str(decoder),
            "joiner": str(joiner),
        }
        self._stream = self._recognizer.create_stream()
        # Frames fed into the current segment. sherpa-onnx aborts the process
        # (LOG(FATAL) in features.cc) when decode_stream/get_result runs on a
        # stream that never received a frame, so partial_text()/finalize()
        # must short-circuit while this is zero.
        self._fed_frames = 0

    # -- StreamingSttEngine interface -------------------------------------

    def accept_waveform(self, samples: np.ndarray, sample_rate: int) -> None:
        """Feed one frame of mono float32 audio in ``[-1, 1]``."""
        frame = np.asarray(samples, dtype=np.float32)
        if frame.ndim != 1:
            raise SttDecodeError(
                f"sherpa streaming engine needs mono (1-D) audio, got shape {frame.shape}"
            )
        if frame.size == 0:
            return
        if int(sample_rate) != self._sample_rate:
            frame = self._resample(frame, int(sample_rate), self._sample_rate)
            if frame.size == 0:
                return
        self._stream.accept_waveform(self._sample_rate, frame)
        self._fed_frames += 1
        self._drain()

    def partial_text(self) -> str:
        """Best transcript so far — for wake matching only, never for action."""
        if self._fed_frames == 0:
            return ""
        self._drain()
        return self._recognizer.get_result(self._stream)

    def finalize(self) -> str:
        """Flush the endpoint, return the segment's provisional text, reset."""
        if self._fed_frames == 0:
            self.reset()
            return ""
        # Feed tail padding so a segment that ended on speech still reaches an
        # endpoint and its last words are decoded rather than dropped.
        self._stream.accept_waveform(
            self._sample_rate,
            np.zeros(int(self._sample_rate * _TAIL_PADDING_SEC), dtype=np.float32),
        )
        self._fed_frames += 1
        self._drain()
        text = self._recognizer.get_result(self._stream)
        self.reset()
        return text

    def reset(self) -> None:
        """Start a new segment: drop recognizer stream state."""
        self._stream = self._recognizer.create_stream()
        self._fed_frames = 0

    def _drain(self) -> None:
        """Decode only while sherpa-onnx says the stream is ready.

        This guard is not an optimisation. ``decode_stream``/``get_result`` on a
        stream holding fewer than the feature window's worth of frames makes
        sherpa-onnx call ``LOG(FATAL)`` in ``features.cc`` ("0 + 39 > n"), which
        aborts the *entire process* — there is no exception to catch. Measured:
        the stream is not ready until ~39 frames (~780 ms at 20 ms/frame), so an
        always-on listener that decoded every 20 ms frame on arrival would die
        within the first second of the first utterance.
        """
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)

    def identity(self) -> SttIdentity:
        """Report the model actually loaded, so a swap is verifiable."""
        detail: dict[str, Any] = {
            **self._files,
            "sample_rate": self._sample_rate,
            "num_threads": self._num_threads,
            "endpoint_rule1_s": _RULE1_MIN_TRAILING_SILENCE,
            "endpoint_rule2_s": _RULE2_MIN_TRAILING_SILENCE,
        }
        return SttIdentity(
            engine=self.name,
            model_path=self._model_dir,
            model_kind="transducer",
            detail=detail,
        )

    # -- internals --------------------------------------------------------

    @staticmethod
    def _resample(frame: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        """Linear-interpolation resample for off-rate input (numpy only)."""
        if src_rate <= 0:
            raise SttDecodeError(f"invalid input sample_rate {src_rate!r}")
        if frame.size == 1 or src_rate == dst_rate:
            return frame
        duration = frame.size / float(src_rate)
        out_len = max(1, int(round(duration * dst_rate)))
        src_pos = np.linspace(0.0, float(frame.size - 1), num=out_len)
        return np.interp(src_pos, np.arange(frame.size), frame).astype(np.float32)
