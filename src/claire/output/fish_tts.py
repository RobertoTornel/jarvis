"""Fish Audio TTS engine: sentence-pipelined synthesis feeding a streaming player.

:mod:`claire.output.fish_client` already owns the wire call, the key rotation and
the timeouts. This module is the layer above it and owns exactly four things:

* chopping the reply into sentences so synthesis of sentence N+1 overlaps the
  playback of sentence N (the round trip is otherwise audible as a gap at every
  full stop);
* releasing that audio in order, through one queue, with a byte ceiling so a
  long reply is never assembled entirely in memory before it starts speaking;
* deciding the sample rate from what the response actually declares (WAV header)
  or from what we asked for (headerless PCM), never from a guess;
* barge-in: :meth:`FishTTS.flush` drops the queued audio and aborts the device
  immediately, so an interruption is heard in tens of milliseconds rather than
  after the current sentence.

The public surface mirrors :class:`claire.output.tts.PiperTTS`
(``start``/``stop``/``speak``/``interrupt``/``is_speaking``/
``get_last_spoken_text``) plus the ``flush`` this engine adds for barge-in, so it
can be dropped in wherever a TTS engine is expected.

Failure policy (rule N10): a missing or empty key file, or a client that cannot
be built, raises the named error from :mod:`~claire.output.fish_client` with the
key-file path in the message. This engine never reports speech it did not play.
"""

from __future__ import annotations

import io
import queue
import re
import threading
import time
import wave
from typing import Any, Callable, List, Optional

from ..debug import debug_log

# Imported at module scope for the error *classes* only. Nothing here constructs
# a FishClient or a FishKeyPool: the key file is not read and no socket is opened
# until start()/speak() call _ensure_client(). Importing this module therefore
# has no network, filesystem or audio side effect.
from .fish_client import (
    FishClient,
    FishConfigError,
    FishEmptyStreamError,
    FishError,
    FishKeyPool,
    FishUnsupportedAudioFormatError,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Fish's conversational latency mode. See MECHANISM 7 at __init__.
DEFAULT_LATENCY = "low"

#: Fish model id used when the config does not name one.
DEFAULT_MODEL = "s1"

#: Where the key file lives when the config does not say. Kept equal to the
#: default in :mod:`claire.config` — a portable ``~`` path, expanded at use
#: time by ``FishKeyPool`` — so the named error can quote a real path.
DEFAULT_KEY_FILE = "~/.config/claire/fish-keys.txt"

#: BACKPRESSURE CEILING (MECHANISM 2). How many bytes of not-yet-played audio may
#: sit in the queue ahead of the player. Exposed as a module constant because it
#: is a policy number, not an implementation detail.
#:
#: Chosen at 512 KiB: about 5.9 s of 16-bit 44.1 kHz mono. That is comfortably
#: more than one network round trip (so sentence N+1 is genuinely synthesised
#: while N is still being heard) and still small enough that a barge-in discards
#: at most half a megabyte and a reply can never be materialised in full before
#: the first word is audible.
MAX_QUEUED_AUDIO_BYTES = 512 * 1024

#: Rates Fish Audio is documented to return. 48000 is deliberately absent.
SUPPORTED_SAMPLE_RATES = (8000, 16000, 24000, 32000, 44100)

#: The rate used whenever the answer does not carry a usable declaration.
#: 44100 rather than 48000: Fish does not support 48000, and a device told to
#: play a 48000 stream that was actually encoded at 44100 comes out pitched down.
DEFAULT_SAMPLE_RATE = 44100
UNSUPPORTED_SAMPLE_RATE = 48000

#: A response beginning with this is a RIFF/WAVE container.
_RIFF_MAGIC = b"RIFF"

#: Upper bound on header sniffing. A malformed header must not stall the
#: utterance for ever: past this many bytes the stream is treated as headerless.
MAX_WAV_HEADER_BYTES = 64 * 1024

#: The client's own budgets (it enforces the 8 s first-chunk deadline and the
#: total cap; this module must not weaken them).
FIRST_CHUNK_TIMEOUT = 8.0
TOTAL_TIMEOUT = 120.0

#: How long _play_loop waits on the queue before re-checking whether it is idle.
_IDLE_POLL_SECONDS = 0.05

#: The one PCM format the output stream is opened with, and therefore the only
#: format that may ever be written to it: signed 16-bit little-endian, one
#: channel. This is a *contract*, not a preference. The format layer converts
#: every payload to it, because a response that declares something else and is
#: written verbatim is not merely lossy - int16 bytes read as float32, float32
#: read as int16, or an MP3 body read as PCM all come out as a full-scale burst.
PLAYBACK_CHANNELS = 1
PLAYBACK_SAMPLE_WIDTH = 2
PLAYBACK_FRAME_BYTES = PLAYBACK_CHANNELS * PLAYBACK_SAMPLE_WIDTH

#: Fewest bytes needed before a body can be classified. Below this a short
#: prefix is ambiguous (``b"\\xff"`` may be the start of an MP3 frame, ``b"RIF"``
#: the start of a WAV), so the bytes are held rather than guessed at.
MIN_FORMAT_SNIFF_BYTES = 4

#: Leading bytes that positively identify a container or codec this layer cannot
#: render, mapped to the name used in the error. Byte magic only, so the check
#: is independent of any language the assistant happens to speak.
#:
#: Played as int16 PCM these are not speech at any volume - measured, an MP3
#: body reads back with a peak of 32768/32768 (0.0 dBFS) and an RMS about 27 dB
#: above her normal speech. Identifying them is what turns that burst into a
#: named failure.
_NON_PCM_MAGIC = (
    (b"ID3", "MP3 with an ID3 tag"),
    (b"\xff\xfb", "MP3"),
    (b"\xff\xfa", "MP3"),
    (b"\xff\xf3", "MP3"),
    (b"\xff\xf2", "MP3"),
    (b"OggS", "Ogg"),
    (b"fLaC", "FLAC"),
    (b"ADIF", "AAC/ADIF"),
    (b"\x1f\x8b", "gzip"),
    (b"<!DO", "an HTML page"),
    (b"<?xm", "an XML document"),
    (b'{"', "a JSON body"),
)

#: Ceiling applied to every block by the device player, in int16 units.
#: -3 dBFS: far above her own speech (measured peak -15 dBFS) and the thinking
#: pad (-8 dBFS), so normal audio is untouched, but nothing that reaches the
#: device can be a full-scale burst. This is a backstop for the format gate
#: above, not the fix: the fix is that unrecognised bytes are never played.
_PEAK_CEILING = 23197  # int(10 ** (-3 / 20) * 32767)


#: How long stop() waits for each worker thread before giving up on it (the
#: threads are daemons, so a worker stuck in a slow network read cannot hold the
#: process open).
_FLUSH_JOIN_SECONDS = 2.0

#: Sentence shaping. Every full stop is a cut — a short sentence is a *feature*
#: for latency, since its audio starts sooner. Only a very short trailing
#: fragment with no terminal punctuation of its own is folded back into the
#: previous sentence. An over-long sentence is hard-split so its first audio does
#: not wait on the whole paragraph being synthesised.
MIN_SENTENCE_CHARS = 12
MAX_SENTENCE_CHARS = 320

_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
        "fig", "no", "vol", "approx", "dept", "inc", "ltd", "est", "cf",
        "e.g", "i.e", "al", "p.s", "a.m", "p.m",
    }
)

