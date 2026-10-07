"""The opt-in faster-whisper path, exposed behind :class:`SttEngine`.

Claire's authoritative recogniser is Parakeet (decision D4); Whisper is
**opt-in only** and is never a fallback for it. This engine is built when — and
only when — ``stt_engine`` names Whisper explicitly
(:func:`create_stt_engine` raises for any other token). A Parakeet-configured
process never imports this module's backend library, never downloads a Whisper
model and never holds one in memory.

This module deliberately contains almost no speech code. It is a thin adapter
over what the project already has:

* :func:`claire.listening.listener._load_faster_whisper_model` performs the
  actual model preparation/loading (which itself calls
  :func:`claire.listening.model_download.prepare_faster_whisper_model`, so a
  retried download and the ``local_files_only=True`` load are unchanged).
* ``VoiceListener._decode_faster_whisper`` is the decode call used in production:
  it takes the shared transcription lock, keeps faster-whisper's lazy segment
  generator inside that lock, and recovers once from a CUDA runtime failure by
  reloading on CPU.
* ``VoiceListener._filter_noisy_segments`` is the one and only place the
  low-confidence / no-speech policy lives, so the events this engine reports are
  the same events the listener reports — not a second, drifting copy.

Why the listener methods are called on a bare instance rather than through
``_transcribe_audio``: ``_transcribe_audio`` ends with

    except Exception as e:
        return "", None, ()

which turns *any* decode failure into an empty-but-successful transcript — the
exact silent-empty-success defect this package exists to prevent. This engine
calls the two methods that ``_transcribe_audio``'s faster-whisper branch calls
(``_decode_faster_whisper`` then ``_filter_noisy_segments``), keeps that
exception propagating, and converts it into a named :class:`SttDecodeError`
instead of an empty string.

Nothing heavy is imported at module import time: ``claire.listening.listener``
(which pulls in the audio stack) is imported on first use, inside
:meth:`WhisperSttEngine._ensure_model`.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import numpy as np

from .base import (
    SttBackendUnavailableError,
    SttDecodeError,
    SttEngine,
    SttError,
    SttIdentity,
    SttModelMissingError,
    SttResult,
)

__all__ = ["WhisperSttEngine", "create_whisper_engine", "ENGINE_NAME", "BACKEND_LABEL"]

#: Value reported in ``SttResult.engine`` / ``SttIdentity.engine``. Matches the
#: ``stt_engine`` configuration token used by the ``stt`` package factory.
ENGINE_NAME = "faster_whisper"

#: The human-facing backend name, matching ``whisper_backend``'s spelling.
BACKEND_LABEL = "faster-whisper"

_BACKEND_PACKAGE = "faster-whisper"
_ACCEPTED_BACKENDS = frozenset({"", "auto", "faster-whisper", "faster_whisper", "mlx"})


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    """Read one configuration value, tolerating plain namespaces in tests."""
    return getattr(cfg, key, default) if cfg is not None else default


def _cfg_float(cfg: Any, key: str, default: float) -> float:
    """Read a float setting, falling back for ``None``/blank values."""
    value = _cfg_get(cfg, key, default)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _noop_low_confidence(_event: Any) -> None:
    """Sink for the listener's per-segment rejection callback.

    ``_filter_noisy_segments`` only *collects* low-confidence events when
    ``on_low_confidence`` is registered; it never calls the callback itself.
    Registering this sink therefore makes the listener record the events while
    this engine returns them on :class:`SttResult` instead of printing them.
    """


class _VadOverridingModel:
    """Delegate to a faster-whisper model, forcing ``vad_filter``.

    ``_decode_faster_whisper_locked`` hardcodes ``vad_filter=False``. Rather
    than copy that call (and its ``TypeError`` fallback) into this module, the
    engine keeps the production call shape and only overrides the single knob
    ``whisper_vad`` is meant to control. Used only when the configured value is
    true, so the default path is byte-for-byte the existing behaviour.
    """

    def __init__(self, model: Any, vad_filter: bool) -> None:
        self._claire_model = model
        self._claire_vad_filter = bool(vad_filter)

    def transcribe(self, audio: Any, **kwargs: Any) -> Any:
        kwargs["vad_filter"] = self._claire_vad_filter
        return self._claire_model.transcribe(audio, **kwargs)

    def __getattr__(self, item: str) -> Any:
        # Keeps ``getattr(model, "model")`` (device reporting, CUDA recovery)
        # working on the real object.
        return getattr(self._claire_model, item)


class WhisperSttEngine:
    """``SttEngine`` implementation backed by the project's faster-whisper path.

    Construction validates configuration only; the model is loaded on the first
    :meth:`transcribe` call, so building an engine is cheap and never touches
    the network or the disk.
    """

    name = ENGINE_NAME

    def __init__(self, cfg: Any = None) -> None:
        self._cfg = cfg
        self._model_name = str(_cfg_get(cfg, "whisper_model", "small") or "small")
        self._backend_pref = str(_cfg_get(cfg, "whisper_backend", "auto") or "auto")
        configured_backend = self._backend_pref.strip().lower()
        if configured_backend not in _ACCEPTED_BACKENDS:
            raise SttError(
                f"unknown whisper_backend {self._backend_pref!r}; "
                "supported values are 'auto', 'faster-whisper', 'mlx' or ''"
            )
        # This engine only ever speaks faster-whisper. 'mlx' is accepted because
        # the shared configuration offers it, but it cannot be served here; the
        # mismatch is reported through identity() rather than hidden.
        self._configured_backend = configured_backend or "auto"

        self._device_pref = str(_cfg_get(cfg, "whisper_device", "auto") or "auto")
        self._compute_pref = str(_cfg_get(cfg, "whisper_compute_type", "int8") or "int8")
        self._vad = bool(_cfg_get(cfg, "whisper_vad", False))
        self._sample_rate = int(_cfg_get(cfg, "sample_rate", 16000) or 16000)
        self._min_audio_duration = _cfg_float(cfg, "whisper_min_audio_duration", 0.3)
        self._min_confidence = _cfg_float(cfg, "whisper_min_confidence", 0.3)
        self._no_speech_threshold = _cfg_float(cfg, "whisper_no_speech_threshold", 0.5)

        self._lock = threading.Lock()
        #: One lock per engine, not per call: the listener serialises every
        #: faster-whisper decode through this lock because a single WhisperModel
        #: must not be driven concurrently. Sharing it across calls is what makes
        #: that guarantee hold for this engine too.
        self._transcribe_lock = threading.Lock()
        self._model: Any = None
        self._listener: Any = None
        self._resolved_device = ""
        self._resolved_compute = ""
        self._cpu_recovery_attempted = False

    # ------------------------------------------------------------------ load

    def _listener_module(self) -> Any:
        """Import (and cache) ``claire.listening.listener`` on first use.

        The import is deliberately *not* at module level: it drags in the audio
        stack and the LLM wiring, and importing the stt package must stay cheap.
        """
        if self._listener is None:
            from claire.listening import listener as _listener_module

            self._listener = _listener_module
        return self._listener

    def _load_attempts(self, device_pref: str) -> list[tuple[str, str]]:
        """The (device, compute_type) ladder, mirroring the listener's.

        faster-whisper/CTranslate2 rejects unsupported compute types and, on a
        machine with no usable CUDA runtime, rejects the GPU device at load
        time; the production listener walks the same ladder. Kept small on
        purpose — the loading itself is still the shared loader.
        """
        compute_types = [self._compute_pref]
        if self._compute_pref == "int8":
            compute_types.extend(["float16", "float32"])
        elif self._compute_pref == "float16":
            compute_types.append("float32")

        attempts: list[tuple[str, str]] = [(device_pref, ct) for ct in compute_types]
        if device_pref in ("auto", "cuda"):
            attempts.extend(("cpu", ct) for ct in compute_types)

        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str]] = []
        for attempt in attempts:
            if attempt not in seen:
                seen.add(attempt)
                unique.append(attempt)
        return unique

    def _named_load_error(self, error: BaseException | None) -> SttError:
        """Turn a failed load into a named error that names the piece and the fix."""
        from claire.listening.model_download import ModelDownloadError

        hint = (
            f"Fetch the model with: python -c \"from claire.listening.model_download "
            f"import prepare_faster_whisper_model as p; p({self._model_name!r})\", "
            f"or point whisper_model at a directory that already holds model.bin, "
            f"config.json, tokenizer.json and vocabulary.* . "
            f"If the backend library is missing, install it with: "
            f"pip install {_BACKEND_PACKAGE}"
        )
        if error is None:
            return SttModelMissingError(self._model_name, hint)
        if isinstance(error, SttError):
            return error

        detail = str(error).lower()
        missing_model = (
            isinstance(error, (ModelDownloadError, FileNotFoundError))
            or "no such file" in detail
            or "unable to open file" in detail
            or "not found" in detail
        )
        if missing_model:
            return SttModelMissingError(self._model_name, hint)
        return SttError(
            f"whisper backend {BACKEND_LABEL!r} could not load model "
            f"{self._model_name!r} (device={self._device_pref!r}, "
            f"compute_type={self._compute_pref!r}): {error}. {hint}"
        )

    def _ensure_model(self) -> Any:
        """Load the faster-whisper model once, lazily and thread-safely."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model

            listener = self._listener_module()
            # The listener imports faster-whisper lazily (Parakeet is the only
            # eager recogniser), so ask it to import on this, the opt-in path,
            # before concluding that the backend is missing.
            ensure = getattr(listener, "_ensure_faster_whisper_imported", None)
            if callable(ensure):
                ensure()
            model_cls = getattr(listener, "WhisperModel", None)
            available = getattr(listener, "FASTER_WHISPER_AVAILABLE", False)
            if model_cls is None or not available:
                cause: BaseException | None
                try:
                    import faster_whisper  # noqa: F401

                    # The library imports, so the failure is inside the
                    # listener's own import guard (it swallows the reason).
                    cause = RuntimeError(
                        "faster-whisper imported, but claire.listening.listener did "
                        "not expose WhisperModel/FASTER_WHISPER_AVAILABLE; "
                        "reinstall the backend and re-run"
                    )
                except Exception as import_error:  # pragma: no cover - env dependent
                    cause = import_error
                raise SttBackendUnavailableError(
                    BACKEND_LABEL, _BACKEND_PACKAGE, cause
                ) from cause

            device_pref = self._device_pref
            probe = getattr(listener, "_probe_windows_cuda_libraries", None)
            if probe is not None:
                device_pref, _missing = probe(device_pref)

            last_error: BaseException | None = None
            for try_device, try_compute in self._load_attempts(device_pref):
                cpu_threads = (os.cpu_count() or 4) if try_device in ("cpu", "auto") else 0
                try:
                    model = listener._load_faster_whisper_model(
                        self._model_name,
                        device=try_device,
                        compute_type=try_compute,
                        cpu_threads=cpu_threads,
                    )
                except Exception as error:  # noqa: BLE001 - reported below, named
                    last_error = error
                    continue

                resolved = str(
                    getattr(getattr(model, "model", None), "device", try_device)
                ).lower()
                self._resolved_device = resolved
                self._resolved_compute = try_compute
                self._model = self._wrap(model)
                return self._model

            raise self._named_load_error(last_error)

    def _wrap(self, model: Any) -> Any:
        """Apply the ``whisper_vad`` override without duplicating the decode call."""
        if isinstance(model, _VadOverridingModel):
            return model
        if self._vad:
            return _VadOverridingModel(model, True)
        return model

    # ------------------------------------------------------------- transcribe

    def _prepare_audio(self, audio: Any, sample_rate: int) -> np.ndarray:
        """Validate/convert one utterance, or raise a named decode error.

        Mirrors the listener's audio handling (same resampler, same short-audio
        gate) except that a rejected buffer raises instead of silently
        vanishing: a caller of this interface cannot tell "" from "the user
        said nothing", so "" is never returned.
        """
        if audio is None:
            raise SttDecodeError(
                f"no audio was supplied to the {BACKEND_LABEL} engine "
                f"(model={self._model_name!r})"
            )
        try:
            samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError) as error:
            raise SttDecodeError(
                f"audio could not be read as mono float32 samples "
                f"(model={self._model_name!r}): {error}"
            ) from error
        if samples.size == 0:
            raise SttDecodeError(
                f"empty audio buffer supplied to the {BACKEND_LABEL} engine "
                f"(model={self._model_name!r}); refusing to report it as a "
                "successful transcription"
            )
        if not np.all(np.isfinite(samples)):
            raise SttDecodeError(
                f"audio contains non-finite samples (NaN or inf) "
                f"(model={self._model_name!r})"
            )

        try:
            rate = int(sample_rate)
        except (TypeError, ValueError):
            rate = 0
        if rate <= 0:
            raise SttDecodeError(
                f"invalid sample_rate {sample_rate!r} for the {BACKEND_LABEL} engine"
            )
        if rate != self._sample_rate:
            samples = self._listener_module()._resample(samples, rate, self._sample_rate)

        duration = samples.size / self._sample_rate
        if duration < self._min_audio_duration:
            raise SttDecodeError(
                f"audio is too short to transcribe: {duration:.2f}s < "
                f"whisper_min_audio_duration={self._min_audio_duration:.2f}s "
                f"(model={self._model_name!r})"
            )
        return samples

    def _adapter(self, listener: Any, model: Any) -> Any:
        """A ``VoiceListener`` that owns nothing and decodes with *model*.

        ``__new__`` skips ``VoiceListener.__init__`` on purpose: starting the
        real listener would open audio devices. Only the attributes the two
        reused methods read are populated.
        """
        adapter = listener.VoiceListener.__new__(listener.VoiceListener)
        adapter.cfg = self._cfg
        adapter.model = model
        adapter.transcribe_lock = self._transcribe_lock
        adapter._whisper_device = self._resolved_device
        adapter._whisper_model_name = self._model_name
        adapter._whisper_cpu_recovery_attempted = self._cpu_recovery_attempted
        adapter.on_low_confidence = _noop_low_confidence
        return adapter

    def transcribe(self, audio: Any, sample_rate: int) -> SttResult:
        """Transcribe one complete utterance of mono float32 audio.

        The buffer is validated *before* the model is loaded, so a rejected
        buffer never triggers a download or a load. Raises a named
        :class:`SttError` for an unavailable backend, a missing model, a decode
        failure or a rejected buffer. Never returns an empty transcript.
        """
        started = time.monotonic()
        samples = self._prepare_audio(audio, sample_rate)
        model = self._ensure_model()
        listener = self._listener_module()
        adapter = self._adapter(listener, model)

        try:
            segments, info = listener.VoiceListener._decode_faster_whisper(adapter, samples)
            filtered, events = listener.VoiceListener._filter_noisy_segments(adapter, segments)
        except SttError:
            raise
        except Exception as error:  # noqa: BLE001 - converted to a named error
            raise SttDecodeError(
                f"{BACKEND_LABEL} could not transcribe {samples.size} samples with "
                f"model {self._model_name!r} (device={self._resolved_device!r}, "
                f"compute_type={self._resolved_compute!r}): {error}"
            ) from error

        # The CUDA recovery inside _decode_faster_whisper replaces the model on
        # the adapter; adopt it so the next call and identity() stay truthful.
        if adapter.model is not model:
            self._model = self._wrap(adapter.model)
            self._resolved_device = str(
                getattr(adapter, "_whisper_device", self._resolved_device) or ""
            )
        self._cpu_recovery_attempted = bool(
            getattr(adapter, "_whisper_cpu_recovery_attempted", False)
        )

        text = " ".join(seg.text for seg in filtered).strip()
        language = getattr(info, "language", None)
        if not isinstance(language, str) or not language.strip():
            language = None

        if not text:
            duration = samples.size / self._sample_rate
            raise SttDecodeError(
                f"{BACKEND_LABEL} produced no transcript for {duration:.2f}s of audio "
                f"(model={self._model_name!r}, device={self._resolved_device!r}, "
                f"low_confidence_events={len(events)}); refusing to report an empty "
                "transcript as a successful transcription"
            )

        return SttResult(
            text=text,
            language=language,
            low_confidence_events=tuple(events),
            engine=ENGINE_NAME,
            elapsed_sec=time.monotonic() - started,
        )

    # --------------------------------------------------------------- identity

    def identity(self) -> SttIdentity:
        """Report the model and backend, without loading or transcribing."""
        loaded = self._model is not None
        detail: dict[str, Any] = {
            "backend": BACKEND_LABEL,
            "configured_backend": self._configured_backend,
            "model": self._model_name,
            "device": self._resolved_device or self._device_pref,
            "compute_type": self._resolved_compute or self._compute_pref,
            "vad_filter": bool(self._vad),
            "min_confidence": self._min_confidence,
            "no_speech_threshold": self._no_speech_threshold,
            "min_audio_duration": self._min_audio_duration,
            "sample_rate": self._sample_rate,
            "low_confidence_events": "exposed_on_SttResult",
            "loaded": loaded,
        }
        return SttIdentity(
            engine=ENGINE_NAME,
            model_path=self._model_name,
            model_kind=BACKEND_LABEL,
            detail=detail,
        )


def create_whisper_engine(cfg: Any) -> SttEngine:
    """Build the faster-whisper :class:`SttEngine` described by ``cfg``.

    The engine is returned unloaded; the first :meth:`WhisperSttEngine.transcribe`
    performs the model preparation and load.
    """
    return WhisperSttEngine(cfg)
