"""Fish Audio TTS over raw HTTP, with a rotating pool of API keys (decision D6).

Claire speaks through Fish Audio's cloud endpoint, and only the reply text leaves
the machine (see ``docs/CLOUD_POLICY.md``). This module is deliberately the
bottom of that stack: it owns the HTTP call, the key rotation and the timeouts,
and nothing else. It does not import a Fish SDK, because a vendor SDK would hide
exactly the two things that matter here — the per-key HTTP status, and the first
byte of the response body.

The call is ``POST https://api.fish.audio/v1/tts`` with ``Authorization: Bearer
<key>`` and the model id in the ``model`` header. The body is JSON::

    {"text": "...", "reference_id": "<voice id>", "latency": "low"}

Fish Audio names the cloned-voice field ``reference_id``; that is the one place
where this module's vocabulary (``voice_id``) differs from the wire format, so
the mapping lives in :data:`VOICE_FIELD` as a named constant instead of inline.

A successful response is raw audio bytes (WAV or headerless PCM), streamed. It
is *not* trusted:

* a 200 whose first non-whitespace byte is ``{`` is an error body, not audio;
* a stream that yields zero bytes is a failure, never an empty success.

Both are the silent-empty-success defect class this codebase keeps meeting:
something reports success while producing nothing.

Key failover — each rule is implemented and commented where it happens:

=======================  =====================================================
status                   meaning
=======================  =====================================================
401/402/403/429          dead key: rotate to the next key, voice id untouched
400/404/405/409/413/422  bad request: raise now, burn no other key
anything else            transient: retry once on the same key, then raise
=======================  =====================================================

Every failure is a named :class:`FishError`. The mirror of that contract on the
recognition side is :class:`claire.listening.stt.base.SttError`; the two are
separate hierarchies on purpose — a synthesis failure is not a transcription
failure — but they share one rule: a failure is never dressed up as an empty
result.

A key value is a secret. It is used for the ``Authorization`` header and for
nothing else: it never appears in ``repr``/``str`` of the pool, and every message
this module raises is passed through :meth:`FishKeyPool.redact_text`, so even a
vendor error body that echoed a key back cannot carry it into a log.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

import requests

# --------------------------------------------------------------------------
# Wire constants
# --------------------------------------------------------------------------

#: The TTS endpoint. Overridable per client so a test or a proxy can retarget it.
ENDPOINT = "https://api.fish.audio/v1/tts"

#: The JSON field Fish Audio uses for the cloned voice. Callers of this module
#: speak in terms of ``voice_id``; only this mapping knows the vendor's name for
#: it, so there is exactly one place to change if Fish renames the field.
VOICE_FIELD = "reference_id"

#: A key that answered one of these is exhausted, unpaid or revoked: rotate.
DEAD_KEY_STATUSES = frozenset({401, 402, 403, 429})

#: The request itself is wrong (bad voice, bad model, too much text). Rotating
#: keys cannot fix any of these, so they must not consume the other keys.
BAD_REQUEST_STATUSES = frozenset({400, 404, 405, 409, 413, 422})

#: How many attempts one key gets for a transient failure: the call, plus one
#: retry. A third attempt is not worth another timeout budget.
_TRANSIENT_ATTEMPTS = 2

_DEFAULT_CHUNK_SIZE = 4096
_LEADING_WHITESPACE = b" \t\r\n"
_MAX_ERROR_BODY = 64 * 1024
_ERROR_BODY_WAIT = 0.5
_REDACTED_TAIL_CHARS = 4
_FINGERPRINT_CHARS = 8


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class FishError(RuntimeError):
    """Base class for every Fish Audio synthesis failure.

    Deliberately a sibling of :class:`claire.listening.stt.base.SttError` rather
    than a subclass: listening and speaking fail for different reasons and a
    caller that handles one should not silently swallow the other. What the two
    share is that every message names the actual cause, and no failure is ever
    reported as an empty result.
    """


class FishKeyError(FishError):
    """Base class for API-key file problems. Carries the exact path."""

    def __init__(self, path: Any, detail: str = "") -> None:
        self.path = str(path)
        message = f"Fish Audio API key file {self.path!r} is unusable: {detail or 'no detail'}"
        super().__init__(message)


class FishKeyFileMissingError(FishKeyError):
    """The configured key file does not exist (or cannot be read)."""


class FishKeyFileEmptyError(FishKeyError):
    """The key file exists but holds no keys after blanks/comments are skipped."""


class FishKeyFilePermissionsError(FishKeyError):
    """The key file is readable by group or others; a key file must be 0600."""


class FishKeyPoolExhaustedError(FishError):
    """Every key in the pool answered as dead; nothing is left to try."""


class FishRequestRejectedError(FishError):
    """The request is malformed for this voice/model: 400/404/405/409/413/422.

    Raised immediately. The remaining keys are *not* tried, because they would
    fail identically and could be rate-limit-punished for it.
    """


class FishApiError(FishError):
    """A non-200 status that is not a dead key and not a bad request."""


class FishErrorBodyError(FishError):
    """HTTP 200 whose body is JSON, not audio. An error dressed as a success."""


class FishStreamError(FishError):
    """The audio stream failed after the response was accepted."""


class FishFirstChunkTimeoutError(FishStreamError):
    """No first audio byte arrived inside ``first_chunk_timeout``."""


class FishTotalTimeoutError(FishStreamError):
    """The whole stream did not finish inside ``total_timeout``."""


class FishEmptyStreamError(FishStreamError):
    """The stream ended after zero bytes; failure, not an empty success."""


class FishUnsupportedAudioFormatError(FishError):
    """The body is audio, but not PCM this player can render.

    Fish Audio answers with WAV when asked (``format: wav``), but the player is
    the only layer that can tell whether the bytes really are 16-bit PCM. When
    they are something else - an MP3 body, a container whose header never
    parses, a codec this build has no decoder for - the honest outcome is a
    *named failure* for that sentence, never "play the bytes anyway". Those
    bytes, reinterpreted as int16 PCM, are a full-scale burst rather than
    speech: measured, an MP3 body reads back at 0.0 dBFS against her normal
    speech at -15 dBFS, roughly 27 dB too loud. Raised, this is audible as a
    reported failure; played, it is the loud sound the owner hears.
    """


class FishTransportError(FishError):
    """The HTTP call itself failed (connection, TLS, socket read)."""


class FishConfigError(FishError):
    """A caller-supplied argument is not usable (e.g. a non-positive timeout)."""


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def fingerprint(value: str) -> str:
    """A stable, non-reversible handle for a secret.

    Eight hex characters of SHA-256. Enough to tell two keys apart in a log,
    useless for recovering either one.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]