#: Ends a sentence: terminal punctuation, optional closing quotes/brackets,
#: then whitespace (or end of text). A decimal point is safe here because the
#: character after "3." is a digit, not whitespace.
_SENTENCE_BOUNDARY_RE = re.compile(r"([.!?\u2026]+)([\"'\u201d\u2019)\]]*)(\s+|$)")


class _Flushed(Exception):
    """Internal: this utterance was flushed; stop synthesising it now."""


# --------------------------------------------------------------------------
# Sample rate / format handling
# --------------------------------------------------------------------------

def _coerce_sample_rate(declared: Any) -> int:
    """Return the rate to hand the device for a ``declared`` response rate.

    The response is the only authority on its own rate, so a supported declared
    rate is returned unchanged. Anything else (48000, a bogus 0, a non-integer)
    falls back to 44100: Fish does not support 48000, and playing a stream at the
    wrong rate is audibly wrong (pitched, wrong tempo) rather than merely lossy.
    """
    try:
        rate = int(declared)
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_RATE
    if rate in SUPPORTED_SAMPLE_RATES:
        return rate
    if rate == UNSUPPORTED_SAMPLE_RATE:
        debug_log(
            "Fish TTS: 48000Hz is not a Fish output rate; playing at "
            f"{DEFAULT_SAMPLE_RATE}Hz instead",
            "tts",
        )
        return DEFAULT_SAMPLE_RATE
    debug_log(
        f"Fish TTS: declared rate {rate!r}Hz is not a Fish rate "
        f"{SUPPORTED_SAMPLE_RATES}; using {DEFAULT_SAMPLE_RATE}Hz",
        "tts",
    )
    return DEFAULT_SAMPLE_RATE


def _riff_data_offset(buf: bytes) -> Optional[int]:
    """Byte offset of the first PCM frame in a RIFF/WAVE buffer, else None.

    ``wave`` parses WAVE headers but exposes no public "where does the audio
    start" accessor, so the chunk walk is done here; ``wave`` remains the
    authority on the *parameters* (see :func:`_parse_wav_header`). Returning
    None means "not a container I can stream", and the caller then treats the
    bytes as headerless PCM rather than dropping them.
    """
    if len(buf) < 12 or buf[:4] != _RIFF_MAGIC or buf[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(buf):
        chunk_id = buf[pos:pos + 4]
        size = int.from_bytes(buf[pos + 4:pos + 8], "little")
        if chunk_id == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)  # chunks are word-aligned
    return None


def _parse_wav_header(buf: bytes) -> Optional[tuple]:
    """``(sample_rate, channels, sample_width, comptype)`` from a WAV header.

    Returns None when the bytes are not a WAVE the stdlib :mod:`wave` will
    accept — which includes a compressed or IEEE-float container, since this
    build has no decoder for one and must never pretend the payload is PCM.

    Uses :mod:`wave` over a :class:`io.BytesIO`. The header need only be
    complete up to the start of the ``data`` chunk — ``wave`` does not read the
    payload at open time — so this works on a partially arrived stream.
    """
    try:
        reader = wave.open(io.BytesIO(buf), "rb")
    except (wave.Error, EOFError, OSError, ValueError):
        return None
    try:
        return (
            int(reader.getframerate()),
            int(reader.getnchannels()),
            int(reader.getsampwidth()),
            str(reader.getcomptype()),
        )
    except (wave.Error, EOFError, OSError):
        return None
    finally:
        try:
            reader.close()
        except Exception:
            pass


