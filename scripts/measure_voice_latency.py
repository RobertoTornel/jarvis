#!/usr/bin/env python3
"""Measure the five voice-pipeline latency numbers for Claire on this machine.

The five numbers the build records (``STATUS.md`` -> *Measured numbers*) are:

======================  =====================================================
measurement key         what is timed
======================  =====================================================
``wake_latency``        ``wake_detection.is_wake_word_detected`` over a
                        supplied wake-phrase list: phrase string -> decision.
                        Text matcher only (no audio, no ASR).
``endpoint_latency``    streaming recogniser over a real wav: last voiced
                        frame handed over -> endpointer deciding silence
                        (``endpoint_silence_ms`` of trailing silence with a
                        non-empty partial transcript).
``first_audio_latency`` Fish Audio TTS request dispatch -> first audio chunk.
``barge_in_latency``    barge-in phrase's first voiced frame -> TTS playback
                        actually stopped (``is_speaking()`` false).
``wake_false_positive_rate``
                        phrases that are *not* the wake name but are accepted,
                        divided by the list size, plus the exact words.
======================  =====================================================

Rules this harness obeys
------------------------
* **Never invent a number (N10).** When a measurement cannot be taken the
  harness prints ``status=not measured``, the reason, the exact path/backend
  that is missing and how to fix it, and exits non-zero. A missing credential,
  model or binary is never turned into ``0.0``.
* **Never print or log a secret.** The Fish Audio key is only ever handed to
  the SDK/engine; it is never read into the report. Every string that reaches
  the output is passed through ``claire.utils.redact.scrub_secrets``. The
  masked form the report carries is at most "file exists / size > 0".
* Offline by default for everything that needs no credential: wake latency and
  the false-positive rate need no audio at all; endpoint latency needs only a
  wav plus the local streaming model. Network measurements (Fish) run only when
  their credential and backend are present, unless ``--offline`` is given.

Exit status: ``0`` every requested measurement taken, ``3`` at least one not
taken (the build is incomplete, not "passing"), ``1`` fatal harness error
(bad arguments, unreadable report path).

Usage
-----
::

    # the offline half (no credentials, no network)
    .venv/bin/python scripts/measure_voice_latency.py --wav /path/to/speech.wav

    # machine-readable, for the build to record
    .venv/bin/python scripts/measure_voice_latency.py --json --quiet

    # a real endpoint run, real-time paced (default), 3 runs
    .venv/bin/python scripts/measure_voice_latency.py --wav speech.wav --endpoint-runs 3

    # compute-only endpoint variant (no sleeping; excludes the audio window)
    .venv/bin/python scripts/measure_voice_latency.py --wav speech.wav --unpaced
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import re
import statistics
import sys
import time
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------
# Make `claire` importable when this file is run from anywhere.
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCHEMA = "claire.voice_latency.v1"

MEASUREMENT_KEYS: tuple[str, ...] = (
    "wake_latency",
    "endpoint_latency",
    "first_audio_latency",
    "barge_in_latency",
    "wake_false_positive_rate",
)

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_NOT_MEASURED = 3

STATUS_MEASURED = "measured"
STATUS_NOT_MEASURED = "not_measured"

#: Stop phrases, mirroring ``get_default_config()["stop_commands"]`` (the
#: ``Settings`` dataclass does not carry them, so the harness keeps its own
#: default and accepts --stop-commands).
DEFAULT_STOP_COMMANDS: tuple[str, ...] = ("stop", "quiet", "shush", "silence", "enough", "shut up")
DEFAULT_STOP_FUZZY_RATIO = 0.8

#: Ordinary words used to pad the generated negative list. None of them is a
#: near-miss of the wake name, so they give the "easy negative" half of the
#: false-positive rate; the generated mutations give the hard half.
COMMON_NEGATIVE_WORDS: tuple[str, ...] = (
    "the", "and", "please", "thanks", "hello", "good", "morning", "evening",
    "weather", "forecast", "traffic", "calendar", "meeting", "remind",
    "reminder", "kitchen", "window", "laptop", "phone", "battery", "volume",
    "brightness", "temperature", "tomorrow", "today", "tonight", "weekend",
    "photos", "email", "messages", "coffee", "dinner", "breakfast", "music",
    "play", "pause", "next", "previous", "news", "train", "airport", "bank",
    "library", "hospital", "grocery", "shopping", "laundry", "garage",
    "clear", "clean", "close", "open", "start", "stop", "quiet", "loud",
    "what", "when", "where", "why", "how", "which", "who", "whose",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "zero", "hundred", "thousand", "minute", "hour", "second", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

#: Where to look for a real wav when --wav is not given. The s3tokenizer
#: asset ships with a dependency in the project venv and is genuine recorded
#: speech (16 kHz mono), which makes the endpoint measurement runnable on
#: mana-pc without adding audio to the repository.
def _default_wav_search_paths() -> list[Path]:
    paths = [
        REPO_ROOT / "tests" / "fixtures" / "voice",
        REPO_ROOT / "examples" / "audio",
        REPO_ROOT / "assets",
        Path.home() / ".local" / "share" / "claire" / "audio",
    ]
    py = f"python{sys.version_info.major}.{sys.version_info.minor}"
    paths.append(
        REPO_ROOT / ".venv" / "lib" / py / "site-packages" / "s3tokenizer" / "assets"
    )
    return paths


class MeasurementUnavailable(RuntimeError):
    """A measurement cannot be taken; carries the reason and the fix.

    Raising this is the *only* correct response to a missing model, binary,
    credential or input. It must never be swallowed into a fabricated value.
    """

    def __init__(self, reason: str, fix: str = "") -> None:
        self.reason = reason
        self.fix = fix
        super().__init__(reason)


class CredentialMissingError(MeasurementUnavailable):
    """A credential file is absent or empty. Names the exact path."""

    def __init__(self, path: str | Path, fix: str) -> None:
        self.path = Path(path)
        super().__init__(
            f"credential file not found or empty: {self.path}",
            fix=fix,
        )


class BackendMissingError(MeasurementUnavailable):
    """A Python module or binary the measurement needs is not importable."""

    def __init__(self, module: str, package: str, cause: BaseException | None = None) -> None:
        self.module = module
        super().__init__(
            f"Python module {module!r} is not importable"
            + (f" ({type(cause).__name__}: {cause})" if cause else ""),
            fix=f"pip install {package}",
        )


class HarnessError(RuntimeError):
    """Fatal: the harness cannot run at all (bad arguments, bad config)."""


# --------------------------------------------------------------------------
# Redaction: nothing reaches stdout/stderr without passing through here.
# --------------------------------------------------------------------------
def _scrub(text: Any) -> Any:
    try:
        from claire.utils.redact import scrub_secrets
    except Exception:  # pragma: no cover - redaction must never be the failure
        return text
    if isinstance(text, str):
        return scrub_secrets(text)
    return text


def _scrub_obj(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, dict):
        return {_scrub_obj(k): _scrub_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_obj(v) for v in value]
    return value


def _jsonable(value: Any) -> Any:
    """Convert to plain JSON types, rounding floats to sub-microsecond noise."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 7)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    item = getattr(value, "item", None)  # numpy scalar
    if callable(item):
        try:
            return _jsonable(item())
        except Exception:
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _jsonable(tolist())
        except Exception:
            pass
    return str(value)


def mask_path(path: Path | None) -> str:
    """The only thing the report is allowed to say about a key file."""
    if path is None:
        return "<unset>"
    try:
        if not path.exists():
            return f"{path} (absent)"
        size = path.stat().st_size
        return f"{path} (present, {size} bytes; value never read into the report)"
    except OSError as exc:
        return f"{path} (unreadable: {type(exc).__name__})"


# --------------------------------------------------------------------------
# Measurement / report model
# --------------------------------------------------------------------------
@dataclass
class Measurement:
    """One of the five numbers, taken or honestly not taken."""

    key: str
    title: str
    method: str
    status: str = STATUS_NOT_MEASURED
    value: float | None = None
    unit: str = "seconds"
    detail: str = ""
    fix: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def measured(self) -> bool:
        return self.status == STATUS_MEASURED and self.value is not None

    def to_json(self) -> dict[str, Any]:
        return _scrub_obj(
            _jsonable(
                {
                    "key": self.key,
                    "title": self.title,
                    "status": self.status,
                    "value": self.value,
                    "unit": self.unit,
                    "method": self.method,
                    "detail": self.detail,
                    "fix": self.fix,
                    "raw": self.raw,
                }
            )
        )