def redact(value: str) -> str:
    """The only form of a key that may leave this module.

    Last four characters plus a fingerprint. A key shorter than eight
    characters gets no tail at all — showing the last four of a six-character
    secret would be most of the secret.
    """
    if not value:
        return "<empty>"
    tag = fingerprint(value)
    if len(value) < 8:
        return f"<redacted:{tag}>"
    return f"…{value[-_REDACTED_TAIL_CHARS:]}:{tag}"


# --------------------------------------------------------------------------
# Key pool
# --------------------------------------------------------------------------


class FishKeyPool:
    """A rotating pool of Fish Audio API keys read from a 0600 file.

    The file is one key per line; blank lines and ``#`` comments are skipped, so
    the file can carry a note about which key is which. Keys are read once at
    construction and kept in memory only.

    Rotation is round-robin and thread-safe: a synthesis that lost its key and a
    synthesis starting concurrently must never be handed the same dead key in
    lockstep.

    Nothing here ever returns a key value except :meth:`next_key`, which exists
    precisely so it can be put in an ``Authorization`` header.
    """

    def __init__(self, path: Any, *, require_private_mode: bool = True) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._index = 0
        self._keys: Tuple[str, ...] = self._load(require_private_mode)

    # -- loading -----------------------------------------------------------

    def _load(self, require_private_mode: bool) -> Tuple[str, ...]:
        if not self.path.exists():
            # A named error, not a silent empty pool: an empty pool would make
            # the very first synthesis fail with "all keys dead", which hides
            # the real cause (a typo'd path) from whoever reads the log.
            raise FishKeyFileMissingError(self.path, f"no such file at {str(self.path)!r}")
        if not self.path.is_file():
            raise FishKeyFileMissingError(self.path, "path exists but is not a regular file")

        if require_private_mode and os.name == "posix":
            try:
                mode = self.path.stat().st_mode & 0o777
            except OSError as exc:
                raise FishKeyFileMissingError(self.path, f"cannot stat ({exc.__class__.__name__})") from exc
            if mode & 0o077:
                raise FishKeyFilePermissionsError(
                    self.path,
                    f"mode is {mode:04o}; a file holding an API key must be 0600 "
                    f"(chmod 600 {self.path})",
                )

        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FishKeyFileMissingError(
                self.path, f"cannot read ({exc.__class__.__name__}: {exc})"
            ) from exc
        except UnicodeDecodeError as exc:
            raise FishKeyError(self.path, "not valid UTF-8 text") from exc

        keys = []
        for line in raw.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue  # blank or comment: not a key
            keys.append(candidate)
        if not keys:
            raise FishKeyFileEmptyError(
                self.path, "it holds no keys (only blank lines and # comments)"
            )
        return tuple(keys)

    # -- use ---------------------------------------------------------------

    def next_key(self) -> str:
        """Return the next key in the rotation and advance the cursor.

        The returned value is the secret itself and must go straight into the
        ``Authorization`` header. Never log it — use :func:`redact` if a
        reference is needed.
        """
        with self._lock:
            key = self._keys[self._index % len(self._keys)]
            self._index += 1
            return key

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def redact_text(self, text: str) -> str:
        """Replace every occurrence of every known key with its redaction.

        Applied to every message this module raises, so even a vendor response
        body that echoed the key back cannot carry it into a log or a traceback.
        """
        if not text:
            return text
        for key in self._keys:
            if key and key in text:
                text = text.replace(key, redact(key))
        return text

    def __repr__(self) -> str:
        # path is safe (configuration, not a secret); the key values never are.
        return f"<FishKeyPool path={str(self.path)!r} keys={len(self._keys)}>"

    __str__ = __repr__


