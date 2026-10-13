"""On-disk cache of short pre-recorded speech clips.

A clip is one short phrase rendered once by the configured voice and kept as a
mono 16-bit WAV under Claire's cache directory. Playing it needs no synthesis
and no network round trip, which is what makes the first sound of a turn
arrive almost immediately.

Every clip's file name carries a key derived from both the phrase and a
fingerprint of the voice that rendered it (engine, voice id, model, and for
Piper the model path and length scale). A change to any of those changes the
key, so a clip rendered in one voice is never played in another.

Rendering happens on a background thread (see :meth:`ClipLibrary.prewarm`).
:meth:`ClipLibrary.get` only reads memory and disk, so a turn that needs a clip
never waits on the network.
"""
from __future__ import annotations

import hashlib
import os
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from ..debug import debug_log

#: Points the cache at a directory of the caller's choosing.
CLIP_CACHE_DIR_ENV = "CLAIRE_CLIP_CACHE_DIR"
#: Category used for every debug line this module emits.
CLIP_DEBUG_CATEGORY = "clips"
#: Sample width of a cached clip on disk. Playback converts nothing.
CLIP_SAMPLE_WIDTH = 2
CLIP_CHANNELS = 1

SynthResult = tuple[np.ndarray, int]
Synth = Callable[[str], SynthResult]


class ClipSynthesisError(RuntimeError):
    """A phrase could not be rendered into a clip."""