def measured(
    key: str,
    title: str,
    method: str,
    value: float,
    *,
    unit: str = "seconds",
    detail: str = "",
    raw: dict[str, Any] | None = None,
) -> Measurement:
    return Measurement(
        key=key,
        title=title,
        method=method,
        status=STATUS_MEASURED,
        value=float(value),
        unit=unit,
        detail=detail,
        raw=raw or {},
    )


def not_measured(key: str, title: str, method: str, exc: BaseException, *, detail: str = "") -> Measurement:
    if isinstance(exc, MeasurementUnavailable):
        reason, fix = exc.reason, exc.fix
    else:
        reason, fix = f"{type(exc).__name__}: {exc}", ""
    if detail:
        reason = f"{reason}; {detail}"
    return Measurement(
        key=key,
        title=title,
        method=method,
        status=STATUS_NOT_MEASURED,
        value=None,
        detail=_scrub(reason),
        fix=_scrub(fix),
    )


# --------------------------------------------------------------------------
# Context: everything a measurement may need, injectable for tests
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    """Inputs for the measurements.

    Every external dependency (clock, sleep, matcher, engine, probe, audio
    reader) is a field so ``tests/test_voice_latency_harness.py`` can drive the
    real measurement code with fakes and no network, audio device or model.
    """

    cfg: Any = None
    wake_word: str = "claire"
    wake_aliases: tuple[str, ...] = ()
    wake_fuzzy_ratio: float = 0.78
    stop_commands: tuple[str, ...] = DEFAULT_STOP_COMMANDS
    stop_fuzzy_ratio: float = DEFAULT_STOP_FUZZY_RATIO

    wake_words: tuple[str, ...] = ()
    negative_words: tuple[str, ...] = ()
    negative_words_generated: bool = False
    negative_seed: int = 0

    wake_matcher: Callable[[str], bool] | None = None
    stop_matcher: Callable[[str], bool] | None = None

    wav_path: Path | None = None
    frame_ms: int = 20
    endpoint_silence_ms: int = 800
    speech_rms_threshold: float = 0.01
    endpoint_runs: int = 3
    pace: bool = True
    streaming_engine: Any = None
    streaming_engine_error: str = ""
    audio_reader: Callable[[Path, int], tuple[Any, int]] | None = None

    fish_key_file: Path | None = None
    fish_text: str = "Claire is measuring her voice latency on this machine."
    fish_runs: int = 3
    fish_probe: Callable[[str, int, str], Sequence[float]] | None = None
    fish_probe_error: str = ""
    offline: bool = False

    tts_engine: Any = None
    tts_engine_error: str = ""
    barge_in_wav: Path | None = None
    barge_in_text: str = "This is a long sentence Claire speaks while the user interrupts her."
    barge_in_runs: int = 1
    barge_in_speak_timeout_sec: float = 5.0
    barge_in_stop_timeout_sec: float = 5.0
    barge_in_poll_sec: float = 0.02

    clock: Callable[[], float] = time.perf_counter
    sleep: Callable[[float], None] = time.sleep


# --------------------------------------------------------------------------
# Wake-phrase lists
# --------------------------------------------------------------------------
def default_wake_phrases(wake_word: str, aliases: Sequence[str]) -> tuple[str, ...]:
    """Wake phrases a correct matcher should accept."""
    phrases: list[str] = [wake_word, *aliases, f"hey {wake_word}", f"{wake_word}, are you there"]
    seen: set[str] = set()
    out: list[str] = []
    for phrase in phrases:
        key = phrase.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(phrase.strip())
    return tuple(out)


def _name_pattern(wake_word: str, aliases: Sequence[str]) -> re.Pattern[str] | None:
    names = {n.strip() for n in [wake_word, *aliases] if n and n.strip()}
    if not names:
        return None
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r"(?<!\w)(?:" + alts + r")(?!\w)", re.IGNORECASE)


def _is_plain_name(phrase: str, wake_word: str, aliases: Sequence[str]) -> bool:
    """True when the literal wake-name rule alone would accept this phrase.

    Such a phrase is a *true* positive, not a false positive, so it is excluded
    from the negative list.
    """
    pattern = _name_pattern(wake_word, aliases)
    if pattern and pattern.search(phrase):
        return True
    primary = wake_word.strip().casefold()
    return bool(primary) and phrase.strip().casefold() == primary


def _mutations(word: str) -> list[str]:
    """Deterministic near-misses: deletions, transpositions, vowel swaps."""
    w = word.strip()
    out: list[str] = []
    for i in range(len(w)):
        out.append(w[:i] + w[i + 1:])  # deletion
    for i in range(len(w) - 1):
        out.append(w[:i] + w[i + 1] + w[i] + w[i + 2:])  # transposition
    for i, ch in enumerate(w):
        if ch in "aeiou":
            for v in "aeiou":
                if v != ch:
                    out.append(w[:i] + v + w[i + 1:])
    return out


def generate_negative_words(
    wake_word: str,
    count: int,
    *,
    seed: int = 0,
    aliases: Sequence[str] = (),
) -> tuple[str, ...]:
    """A deterministic list of phrases that are *not* the wake name.

    Half of it is ordinary speech, half is letter-level near-misses of the wake
    name. Phrases the literal name rule would accept (the bare wake word, an
    alias, or a sentence containing it as a whole word) are excluded, because
    those are true positives; what remains is a genuine negative list, and any
    acceptance by ``is_wake_word_detected`` is a false positive.
    """
    primary = (wake_word or "").strip().casefold()
    candidates: list[str] = list(COMMON_NEGATIVE_WORDS)
    if primary:
        candidates.extend(_mutations(primary))
        rng = random.Random(seed)
        letters = "abcdefghijklmnopqrstuvwxyz"
        guard = 0
        while len(candidates) < max(count, 1) * 3 and guard < 10000:
            guard += 1
            i = rng.randrange(len(primary))
            candidates.append(primary[:i] + rng.choice(letters) + primary[i + 1:])
        rng.shuffle(candidates)

    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        key = cand.casefold()
        if key in seen:
            continue
        if _is_plain_name(cand, wake_word, aliases):
            continue
        seen.add(key)
        out.append(cand)
        if len(out) >= max(count, 1):
            break
    return tuple(out)


def read_word_list(path: Path, *, what: str) -> tuple[str, ...]:
    """Read one phrase per line; ``#`` starts a comment; blanks are skipped."""
    p = Path(path)
    if not p.is_file():
        raise MeasurementUnavailable(
            f"no {what} word list at {p}",
            fix=f"write one phrase per line to {p}, or pass --generate-negative-words / drop --{what}-words-file",
        )
    words: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            words.append(line)
    if not words:
        raise MeasurementUnavailable(
            f"{what} word list at {p} contains no phrases",
            fix=f"put at least one phrase per line in {p}",
        )
    return tuple(words)


def build_wake_matcher(ctx: Ctx) -> Callable[[str], bool]:
    """The real matcher from ``wake_detection`` unless a fake was injected."""
    if ctx.wake_matcher is not None:
        return ctx.wake_matcher

    from claire.listening.wake_detection import is_wake_word_detected

    wake_word = ctx.wake_word
    aliases = list(ctx.wake_aliases)
    ratio = float(ctx.wake_fuzzy_ratio)

    def matcher(text: str) -> bool:
        return bool(is_wake_word_detected(text, wake_word, aliases, ratio))

    return matcher


def build_stop_matcher(ctx: Ctx) -> Callable[[str], bool]:
    if ctx.stop_matcher is not None:
        return ctx.stop_matcher

    from claire.listening.wake_detection import is_stop_command

    commands = list(ctx.stop_commands)
    ratio = float(ctx.stop_fuzzy_ratio)

    def matcher(text: str) -> bool:
        return bool(is_stop_command(text, commands, ratio))

    return matcher


# --------------------------------------------------------------------------
# 1. Wake latency and 5. wake false-positive rate  (no audio, no network)
# --------------------------------------------------------------------------
WAKE_METHOD = (
    "wall-clock seconds for claire.listening.wake_detection.is_wake_word_detected(phrase) over the "
    "supplied wake-phrase list, median of N phrases: from the phrase string in memory to the boolean "
    "decision. Text matcher only -- no audio, no ASR, so this is NOT acoustic wake latency."
)
WAKE_FP_METHOD = (
    "count of supplied/generated non-wake phrases that wake_detection.is_wake_word_detected accepts, "
    "divided by the number of phrases (rate = woke / total). Each phrase is fed as text; no audio is "
    "used. Phrases the literal wake-name rule alone accepts are excluded from the list (they are true "
    "positives), so every acceptance here is a fuzzy false positive."
)