# --------------------------------------------------------------------------
# Streaming helper: first-byte deadline
# --------------------------------------------------------------------------


class _ChunkPump:
    """Read ``chunk_iter`` on a daemon thread and hand chunks over a queue.

    This exists for one reason: ``requests`` has no per-chunk timeout. Its
    ``timeout=`` bounds a single socket read only, and the deadline that matters
    here is "has the *first* byte arrived yet". Reading on a thread lets the
    consumer wait on the queue with its own deadline and abandon a request that
    is sitting open, instead of blocking until the vendor times out.
    """

    _END = ("end", None)

    def __init__(self, chunk_iter: Iterator[bytes]) -> None:
        self._queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(chunk_iter,), name="fish-tts-stream", daemon=True
        )
        self._thread.start()

    def _run(self, chunk_iter: Iterator[bytes]) -> None:
        try:
            for chunk in chunk_iter:
                if self._stop.is_set():
                    break
                self._queue.put(("chunk", chunk))
        except BaseException as exc:  # noqa: BLE001 - handed to the consumer
            if not self._stop.is_set():
                self._queue.put(("error", exc))
        finally:
            # An abandoned pump stays quiet: the consumer already gave up and
            # its own error is the one that must surface.
            if not self._stop.is_set():
                self._queue.put(self._END)

    def get(self, timeout: float) -> Tuple[str, Any]:
        return self._queue.get(timeout=timeout)

    def stop(self) -> None:
        self._stop.set()


class _DeadKey(Exception):
    """Internal control flow: this key is dead, rotate to the next one."""

    def __init__(self, public: FishError) -> None:
        super().__init__(str(public))
        self.public = public