def clip_cache_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """Where rendered clips are kept.

    ``$CLAIRE_CLIP_CACHE_DIR``, else ``$XDG_CACHE_HOME/claire/clips``, else
    ``~/.cache/claire/clips``, beside the phone photos and camera frames,
    outside the repository, never in it.
    """
    source = os.environ if env is None else env
    override = str(source.get(CLIP_CACHE_DIR_ENV, "") or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(source.get("XDG_CACHE_HOME", "") or "").strip()
    if not base:
        home = str(source.get("HOME", "") or "").strip() or os.path.expanduser("~")
        base = os.path.join(home, ".cache")
    return Path(base) / "claire" / "clips"


def voice_fingerprint(config: Any) -> str:
    """A short digest of the voice a clip must be rendered in.

    The digest covers every setting that changes how the phrase sounds, so
    editing any of them re-renders the clips instead of serving stale audio in
    the wrong voice.
    """
    engine = str(getattr(config, "tts_engine", "fish") or "fish").strip().lower()
    if engine == "fish":
        parts = [
            engine,
            str(getattr(config, "fish_voice_id", None) or ""),
            str(getattr(config, "fish_model", None) or ""),
        ]
    elif engine == "piper":
        parts = [
            engine,
            str(getattr(config, "tts_piper_model_path", None)
                or getattr(config, "tts_voice", None) or ""),
            str(getattr(config, "tts_piper_length_scale", "")),
            str(getattr(config, "tts_piper_speaker", "")),
        ]
    else:
        parts = [engine, str(getattr(config, "tts_voice", None) or "")]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def clip_cache_key(phrase: str, voice: str) -> str:
    """The cache file's stem for ``phrase`` in voice ``voice``."""
    return hashlib.sha256(f"{voice}\x1f{phrase}".encode("utf-8")).hexdigest()[:16]


def clip_path(phrase: str, voice: str, directory: Optional[Path] = None) -> Path:
    """The path a clip is written to and read from."""
    base = Path(directory) if directory is not None else clip_cache_dir()
    return base / f"{clip_cache_key(phrase, voice)}.wav"


@dataclass(frozen=True)
class Clip:
    """One phrase's audio, already in the layout the output device wants."""

    phrase: str
    samples: np.ndarray
    sample_rate: int
    path: Optional[Path] = None

    @property
    def duration(self) -> float:
        """Length in seconds."""
        return float(self.samples.size) / float(self.sample_rate or 1)


def write_clip(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    """Publish ``samples`` as a mono 16-bit WAV at ``path``.

    Written to a temporary sibling and renamed, so a reader never sees a
    half-written clip and two writers cannot interleave.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = np.ascontiguousarray(np.asarray(samples, dtype=np.int16))
    temporary = path.with_name(path.name + ".part")
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(CLIP_CHANNELS)
        handle.setsampwidth(CLIP_SAMPLE_WIDTH)
        handle.setframerate(int(sample_rate))
        handle.writeframes(payload.tobytes())
    os.replace(str(temporary), str(path))


def load_clip(path: Path, phrase: str = "") -> Optional[Clip]:
    """Read a cached clip, or None when it is absent or unreadable.

    An unreadable file is reported as "no clip" rather than raising: the
    caller has a working fallback, and a corrupt cache entry should never take
    the turn down with it.
    """
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = int(handle.getframerate())
            frames = handle.readframes(handle.getnframes())
    except Exception as exc:
        debug_log(f"clips: ignoring unreadable clip {path.name}: {exc!r}", CLIP_DEBUG_CATEGORY)
        return None

    try:
        if width == CLIP_SAMPLE_WIDTH:
            samples = np.frombuffer(frames, dtype=np.int16)
        elif width == 1:
            samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) * 256
        elif width == 4:
            samples = (np.frombuffer(frames, dtype=np.int32) >> 16).astype(np.int16)
        else:
            raise ClipSynthesisError(f"unsupported sample width {width}")
        if channels > 1 and samples.size:
            samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
        samples = np.ascontiguousarray(samples)
    except Exception as exc:
        debug_log(f"clips: ignoring malformed clip {path.name}: {exc!r}", CLIP_DEBUG_CATEGORY)
        return None

    if samples.size == 0 or rate <= 0:
        debug_log(f"clips: ignoring empty clip {path.name}", CLIP_DEBUG_CATEGORY)
        return None
    return Clip(phrase=phrase, samples=samples, sample_rate=rate, path=path)


# --- Rendering --------------------------------------------------------------


def _pcm_stream_to_samples(raw: bytes) -> SynthResult:
    """Convert a Fish response body into int16 mono samples.

    The response may be a RIFF container or headerless PCM at any of Fish's
    rates; ``_PcmStreamFormat`` is the one component that already resolves that
    from the bytes themselves, so it is reused rather than re-derived here.
    """
    from .fish_tts import _PcmStreamFormat

    if not raw:
        raise ClipSynthesisError("the synthesiser returned no audio")
    fmt = _PcmStreamFormat()
    payload = bytearray(fmt.feed(raw))
    payload.extend(fmt.flush_tail())
    if not fmt.resolved or not payload:
        raise ClipSynthesisError("the synthesiser returned audio in an unknown format")
    samples = np.frombuffer(bytes(payload), dtype=np.int16).copy()
    if samples.size == 0:
        raise ClipSynthesisError("the synthesiser returned an empty clip")
    return samples, int(fmt.sample_rate)


def _render_with_fish(config: Any, phrase: str) -> SynthResult:
    from .fish_client import FishClient, FishKeyPool
    from .fish_tts import DEFAULT_KEY_FILE, DEFAULT_LATENCY, DEFAULT_MODEL

    voice_id = getattr(config, "fish_voice_id", None) or getattr(config, "tts_voice", None)
    if not voice_id:
        raise ClipSynthesisError("no fish_voice_id is configured")
    key_file = getattr(config, "fish_key_file", None) or DEFAULT_KEY_FILE
    client = FishClient(FishKeyPool(key_file))
    chunks = client.synthesise(
        phrase,
        str(voice_id),
        latency=str(getattr(config, "fish_latency", None) or DEFAULT_LATENCY),
        model=str(getattr(config, "fish_model", None) or DEFAULT_MODEL),
    )
    return _pcm_stream_to_samples(b"".join(chunks))


def _render_with_piper(config: Any, phrase: str) -> SynthResult:
    from .tts import PiperTTS, _get_default_piper_model_path

    model_path = getattr(config, "tts_piper_model_path", None) or _get_default_piper_model_path()
    if not model_path or not Path(str(model_path)).exists():
        raise ClipSynthesisError(
            "no Piper voice model is present, and rendering a clip must not "
            "start a download in the background"
        )
    engine = PiperTTS(
        enabled=True,
        model_path=model_path,
        speaker=getattr(config, "tts_piper_speaker", None),
        length_scale=float(getattr(config, "tts_piper_length_scale", 1.0) or 1.0),
        noise_scale=float(getattr(config, "tts_piper_noise_scale", 0.667) or 0.667),
        noise_w=float(getattr(config, "tts_piper_noise_w", 0.8) or 0.8),
        sentence_silence=float(getattr(config, "tts_piper_sentence_silence", 0.2) or 0.2),
    )
    return engine.synthesise_pcm(phrase)


def render_phrase(config: Any, phrase: str) -> SynthResult:
    """Render ``phrase`` with the configured engine.

    The configured engine is tried first. When it is Fish and it fails, Piper
    is the offline fallback the speech layer already relies on, and it is used
    only when its voice model is already on disk.
    """
    engine = str(getattr(config, "tts_engine", "fish") or "fish").strip().lower()
    if engine == "piper":
        return _render_with_piper(config, phrase)
    if engine == "fish":
        try:
            return _render_with_fish(config, phrase)
        except Exception as exc:
            debug_log(
                f"clips: Fish rendering failed for {phrase!r} ({exc!r}); trying Piper",
                CLIP_DEBUG_CATEGORY,
            )
            return _render_with_piper(config, phrase)
    raise ClipSynthesisError(f"engine {engine!r} cannot render a clip")


class ClipLibrary:
    """Resolves phrases to clips, rendering and caching the missing ones.

    ``get`` never synthesises, so it is safe on the turn's critical path.
    ``prewarm`` and ``get_or_generate`` render on a background thread and are
    what fills the cache.
    """

    def __init__(
        self,
        config: Any,
        *,
        synth: Optional[Synth] = None,
        directory: Optional[Path] = None,
        voice: Optional[str] = None,
    ) -> None:
        self.config = config
        self.voice = voice if voice is not None else voice_fingerprint(config)
        self.directory = Path(directory) if directory is not None else clip_cache_dir()
        self._synth: Synth = synth if synth is not None else (lambda text: render_phrase(config, text))
        self._memory: dict[str, Clip] = {}
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def path_for(self, phrase: str) -> Path:
        """The file this library reads and writes ``phrase`` from."""
        return clip_path(phrase, self.voice, self.directory)

    def get(self, phrase: str) -> Optional[Clip]:
        """Return the cached clip for ``phrase``, or None without rendering."""
        if not phrase:
            return None
        with self._lock:
            cached = self._memory.get(phrase)
        if cached is not None:
            return cached
        clip = load_clip(self.path_for(phrase), phrase)
        if clip is None:
            return None
        with self._lock:
            self._memory[phrase] = clip
        return clip

    def get_or_generate(self, phrase: str) -> Optional[Clip]:
        """Return the clip for ``phrase``, rendering it when it is missing."""
        clip = self.get(phrase)
        if clip is not None:
            return clip
        return self._generate(phrase)

    def available(self, phrases: Iterable[str]) -> list[Clip]:
        """Every phrase that already has a clip, in the order given."""
        found = []
        for phrase in phrases:
            clip = self.get(phrase)
            if clip is not None:
                found.append(clip)
        return found

    def prewarm(self, phrases: Sequence[str]) -> Optional[threading.Thread]:
        """Render missing clips on a daemon thread; returns that thread.

        Returns None when there is nothing to render. A phrase already being
        rendered is left to the thread that has it, so two callers cannot
        render the same phrase twice.
        """
        pending = [phrase for phrase in dict.fromkeys(phrases) if phrase and self.get(phrase) is None]
        if not pending:
            return None
        thread = threading.Thread(
            target=self._prewarm,
            args=(pending,),
            daemon=True,
            name="clip-prewarm",
        )
        thread.start()
        return thread

    # -- internals ---------------------------------------------------------

    def _prewarm(self, phrases: Sequence[str]) -> None:
        for phrase in phrases:
            try:
                self.get_or_generate(phrase)
            except Exception as exc:  # get_or_generate swallows its own; belt and braces
                debug_log(
                    f"clips: prewarm failed for {phrase!r}: {exc!r}", CLIP_DEBUG_CATEGORY
                )

    def _generate(self, phrase: str) -> Optional[Clip]:
        if not phrase:
            return None
        with self._lock:
            if phrase in self._inflight:
                return None
            self._inflight.add(phrase)
        try:
            samples, rate = self._synth(phrase)
        except Exception as exc:
            debug_log(
                f"clips: cannot render {phrase!r}: {exc!r}", CLIP_DEBUG_CATEGORY
            )
            return None
        finally:
            with self._lock:
                self._inflight.discard(phrase)

        if samples is None or len(samples) == 0 or int(rate) <= 0:
            debug_log(f"clips: renderer produced nothing for {phrase!r}", CLIP_DEBUG_CATEGORY)
            return None

        path = self.path_for(phrase)
        try:
            write_clip(path, samples, int(rate))
        except Exception as exc:
            debug_log(
                f"clips: cannot write {path.name}: {exc!r}", CLIP_DEBUG_CATEGORY
            )
            return None

        clip = Clip(phrase=phrase, samples=np.asarray(samples, dtype=np.int16),
                    sample_rate=int(rate), path=path)
        with self._lock:
            self._memory[phrase] = clip
        debug_log(
            f"clips: rendered {phrase!r} at {rate}Hz ({clip.duration:.2f}s)",
            CLIP_DEBUG_CATEGORY,
        )
        return clip