def measure_wake_latency(ctx: Ctx) -> Measurement:
    words = tuple(ctx.wake_words) or default_wake_phrases(ctx.wake_word, ctx.wake_aliases)
    if not words:
        raise MeasurementUnavailable(
            "no wake phrases supplied and no wake word configured",
            fix="pass --wake-words-file PATH or --wake-word WORD",
        )
    matcher = build_wake_matcher(ctx)

    samples: list[float] = []
    detected: list[str] = []
    missed: list[str] = []
    for phrase in words:
        t0 = ctx.clock()
        hit = bool(matcher(phrase.casefold()))
        samples.append(ctx.clock() - t0)
        (detected if hit else missed).append(phrase)

    return measured(
        "wake_latency",
        "wake latency",
        WAKE_METHOD,
        statistics.median(samples),
        unit="seconds",
        detail=(
            f"median of {len(samples)} phrases; detection rate "
            f"{len(detected)}/{len(samples)}"
        ),
        raw={
            "phrases": len(words),
            "measures": "text wake-matcher decision cost, NOT acoustic wake latency",
            "acoustic_wake_latency": (
                "not measured: needs a wake-word wav through the streaming recogniser, which this "
                "harness does not drive"
            ),
            "wake_word": ctx.wake_word,
            "aliases": list(ctx.wake_aliases),
            "fuzzy_ratio": ctx.wake_fuzzy_ratio,
            "samples_sec": samples,
            "min_sec": min(samples),
            "max_sec": max(samples),
            "p95_sec": sorted(samples)[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))],
            "detection_rate": len(detected) / len(samples),
            "detected_words": detected,
            "missed_words": missed,
        },
    )


def measure_wake_false_positive_rate(ctx: Ctx) -> Measurement:
    words = tuple(ctx.negative_words)
    generated = bool(ctx.negative_words_generated)
    if not words:
        words = generate_negative_words(
            ctx.wake_word, 40, seed=ctx.negative_seed, aliases=ctx.wake_aliases
        )
        generated = True
        # Record the generated list so the number is reproducible from the report.
    if not words:
        raise MeasurementUnavailable(
            "could not build a non-wake phrase list",
            fix="pass --negative-words-file PATH or --generate-negative-words N",
        )

    matcher = build_wake_matcher(ctx)
    woke = [w for w in words if bool(matcher(w.casefold()))]
    rate = len(woke) / len(words)
    literal_names = [w for w in words if _is_plain_name(w, ctx.wake_word, ctx.wake_aliases)]

    return measured(
        "wake_false_positive_rate",
        "wake false-positive rate",
        WAKE_FP_METHOD,
        rate,
        unit="rate",
        detail=(
            f"{len(woke)} of {len(words)} non-wake phrases woke her"
            + (" (list generated in-process)" if generated else " (supplied list)")
            + (f"; woke: {', '.join(woke)}" if woke else "; woke: none")
            + (
                f"; {len(literal_names)} supplied phrase(s) contain the wake name as a whole word and "
                "are therefore true positives, not false positives"
                if literal_names
                else ""
            )
        ),
        raw={
            "total_phrases": len(words),
            "woke_count": len(woke),
            "woke_words": woke,
            "clean_words": [w for w in words if w not in woke],
            "literal_name_phrases": literal_names,
            "word_list_generated": generated,
            "seed": ctx.negative_seed if generated else None,
            "wake_word": ctx.wake_word,
            "aliases": list(ctx.wake_aliases),
            "fuzzy_ratio": ctx.wake_fuzzy_ratio,
            "word_list": list(words),
        },
    )


# --------------------------------------------------------------------------
# 2. Endpoint latency (real wav + local streaming recogniser)
# --------------------------------------------------------------------------
ENDPOINT_METHOD = (
    "wall-clock seconds from the last voiced frame handed to the streaming recogniser to the endpointer "
    "deciding the utterance is over (endpoint_silence_ms of trailing frames with a non-empty partial "
    "transcript), median of N runs over one real wav. Frames are paced at frame_ms by default so the "
    "value includes the configured endpoint window; raw.transcript_settle_sec is the recogniser's own "
    "text-settle time and raw.finalize_sec the flush cost."
)