def _to_playback_pcm(data: bytes, channels: int, sample_width: int) -> bytes:
    """Convert whole frames of ``data`` to signed 16-bit little-endian mono.

    The stream contract is fixed (see :data:`PLAYBACK_FRAME_BYTES`); this is the
    one place that makes an arbitrary declared PCM layout satisfy it. Without it
    a declared layout is *reinterpreted* rather than converted, and the audible
    result of that is a loud, harsh burst.
    """
    import numpy as np  # local: the module imports with no audio/numeric cost

    if sample_width == 1:
        # 8-bit WAV is unsigned with a 128 bias; 8 bits are the top 8 of 16.
        flat = (np.frombuffer(data, dtype=np.uint8).astype(np.int16) - 128) * 256
    elif sample_width == 2:
        flat = np.frombuffer(data, dtype="<i2")
    elif sample_width == 3:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        value = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        value = (value << 8) >> 8  # sign-extend 24 bits into int32
        flat = (value >> 8).astype(np.int16)  # keep the top 16 bits
    elif sample_width == 4:
        flat = (np.frombuffer(data, dtype="<i4") >> 16).astype(np.int16)
    else:  # pragma: no cover - guarded by the caller, kept as a hard stop
        raise ValueError(f"unsupported sample width {sample_width}")
    if channels > 1:
        frames = flat.reshape(-1, channels).astype(np.int32)
        flat = (frames.sum(axis=1) // channels).astype(np.int16)
    return np.ascontiguousarray(flat, dtype="<i2").tobytes()


class _PcmStreamFormat:
    """Resolve a Fish response into ``(rate, frame_bytes)`` plus payload bytes.

    Fish answers with either a WAV container or headerless PCM, and the two
    arrive on the same socket with no external hint. This class sniffs the first
    bytes, keeps only as many as a header can need, and from then on returns the
    payload in the one format the device stream accepts — the stream is never
    buffered whole (MECHANISM 2).

    Three guarantees, each of which was a real defect before it existed:

    * bytes that positively identify a codec this layer cannot render (an MP3
      body, an HTML error page) raise
      :class:`~claire.output.fish_client.FishUnsupportedAudioFormatError`
      instead of being played as if they were PCM — played, they are a
      full-scale burst, measured at 0.0 dBFS against her speech at -15 dBFS;
    * a RIFF container whose header never resolves is a *reported failure* once
      the sniff budget is spent, not a payload to blast out header and all;
    * the bytes returned are always whole frames of 16-bit mono, so the device
      never sees a partial sample (``RawOutputStream.write`` rejects one, and
      the rejection used to be swallowed as silence).
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        #: A trailing partial frame held back so every emitted block is whole
        #: frames: RawOutputStream.write rejects a buffer that is not a multiple
        #: of the sample size, and that rejection used to be swallowed as silence.
        self._carry = b""
        self.resolved = False
        self.sample_rate: int = DEFAULT_SAMPLE_RATE
        #: Layout of what the device receives - always the stream contract.
        self.frame_bytes: int = PLAYBACK_FRAME_BYTES
        #: Layout the response declared, before conversion.
        self.channels: int = PLAYBACK_CHANNELS
        self.sample_width: int = PLAYBACK_SAMPLE_WIDTH
        self._source_frame_bytes: int = PLAYBACK_FRAME_BYTES

    def feed(self, chunk: bytes) -> bytes:
        """Absorb ``chunk``; return the payload bytes that are ready to play."""
        if self.resolved:
            return self._emit(chunk)
        if not chunk:
            return b""
        self._buf.extend(chunk)

        # Too few bytes to classify: b"\xff" may begin an MP3 frame and b"RIF"
        # may begin a WAV, so hold rather than guess. A real response arrives in
        # multi-kilobyte chunks and is never delayed by this.
        if len(self._buf) < MIN_FORMAT_SNIFF_BYTES:
            return b""

        # Headerless PCM: anything that is not a RIFF container. Decide as soon
        # as the bytes can no longer spell RIFF, but never play a body we can
        # positively identify as a codec this layer cannot render.
        if bytes(self._buf[:4]) != _RIFF_MAGIC:
            self._reject_known_non_pcm(bytes(self._buf))
            self._resolve(DEFAULT_SAMPLE_RATE, PLAYBACK_CHANNELS, PLAYBACK_SAMPLE_WIDTH)
            return self._take_all()

        header = _parse_wav_header(bytes(self._buf))
        offset = _riff_data_offset(bytes(self._buf)) if header else None
        if header is not None and offset is not None:
            rate, channels, width, comptype = header
            if comptype != "NONE" or width not in (1, 2, 3, 4) or channels < 1:
                raise FishUnsupportedAudioFormatError(
                    f"Fish Audio returned a WAVE container this player cannot "
                    f"render (compression={comptype!r}, channels={channels}, "
                    f"sample_width={width}); refusing to write it into a 16-bit "
                    f"mono stream, where it would be a full-scale burst rather "
                    f"than speech (N10)."
                )
            # Play at the rate the response declares (MECHANISM 4), and convert
            # the declared layout to the stream's one supported layout.
            self._resolve(_coerce_sample_rate(rate), channels, width)
            header_part = bytes(self._buf[:offset])
            del self._buf[:offset]
            debug_log(
                f"Fish TTS: WAV header {len(header_part)} bytes, "
                f"rate={self.sample_rate}Hz source={channels}ch/{width * 8}bit "
                f"-> played {PLAYBACK_CHANNELS}ch/16bit",
                "tts",
            )
            return self._take_all()

        if len(self._buf) > MAX_WAV_HEADER_BYTES:
            # A "RIFF" prefix that never yields a parseable header. Before this
            # guard existed the whole body - header and all - was declared
            # headerless PCM and played, which for a container stdlib `wave`
            # will not open (IEEE-float WAVE, for one) is a full-scale burst.
            raise FishUnsupportedAudioFormatError(
                f"Fish Audio returned a RIFF/WAVE body whose header never "
                f"resolved within {MAX_WAV_HEADER_BYTES} bytes; refusing to play "
                f"it as raw PCM, which would be a full-scale burst rather than "
                f"speech (N10)."
            )
        return b""  # still inside the header: hold these bytes back

    def flush_tail(self) -> bytes:
        """Release (and drop) a trailing incomplete frame at end of sentence.

        Returns ``b""``: a partial sample cannot be written to the device, so
        the honest outcome is to drop the single dangling byte rather than hand
        the stream a buffer it will reject. Only reachable when a sentence's
        payload is not a whole number of frames.
        """
        if self._carry:
            debug_log(
                f"Fish TTS: dropping {len(self._carry)} trailing byte(s) that "
                f"do not complete a {self._source_frame_bytes}-byte frame",
                "tts",
            )
            self._carry = b""
        return b""

    def _reject_known_non_pcm(self, buf: bytes) -> None:
        """Raise if ``buf`` positively identifies a codec we cannot render."""
        for magic, name in _NON_PCM_MAGIC:
            if buf.startswith(magic):
                raise FishUnsupportedAudioFormatError(
                    f"Fish Audio answered with {name}, not PCM audio "
                    f"(leading bytes {buf[:8]!r}). An int16 stream would read "
                    f"those bytes back as a full-scale burst - measured at "
                    f"0.0 dBFS against her speech at -15 dBFS - so this sentence "
                    f"is reported as failed instead of played (N10)."
                )

    def _resolve(self, rate: int, channels: int, sample_width: int) -> None:
        self.sample_rate = rate
        self.channels = max(1, channels)
        self.sample_width = sample_width
        self._source_frame_bytes = max(1, self.channels * sample_width)
        # What the device receives is always 16-bit mono, whatever was declared.
        self.frame_bytes = PLAYBACK_FRAME_BYTES
        self.resolved = True

    def _emit(self, chunk: bytes) -> bytes:
        """Return whole 16-bit mono frames for ``chunk``, carrying any remainder."""
        source_frame = self._source_frame_bytes
        data = self._carry + chunk
        whole = len(data) - (len(data) % source_frame)
        self._carry = data[whole:]
        if whole == 0:
            return b""
        body = data[:whole]
        if self.channels == PLAYBACK_CHANNELS and self.sample_width == PLAYBACK_SAMPLE_WIDTH:
            return body  # her normal path: already exactly what the stream wants
        return _to_playback_pcm(body, self.channels, self.sample_width)

    def _take_all(self) -> bytes:
        payload = self._emit(bytes(self._buf))
        del self._buf[:]
        return payload


# --------------------------------------------------------------------------
# Sentence chunking
# --------------------------------------------------------------------------

def _split_sentences(text: str) -> List[str]:
    """Split a reply into synthesis-sized sentences, in order.

    Split at every sentence end (respecting common abbreviations and decimals),
    which is what makes prefetch possible. The only merge is a short trailing
    fragment that carries no punctuation of its own: synthesising "...and then"
    as its own request buys nothing, while merging a complete short sentence like
    "Yes." would delay the one word the user is waiting for.
    """
    text = (text or "").strip()
    if not text:
        return []

    sentences: List[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        # Keep "Dr. Smith" and "e.g. this" in one sentence: an abbreviation is
        # not a full stop for speech purposes.
        token = text[start:match.end(1)]
        last_word = re.split(r"[\s(]", token)[-1].lower().rstrip(".")
        if last_word in _ABBREVIATIONS:
            continue
        piece = text[start:match.end(3)].strip()
        if piece:
            sentences.append(piece)
        start = match.end(3)
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    if not sentences:
        return [text]

    merged: List[str] = []
    for piece in sentences:
        if merged and len(piece) < MIN_SENTENCE_CHARS and not piece[-1] in ".!?\u2026":
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)

    out: List[str] = []
    for piece in merged:
        while len(piece) > MAX_SENTENCE_CHARS:
            cut = piece.rfind(" ", 0, MAX_SENTENCE_CHARS)
            if cut <= 0:
                cut = MAX_SENTENCE_CHARS
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return out


# --------------------------------------------------------------------------
# Default player (lazily imported sounddevice)
# --------------------------------------------------------------------------

class _SoundDevicePlayer:
    """The default player: blocking ``RawOutputStream`` writes at a given rate.

    Duck-typed interface the engine relies on, which is also what a test double
    implements::

        open(sample_rate)   start (or restart) the output at this rate; a
                            (re)open clears any previous abort
        write(chunk)        block until the device has taken the audio
        abort()             stop now and discard buffered audio
        close()             release the device
    """

    def __init__(self, device: Any = None) -> None:
        self._stream: Any = None
        self._aborted = threading.Event()
        # ``None`` lets PortAudio choose, which is what this used to do
        # unconditionally. It is kept as an option, but the engine now passes
        # the resolved device so speech goes where the user's system routes it.
        self._device = device
        # A trailing partial frame: ``RawOutputStream.write`` raises
        # ValueError('len(data) not divisible by samplesize') on a buffer that
        # is not a whole number of frames, and that raise used to be caught and
        # discarded, so the block fell silent with nothing reported.
        self._carry = b""

    def open(self, sample_rate: int) -> None:
        import sounddevice as sd  # imported here so importing this module needs no audio stack

        self._close_stream()
        self._aborted.clear()
        stream = sd.RawOutputStream(
            samplerate=int(sample_rate),
            channels=PLAYBACK_CHANNELS,
            dtype="int16",
            blocksize=0,
            device=self._device,
        )
        stream.start()
        self._stream = stream
        debug_log(
            f"Fish TTS: output stream open at {sample_rate}Hz "
            f"({PLAYBACK_CHANNELS}ch, int16)",
            "tts",
        )

    def write(self, chunk: bytes) -> None:
        stream = self._stream
        if stream is None or not chunk:
            return
        data = self._carry + bytes(chunk)
        whole = len(data) - (len(data) % PLAYBACK_FRAME_BYTES)
        self._carry = data[whole:]
        if whole == 0:
            return
        try:
            stream.write(_limit_peak(data[:whole]))
        except Exception as exc:  # a device lost mid-sentence: report, never fake success
            if not self._aborted.is_set():
                debug_log(f"Fish TTS: device write failed: {exc}", "tts")

    def abort(self) -> None:
        self._aborted.set()
        self._carry = b""
        stream = self._stream
        if stream is None:
            return
        try:
            # Deliberately NOT under portaudio_lock: abort() has to interject into
            # the write() that is blocked inside the audio thread. Taking the lock
            # here would queue behind that blocked write and turn a barge-in into
            # a wait for the end of the sentence — the exact latency flush()
            # exists to remove.
            stream.abort()
        except Exception as exc:
            debug_log(f"Fish TTS: device abort failed: {exc}", "tts")

    def close(self) -> None:
        self._close_stream()

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        self._carry = b""
        if stream is None:
            return
        try:
            stream.close()
        except Exception as exc:
            debug_log(f"Fish TTS: device close failed: {exc}", "tts")


def _limit_peak(block: bytes) -> bytes:
    """Return ``block`` unchanged, or scaled so its peak is at most the ceiling.

    A backstop, not the fix: the format gate above is what stops unrecognised
    bytes from ever reaching here. This exists so that a bug *below* the gate -
    a stale buffer, a mis-sized chunk, a response nobody classified - still
    cannot put a full-scale burst on the speaker. Her own speech peaks around
    -15 dBFS, so the ceiling is never reached by normal audio and normal audio
    is returned byte-for-byte.
    """
    import numpy as np  # local: only this hot path needs the numeric stack

    samples = np.frombuffer(block, dtype="<i2")
    if samples.size == 0:
        return block
    peak = int(np.abs(samples.astype(np.int32)).max())
    if peak <= _PEAK_CEILING:
        return block
    scale = _PEAK_CEILING / float(peak)
    limited = np.clip(
        np.rint(samples.astype(np.float64) * scale), -32768.0, 32767.0
    ).astype("<i2")
    debug_log(
        f"Fish TTS: peak {peak} exceeds the {_PEAK_CEILING} ceiling; scaled by "
        f"{scale:.3f} before the device",
        "tts",
    )
    return limited.tobytes()


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

class FishTTS:
    """Fish Audio speech output with sentence prefetch and fast barge-in.

    Both collaborators are injectable — ``client`` (anything with
    ``synthesise(text, voice_id, latency, model, first_chunk_timeout,
    total_timeout)`` yielding byte chunks) and ``player`` — so tests need neither
    network nor sound device. When either is omitted it is built lazily on first
    use, and only then.
    """

    def __init__(
        self,
        config: Any = None,
        *,
        enabled: bool = True,
        client: Any = None,
        player: Any = None,
        voice_id: Optional[str] = None,
        key_file: Optional[str] = None,
        latency: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.config = config
        self.enabled = enabled
        self.client = client
        self.player = player

        # Config comes from a duck-typed object: getattr with a safe default so
        # an older config without the fish fields still produces a usable engine
        # (and a missing key file still raises a named error, not AttributeError).
        config_voice = getattr(config, "fish_voice_id", None)
        config_key_file = getattr(config, "fish_key_file", None)
        config_latency = getattr(config, "fish_latency", None)
        config_model = getattr(config, "fish_model", None)

        self.voice_id = voice_id if voice_id is not None else config_voice
        self.key_file = key_file or config_key_file or DEFAULT_KEY_FILE
        self.model = model or config_model or DEFAULT_MODEL

        # MECHANISM 7 — latency defaults to 'low'. 'normal' adds roughly 3.4 s of
        # wait *before the first audio*, and that wait grows with the length of
        # the reply, so it can never be the conversational default; it is opt-in
        # only through fish_latency, and to a spoken reply a 3.4 s silence is
        # indistinguishable from having been ignored.
        chosen_latency = (latency or config_latency or DEFAULT_LATENCY)
        chosen_latency = str(chosen_latency).strip().lower()
        if chosen_latency not in ("low", "normal"):
            chosen_latency = DEFAULT_LATENCY
        self.latency = chosen_latency

        # Threading / queue state (same shape as the other engines).
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._play_q: "queue.Queue[Any]" = queue.Queue()
        self._stop = threading.Event()
        self._is_speaking = threading.Event()
        self._last_spoken_text: str = ""
        self._state_lock = threading.Lock()

        self._synth_thread: Optional[threading.Thread] = None
        self._play_thread: Optional[threading.Thread] = None

        # Backpressure accounting (MECHANISM 2): bytes enqueued and not yet
        # consumed, guarded by its own condition so the producer can be woken
        # both by playback draining and by flush().
        self._bp = threading.Condition()
        self._queued_bytes = 0

        # Generation counter: every flush() bumps it, so audio synthesised for a
        # barged-in utterance can be recognised and dropped instead of played.
        self._generation = 0
        self._utt_seq = 0
        self._pending = 0

        self._player_rate: Optional[int] = None
        self._utt_played_bytes: dict = {}
        self._utt_frame_bytes: dict = {}
        self._utt_rate: dict = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Build the client and player and start the two worker threads.

        Raises the named :class:`~claire.output.fish_client.FishError` subclass
        when the key file is missing/empty or the client cannot be built — this
        engine never reports speech it cannot produce (rule N10).
        """
        if not self.enabled or self._synth_thread is not None:
            return
        # MECHANISM 6 — the client (and with it the key file) is built here, on
        # the caller's thread, and its named failure is allowed to propagate. The
        # failure this prevents is the worst one in TTS: the engine printing
        # something friendly, reporting "spoke", and leaving the user in silence
        # because the key file was absent or empty (rule N10).
        # Client first: its failure (bad/missing key file) is the informative one.
        self._ensure_client()
        self._ensure_player()
        with self._state_lock:
            if self._synth_thread is not None:
                return
            self._stop.clear()
            # A previous stop() left shutdown sentinels (and possibly unspoken
            # text) in the queues; a restarted worker would read a sentinel as
            # its first item and exit, so the next reply would never be spoken.
            for q in (self._q, self._play_q):
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
            self._synth_thread = threading.Thread(
                target=self._synth_loop, name="fish-tts-synth", daemon=True
            )
            self._play_thread = threading.Thread(
                target=self._play_loop, name="fish-tts-play", daemon=True
            )
            self._synth_thread.start()
            self._play_thread.start()

    def stop(self) -> None:
        """Stop everything; safe to call when never started."""
        try:
            self.flush()
        except Exception:
            pass
        self._stop.set()
        with self._bp:
            self._bp.notify_all()
        for q in (self._q, self._play_q):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        for thread in (self._synth_thread, self._play_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=_FLUSH_JOIN_SECONDS)
        self._synth_thread = None
        self._play_thread = None
        # _stop is deliberately left set: a worker still inside a slow client
        # call must exit at its next check rather than carrying on into a queue
        # nobody is draining. start() clears it when (re)starting.
        player = self.player
        if player is not None:
            try:
                player.close()
            except Exception as exc:
                debug_log(f"Fish TTS: player close failed: {exc}", "tts")
        self._player_rate = None

    # -- speech ------------------------------------------------------------

    def speak(
        self,
        text: str,
        completion_callback: Optional[Callable[[], None]] = None,
        duration_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Queue ``text`` to be spoken; callback fires after it has been played."""
        if not self.enabled or not text or not text.strip():
            return
        # Raises the named error for an unusable key file / client (N10). Called
        # before anything is queued, so a failure is reported to the caller
        # rather than disappearing onto a worker thread.
        self.start()
        if not self.enabled or self._synth_thread is None:
            return

        # Matches PiperTTS: the engine speaks the preprocessed text (markdown,
        # raw URLs and code fences stripped) and remembers that same text.
        from .tts import _preprocess_for_speech  # local: heavy module, one-time cost

        processed = _preprocess_for_speech(text)
        if not processed or not processed.strip():
            return

        with self._state_lock:
            self._utt_seq += 1
            utterance_id = self._utt_seq
            self._pending += 1
            self._is_speaking.set()
            generation = self._generation
        self._q.put((processed, utterance_id, completion_callback, duration_callback, generation))

    def interrupt(self) -> None:
        """Stop current speech immediately (barge-in). Delegates to flush()."""
        self.flush()

    # MECHANISM 5 — flush(): drop the queue and abort the device without waiting
    # for the blocked device write. The failure it prevents is the barge-in that
    # takes effect only at the end of the current sentence: by then the user has
    # been talked over for seconds. It is also safe when idle (the common case:
    # a wake word firing while nothing is speaking), so callers never need to
    # check is_speaking() first.
    def flush(self) -> None:
        """Stop playback now, discard queued audio. Safe when not speaking."""
        with self._state_lock:
            self._generation += 1
            self._pending = 0
            self._is_speaking.clear()
        # Bookkeeping for utterances that are being abandoned (their "end"
        # markers are dropped below, so these entries would otherwise linger).
        self._utt_played_bytes.clear()
        self._utt_frame_bytes.clear()
        self._utt_rate.clear()
        # Drop everything queued but not yet handed to the device, including the
        # "end" markers, whose utterances are being abandoned.
        while True:
            try:
                self._play_q.get_nowait()
            except queue.Empty:
                break
        # Release any producer blocked on the byte ceiling, and reset the count:
        # the audio it was waiting to enqueue has just been discarded.
        with self._bp:
            self._queued_bytes = 0
            self._bp.notify_all()
        player = self.player
        if player is not None:
            try:
                player.abort()
            except Exception as exc:
                debug_log(f"Fish TTS: player abort failed: {exc}", "tts")

    # -- public surface matching PiperTTS ----------------------------------

    def is_speaking(self) -> bool:
        return self._is_speaking.is_set()

    def get_last_spoken_text(self) -> str:
        return self._last_spoken_text

    def queued_audio_bytes(self) -> int:
        """Bytes queued ahead of the player right now (backpressure diagnostics)."""
        with self._bp:
            return self._queued_bytes

    # -- collaborator construction ----------------------------------------

    def _ensure_client(self) -> Any:
        """Return the Fish client, building it on first use.

        Building reads the key file, which is where the named key errors come
        from (:class:`FishKeyFileMissingError`, ``FishKeyFileEmptyError``,
        ``FishKeyFilePermissionsError``): each already names the path, and none
        of them is swallowed here, so a caller can never mistake a broken key
        file for speech that happened.
        """
        if self.client is None:
            try:
                self.client = FishClient(FishKeyPool(self.key_file))
            except FishError:
                raise  # named, and already carries the path
            except Exception as exc:
                # "cannot start" must be reported, not converted into silence.
                raise FishConfigError(
                    f"Fish Audio client could not be started for key file "
                    f"{self.key_file!r}: {exc.__class__.__name__}: {exc}"
                ) from exc
            if not self.voice_id:
                # A key file alone cannot speak: without a voice id Fish would be
                # asked for the default voice and the reply would come back in
                # the wrong one. Refuse rather than claim the reply was spoken.
                raise FishConfigError(
                    f"Fish Audio voice id is not configured for key file "
                    f"{self.key_file!r}; refusing to report speech that cannot be "
                    f"made (N10)."
                )
        if not hasattr(self.client, "synthesise"):
            raise FishConfigError(
                f"Fish Audio client for key file {self.key_file!r} has no "
                f"synthesise(); refusing to report speech that cannot be made (N10)."
            )
        return self.client

    def _ensure_player(self) -> Any:
        if self.player is None:
            try:
                self.player = _SoundDevicePlayer(device=self._resolve_output_device())
            except Exception as exc:
                raise FishError(
                    f"Fish Audio playback device could not be opened: "
                    f"{exc.__class__.__name__}: {exc}"
                ) from exc
        return self.player

    def _resolve_output_device(self) -> Any:
        """Which speaker to play through, from the config the engine was given.

        A missing config (a test double, or an older caller) keeps the previous
        PortAudio-chosen default rather than guessing.
        """
        cfg = getattr(self, "config", None)
        if cfg is None:
            return None
        from ..utils.audio_capture import resolve_output_device

        import sounddevice as sd

        return resolve_output_device(sd, getattr(cfg, "voice_output_device", None))["device"]

    # -- synthesis worker --------------------------------------------------

    def _synth_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            text, utterance_id, completion_callback, duration_callback, generation = item
            try:
                self._synth_utterance(text, utterance_id, generation)
            except _Flushed:
                # flush() already zeroed the pending count and cleared the
                # speaking flag for this utterance.
                continue
            except Exception as exc:
                # Report and carry on: a failed utterance is a failure, never an
                # empty success, and its completion callback is NOT called (N10).
                self._report_synthesis_failure(exc)
                with self._state_lock:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._is_speaking.clear()
                continue
            if generation != self._generation:
                continue
            self._play_q.put(
                ("end", generation, utterance_id, completion_callback, duration_callback)
            )

    def _synth_utterance(self, text: str, utterance_id: int, generation: int) -> None:
        """Synthesise and enqueue every sentence of one utterance, in order."""
        produced = 0
        for sentence in _split_sentences(text):
            if generation != self._generation:
                raise _Flushed()
            # MECHANISM 1 (PREFETCH) — each sentence is fetched and enqueued here
            # while _play_loop is already writing the previous one to the device.
            # Without it every full stop carries a full network round trip plus
            # the first-chunk wait, and the voice stutters in the middle of a
            # reply. The single ordered queue below is what keeps the release
            # strictly in order: a later sentence can be *ready* early but can
            # never *start* before an earlier one has finished.
            fmt = _PcmStreamFormat()
            sentence_bytes = 0
            for raw in self.client.synthesise(
                sentence,
                voice_id=self.voice_id,
                latency=self.latency,
                model=self.model,
                first_chunk_timeout=FIRST_CHUNK_TIMEOUT,
                total_timeout=TOTAL_TIMEOUT,
            ):
                # MECHANISM 3 — copy into stable memory (bytes(...)) before the
                # chunk is queued for the player. Handing the player a view of a
                # buffer the client or the reader reuses is how a streamed reply
                # acquires audible crackling: the bytes change under the device.
                chunk = bytes(raw) if raw else b""
                if not chunk:
                    continue
                payload = fmt.feed(chunk)
                if not payload:
                    continue
                self._enqueue_audio(
                    generation, utterance_id, text, fmt.sample_rate, fmt.frame_bytes, payload
                )
                sentence_bytes += len(payload)
            # A dangling byte that cannot complete a frame is dropped here rather
            # than handed to a stream that would reject the whole buffer.
            fmt.flush_tail()
            if sentence_bytes == 0:
                # The client already treats a zero-byte stream as failure; this
                # covers the remaining case of a stream that was header only.
                raise FishEmptyStreamError(
                    f"Fish Audio returned no audio for a {len(sentence)}-character "
                    f"sentence of the reply; refusing to report speech that was "
                    f"never produced (N10)."
                )
            produced += sentence_bytes
        if produced == 0:
            raise FishEmptyStreamError(
                "Fish Audio returned no audio for the reply; refusing to report "
                "speech that was never produced (N10)."
            )

    def _enqueue_audio(
        self,
        generation: int,
        utterance_id: int,
        text: str,
        sample_rate: int,
        frame_bytes: int,
        payload: bytes,
    ) -> None:
        # MECHANISM 2 (BACKPRESSURE) — block the producer once more than
        # MAX_QUEUED_AUDIO_BYTES of unplayed audio is queued. Without this the
        # synth thread races through the whole reply and holds all of it in RAM,
        # so the first word is delayed by the entire synthesis and a barge-in has
        # megabytes to throw away.
        with self._bp:
            while (
                self._queued_bytes >= MAX_QUEUED_AUDIO_BYTES
                and generation == self._generation
                and not self._stop.is_set()
            ):
                self._bp.wait(timeout=0.05)
            if generation != self._generation:
                raise _Flushed()
            self._queued_bytes += len(payload)
        self._play_q.put(
            ("audio", generation, utterance_id, text, sample_rate, frame_bytes, payload)
        )

    def _report_synthesis_failure(self, exc: Exception) -> None:
        message = f"Fish TTS synthesis failed: {exc}"
        debug_log(message, "tts")
        print(f"  \u26a0\ufe0f {message}", flush=True)

    # -- playback worker ---------------------------------------------------

    def _play_loop(self) -> None:
        current_utt: Optional[int] = None
        current_rate: Optional[int] = None
        while not self._stop.is_set():
            try:
                item = self._play_q.get(timeout=_IDLE_POLL_SECONDS)
            except queue.Empty:
                self._clear_speaking_if_idle()
                continue
            if item is None:
                break
            kind = item[0]

            if kind == "audio":
                _, generation, utterance_id, text, rate, frame_bytes, payload = item
                # Release the producer's backpressure slot first: the audio has
                # left the queue, whether or not it turns out to be stale.
                with self._bp:
                    self._queued_bytes = max(0, self._queued_bytes - len(payload))
                    self._bp.notify_all()
                if generation != self._generation:
                    # Flushed after this item was queued: dropping it is the
                    # whole point of the barge-in.
                    continue
                if utterance_id != current_utt:
                    current_utt = utterance_id
                    new_utterance = True
                    self._utt_played_bytes[utterance_id] = 0
                    self._utt_frame_bytes[utterance_id] = frame_bytes
                    self._utt_rate[utterance_id] = rate
                else:
                    new_utterance = False
                if rate != current_rate:
                    # MECHANISM 4 — the player is (re)opened at the rate this
                    # response declared. A rate change can only land here, at a
                    # sentence boundary while the queue is in order, so it never
                    # resamples the middle of a sentence.
                    current_rate = rate
                    try:
                        self._open_player(rate)
                    except FishError as exc:
                        # A device that will not open must not kill this loop:
                        # that would silence every later reply. The utterance is
                        # dropped instead, so nothing is claimed as spoken, and
                        # keeping the loop alive also keeps draining the queue so
                        # the producer cannot wedge on the byte ceiling.
                        self._report_synthesis_failure(exc)
                        current_rate = None  # a later sentence/utterance retries
                        continue
                player = self.player
                if player is not None:
                    try:
                        player.write(payload)
                    except Exception as exc:
                        debug_log(f"Fish TTS: playback write failed: {exc}", "tts")
                        continue
                # get_last_spoken_text() is recorded only once audio has actually
                # been handed to the device, and only if the utterance was not
                # flushed while that write was in flight: text that was merely
                # synthesised (or barged in before a byte was heard) is never
                # reported as spoken (N10).
                if generation != self._generation:
                    continue
                if new_utterance:
                    self._last_spoken_text = text
                self._utt_played_bytes[utterance_id] = (
                    self._utt_played_bytes.get(utterance_id, 0) + len(payload)
                )
                continue

            if kind == "end":
                _, generation, utterance_id, completion_callback, duration_callback = item
                # Release whatever the jitter buffer still holds: the utterance is
                # over and those bytes are the tail of the sentence. This runs
                # before `played` is read so the tail counts as spoken instead of
                # being silently dropped, and only for a live generation - a
                # flushed utterance's held audio is meant to be discarded.
                played = self._utt_played_bytes.pop(utterance_id, 0)
                frame_bytes = self._utt_frame_bytes.pop(utterance_id, 2)
                played_rate = self._utt_rate.pop(utterance_id, DEFAULT_SAMPLE_RATE)
                current_utt = None
                if generation != self._generation:
                    continue
                # Mark not-speaking before the completion callback runs, the way
                # PiperTTS does: a listener that resumes on the callback must not
                # find is_speaking() still true and think it is being talked over.
                self._finish_utterance()
                if duration_callback is not None and played:
                    try:
                        duration_callback(played / float(max(1, played_rate) * max(1, frame_bytes)))
                    except Exception as exc:
                        debug_log(f"Fish TTS: duration callback error: {exc}", "tts")
                # Only a reply that actually reached the device reports
                # completion; a flushed or failed one must not look like success.
                if completion_callback is not None and played > 0:
                    try:
                        completion_callback()
                    except Exception as exc:
                        print(f"  \u26a0\ufe0f Fish TTS completion callback error: {exc}", flush=True)
        self._clear_speaking_if_idle()

    def _open_player(self, sample_rate: int) -> None:
        player = self.player
        if player is None or self._player_rate == sample_rate:
            self._player_rate = sample_rate
            return
        try:
            player.open(sample_rate)
        except Exception as exc:
            raise FishError(
                f"Fish Audio playback at {sample_rate}Hz could not be opened: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        self._player_rate = sample_rate

    def _clear_speaking_if_idle(self) -> None:
        with self._state_lock:
            if self._pending == 0:
                self._is_speaking.clear()

    def _finish_utterance(self) -> None:
        """A queued utterance reached its "end" marker: it is no longer pending.

        Without this the pending count would only ever grow, is_speaking() would
        stay true for ever after the first reply, and the listener would never
        believe Claire had stopped talking.
        """
        with self._state_lock:
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self._is_speaking.clear()
