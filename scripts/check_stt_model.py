#!/usr/bin/env python3
"""Answer one question, in one command: which speech model is loaded here?

The authoritative recogniser is chosen by configuration alone
(``stt_engine`` / ``stt_model_path`` / ``stt_model_kind``), so after dropping in
a finetuned Parakeet the only way to be sure which model *actually* answered is
to build the engine and ask it for its :meth:`identity`.

Usage::

    python scripts/check_stt_model.py
    python scripts/check_stt_model.py --model-path ~/models/parakeet.nemo --kind nemo
    python scripts/check_stt_model.py --wav samples/turn.wav

Exit codes (never a bare traceback, never a silent empty success):

* ``0`` -- the engine was built and reported its identity; if ``--wav`` was
  given, a non-empty transcript was produced.
* ``2`` -- the model is not configured, or the named path does not exist.
* ``3`` -- the engine could not be built (missing backend, bad model file...).
* ``4`` -- the ``--wav`` file could not be read, or transcribed to nothing.

Importing this module has no side effects: nothing is loaded until ``main`` runs.
"""

from __future__ import annotations

import argparse
import sys
import wave
from dataclasses import is_dataclass, replace as dataclass_replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

__all__ = ["build_parser", "main", "read_wav_mono_float32"]

EXIT_OK = 0
EXIT_MODEL_MISSING = 2
EXIT_ENGINE_FAILED = 3
EXIT_AUDIO_FAILED = 4

KINDS = ("auto", "nemo", "onnx")

_HINT = (
    "Download the model and point stt_model_path at it, "
    "or pass --model-path explicitly."
)


def build_parser() -> argparse.ArgumentParser:
    """The command line, exposed so tests and callers can introspect it."""
    parser = argparse.ArgumentParser(
        prog="check_stt_model",
        description=(
            "Build the configured speech-to-text engine and print which model "
            "is actually loaded."
        ),
    )
    parser.add_argument(
        "--model-path",
        default=None,
        help=(
            "Path to the model file (*.nemo) or directory (ONNX export). "
            "Defaults to stt_model_path from claire.config.load_settings()."
        ),
    )
    parser.add_argument(
        "--kind",
        default=None,
        choices=KINDS,
        help=(
            "How to interpret --model-path. Defaults to stt_model_kind from "
            "settings, itself defaulting to 'auto'."
        ),
    )
    parser.add_argument(
        "--wav",
        default=None,
        help="Optional 16-bit PCM WAV file to transcribe and print.",
    )
    return parser


def _ensure_claire_importable() -> None:
    """Make ``src/`` importable when the checkout is not installed."""
    try:
        import claire  # noqa: F401
        return
    except Exception:
        pass
    src = Path(__file__).resolve().parents[1] / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _stt_package() -> Any:
    """The ``claire.listening.stt`` package (imported lazily, never at import)."""
    _ensure_claire_importable()
    from claire.listening import stt

    return stt


def _load_settings() -> Any:
    """Settings from claire.config, or a minimal stand-in when unavailable.

    The fallback keeps the smoke check honest: with no settings the model path
    is simply unset, which reports as a named missing-model error rather than
    crashing on import.
    """
    fallback = SimpleNamespace(
        stt_engine="parakeet",
        stt_model_path="",
        stt_model_kind="auto",
        stt_device="cpu",
    )
    try:
        _ensure_claire_importable()
        from claire.config import load_settings

        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - a smoke check must not traceback
        print(
            f"WARNING: could not read claire settings ({type(exc).__name__}: {exc}); "
            "using built-in defaults",
            file=sys.stderr,
        )
        return fallback

    for name, value in vars(fallback).items():
        if getattr(settings, name, None) is None:
            try:
                setattr(settings, name, value)
            except Exception:  # frozen/picky object, fall back wholesale
                return fallback
    return settings


def _config_for(settings: Any, model_path: str, kind: str) -> Any:
    """A settings-like object whose stt_* fields describe this invocation."""
    if is_dataclass(settings) and not isinstance(settings, type):
        try:
            return dataclass_replace(
                settings, stt_model_path=model_path, stt_model_kind=kind
            )
        except Exception:
            pass
    cfg = SimpleNamespace(
        stt_engine=getattr(settings, "stt_engine", "parakeet") or "parakeet",
        stt_model_path=model_path,
        stt_model_kind=kind,
        stt_device=getattr(settings, "stt_device", "cpu") or "cpu",
    )
    return cfg