class _Transient(Exception):
    """Internal control flow: a retryable failure; retry once, then raise."""

    def __init__(self, public: FishError) -> None:
        super().__init__(str(public))
        self.public = public


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class FishClient:
    """Synthesise speech through Fish Audio, failing over between keys.

    Args:
        key_pool: the rotating key source.
        endpoint: TTS endpoint; override for a proxy or a test double.
        session: an object with ``post(...)``. Defaults to a fresh
            ``requests.Session`` so connections are reused across chunks.
        chunk_size: streaming read size in bytes.
        connect_timeout: TCP/TLS connect budget, separate from both of the
            synthesis budgets.
    """

    def __init__(
        self,
        key_pool: FishKeyPool,
        *,
        endpoint: str = ENDPOINT,
        session: Optional[Any] = None,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        connect_timeout: float = 5.0,
    ) -> None:
        self.key_pool = key_pool
        self.endpoint = endpoint
        self.chunk_size = chunk_size
        self.connect_timeout = connect_timeout
        self._session = session if session is not None else requests.Session()

    # -- public API --------------------------------------------------------

    def synthesise(
        self,
        text: str,
        voice_id: str,
        latency: str = "low",
        model: str = "s1",
        first_chunk_timeout: float = 8.0,
        total_timeout: float = 120.0,
    ) -> Iterator[bytes]:
        """Yield audio byte chunks for ``text``, failing over between keys.

        ``first_chunk_timeout`` is *separate from and much shorter than*
        ``total_timeout`` on purpose: it is the budget for the first byte of
        audio. Without it a request that never speaks (auth accepted, worker
        stuck) would sit open for the whole ``total_timeout`` before anything
        was reported, so the listener would hang for two minutes on a dead
        request instead of failing in eight seconds.

        ``voice_id`` is fixed for the whole call. A key failover changes the key
        and nothing else — the voice the user configured is never swapped for a
        fallback because a key died.
        """
        # Validate eagerly, before the generator starts: a caller that passed a
        # bad budget should learn at the call site, not on the first next().
        if not isinstance(text, str) or not text.strip():
            raise FishRequestRejectedError(
                "Fish TTS refuses an empty text; there is nothing to synthesise "
                "(not sending a request and not burning a key)."
            )
        if not voice_id:
            raise FishConfigError("Fish TTS needs a voice_id; none was configured.")
        if not model:
            raise FishConfigError("Fish TTS needs a model id; none was configured.")
        for name, value in (("first_chunk_timeout", first_chunk_timeout), ("total_timeout", total_timeout)):
            if not isinstance(value, (int, float)) or value <= 0:
                raise FishConfigError(f"{name} must be a positive number of seconds, got {value!r}.")
        if first_chunk_timeout > total_timeout:
            raise FishConfigError(
                f"first_chunk_timeout ({first_chunk_timeout}s) exceeds total_timeout "
                f"({total_timeout}s); the first-byte budget is meant to be the shorter one."
            )
        return self._stream(text, voice_id, latency, model, first_chunk_timeout, total_timeout)

    # -- rotation loop -----------------------------------------------------

    def _stream(
        self,
        text: str,
        voice_id: str,
        latency: str,
        model: str,
        first_chunk_timeout: float,
        total_timeout: float,
    ) -> Iterator[bytes]:
        started = time.monotonic()
        pool = self.key_pool
        pool_size = len(pool)
        keys_tried = 0
        last_dead: Optional[FishError] = None

        while keys_tried < pool_size:
            key = pool.next_key()
            keys_tried += 1
            attempt = 0
            while True:
                attempt += 1
                yielded = False
                # Each attempt gets its own first-byte clock; the total budget
                # below is shared across failovers so the whole call stays
                # bounded.
                attempt_started = time.monotonic()
                try:
                    for chunk in self._attempt(
                        key,
                        text,
                        voice_id,
                        latency,
                        model,
                        first_chunk_timeout,
                        total_timeout,
                        started,
                        attempt_started,
                        keys_tried,
                        pool_size,
                    ):
                        yielded = True
                        yield chunk
                    return  # the stream completed: success
                except _DeadKey as exc:
                    # 401/402/403/429: the key is dead. Rotate to the next one.
                    # The voice id, model and body are untouched — only the
                    # credential changes.
                    last_dead = exc.public
                    break
                except _Transient as exc:
                    # Anything else is transient: retry once on the *same* key,
                    # then raise. Never retry once audio has already been handed
                    # to the caller — replaying from the top would duplicate
                    # speech that may already be playing.
                    if attempt >= _TRANSIENT_ATTEMPTS or yielded:
                        raise exc.public from exc.__cause__
                    # else: fall through and loop once more on this same key

        if last_dead is None:  # pragma: no cover - pool_size >= 1 is guaranteed
            last_dead = FishApiError("no key produced a response")
        raise FishKeyPoolExhaustedError(
            pool.redact_text(
                f"All {pool_size} Fish Audio key(s) were rejected as dead; last failure: {last_dead} "
                f"Voice {voice_id!r} was never changed."
            )
        )

    # -- one attempt on one key -------------------------------------------

    def _attempt(
        self,
        key: str,
        text: str,
        voice_id: str,
        latency: str,
        model: str,
        first_chunk_timeout: float,
        total_timeout: float,
        started: float,
        attempt_started: float,
        keys_tried: int,
        pool_size: int,
    ) -> Iterator[bytes]:
        headers = {
            "Authorization": f"Bearer {key}",
            "model": model,
            "Content-Type": "application/json",
        }
        body = {  # text, voice id, latency, container
            "text": text,
            VOICE_FIELD: voice_id,
            "latency": latency,
            # Ask for WAV, and this is not a preference: the playback layer
            # understands RIFF/WAVE and headerless PCM, and treats anything else
            # as PCM. Fish Audio's default response is MP3, so without this the
            # MP3 bytes were written straight into an int16 stream and Claire
            # came out as noise - loud, confident, unintelligible noise. WAV
            # costs bandwidth and buys correctness; "pcm" is smaller and would
            # also work, but WAV carries its own sample rate and channel count
            # so the player never has to assume them.
            "format": "wav",
        }
        try:
            response = self._session.post(
                self.endpoint,
                headers=headers,
                json=body,
                stream=True,
                # The socket read gets the first-chunk budget too: it is the
                # deadline for "the vendor started answering". The thread-side
                # deadline below still applies, because a fake or a buffered
                # response can ignore the socket timeout entirely.
                timeout=(self.connect_timeout, first_chunk_timeout),
            )
        except requests.exceptions.RequestException as exc:
            raise _Transient(
                self._fail(
                    FishTransportError,
                    f"Fish TTS request to {self.endpoint} failed before a response "
                    f"arrived ({exc.__class__.__name__}) on key "
                    f"{redact(key)} ({keys_tried}/{pool_size}).",
                )
            ) from exc

        try:
            status = int(getattr(response, "status_code", 0) or 0)
            if status in DEAD_KEY_STATUSES:
                # Dead key: rotate. Named redacted form only — never the value.
                raise _DeadKey(
                    self._fail(
                        FishApiError,
                        f"Fish TTS key {redact(key)} is dead: HTTP {status} "
                        f"(key {keys_tried}/{pool_size}). Voice {voice_id!r} and model "
                        f"{model!r} are unchanged; rotating to the next key. "
                        f"{self._detail(response)}",
                    )
                )
            if status in BAD_REQUEST_STATUSES:
                # Bad request: the other keys would fail identically. Raise at
                # once and burn none of them.
                raise self._fail(
                    FishRequestRejectedError,
                    f"Fish TTS rejected the request with HTTP {status} on key "
                    f"{redact(key)}: this is a bad request, not a bad key, so no "
                    f"other key is tried. Voice {voice_id!r}, model {model!r}, "
                    f"{len(text)} chars of text. {self._detail(response)}",
                )
            if status != 200:
                # Anything else (5xx, 408, an unexpected code) is transient.
                raise _Transient(
                    self._fail(
                        FishApiError,
                        f"Fish TTS returned HTTP {status} on key {redact(key)} "
                        f"({keys_tried}/{pool_size}): treating it as transient and "
                        f"retrying this same key once. {self._detail(response)}",
                    )
                )
            # 200: the body must be audio. _read_audio enforces first-byte and
            # total deadlines, the JSON-error-body check and the zero-byte rule.
            yield from self._read_audio(
                response,
                key,
                voice_id,
                first_chunk_timeout,
                total_timeout,
                started,
                attempt_started,
                keys_tried,
                pool_size,
            )
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass

    # -- reading the audio stream -----------------------------------------

    def _read_audio(
        self,
        response: Any,
        key: str,
        voice_id: str,
        first_chunk_timeout: float,
        total_timeout: float,
        started: float,
        attempt_started: float,
        keys_tried: int,
        pool_size: int,
    ) -> Iterator[bytes]:
        pump = _ChunkPump(response.iter_content(chunk_size=self.chunk_size))
        try:
            data_seen = False
            pending_whitespace = []
            # The first-byte clock restarts for each attempt, so a key that just
            # failed cannot eat the next key's first-byte budget. The total
            # budget does not restart: it bounds the whole call, failovers
            # included. The first-byte deadline is still capped by it.
            first_deadline = min(attempt_started + first_chunk_timeout, started + total_timeout)
            total_deadline = started + total_timeout

            while True:
                # Before the first byte: the short first-chunk budget. After it:
                # the remaining total budget.
                deadline = total_deadline if data_seen else first_deadline
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise self._timeout_failure(
                        data_seen, key, first_chunk_timeout, total_timeout, keys_tried, pool_size
                    )
                try:
                    kind, payload = pump.get(budget)
                except queue.Empty:
                    raise self._timeout_failure(
                        data_seen, key, first_chunk_timeout, total_timeout, keys_tried, pool_size
                    ) from None

                if kind == "chunk":
                    chunk = bytes(payload or b"")
                    if not chunk:
                        continue  # empty reads are not data and not failure
                    if not data_seen:
                        stripped = chunk.lstrip(_LEADING_WHITESPACE)
                        if not stripped:
                            # Whitespace-only so far. Keep it (it is audio
                            # padding) but do not call it the first byte yet.
                            pending_whitespace.append(chunk)
                            continue
                        if stripped[:1] == b"{":
                            # 200 + JSON = an error body wearing a success code.
                            detail = self._error_body_detail(stripped, pump)
                            raise _Transient(
                                self._fail(
                                    FishErrorBodyError,
                                    f"Fish TTS returned HTTP 200 but the body is JSON, not "
                                    f"audio (first non-whitespace byte was '{{'). "
                                    f"Voice {voice_id!r} on key "
                                    f"{redact(key)} ({keys_tried}/{pool_size}). Body: {detail}",
                                )
                            )
                        data_seen = True
                        for buffered in pending_whitespace:
                            yield buffered
                        pending_whitespace.clear()
                    yield chunk

                elif kind == "error":
                    exc = payload
                    raise _Transient(
                        self._fail(
                            FishStreamError,
                            f"Fish TTS audio stream broke on key {redact(key)} "
                            f"({keys_tried}/{pool_size}) after "
                            f"{'the first byte' if data_seen else 'no bytes'}: "
                            f"{exc.__class__.__name__}: {exc}",
                        )
                    ) from (exc if isinstance(exc, BaseException) else None)

                else:  # "end"
                    if not data_seen:
                        # Zero bytes is a failure. Reporting it as an empty
                        # success would hand the caller silence and call it
                        # speech.
                        raise _Transient(
                            self._fail(
                                FishEmptyStreamError,
                                f"Fish TTS returned HTTP 200 but the audio stream yielded "
                                f"zero bytes on key {redact(key)} ({keys_tried}/{pool_size}). "
                                f"This is a failure, not an empty success.",
                            )
                        )
                    return
        finally:
            pump.stop()  # never leave a reader thread blocked on a dead socket

    def _timeout_failure(
        self,
        data_seen: bool,
        key: str,
        first_chunk_timeout: float,
        total_timeout: float,
        keys_tried: int,
        pool_size: int,
    ) -> _Transient:
        if not data_seen:
            # Separate and much shorter than the total budget on purpose: a dead
            # request must not sit open for the full total_timeout.
            return _Transient(
                self._fail(
                    FishFirstChunkTimeoutError,
                    f"No first audio byte from Fish TTS within {first_chunk_timeout:g}s "
                    f"(total budget {total_timeout:g}s) on key {redact(key)} "
                    f"({keys_tried}/{pool_size}); aborting the request rather than "
                    f"leaving it open.",
                )
            )
        return _Transient(
            self._fail(
                FishTotalTimeoutError,
                f"Fish TTS audio stream exceeded the total budget of {total_timeout:g}s "
                f"after the first byte on key {redact(key)} ({keys_tried}/{pool_size}); "
                f"aborting.",
            )
        )

    # -- message helpers ---------------------------------------------------

    def _fail(self, error_class: type, message: str) -> FishError:
        """Build an error, with the pool's keys scrubbed out of the message."""
        return error_class(self.key_pool.redact_text(message))

    def _detail(self, response: Any) -> str:
        """A short, key-free excerpt of an error response body, if there is one."""
        try:
            raw = getattr(response, "text", "") or ""
        except Exception:  # noqa: BLE001 - a body we cannot read is not an error
            return ""
        if not isinstance(raw, str) or not raw.strip():
            return ""
        return f"Body: {self.key_pool.redact_text(raw.strip())[:200]}"

    def _error_body_detail(self, first: bytes, pump: _ChunkPump) -> str:
        """Collect a bounded JSON error body and pull the human message out."""
        buffer = bytearray(first)
        deadline = time.monotonic() + _ERROR_BODY_WAIT
        while len(buffer) < _MAX_ERROR_BODY:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                kind, payload = pump.get(remaining)
            except queue.Empty:
                break
            if kind != "chunk":
                break
            buffer.extend(bytes(payload or b""))
        text = bytes(buffer).decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            for field in ("message", "error", "detail", "msg"):
                candidate = parsed.get(field)
                if isinstance(candidate, str) and candidate:
                    text = candidate
                    break
                if isinstance(candidate, dict) and isinstance(candidate.get("message"), str):
                    text = candidate["message"]
                    break
        # Redacted here as well as in _fail: this text comes from the vendor.
        return self.key_pool.redact_text(text)[:200]