def read_wav_mono(path: Path, target_sr: int) -> tuple[Any, int]:
    """Read a wav as mono float32 in [-1, 1], resampled to ``target_sr``.

    Uses the stdlib ``wave`` module only, so it works with any file the audio
    stack accepts (8/16/32-bit PCM, 32-bit float) and needs no extra package.
    """
    import numpy as np

    p = Path(path)
    if not p.is_file():
        raise MeasurementUnavailable(
            f"no wav at {p}",
            fix="pass --wav PATH pointing at a 16 kHz mono wav of real speech",
        )
    try:
        with wave.open(str(p), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            frames = w.getnframes()
            raw = w.readframes(frames)
    except wave.Error as exc:
        raise MeasurementUnavailable(
            f"{p} is not a readable PCM wav ({exc})",
            fix="supply an uncompressed 16-bit PCM wav, e.g. with: ffmpeg -i in.wav -ar 16000 -ac 1 out.wav",
        ) from exc

    if width == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        as_int = np.frombuffer(raw, dtype="<i4").astype(np.float32)
        # 32-bit wavs are either int32 PCM or IEEE float; floats stay tiny.
        audio = as_int / 2147483648.0 if float(np.max(np.abs(as_int), initial=0.0)) > 1.5 else as_int
    else:
        raise MeasurementUnavailable(
            f"{p} has {width}-byte samples, which this reader does not support",
            fix="convert to 16-bit PCM: ffmpeg -i in.wav -ar 16000 -ac 1 -sample_fmt s16 out.wav",
        )

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    audio = np.ascontiguousarray(audio, dtype=np.float32)

    if rate != target_sr and audio.size:
        if rate <= 0 or target_sr <= 0:
            raise MeasurementUnavailable(f"{p} reports sample rate {rate}", fix="use a wav with a valid rate")
        n_out = int(round(audio.size * target_sr / rate))
        src_idx = np.linspace(0.0, audio.size - 1, num=max(n_out, 1), dtype=np.float64)
        audio = np.interp(src_idx, np.arange(audio.size), audio).astype(np.float32)
    return audio, int(target_sr)


def find_default_wav() -> Path | None:
    import os

    env = os.environ.get("CLAIRE_LATENCY_WAV")
    if env:
        return Path(env).expanduser()
    for directory in _default_wav_search_paths():
        try:
            if directory.is_dir():
                for cand in sorted(directory.glob("*.wav")):
                    if cand.is_file():
                        return cand
        except OSError:
            continue
    return None


def _endpoint_once(ctx: Ctx, engine: Any, audio: Any, sample_rate: int) -> dict[str, Any]:
    """One paced/unpaced pass over the wav. Returns the raw quantities."""
    import numpy as np

    frame_len = max(1, int(round(sample_rate * ctx.frame_ms / 1000.0)))
    if audio.size < frame_len * 2:
        raise MeasurementUnavailable(
            f"wav at {ctx.wav_path} is shorter than two {ctx.frame_ms} ms frames",
            fix="supply at least ~1 s of speech",
        )
    endpoint_frames = max(1, int(math.ceil(ctx.endpoint_silence_ms / float(ctx.frame_ms))))

    engine.reset()
    t_first_submit: float | None = None
    t_last_voiced: float | None = None
    t_endpoint: float | None = None
    silence_run = 0
    voiced_frames = 0
    silent_frames = 0
    frame_trace: list[tuple[float, str]] = []  # (submit clock, partial text)
    last_voiced_idx = -1
    n_frames = int(audio.size // frame_len)

    for i in range(n_frames):
        frame = np.ascontiguousarray(audio[i * frame_len:(i + 1) * frame_len])
        t_submit = ctx.clock()
        engine.accept_waveform(frame, sample_rate)
        t_done = ctx.clock()
        if t_first_submit is None:
            t_first_submit = t_submit
        rms = float(np.sqrt(float(np.mean(frame.astype(np.float64) ** 2))))
        partial = str(engine.partial_text() or "")
        frame_trace.append((t_done, partial))
        if rms >= ctx.speech_rms_threshold:
            voiced_frames += 1
            t_last_voiced = t_done
            last_voiced_idx = len(frame_trace) - 1
            silence_run = 0
        else:
            if t_last_voiced is not None:
                silent_frames += 1
                silence_run += 1
                if silence_run >= endpoint_frames and partial.strip():
                    t_endpoint = t_done
                    break
        if ctx.pace:
            ctx.sleep(ctx.frame_ms / 1000.0)

    if t_last_voiced is None:
        raise MeasurementUnavailable(
            f"no speech above the RMS threshold {ctx.speech_rms_threshold} in {ctx.wav_path}",
            fix="lower --speech-rms-threshold for a quiet recording, or supply a louder wav",
        )

    t_finalize_start = ctx.clock()
    transcript = str(engine.finalize() or "")
    t_finalize_end = ctx.clock()

    if not transcript.strip():
        raise MeasurementUnavailable(
            f"the streaming recogniser returned an empty transcript over {ctx.wav_path} "
            f"(engine={getattr(engine, 'name', type(engine).__name__)!r})",
            fix="check the streaming model directory (streaming_stt_model_dir) and the wav's sample rate",
        )

    # The recogniser's own settle time: the last moment partial_text changed
    # *after* speech stopped. 0.0 means the text was already final when the
    # speech ended, which is the good case.
    settle_sec = 0.0
    for idx in range(1, len(frame_trace)):
        t_done, partial = frame_trace[idx]
        if idx > last_voiced_idx and partial != frame_trace[idx - 1][1]:
            settle_sec = max(settle_sec, t_done - t_last_voiced)

    reached = t_endpoint is not None
    latency = (t_endpoint - t_last_voiced) if reached else (t_finalize_end - t_last_voiced)

    return {
        "latency_sec": latency,
        "endpoint_reached_in_wav": reached,
        "trailing_silence_audio_sec": silent_frames * ctx.frame_ms / 1000.0,
        "endpoint_window_sec": ctx.endpoint_silence_ms / 1000.0,
        "transcript_settle_sec": settle_sec,
        "finalize_sec": t_finalize_end - t_finalize_start,
        "feed_sec": (frame_trace[-1][0] - t_first_submit) if frame_trace and t_first_submit is not None else None,
        "voiced_frames": voiced_frames,
        "silent_frames": silent_frames,
        "frames": n_frames,
        "transcript": transcript[:120],
    }


def measure_endpoint_latency(ctx: Ctx) -> Measurement:
    wav = Path(ctx.wav_path) if ctx.wav_path else find_default_wav()
    if wav is None or not Path(wav).is_file():
        raise MeasurementUnavailable(
            "no wav supplied and none found in "
            + ", ".join(str(p) for p in _default_wav_search_paths()),
            fix="pass --wav PATH (a real 16 kHz mono recording, e.g. a LibriSpeech clip)",
        )
    ctx = replace(ctx, wav_path=Path(wav))

    engine = ctx.streaming_engine
    if engine is None:
        err = ctx.streaming_engine_error or "streaming_stt_engine is not configured/present"
        if "sherpa_streaming" in err:
            fix = (
                "implement src/claire/listening/stt/sherpa_streaming.py (SherpaStreamingEngine) -- the "
                "factory create_streaming_stt_engine already expects it"
            )
        elif "Speech model not found" in err or "not found" in err:
            fix = (
                "download the streaming zipformer bundle into the path named in the error above (see "
                "src/claire/listening/model_download.py), or point streaming_stt_model_dir at an "
                "existing bundle, then re-run"
            )
        elif "disabled" in err:
            fix = "set streaming_stt_engine='sherpa' in config.json and a present streaming_stt_model_dir"
        else:
            fix = "configure streaming_stt_engine='sherpa' with a present streaming_stt_model_dir"
        raise MeasurementUnavailable(f"no streaming recogniser available: {err}", fix=fix)

    sample_rate = int(getattr(ctx.cfg, "sample_rate", 16000) or 16000)
    reader = ctx.audio_reader or read_wav_mono
    audio, sample_rate = reader(Path(wav), sample_rate)

    runs = max(1, int(ctx.endpoint_runs))
    raw_runs: list[dict[str, Any]] = []
    for _ in range(runs):
        raw_runs.append(_endpoint_once(ctx, engine, audio, sample_rate))

    values = [r["latency_sec"] for r in raw_runs]
    identity = ""
    try:
        ident = engine.identity() if hasattr(engine, "identity") else None
        identity = ident.describe() if hasattr(ident, "describe") else (str(ident) if ident else "")
    except Exception as exc:  # identity is reporting only; never fatal
        identity = f"<identity failed: {type(exc).__name__}>"

    return measured(
        "endpoint_latency",
        "endpoint latency",
        ENDPOINT_METHOD,
        statistics.median(values),
        unit="seconds",
        detail=(
            f"median of {runs} run(s) over {wav}; engine={getattr(engine, 'name', type(engine).__name__)}"
            + ("" if all(r["endpoint_reached_in_wav"] for r in raw_runs) else
               " (some runs hit the end of the wav before the endpoint window elapsed)")
        ),
        raw={
            "wav": str(wav),
            "sample_rate": sample_rate,
            "frame_ms": ctx.frame_ms,
            "endpoint_silence_ms": ctx.endpoint_silence_ms,
            "paced": ctx.pace,
            "speech_rms_threshold": ctx.speech_rms_threshold,
            "runs": raw_runs,
            "values_sec": values,
            "engine_identity": identity,
        },
    )


# --------------------------------------------------------------------------
# 3. First-audio latency (Fish Audio) and 4. barge-in latency
# --------------------------------------------------------------------------
FIRST_AUDIO_METHOD = (
    "wall-clock seconds from the Fish Audio TTS request dispatch to the first audio chunk received, "
    "median of N runs, streamed through the fish_audio_sdk the app depends on (requirements.txt). "
    "The app-level FishTTS wrapper is not in this timing unless --fish-probe-module supplies it."
)
BARGE_IN_METHOD = (
    "wall-clock seconds from the barge-in phrase's first voiced frame handed to the streaming "
    "recogniser to TTS playback actually stopping (tts.is_speaking() false after tts.interrupt()), "
    "while the Fish engine is audibly speaking; median of N runs. The phrase is fed from a wav, so no "
    "microphone is needed, but the real TTS output device is."
)


def _fish_credential_blocker(ctx: Ctx) -> MeasurementUnavailable | None:
    if ctx.offline:
        return MeasurementUnavailable(
            "--offline requested: the Fish Audio measurement needs the network",
            fix="drop --offline to take this measurement",
        )
    key_file = ctx.fish_key_file
    if key_file is None:
        return MeasurementUnavailable(
            "no Fish Audio credential file configured (fish_key_file unset)",
            fix="set fish_key_file in config.json to a 0600 file outside the repo holding the Fish Audio key",
        )
    key_file = Path(key_file).expanduser()
    if not key_file.is_file():
        return CredentialMissingError(
            key_file,
            fix=(
                f"write the Fish Audio API key into {key_file} (mode 0600, outside the repository) and "
                "re-run; the key value is never printed"
            ),
        )
    try:
        if key_file.stat().st_size == 0:
            return CredentialMissingError(
                key_file,
                fix=f"{key_file} is empty: write the Fish Audio API key into it (mode 0600)",
            )
    except OSError as exc:
        return MeasurementUnavailable(
            f"cannot stat the Fish Audio credential file {key_file} ({type(exc).__name__})",
            fix=f"check permissions on {key_file}",
        )
    return None


def load_probe_from_module(spec: str) -> Callable[[str, int, str], Sequence[float]]:
    """Load ``module:function`` as the first-audio probe.

    The function is called as ``fn(text, runs, key_file)`` and must return one
    latency in seconds per run.
    """
    if ":" not in spec:
        raise MeasurementUnavailable(
            f"--fish-probe-module {spec!r} is not 'module:function'",
            fix="pass --fish-probe-module mypackage.mymodule:probe",
        )
    module_name, func_name = spec.split(":", 1)
    try:
        import importlib

        module = importlib.import_module(module_name)
    except Exception as exc:
        raise MeasurementUnavailable(
            f"cannot import probe module {module_name!r} ({type(exc).__name__}: {exc})",
            fix=f"check that {module_name} is importable from {REPO_ROOT}",
        ) from exc
    func = getattr(module, func_name, None)
    if not callable(func):
        raise MeasurementUnavailable(
            f"probe module {module_name!r} has no callable {func_name!r}",
            fix=f"define {func_name}(text, runs, key_file) in {module_name}",
        )
    return func


def build_fish_probe(ctx: Ctx, key_file: Path) -> Callable[[str, int, str], Sequence[float]]:
    """The default probe: stream from Fish Audio through ``fish_audio_sdk``."""
    try:
        import fish_audio_sdk as sdk
    except Exception as exc:
        raise BackendMissingError("fish_audio_sdk", "'fish-audio-sdk>=1.3.0'", exc) from exc

    api_key = key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise CredentialMissingError(
            key_file,
            fix=f"write the Fish Audio API key into {key_file} (mode 0600); it is never printed",
        )

    session_factory = getattr(sdk, "Session", None)
    request_cls = getattr(sdk, "TTSRequest", None)
    if session_factory is None or request_cls is None:
        raise BackendMissingError(
            "fish_audio_sdk",
            "'fish-audio-sdk>=1.3.0' (missing Session/TTSRequest)",
        )

    voice_id = getattr(ctx.cfg, "fish_voice_id", None)
    latency_mode = getattr(ctx.cfg, "fish_latency", None)
    model = getattr(ctx.cfg, "fish_model", None)

    def probe(text: str, runs: int, _key_file: str) -> Sequence[float]:
        session = session_factory(api_key)
        out: list[float] = []
        for _ in range(max(1, int(runs))):
            kwargs: dict[str, Any] = {"text": text}
            if voice_id:
                kwargs["reference_id"] = voice_id
            if latency_mode:
                kwargs["latency"] = latency_mode
            if model:
                kwargs["model"] = model
            request = request_cls(**kwargs)
            t0 = time.perf_counter()
            stream = session.tts(request)
            for chunk in stream:
                data = chunk if isinstance(chunk, (bytes, bytearray)) else chunk.read(1)
                if data:
                    out.append(time.perf_counter() - t0)
                    break
            else:
                raise MeasurementUnavailable(
                    "Fish Audio returned no audio chunk",
                    fix="check the Fish Audio key, fish_voice_id and account quota",
                )
            if hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass
        return out

    return probe


def measure_first_audio_latency(ctx: Ctx) -> Measurement:
    blocker = _fish_credential_blocker(ctx)
    if blocker is not None:
        raise blocker

    key_file = Path(ctx.fish_key_file).expanduser()
    probe = ctx.fish_probe
    if probe is None:
        if ctx.fish_probe_error:
            raise MeasurementUnavailable(
                ctx.fish_probe_error,
                fix="install the declared dependency: pip install 'fish-audio-sdk>=1.3.0'",
            )
        probe = build_fish_probe(ctx, key_file)

    runs = max(1, int(ctx.fish_runs))
    samples = [float(v) for v in probe(ctx.fish_text, runs, str(key_file))]
    if not samples:
        raise MeasurementUnavailable(
            "the Fish Audio probe returned no samples",
            fix="check the probe implementation; no number is invented here",
        )

    return measured(
        "first_audio_latency",
        "first-audio latency (Fish)",
        FIRST_AUDIO_METHOD,
        statistics.median(samples),
        unit="seconds",
        detail=(
            f"median of {len(samples)} run(s) for {ctx.fish_text!r}; credential {mask_path(key_file)}"
        ),
        raw={
            "samples_sec": samples,
            "runs": len(samples),
            "min_sec": min(samples),
            "max_sec": max(samples),
            "text": ctx.fish_text,
            "credential_file": mask_path(key_file),
            "voice_id": getattr(ctx.cfg, "fish_voice_id", None),
            "latency_mode": getattr(ctx.cfg, "fish_latency", None),
        },
    )


def _tts_blocker(ctx: Ctx) -> MeasurementUnavailable | None:
    if ctx.tts_engine is not None:
        return None
    if ctx.tts_engine_error:
        return MeasurementUnavailable(
            ctx.tts_engine_error,
            fix=(
                "the barge-in measurement needs the app's Fish engine: add a FishTTS engine to "
                "src/claire/output/tts.py (engine 'fish') and re-run"
            ),
        )
    return MeasurementUnavailable(
        "no TTS engine available",
        fix="configure fish_key_file and the Fish engine (src/claire/output/tts.py, engine 'fish')",
    )


def seek_default_tts_engine(ctx: Ctx) -> tuple[Any, str]:
    """Resolve the app's Fish TTS engine, refusing to measure the wrong engine.

    ``create_tts_engine(engine="fish")`` currently falls back to Piper, so the
    harness checks that the module actually carries a Fish engine and that the
    returned object is a Fish engine before timing anything.
    """
    try:
        from claire import output  # noqa: F401  (import path check)
        from claire.output import tts as tts_module
    except Exception as exc:
        return None, f"claire.output.tts is not importable ({type(exc).__name__}: {exc})"

    has_fish = any(
        hasattr(tts_module, name) for name in ("FishTTS", "create_fish_tts_engine")
    )
    if not has_fish:
        return None, (
            "this checkout has no Fish engine in claire.output.tts "
            "(found: PiperTTS, ChatterboxTTS) -- the Wave-1 FishTTS engine is not here yet"
        )

    factory = getattr(tts_module, "create_fish_tts_engine", None)
    try:
        if callable(factory):
            engine = factory()
        else:
            engine = tts_module.create_tts_engine(
                engine="fish",
                enabled=True,
                voice=getattr(ctx.cfg, "fish_voice_id", None),
            )
    except Exception as exc:
        return None, f"could not build the Fish engine ({type(exc).__name__}: {exc})"

    if "fish" not in type(engine).__name__.casefold():
        return None, (
            f"create_tts_engine(engine='fish') returned {type(engine).__name__}; refusing to measure "
            "barge-in against the wrong engine"
        )
    return engine, ""


def measure_barge_in_latency(ctx: Ctx) -> Measurement:
    blockers: list[str] = []
    blocker = _fish_credential_blocker(ctx)
    if blocker is not None:
        blockers.append(blocker.reason)
    tts_blocker = _tts_blocker(ctx)
    if tts_blocker is not None:
        blockers.append(tts_blocker.reason)
    if ctx.streaming_engine is None:
        blockers.append(
            "no streaming recogniser: "
            + (ctx.streaming_engine_error or "streaming_stt_engine is not configured/present")
        )
    if ctx.barge_in_wav is None or not Path(ctx.barge_in_wav).is_file():
        blockers.append(
            f"no barge-in wav (--barge-in-wav): {ctx.barge_in_wav or '<unset>'}"
        )
    if blockers:
        raise MeasurementUnavailable(
            "barge-in needs Fish playback, the local streaming recogniser and a barge-in wav; missing: "
            + "; ".join(blockers),
            fix=(
                "set fish_key_file, land the Fish engine (src/claire/output/tts.py), provide a sherpa "
                "streaming model, and pass --barge-in-wav PATH"
            ),
        )

    sample_rate = int(getattr(ctx.cfg, "sample_rate", 16000) or 16000)
    reader = ctx.audio_reader or read_wav_mono
    audio, sample_rate = reader(Path(ctx.barge_in_wav), sample_rate)
    frame_len = max(1, int(round(sample_rate * ctx.frame_ms / 1000.0)))
    wake = build_wake_matcher(ctx)
    stop = build_stop_matcher(ctx)
    tts = ctx.tts_engine
    engine = ctx.streaming_engine

    values: list[float] = []
    for _ in range(max(1, int(ctx.barge_in_runs))):
        engine.reset()
        tts.interrupt()
        tts.speak(ctx.barge_in_text)
        deadline = ctx.clock() + ctx.barge_in_speak_timeout_sec
        while not bool(tts.is_speaking()) and ctx.clock() < deadline:
            ctx.sleep(ctx.barge_in_poll_sec)
        if not bool(tts.is_speaking()):
            raise MeasurementUnavailable(
                f"TTS never reported speaking within {ctx.barge_in_speak_timeout_sec}s "
                f"({type(tts).__name__}); is an audio output device available?",
                fix="check the output device and that the Fish engine is enabled",
            )

        barge_start: float | None = None
        t_detected: float | None = None
        import numpy as np

        for i in range(int(audio.size // frame_len)):
            frame = np.ascontiguousarray(audio[i * frame_len:(i + 1) * frame_len])
            rms = float(np.sqrt(float(np.mean(frame.astype(np.float64) ** 2))))
            engine.accept_waveform(frame, sample_rate)
            t_done = ctx.clock()
            if barge_start is None and rms >= ctx.speech_rms_threshold:
                barge_start = t_done
            if barge_start is None:
                continue
            text = str(engine.partial_text() or "").casefold()
            if text and (stop(text) or wake(text)):
                t_detected = t_done
                break
            ctx.sleep(ctx.frame_ms / 1000.0)

        if t_detected is None or barge_start is None:
            raise MeasurementUnavailable(
                f"no stop/wake phrase was recognised in the barge-in wav {ctx.barge_in_wav}",
                fix="use a barge-in wav that clearly says 'stop' (or the wake word)",
            )

        tts.interrupt()
        deadline = ctx.clock() + ctx.barge_in_stop_timeout_sec
        while bool(tts.is_speaking()) and ctx.clock() < deadline:
            ctx.sleep(ctx.barge_in_poll_sec)
        if bool(tts.is_speaking()):
            raise MeasurementUnavailable(
                f"TTS was still speaking {ctx.barge_in_stop_timeout_sec}s after interrupt()",
                fix="check the Fish engine's interrupt() path; no number is invented here",
            )
        values.append(ctx.clock() - barge_start)

    return measured(
        "barge_in_latency",
        "barge-in latency",
        BARGE_IN_METHOD,
        statistics.median(values),
        unit="seconds",
        detail=(
            f"median of {len(values)} run(s); from the first voiced frame of {ctx.barge_in_wav} to "
            f"playback stopped ({type(tts).__name__})"
        ),
        raw={
            "samples_sec": values,
            "barge_in_wav": str(ctx.barge_in_wav),
            "text_spoken": ctx.barge_in_text,
            "frame_ms": ctx.frame_ms,
            "engine": getattr(engine, "name", type(engine).__name__),
            "tts_engine": type(tts).__name__,
        },
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
#: The method string is reported even when the measurement was not taken, so a
#: reader always knows what would have been timed.
METHOD_BY_KEY: dict[str, str] = {
    "wake_latency": WAKE_METHOD,
    "endpoint_latency": ENDPOINT_METHOD,
    "first_audio_latency": FIRST_AUDIO_METHOD,
    "barge_in_latency": BARGE_IN_METHOD,
    "wake_false_positive_rate": WAKE_FP_METHOD,
}

_TABLE: tuple[tuple[str, str, Callable[[Ctx], Measurement]], ...] = (
    ("wake_latency", "wake latency", measure_wake_latency),
    ("endpoint_latency", "endpoint latency", measure_endpoint_latency),
    ("first_audio_latency", "first-audio latency (Fish)", measure_first_audio_latency),
    ("barge_in_latency", "barge-in latency", measure_barge_in_latency),
    ("wake_false_positive_rate", "wake false-positive rate", measure_wake_false_positive_rate),
)


def normalise_key(text: str) -> str:
    return (text or "").strip().lower().replace("-", "_").replace(" ", "_")


TITLE_BY_KEY: dict[str, str] = {key: title for key, title, _ in _TABLE}

#: Measurements that drive native code (sherpa-onnx) or the audio device. A
#: native ``LOG(FATAL)`` aborts the whole process and cannot be caught in
#: Python, so these run in a child process by default: a crash then becomes an
#: honest "not measured" for that one number instead of losing the report.
ISOLATED_KEYS: frozenset[str] = frozenset({"endpoint_latency", "barge_in_latency"})


def dispatch_measurement(key: str, ctx: Ctx) -> Measurement:
    for candidate, title, fn in _TABLE:
        if candidate == key:
            method = METHOD_BY_KEY.get(key, "")
            try:
                return fn(ctx)
            except MeasurementUnavailable as exc:
                return not_measured(key, title, method, exc)
            except Exception as exc:  # never fabricated, never fatal for the others
                return not_measured(key, title, method, exc)
    raise HarnessError(f"unknown measurement key {key!r}")


def _child_args(raw_argv: Sequence[str], key: str) -> list[str]:
    executable = sys.executable or "python3"
    return [executable, str(Path(__file__).resolve()), *raw_argv, "--internal-measurement", key]


def isolated_measurement(
    key: str,
    raw_argv: Sequence[str],
    *,
    timeout: float,
    runner: Callable[..., Any] | None = None,
) -> Measurement:
    """Take one native-driving measurement in a child process.

    The child prints exactly one measurement as JSON on its last stdout line.
    If it aborts (exit 255 with a native message on stderr), times out, or
    prints nothing usable, that is reported as *not measured* naming the child's
    exit code and stderr tail -- never as a number.
    """
    import subprocess

    title = TITLE_BY_KEY.get(key, key)
    method = METHOD_BY_KEY.get(key, "")
    argv = _child_args(raw_argv, key)
    runner = runner or subprocess.run
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # TimeoutExpired and anything else
        if type(exc).__name__ == "TimeoutExpired":
            return not_measured(
                key,
                title,
                method,
                MeasurementUnavailable(
                    f"the {key!r} measurement child process exceeded {timeout:g}s and was abandoned",
                    fix="raise --isolate-timeout, or shorten the wav / reduce --endpoint-runs",
                ),
            )
        reason = f"the {key!r} child process could not be run: {type(exc).__name__}: {exc}"
        return not_measured(
            key,
            title,
            method,
            MeasurementUnavailable(
                reason,
                fix="check that this script can be run with this interpreter (sys.executable)",
            ),
        )

    stdout = getattr(proc, "stdout", "") or ""
    stderr = getattr(proc, "stderr", "") or ""
    returncode = getattr(proc, "returncode", None)
    payload = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            break

    if payload and isinstance(payload, dict) and payload.get("key") == key:
        try:
            return Measurement(
                key=key,
                title=_scrub(payload.get("title", title)),
                method=_scrub(payload.get("method", method)),
                status=payload.get("status", STATUS_NOT_MEASURED),
                value=payload.get("value"),
                unit=payload.get("unit", "seconds"),
                detail=_scrub(payload.get("detail", "")),
                fix=_scrub(payload.get("fix", "")),
                raw=_scrub_obj(payload.get("raw", {}) or {}),
            )
        except Exception:
            pass

    stderr_tail = " | ".join(stderr.strip().splitlines()[-3:])[:400]
    reason = (
        f"the {key!r} measurement child process ended without a usable report "
        f"(exit code {returncode}); a native library abort is not catchable in-process"
    )
    if stderr_tail:
        reason += f"; stderr: {stderr_tail}"
    if "features.cc" in stderr_tail or "GetFrames" in stderr_tail:
        fix = (
            "the native streaming encoder aborted on chunks smaller than its ~390 ms chunk "
            "(sherpa-onnx calls decode_stream before the stream is ready); re-run with "
            "--frame-ms 400 to take the number today, and fix "
            "src/claire/listening/stt/sherpa_streaming.py to guard decode_stream with "
            "recognizer.is_ready(stream)"
        )
    else:
        fix = (
            "fix the native streaming stack (src/claire/listening/stt/sherpa_streaming.py / the "
            "sherpa-onnx model) and re-run; the number is not invented here"
        )
    return not_measured(key, title, method, MeasurementUnavailable(reason, fix=fix))


def run_measurements(
    ctx: Ctx,
    keys: Sequence[str],
    *,
    isolate_keys: Iterable[str] = (),
    raw_argv: Sequence[str] | None = None,
    isolate_timeout: float = 900.0,
    runner: Callable[..., Any] | None = None,
) -> list[Measurement]:
    wanted = [normalise_key(k) for k in keys]
    known = {key for key, _, _ in _TABLE}
    unknown = [k for k in wanted if k not in known]
    if unknown:
        raise HarnessError(f"unknown measurement key(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}")

    isolated = {normalise_key(k) for k in isolate_keys}
    results: list[Measurement] = []
    for key, title, fn in _TABLE:
        if key not in wanted:
            continue
        if key in isolated and raw_argv is not None:
            results.append(isolated_measurement(key, raw_argv, timeout=isolate_timeout, runner=runner))
            continue
        results.append(dispatch_measurement(key, ctx))
    return results


@dataclass
class Report:
    measurements: list[Measurement]
    host: str
    repo_root: str
    when_utc: str
    python: str
    platform: str

    @property
    def not_measured(self) -> list[str]:
        return [m.key for m in self.measurements if not m.measured]

    @property
    def exit_code(self) -> int:
        return EXIT_OK if not self.not_measured else EXIT_NOT_MEASURED

    def to_json(self) -> dict[str, Any]:
        return _scrub_obj(
            _jsonable(
                {
                    "schema": SCHEMA,
                    "host": self.host,
                    "platform": self.platform,
                    "python": self.python,
                    "when_utc": self.when_utc,
                    "repo_root": self.repo_root,
                    "measurements": [m.to_json() for m in self.measurements],
                    "not_measured": self.not_measured,
                    "all_measured": not self.not_measured,
                    "exit_code": self.exit_code,
                }
            )
        )

    def render_table(self) -> str:
        lines: list[str] = []
        lines.append(
            f"Claire voice-latency harness ({SCHEMA}) -- host={self.host}, python={self.python}"
        )
        lines.append(f"taken {self.when_utc} UTC, repo={self.repo_root}")
        lines.append("")
        width = max(len(m.title) for m in self.measurements) if self.measurements else 20
        lines.append(f"  {'MEASUREMENT':<{width}}  {'STATUS':<12}  {'VALUE':>12}  {'UNIT':<6}")
        lines.append(f"  {'-' * width}  {'-' * 12}  {'-' * 12}  {'-' * 6}")
        for m in self.measurements:
            if m.measured:
                value = f"{m.value:.6f}" if m.unit == "seconds" else f"{m.value:.4f}"
                status = "measured"
            else:
                value = "--"
                status = "NOT MEASURED"
            lines.append(
                f"  {m.title:<{width}}  {status:<12}  {value:>12}  {m.unit:<6}"
            )
        lines.append("")
        lines.append("Methods (what was timed, from which event to which)")
        for m in self.measurements:
            lines.append(f"  [{m.key}] {m.method or '<no method: measurement not taken>'}")
        lines.append("")
        lines.append("Raw values")
        for m in self.measurements:
            if m.raw:
                lines.append(f"  {m.key}: {json.dumps(_scrub_obj(_jsonable(m.raw)), sort_keys=True)}")
            else:
                lines.append(f"  {m.key}: <none>")
        lines.append("")
        unmeasured = [m for m in self.measurements if not m.measured]
        if unmeasured:
            lines.append("NOT MEASURED (no number invented -- N10)")
            for m in unmeasured:
                lines.append(f"  {m.key}: {m.detail}")
                if m.fix:
                    lines.append(f"      fix: {m.fix}")
            lines.append("")
            lines.append(
                f"exit status {self.exit_code}: {len(unmeasured)} of {len(self.measurements)} "
                "measurement(s) not taken"
            )
        else:
            lines.append(f"exit status {self.exit_code}: all {len(self.measurements)} measurements taken")
        return _scrub("\n".join(lines))


# --------------------------------------------------------------------------
# Configuration / CLI
# --------------------------------------------------------------------------
def _default_settings_dict() -> dict[str, Any]:
    try:
        from claire import config as cfg_mod

        return dict(cfg_mod.get_default_config())
    except Exception:
        return {}


def load_harness_config(*, use_config: bool) -> tuple[Any, dict[str, Any]]:
    """Return (settings object, plain default dict) without ever failing hard."""
    defaults = _default_settings_dict()
    if not use_config:
        return None, defaults
    try:
        from claire import config as cfg_mod

        return cfg_mod.load_settings(), defaults
    except Exception as exc:
        print(f"[harness] config could not be loaded ({type(exc).__name__}: {exc}); using defaults", file=sys.stderr)
        return None, defaults


def _cfg(cfg: Any, defaults: dict[str, Any], key: str, fallback: Any = None) -> Any:
    if cfg is not None and hasattr(cfg, key):
        value = getattr(cfg, key)
        if value is not None:
            return value
    return defaults.get(key, fallback)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="measure_voice_latency.py",
        description=(
            "Measure Claire's five voice-pipeline latency numbers. A measurement that cannot be taken "
            "is reported as 'not measured' with the missing path/backend and a fix, and the process "
            "exits 3 -- no number is ever invented (N10)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "measurement keys: " + ", ".join(MEASUREMENT_KEYS) + "\n"
            "exit status: 0 all requested measurements taken; 3 some not taken; 1 fatal harness error"
        ),
    )
    p.add_argument("--json", action="store_true", help="print one JSON report object on stdout (table goes to stderr)")
    p.add_argument("--quiet", action="store_true", help="do not print the human table at all")
    p.add_argument("--offline", action="store_true", help="never touch the network: Fish measurements are reported as not measured")
    p.add_argument("--skip", default="", help="comma-separated measurement keys to leave out entirely")
    p.add_argument("--only", default="", help="comma-separated measurement keys to take (default: all five)")
    p.add_argument("--no-config", action="store_true", help="ignore config.json; use built-in defaults")
    p.add_argument(
        "--no-isolate",
        action="store_true",
        help=(
            "run the native-driving measurements (endpoint, barge-in) in this process instead of a child "
            "process; a native abort then kills the whole report"
        ),
    )
    p.add_argument("--isolate-timeout", type=float, default=900.0, help="seconds before a child measurement is abandoned (default 900)")
    p.add_argument(
        "--internal-measurement",
        default=None,
        help=argparse.SUPPRESS,  # child-process mode: run one measurement, print it as JSON
    )

    w = p.add_argument_group("wake detection (no audio needed)")
    w.add_argument("--wake-word", default=None, help="wake name (default: config wake_word, else 'claire')")
    w.add_argument("--wake-aliases", default=None, help="comma-separated aliases (default: config wake_aliases)")
    w.add_argument("--wake-fuzzy-ratio", type=float, default=None, help="fuzzy ratio (default: config wake_fuzzy_ratio)")
    w.add_argument("--wake-words-file", default=None, help="file with one wake phrase per line (true positives)")
    w.add_argument("--negative-words-file", default=None, help="file with one non-wake phrase per line")
    w.add_argument("--generate-negative-words", type=int, default=None, help="size of the generated non-wake list (default 40)")
    w.add_argument("--seed", type=int, default=7, help="seed for the generated list (default 7)")
    w.add_argument("--dump-word-lists", default=None, help="directory: write the exact lists used as wake_words.txt / negative_words.txt")

    e = p.add_argument_group("endpoint latency (needs a wav + the local streaming model)")
    e.add_argument("--wav", default=None, help="real 16 kHz mono wav of speech (default: search fixtures, then $CLAIRE_LATENCY_WAV)")
    e.add_argument(
        "--streaming-model-dir",
        default=None,
        help="streaming zipformer bundle to use instead of config streaming_stt_model_dir (for a model that is present on this machine)",
    )
    e.add_argument("--frame-ms", type=int, default=None, help="frame size fed to the recogniser (default: config vad_frame_ms, else 20)")
    e.add_argument("--endpoint-silence-ms", type=int, default=None, help="trailing silence that ends the utterance (default: config endpoint_silence_ms)")
    e.add_argument("--endpoint-runs", type=int, default=None, help="runs to median over (default 3)")
    e.add_argument("--unpaced", action="store_true", help="do not sleep between frames (measures compute only)")
    e.add_argument("--speech-rms-threshold", type=float, default=0.01, help="RMS above which a frame counts as voiced (default 0.01)")

    f = p.add_argument_group("Fish Audio (credentials + network)")
    f.add_argument("--fish-key-file", default=None, help="path to the 0600 key file (default: config fish_key_file); value is never printed")
    f.add_argument("--fish-text", default=None, help="text to synthesise for the first-audio measurement")
    f.add_argument("--fish-runs", type=int, default=None, help="runs to median over (default 3)")
    f.add_argument(
        "--fish-probe-module",
        default=None,
        help="'module:function' used instead of fish_audio_sdk; called as fn(text, runs, key_file) -> list of seconds",
    )
    f.add_argument("--barge-in-wav", default=None, help="wav of a person saying 'stop' over the TTS playback")
    f.add_argument("--barge-in-runs", type=int, default=None, help="runs to median over (default 1)")
    return p


def build_ctx(args: argparse.Namespace, cfg: Any, defaults: dict[str, Any]) -> Ctx:
    wake_word = args.wake_word or str(_cfg(cfg, defaults, "wake_word", "claire"))
    if args.wake_aliases is not None:
        aliases = tuple(a.strip() for a in args.wake_aliases.split(",") if a.strip())
    else:
        aliases = tuple(_cfg(cfg, defaults, "wake_aliases", []) or [])
    fuzzy = args.wake_fuzzy_ratio if args.wake_fuzzy_ratio is not None else float(
        _cfg(cfg, defaults, "wake_fuzzy_ratio", 0.78)
    )
    stop_commands = tuple(defaults.get("stop_commands", DEFAULT_STOP_COMMANDS))
    stop_ratio = float(defaults.get("stop_command_fuzzy_ratio", DEFAULT_STOP_FUZZY_RATIO))

    wake_words = read_word_list(Path(args.wake_words_file), what="wake") if args.wake_words_file else ()
    negative_words: tuple[str, ...] = ()
    generated = False
    if args.negative_words_file:
        negative_words = read_word_list(Path(args.negative_words_file), what="negative")
    else:
        size = args.generate_negative_words if args.generate_negative_words is not None else 40
        negative_words = generate_negative_words(wake_word, size, seed=args.seed, aliases=aliases)
        generated = True

    fish_key_file = None
    raw_key = args.fish_key_file or _cfg(cfg, defaults, "fish_key_file", None)
    if raw_key:
        fish_key_file = Path(str(raw_key)).expanduser()

    frame_ms = args.frame_ms if args.frame_ms is not None else int(_cfg(cfg, defaults, "vad_frame_ms", 20))
    silence_ms = (
        args.endpoint_silence_ms
        if args.endpoint_silence_ms is not None
        else int(_cfg(cfg, defaults, "endpoint_silence_ms", 800))
    )

    # A config object is always available, so cfg reads are well defined with
    # --no-config too; --streaming-model-dir overlays the model path without
    # touching the user's config.json.
    engine_cfg: Any = cfg if cfg is not None else _SimpleCfg(defaults)
    if args.streaming_model_dir:
        engine_cfg = CfgOverlay(
            engine_cfg,
            streaming_stt_model_dir=str(Path(args.streaming_model_dir).expanduser()),
        )

    ctx = Ctx(
        cfg=engine_cfg,
        wake_word=wake_word,
        wake_aliases=aliases,
        wake_fuzzy_ratio=fuzzy,
        stop_commands=stop_commands,
        stop_fuzzy_ratio=stop_ratio,
        wake_words=wake_words,
        negative_words=negative_words,
        negative_words_generated=generated,
        negative_seed=args.seed,
        wav_path=Path(args.wav).expanduser() if args.wav else None,
        frame_ms=max(1, int(frame_ms)),
        endpoint_silence_ms=max(1, int(silence_ms)),
        speech_rms_threshold=float(args.speech_rms_threshold),
        endpoint_runs=args.endpoint_runs if args.endpoint_runs is not None else 3,
        pace=not args.unpaced,
        fish_key_file=fish_key_file,
        fish_text=args.fish_text or Ctx.fish_text,
        fish_runs=args.fish_runs if args.fish_runs is not None else 3,
        fish_probe_error="",
        offline=bool(args.offline),
        barge_in_wav=Path(args.barge_in_wav).expanduser() if args.barge_in_wav else None,
        barge_in_runs=args.barge_in_runs if args.barge_in_runs is not None else 1,
    )

    if args.fish_probe_module:
        try:
            ctx.fish_probe = load_probe_from_module(args.fish_probe_module)
        except MeasurementUnavailable as exc:
            ctx.fish_probe = None
            ctx.fish_probe_error = exc.reason

    # Resolve the local streaming recogniser once, honestly. When the
    # measurement will run in a child process, the parent does not load the
    # native model at all: a LOG(FATAL) during load would otherwise kill the
    # parent before the child could report.
    if (
        _wants(ctx, args, "endpoint_latency") and not _will_isolate(args, "endpoint_latency")
    ) or (
        _wants(ctx, args, "barge_in_latency") and not _will_isolate(args, "barge_in_latency")
    ):
        engine, err = try_create_streaming_engine(engine_cfg, defaults)
        ctx.streaming_engine = engine
        ctx.streaming_engine_error = err

    # Resolve the app's Fish TTS engine for barge-in, refusing the wrong engine.
    if _wants(ctx, args, "barge_in_latency") and not _will_isolate(args, "barge_in_latency"):
        engine, err = seek_default_tts_engine(ctx)
        ctx.tts_engine = engine
        ctx.tts_engine_error = err

    return ctx


def requested_keys(args: argparse.Namespace) -> list[str]:
    if args.only.strip():
        keys = [normalise_key(k) for k in args.only.split(",") if k.strip()]
    else:
        keys = list(MEASUREMENT_KEYS)
    skip = {normalise_key(k) for k in args.skip.split(",") if k.strip()}
    return [k for k in keys if k not in skip]


def _wants(ctx: Ctx, args: argparse.Namespace, key: str) -> bool:
    return key in requested_keys(args)


def _will_isolate(args: argparse.Namespace, key: str) -> bool:
    """True when this key will run in a child process (so the parent need not load it)."""
    if getattr(args, "internal_measurement", None):
        return False
    return key in ISOLATED_KEYS and not bool(getattr(args, "no_isolate", False))


def try_create_streaming_engine(cfg: Any, defaults: dict[str, Any]) -> tuple[Any, str]:
    """Create the real streaming engine, or return (None, exact reason)."""
    if cfg is None:
        cfg = _SimpleCfg(defaults)
    try:
        from claire.listening.stt import create_streaming_stt_engine
    except Exception as exc:
        return None, f"claire.listening.stt is not importable ({type(exc).__name__}: {exc})"
    try:
        engine = create_streaming_stt_engine(cfg)
    except ModuleNotFoundError as exc:
        return None, (
            f"streaming engine module missing ({exc}); create_streaming_stt_engine imported "
            f"{exc.name!r} -- implement src/claire/listening/stt/sherpa_streaming.py"
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if engine is None:
        return None, (
            "streaming_stt_engine is disabled/empty in the configuration, so there is no endpoint "
            "detector to time"
        )
    return engine, ""


class _SimpleCfg:
    """Config stand-in built from the plain default dict (used with --no-config)."""

    def __init__(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            setattr(self, key, value)


class CfgOverlay:
    """A config object with a few fields overridden on top of another one.

    Used by ``--streaming-model-dir`` so the harness can time a model that is
    present on the machine without editing the user's config.json.
    """

    def __init__(self, base: Any, **overrides: Any) -> None:
        self._base = base
        self._overrides = dict(overrides)

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        base = self._base
        if base is not None:
            return getattr(base, name)
        raise AttributeError(name)

    def overlay(self, **overrides: Any) -> "CfgOverlay":
        merged = dict(self._overrides)
        merged.update(overrides)
        return CfgOverlay(self._base, **merged)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(raw_argv)

    try:
        keys = requested_keys(args)
    except Exception as exc:
        print(f"[harness] fatal: {exc}", file=sys.stderr)
        return EXIT_FATAL
    if not keys:
        print("[harness] fatal: no measurements requested (--only/--skip left nothing)", file=sys.stderr)
        return EXIT_FATAL
    unknown = [k for k in keys if k not in {key for key, _, _ in _TABLE}]
    if unknown:
        print(
            f"[harness] fatal: unknown measurement key(s): {', '.join(unknown)}; known: "
            + ", ".join(MEASUREMENT_KEYS),
            file=sys.stderr,
        )
        return EXIT_FATAL

    try:
        cfg, defaults = load_harness_config(use_config=not args.no_config)
        ctx = build_ctx(args, cfg, defaults)
    except MeasurementUnavailable as exc:
        print(f"[harness] fatal: {exc.reason}", file=sys.stderr)
        if exc.fix:
            print(f"[harness] fix: {exc.fix}", file=sys.stderr)
        return EXIT_FATAL
    except Exception as exc:
        print(f"[harness] fatal: {type(exc).__name__}: {_scrub(str(exc))}", file=sys.stderr)
        return EXIT_FATAL

    if args.dump_word_lists:
        dump_dir = Path(args.dump_word_lists).expanduser()
        dump_dir.mkdir(parents=True, exist_ok=True)
        wake_list = ctx.wake_words or default_wake_phrases(ctx.wake_word, ctx.wake_aliases)
        (dump_dir / "wake_words.txt").write_text("\n".join(wake_list) + "\n", encoding="utf-8")
        (dump_dir / "negative_words.txt").write_text("\n".join(ctx.negative_words) + "\n", encoding="utf-8")

    # Child-process mode: run exactly one measurement and print it as JSON, so a
    # native abort in the parent's view is just a failed child, not a lost report.
    if args.internal_measurement:
        key = normalise_key(args.internal_measurement)
        if key not in {k for k, _, _ in _TABLE}:
            print(f"[harness] fatal: unknown measurement key {key!r}", file=sys.stderr)
            return EXIT_FATAL
        measurement = dispatch_measurement(key, ctx)
        print(json.dumps(measurement.to_json(), sort_keys=True))
        return EXIT_OK if measurement.measured else EXIT_NOT_MEASURED

    isolate_keys = () if args.no_isolate else ISOLATED_KEYS
    measurements = run_measurements(
        ctx,
        keys,
        isolate_keys=isolate_keys,
        raw_argv=raw_argv,
        isolate_timeout=float(args.isolate_timeout),
    )

    import datetime as _dt

    report = Report(
        measurements=measurements,
        host=platform.node() or "unknown",
        repo_root=str(REPO_ROOT),
        when_utc=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        python=platform.python_version(),
        platform=platform.platform(),
    )

    table = report.render_table()
    if args.json:
        print(json.dumps(report.to_json(), sort_keys=True))
        if not args.quiet:
            print(_scrub(table), file=sys.stderr)
    elif not args.quiet:
        print(_scrub(table))
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
