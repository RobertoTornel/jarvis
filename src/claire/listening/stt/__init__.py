"""Speech-to-text engines for Claire.

Importing this package must never fail because an optional backend is missing;
the heavy imports live inside the engines and inside :func:`create_stt_engine`.
"""

from __future__ import annotations

from typing import Any

from .base import (
    SttBackendUnavailableError,
    SttDecodeError,
    SttEngine,
    SttError,
    SttIdentity,
    SttModelMissingError,
    SttResult,
    StreamingSttEngine,
    require_existing_path,
)

__all__ = [
    "PARAKEET_ENGINE_NAMES",
    "SttBackendUnavailableError",
    "SttDecodeError",
    "SttEngine",
    "SttError",
    "SttIdentity",
    "SttModelMissingError",
    "SttResult",
    "StreamingSttEngine",
    "WHISPER_ENGINE_NAMES",
    "create_stt_engine",
    "create_streaming_stt_engine",
    "require_existing_path",
]


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    return getattr(cfg, key, default) if cfg is not None else default


#: ``stt_engine`` tokens selecting each recogniser family. Parakeet is the one
#: authoritative recogniser (decision D4); Whisper is opt-in only.
PARAKEET_ENGINE_NAMES = ("parakeet", "parakeet_onnx", "nemo")
WHISPER_ENGINE_NAMES = ("faster_whisper", "faster-whisper", "whisper")


def create_stt_engine(cfg: Any) -> SttEngine:
    """Build the authoritative recogniser named by ``cfg``.

    The engine is chosen by configuration alone, which is what lets a
    finetuned Parakeet drop in as a path change:

    * ``stt_engine``: ``"parakeet"`` (default) or ``"faster_whisper"``.
    * ``stt_model_path``: a ``*.nemo`` file, or a directory holding an ONNX
      export.
    * ``stt_model_kind``: ``"auto"`` (default), ``"nemo"`` or ``"onnx"``.

    Whisper is **opt-in only**: it is built when — and only when —
    ``stt_engine`` names it explicitly. Any other token raises
    :class:`SttError` rather than quietly selecting a fallback recogniser, so a
    mistyped setting can never masquerade as a working one.
    """
    engine = str(_cfg_get(cfg, "stt_engine", "parakeet") or "parakeet").strip().lower()
    model_path = str(_cfg_get(cfg, "stt_model_path", "") or "").strip()
    kind = str(_cfg_get(cfg, "stt_model_kind", "auto") or "auto").strip().lower()
    device = str(_cfg_get(cfg, "stt_device", "cpu") or "cpu").strip().lower()

    if engine in PARAKEET_ENGINE_NAMES:
        from .parakeet import create_parakeet_engine

        return create_parakeet_engine(model_path=model_path, kind=kind, device=device)

    if engine in WHISPER_ENGINE_NAMES:
        from .whisper_engine import create_whisper_engine

        return create_whisper_engine(cfg)

    raise SttError(
        f"unknown stt_engine {engine!r}; supported: 'parakeet' or "
        "'faster_whisper' (Parakeet is the default and the documented "
        "recogniser; Whisper is opt-in only and never a fallback)"
    )


def create_streaming_stt_engine(cfg: Any) -> StreamingSttEngine | None:
    """Build the always-on streaming recogniser, or ``None`` when disabled.

    Returns ``None`` rather than a stub when sherpa-onnx is not configured:
    wake detection then runs on whatever the authoritative path produces, and
    the caller is expected to say so rather than pretend it is streaming.
    """
    engine = str(_cfg_get(cfg, "streaming_stt_engine", "sherpa") or "").strip().lower()
    if not engine:
        return None
    if engine != "sherpa":
        raise SttError(f"unknown streaming_stt_engine {engine!r}; supported: 'sherpa' or ''")

    from .sherpa_streaming import SherpaStreamingEngine

    return SherpaStreamingEngine(
        model_dir=str(_cfg_get(cfg, "streaming_stt_model_dir", "") or ""),
        sample_rate=int(_cfg_get(cfg, "sample_rate", 16000) or 16000),
        num_threads=int(_cfg_get(cfg, "streaming_stt_num_threads", 2) or 2),
    )