def read_wav_mono_float32(path: str | Path) -> tuple[Any, int]:
    """Read a 16-bit PCM WAV as mono float32 in [-1, 1], plus its sample rate.

    Stereo (or N-channel) audio is downmixed by averaging the channels; this is
    the shape :meth:`SttEngine.transcribe` expects. Only 16-bit PCM is accepted,
    because guessing at a compressed or float format is how a smoke check starts
    lying about what it verified.
    """
    import numpy as np

    wav_path = Path(path)
    try:
        with wave.open(str(wav_path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Audio file not found at {str(wav_path)!r}. Pass an existing 16-bit PCM WAV."
        ) from exc
    except wave.Error as exc:
        raise ValueError(
            f"Could not read WAV file {str(wav_path)!r}: {exc}. "
            "Only uncompressed 16-bit PCM WAV is supported."
        ) from exc

    if sample_width != 2:
        raise ValueError(
            f"WAV file {str(wav_path)!r} has {sample_width * 8}-bit samples; "
            "only 16-bit PCM is supported."
        )
    if channels < 1:
        raise ValueError(f"WAV file {str(wav_path)!r} reports {channels} channels.")

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32), int(sample_rate)


def _print_identity(identity: Any) -> None:
    engine = getattr(identity, "engine", "") or ""
    model_path = getattr(identity, "model_path", "") or ""
    model_kind = getattr(identity, "model_kind", "") or ""
    detail = getattr(identity, "detail", {}) or {}
    print(f"engine:   {engine}")
    print(f"kind:     {model_kind}")
    print(f"path:     {model_path}")
    print(f"detail:   {', '.join(f'{k}={v}' for k, v in sorted(detail.items()))}")
    describe = getattr(identity, "describe", None)
    if callable(describe):
        print(f"identity: {describe()}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the smoke check; returns the process exit code."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    stt = _stt_package()
    settings = _load_settings()

    raw_path = args.model_path
    if raw_path is None:
        raw_path = getattr(settings, "stt_model_path", "") or ""
    model_path = str(raw_path).strip()
    kind = str(
        args.kind or getattr(settings, "stt_model_kind", "auto") or "auto"
    ).strip().lower()
    if kind not in KINDS:
        kind = "auto"

    if not model_path:
        print(
            "ERROR: no speech model configured: stt_model_path is unset "
            f"(looked for: {'<unset>'!r}). {_HINT}",
            file=sys.stderr,
        )
        return EXIT_MODEL_MISSING

    print(f"checking speech model: kind={kind} path={model_path}")

    try:
        resolved = stt.require_existing_path(model_path, _HINT)
    except stt.SttError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_MODEL_MISSING
    except Exception as exc:  # noqa: BLE001 - named failure, never a traceback
        print(
            f"ERROR: could not check speech model at path {model_path!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_MODEL_MISSING

    cfg = _config_for(settings, str(resolved), kind)
    try:
        engine = stt.create_stt_engine(cfg)
        identity = engine.identity()
    except Exception as exc:  # noqa: BLE001 - named failure, never a traceback
        print(
            f"ERROR: could not load the speech engine for path {str(resolved)!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_ENGINE_FAILED

    _print_identity(identity)

    if args.wav is None:
        print("OK: speech engine loaded.")
        return EXIT_OK

    wav_path = str(args.wav)
    try:
        audio, sample_rate = read_wav_mono_float32(wav_path)
    except Exception as exc:  # noqa: BLE001 - named failure, never a traceback
        print(
            f"ERROR: could not read audio file {wav_path!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_AUDIO_FAILED

    print(
        f"audio:    {wav_path} ({audio.shape[0]} samples @ {sample_rate} Hz, mono float32)"
    )
    try:
        result = engine.transcribe(audio, sample_rate)
    except Exception as exc:  # noqa: BLE001 - named failure, never a traceback
        print(
            f"ERROR: transcription failed for {wav_path!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_AUDIO_FAILED

    text = getattr(result, "text", "")
    print(f"transcript: {text!r}")
    if not isinstance(text, str) or not text.strip():
        print(
            f"ERROR: engine {getattr(result, 'engine', '')!r} returned an empty "
            f"transcript for {wav_path!r}; that is a failure, not a silent success.",
            file=sys.stderr,
        )
        return EXIT_AUDIO_FAILED

    print("OK: transcript produced.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
