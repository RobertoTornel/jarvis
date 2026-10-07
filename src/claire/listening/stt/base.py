"""Speech-to-text engines behind one swappable interface.

Claire has two recognisers with different jobs (decision D3/D4):

* the **streaming** engine (sherpa-onnx zipformer) is the always-on listener.
  Its output is *provisional*: it is used for wake detection and pre-wake
  context, and must never become the text a turn acts on.
* the **authoritative** engine (Parakeet v2) transcribes the turn itself. The
  text it produces is the one used for intent, tool calls, the reply and memory.

Both are reached through :class:`SttEngine`, so swapping the model — including
dropping in a finetuned Parakeet — is a configuration change, never a code
change.

Every engine failure is a *named* error. A missing model file never degrades
into an empty transcript: an empty transcript would look like "the user said
nothing", which is the same silent-empty-success failure that upstream's
screenshot tool has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np


class SttError(RuntimeError):
    """Base class for every speech-to-text failure."""


class SttModelMissingError(SttError):
    """The configured model file or directory does not exist.

    The message names the exact path that was looked for and the command that
    would fetch it, so the failure is actionable without reading the code.
    """

    def __init__(self, path: str, hint: str = "") -> None:
        self.path = path
        self.hint = hint
        detail = f"Speech model not found at {path!r}."
        if hint:
            detail += f" {hint}"
        super().__init__(detail)


class SttBackendUnavailableError(SttError):
    """The backend library for this engine is not importable.

    Names the import that failed and the package that provides it.
    """

    def __init__(self, backend: str, package: str, cause: BaseException | None = None) -> None:
        self.backend = backend
        self.package = package
        super().__init__(
            f"Speech backend {backend!r} is unavailable. "
            f"Install it with: pip install {package}"
            + (f" (import error: {cause})" if cause else "")
        )


class SttDecodeError(SttError):
    """The model was present but could not produce a transcript."""


@dataclass(frozen=True)
class SttResult:
    """What an engine returns for one utterance."""

    text: str
    language: str | None = None
    low_confidence_events: tuple[Any, ...] = ()
    engine: str = ""
    # Wall-clock seconds spent inside the engine for this call.
    elapsed_sec: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise SttError(f"engine {self.engine!r} returned a non-string transcript")


@dataclass(frozen=True)
class SttIdentity:
    """What model actually answered, so a swap is verifiable in one command."""

    engine: str
    model_path: str
    model_kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        extra = ", ".join(f"{k}={v}" for k, v in sorted(self.detail.items()))
        base = f"{self.engine} | kind={self.model_kind} | path={self.model_path}"
        return f"{base} | {extra}" if extra else base


@runtime_checkable
class SttEngine(Protocol):
    """An utterance-in, transcript-out recogniser."""

    name: str

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> SttResult:
        """Transcribe one complete utterance of mono float32 audio."""
        ...

    def identity(self) -> SttIdentity:
        """Report which model is loaded, without transcribing anything."""
        ...


@runtime_checkable
class StreamingSttEngine(Protocol):
    """An incremental recogniser fed audio frames as they arrive.

    Used only for wake detection and pre-wake context. Its output is
    provisional by definition and must never be written to memory.
    """

    name: str

    def accept_waveform(self, samples: np.ndarray, sample_rate: int) -> None:
        """Feed a frame of mono float32 audio."""
        ...

    def partial_text(self) -> str:
        """Best transcript so far, for wake matching only."""
        ...

    def finalize(self) -> str:
        """Flush and return the final provisional transcript for this segment."""
        ...

    def reset(self) -> None:
        """Start a new segment, discarding incremental state."""
        ...


def require_existing_path(path: str | Path, hint: str = "") -> Path:
    """Return ``path`` as a Path, or raise a named, actionable error."""
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise SttModelMissingError(str(resolved), hint)
    return resolved
