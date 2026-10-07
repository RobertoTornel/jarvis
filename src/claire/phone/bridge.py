"""Claire's phone bridge: the owner's phone talks to her over loopback HTTP.

Spec: ``src/claire/phone/bridge.spec.md`` (R-IO-1, R-LOC-1, R-VISION-1).

The phone is the one device the owner always has with them, and it carries two
senses this laptop cannot supply: a camera and a position.  This module is the
small HTTP surface that lets the phone hand both to Claire, and hear what she
has to say back.

The eight routes, five of them canonical
----------------------------------------

========================  ============================================
``POST /text``            a typed utterance
``POST /transcribe``      one utterance of **audio** (WAV), recognised by the
                          voice loop's own engine and answered with its text
``POST /see``             one photo, plus an optional question
``POST /loc``             latitude / longitude
``GET  /reply``           whatever Claire has to say next (drained once)
``GET  /health``          reachability, masked token handle, capability verdict
``POST /location``        alias of ``/loc`` -- see below
``GET  /where``           alias of ``/reply`` -- see below
========================  ============================================

The last two are not a second design.  The phone client shipped as 0.2 calls
``/transcribe``, ``/see``, ``/location``, ``/where`` and ``/health`` on port 8731
and authenticates with ``X-Claire-Token``; this bridge serves ``/text``,
``/transcribe``, ``/see``, ``/loc``, ``/reply`` and ``/health`` and authenticates
with ``Authorization: Bearer``.  Renaming either side silently breaks a device
the owner is holding, so the drift is absorbed here -- in one table
(:data:`ROUTE_ALIASES`) and one extra accepted header
(:data:`LEGACY_TOKEN_HEADER`) -- while the canonical spellings stay canonical.

The port is 8788, not 8765: Claire's own web panel had already claimed 8765 and
``dsh web`` holds 8731, so the old default could not bind at all.

Five invariants, each enforced mechanically
------------------------------------------

1. **Authentication is mandatory, not optional.**  Every endpoint — including
   ``/health`` — requires ``Authorization: Bearer <token>``, or the
   compatibility spelling ``X-Claire-Token``.  Both are compared with
   :func:`hmac.compare_digest`, never ``==`` (which leaks the match position
   through timing).  A request with a missing or wrong token gets ``401`` and is
   *not processed*: the handler returns before the utterance sink, the
   recogniser, the photo store or the reply queue is touched.  The response body
   for "no token" and "wrong token" is byte-identical, so neither is an oracle,
   and the compatibility header is no weaker than the bearer one.
2. **The token is generated on first use and only ever reported masked.**  It is
   created with :func:`secrets.token_urlsafe` into the project's 0600 key store
   (:mod:`claire.platform.keys`, name :data:`TOKEN_KEY_NAME`) and reported as
   last-4 plus a SHA-256 fingerprint — via :meth:`PhoneBridge.masked_token`,
   never as a raw value.  No response body, log line, repr or error string in
   this module carries the token.
3. **Loopback by default, public only by explicit opt-in.**  :data:`DEFAULT_HOST`
   is ``127.0.0.1``.  Binding anywhere else raises
   :class:`PublicBindRefusedError` before a socket exists, unless the caller
   passes ``allow_public_bind=True`` — and then a loud, plain-words warning says
   the traffic is unencrypted plain HTTP and a tunnel is the right answer.
4. **No fabrication about photos.**  ``POST /see`` stores the bytes and hands
   the *path* to the existing vision seam (:func:`claire.senses.camera.
   describe_image`, or an injected describer).  If no vision model or key is
   configured, the reply says exactly that and carries no ``description`` field.
   A confident sentence about an image nobody looked at is the worst thing this
   module could produce, so it is made unreachable.
5. **Named failures with the fix.**  An oversized body, an unknown content type,
   a bad coordinate, undecodable audio and a missing sink each produce a named
   error (``code`` + ``message`` + ``remedy``) and the honest HTTP status — 413,
   415, 400, 503 — never a silent empty 200.

One recogniser, not two
-----------------------

``POST /transcribe`` does not bring its own speech-to-text.  It asks
:func:`claire.listening.stt.create_stt_engine` for the engine the voice loop is
configured with (see :attr:`PhoneBridge.transcriber`), so a transcript that
arrives from the phone was produced by the same authoritative recogniser that
would have produced it had the owner spoken into the laptop's own microphone.
Injecting an engine, or a factory, is how the tests cover the route without
loading a 0.6B model.

Injection, so tests need no socket
----------------------------------

:class:`PhoneBridge` takes its collaborators as constructor arguments — the key
store, the utterance sink, the vision describer, the reply queue, the location
sink, the recogniser and the photo directory.  :meth:`PhoneBridge.handle` is the
whole request path as an ordinary function call over ``(method, path,
content_type, body)``, and :class:`PhoneRequestHandler` is a thin adapter that
only reads a body and writes a response.  The suite in
``tests/test_phone_bridge.py`` drives ``handle`` directly for most checks — and
over a real socket for the ones where that is the only way to see the truth,
because the 411-on-every-GET bug lived precisely in the layer ``handle`` skips.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import parse_qs, urlsplit

from ..platform.keys import (
    KeyNotFoundError,
    KeyStore,
    KeyStoreError,
    fingerprint,
    mask,
)

__all__ = [
    "ALLOWED_IMAGE_CONTENT_TYPES",
    "ALLOWED_SAY_COMMANDS",
    "AuthenticationError",
    "BEARER_SCHEME",
    "BodyTooLargeError",
    "BridgeResponse",
    "CONTENT_TYPE_JSON",
    "DEFAULT_DESCRIBER",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENDPOINTS",
    "HOST_ENV",
    "InternalError",
    "LOG",
    "LengthRequiredError",
    "LocationRecord",
    "MAX_BODY_BYTES",
    "MIN_TOKEN_LENGTH",
    "MethodNotAllowedError",
    "PHOTO_DIR_ENV",
    "PORT_ENV",
    "PhoneBridge",
    "PhoneBridgeError",
    "PhoneRequestHandler",
    "PhotoStoreError",
    "PublicBindRefusedError",
    "QueueUnconfiguredError",
    "ReplyQueue",
    "RouteNotFoundError",
    "SayRejectedError",
    "SinkFailedError",
    "SinkUnconfiguredError",
    "TOKEN_KEY_NAME",
    "TokenUnavailableError",
    "UnsupportedContentTypeError",
    "InvalidPayloadError",
    "VisionDescriberError",
    "check_token",
    "create_server",
    "default_host",
    "default_photo_dir",
    "default_port",
    "is_loopback_host",
    "media_type",
    "parse_bearer_token",
    "read_request_body",
    "resolve_bind_host",
    "sanitise_for_log",
    "serve",
]

LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Facts this module is built around
# --------------------------------------------------------------------------- #

#: The bind host used when the caller does not choose one.  Loopback, always:
#: a phone bridge on a public interface is unencrypted plain HTTP.
DEFAULT_HOST = "127.0.0.1"

#: The default port.  Deliberately not 8000/8080 (shared by every dev server) and
#: deliberately not 8765 any more: Claire's own web panel
#: (``python -m claire.panel.server``) had already claimed 8765, so
#: ``python -m claire.phone serve`` could not bind at all and the two services
#: silently fought over one port.  8731, which the shipped 0.2 phone client
#: hard-coded, is held by ``dsh web`` on this machine.  8788 is free on this host
#: and is the port this project now documents for the phone bridge; set
#: ``$CLAIRE_PHONE_PORT`` to move it.
DEFAULT_PORT = 8788

#: Environment overrides, for ops and for tests.  Read at call time so a test
#: can redirect them with ``monkeypatch.setenv``.
HOST_ENV = "CLAIRE_PHONE_HOST"
PORT_ENV = "CLAIRE_PHONE_PORT"
PHOTO_DIR_ENV = "CLAIRE_PHONE_PHOTO_DIR"

#: A phone photo is megabytes, not gigabytes.  Bodies above this are refused
#: with a named 413 *before* being read into memory where possible.
MAX_BODY_BYTES = 8 * 1024 * 1024

#: The JSON content type ``/text``, ``/loc`` and the JSON form of ``/see`` take.
CONTENT_TYPE_JSON = "application/json"

#: Raw image bodies ``/see`` accepts, mapped to the extension they are stored
#: under.  Anything else is refused (415) rather than guessed at.
ALLOWED_IMAGE_CONTENT_TYPES: Dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

#: The bearer scheme, per RFC 6750.  The scheme name is case-insensitive.
BEARER_SCHEME = "Bearer"

#: The key-store name the bridge token lives under.
TOKEN_KEY_NAME = "PHONE_BRIDGE_TOKEN"

#: Entropy of a freshly generated token, in bytes (``secrets.token_urlsafe``).
TOKEN_BYTES = 32

#: A stored token shorter than this is refused rather than accepted: a weak
#: token silently authenticating the phone would be worse than a named error.
MIN_TOKEN_LENGTH = 16

#: Characters kept in a redacted rendering (the project's masking convention).
REDACT_TAIL = 4

#: An optional question longer than this is refused; it is a spoken sentence,
#: not a document.
MAX_QUESTION_CHARS = 2000

#: Raw audio bodies ``/transcribe`` accepts.  WAV only, on purpose: the phone
#: records 16-bit PCM and {@code ClaireWire} writes a canonical RIFF header, and a
#: second container (ogg/opus, m4a) would mean a second decoder and a second way
#: for the two ends to disagree.  ``audio/x-wav`` and friends are the spellings
#: Android's own HTTP stacks use for the same bytes.
ALLOWED_AUDIO_CONTENT_TYPES: FrozenSet[str] = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/vnd.wave",
    }
)

#: A longer body than this of 16 kHz mono PCM is a stuck microphone, not a
#: sentence.  120 s is the ceiling; the phone's own segmenter closes at 15 s.
MAX_AUDIO_SECONDS = 120

#: Below this there is nothing to recognise, and running a 0.6B model over 20 ms
#: of audio is a waste of the laptop's afternoon.
MIN_AUDIO_SECONDS = 0.05

#: The rate the phone records at and the rate a caller should aim for.  Not
#: enforced: the engine resamples, so an unusual rate is a fact to report, never
#: a rejection.
PREFERRED_AUDIO_SAMPLE_RATE = 16_000

#: The header the shipped 0.2 phone client authenticates with.  ``Authorization:
#: Bearer`` is the canonical one and the only one this module's spec leads with;
#: this second spelling exists so the APK already in the owner's hand keeps
#: working against the current bridge.  It is compared with the same
#: :func:`hmac.compare_digest`, and a wrong value is answered exactly like a
#: missing one.
LEGACY_TOKEN_HEADER = "X-Claire-Token"

#: Send this instead of guessing when no recogniser can be built.  Names the
#: config key, the check command and the spec, because "transcription is
#: unavailable" on its own is not actionable from a phone screen.
DEFAULT_TRANSCRIBE_REMEDY = (
    "Set 'stt_model_path' in ~/.config/claire/config.json to a *.nemo file or an "
    "ONNX export directory (see src/claire/listening/stt/stt.spec.md), check it "
    "with 'python scripts/check_stt_model.py --model-path <path>', then send the "
    "audio again.  The bridge loads the same authoritative recogniser the voice "
    "loop uses and will not invent a second one."
)

#: The whole vocabulary ``POST /say`` may name.  Deliberately tiny and closed:
#: every entry is something the shipped phone client already implements, so the
#: bridge can ask the phone to *do* a thing without the phone having to guess
#: what a new verb means.  A command outside this tuple is a named 400, never a
#: silently-ignored field.
ALLOWED_SAY_COMMANDS: Tuple[str, ...] = ("photo", "location")

#: What to do when ``/say`` names no command the phone knows.  Always names the
#: whole vocabulary, so the caller never has to read the source to recover.
DEFAULT_SAY_REMEDY = (
    "POST a JSON object naming one of the allowed commands ("
    + ", ".join(ALLOWED_SAY_COMMANDS)
    + '), for example {"command": "photo", "question": "what is this?"}, or '
    '{"text": "good morning"} to queue a sentence for the phone to say.'
)

#: Endpoint table: path -> allowed methods.  Order is the spec's order, with the
#: two compatibility spellings from the shipped 0.2 client immediately after the
#: canonical endpoint they resolve to.
ENDPOINTS: Dict[str, Tuple[str, ...]] = {
    "/text": ("POST",),
    "/say": ("POST",),
    "/transcribe": ("POST",),
    "/see": ("POST",),
    "/loc": ("POST",),
    "/location": ("POST",),
    "/reply": ("GET",),
    "/where": ("GET",),
    "/health": ("GET",),
}

#: Routes that are the *same handler* under an older name.  The shipped 0.2
#: client calls ``/location`` and ``/where``; the bridge serves ``/loc`` and
#: ``/reply``.  Rather than 404 a working phone, the alias is a one-line
#: translation, and the alias table is the only place that knows about it.
ROUTE_ALIASES: Dict[str, str] = {
    "/location": "/loc",
    "/where": "/reply",
}

#: Methods this API never reads a body for.  An ordinary client sends no
#: ``Content-Length`` with them, so demanding one turned every ``GET /health``
#: and ``GET /reply`` into a 411 before routing even happened.  Body-carrying
#: methods (POST, PUT, PATCH) still require a length: a promised body that is
#: not framed stays a named 411.
BODYLESS_METHODS: FrozenSet[str] = frozenset({"GET", "HEAD", "DELETE", "OPTIONS"})

#: Send this instead of guessing when the vision route is not configured.
DEFAULT_VISION_REMEDY = (
    "Configure the project vision route: export OMNIROUTE_API_KEY=<key> and "
    "make sure OmniRouter answers at its /v1 base URL "
    "(curl -s http://127.0.0.1:20129/v1/models), then send the photo again. "
    "See src/claire/senses/camera.spec.md."
)

#: Header a raw image POST may carry its question in.
QUESTION_HEADER = "X-Claire-Question"

#: Statuses, named so the tests and the spec can agree without magic numbers.
HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_LENGTH_REQUIRED = 411
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_UNSUPPORTED_MEDIA_TYPE = 415
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503

#: The sentinel meaning "use the project's own vision seam".  ``None`` means
#: something else entirely — *no describer is configured* — and the two must
#: never be conflated, which is why this is a distinct object.
class _ProjectDescriber:
    """Sentinel: resolve :func:`claire.senses.camera.describe_image` on use."""

    def __repr__(self) -> str:  # pragma: no cover - trivial, kept for logs
        return "<project vision seam>"


DEFAULT_DESCRIBER = _ProjectDescriber()


# --------------------------------------------------------------------------- #
# Named errors: every one carries a code, a message and a way forward
# --------------------------------------------------------------------------- #

class PhoneBridgeError(Exception):
    """Base class: a named failure with a code, a status and a remedy.

    ``str(error)`` is always safe to log: no error in this module is ever built
    from a token value.
    """

    #: Machine-readable code, stable across releases.
    code = "phone_bridge_error"
    #: HTTP status this maps to, or ``None`` when it is not an HTTP answer
    #: (a refused bind, for instance, happens before any request exists).
    status: Optional[int] = 500

    def __init__(
        self,
        message: str,
        *,
        remedy: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.remedy = remedy
        self.cause = cause

    def as_payload(self) -> Dict[str, Any]:
        """The error as the JSON object every failure response carries."""
        payload: Dict[str, Any] = {
            "type": type(self).__name__,
            "code": self.code,
            "message": str(self),
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.remedy:
            payload["remedy"] = self.remedy
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={str(self)!r})"


class AuthenticationError(PhoneBridgeError):
    """Missing or wrong bearer token.  One error for both, on purpose.

    Distinguishing them would tell an unauthenticated scanner that a route
    exists and merely wants a token, and would make the two cases separable by
    response text.  The message below is therefore identical for both.
    """

    code = "unauthorized"
    status = HTTP_UNAUTHORIZED

    def __init__(self) -> None:
        super().__init__(
            "missing or invalid bearer token; this request was not processed",
            remedy=(
                "Send the header 'Authorization: Bearer <token>'. The token "
                f"lives in the key store under {TOKEN_KEY_NAME}: read its "
                "masked handle with GET /health from the laptop, or print the "
                "raw value on the laptop itself with 'python -m claire.phone "
                f"keys show {TOKEN_KEY_NAME} --reveal'. That flag is the only "
                "way this project will show a secret, and it exists because the "
                "phone has to be given the value once."
            ),
        )


class TokenUnavailableError(PhoneBridgeError):
    """The key store could not supply or create a bridge token."""

    code = "token_unavailable"
    status = 500

    def __init__(self, message: str, *, remedy: str = "", cause: Any = None) -> None:
        super().__init__(message, remedy=remedy, cause=cause)


class InternalError(PhoneBridgeError):
    """An unexpected failure inside the bridge.  Named, logged, never silent."""

    code = "internal_error"
    status = 500

    def __init__(self, cause: BaseException, *, redact: Any = None) -> None:
        detail = f"{type(cause).__name__}: {cause}"
        if callable(redact):
            detail = redact(detail)
        super().__init__(
            f"the bridge failed while handling this request ({detail})",
            remedy=(
                "This is a bug in the phone bridge rather than something the "
                "phone did wrong. The full traceback is in Claire's log; please "
                "report it with the endpoint and method used."
            ),
            cause=cause,
        )


class BodyTooLargeError(PhoneBridgeError):
    """The request body is over the limit.  Named 413, not a truncated read."""

    code = "body_too_large"
    status = HTTP_PAYLOAD_TOO_LARGE

    def __init__(self, observed: Optional[int], limit: int) -> None:
        size = "unknown size" if observed is None else f"{observed} bytes"
        super().__init__(
            f"request body is {size}, over the {limit}-byte limit",
            remedy=(
                "Shrink the photo before sending (for example: magick photo.jpg "
                "-resize 2048x2048 smaller.jpg), or raise the limit deliberately "
                "with PhoneBridge(max_body_bytes=...) if you really mean it."
            ),
        )
        self.observed = observed
        self.limit = limit


class LengthRequiredError(PhoneBridgeError):
    """A body-bearing request arrived without a usable Content-Length."""

    code = "length_required"
    status = HTTP_LENGTH_REQUIRED

    def __init__(self, observed: Any = None) -> None:
        detail = "" if observed is None else f" (got {observed!r})"
        super().__init__(
            f"Content-Length header is missing or not a number{detail}",
            remedy=(
                "Send a Content-Length header with the exact body size. Most "
                "HTTP clients do this already; if you are hand-writing a "
                "request, add it (curl does it for --data and --data-binary)."
            ),
        )


class UnsupportedContentTypeError(PhoneBridgeError):
    """An unknown content type is refused rather than guessed at."""

    code = "unsupported_content_type"
    status = HTTP_UNSUPPORTED_MEDIA_TYPE

    def __init__(self, received: str, allowed: Sequence[str]) -> None:
        shown = received.strip() or "<no Content-Type header>"
        super().__init__(
            f"unsupported content type {shown!r} for this endpoint",
            remedy="Send one of: " + ", ".join(sorted(allowed)) + ".",
        )
        self.received = received
        self.allowed = tuple(allowed)


class InvalidPayloadError(PhoneBridgeError):
    """The body was the right type but the wrong shape."""

    code = "invalid_payload"
    status = HTTP_BAD_REQUEST

    def __init__(
        self, message: str, *, remedy: str = "", cause: Any = None
    ) -> None:
        super().__init__(message, remedy=remedy, cause=cause)


class RouteNotFoundError(PhoneBridgeError):
    """No such endpoint."""

    code = "not_found"
    status = HTTP_NOT_FOUND

    def __init__(self, path: str) -> None:
        super().__init__(
            f"no such endpoint: {path or '/'}",
            remedy="Known endpoints: " + ", ".join(
                f"{methods[0]} {route}" for route, methods in ENDPOINTS.items()
            ) + ".",
        )


class MethodNotAllowedError(PhoneBridgeError):
    """The endpoint exists but not for this method."""

    code = "method_not_allowed"
    status = HTTP_METHOD_NOT_ALLOWED

    def __init__(self, path: str, method: str, allowed: Sequence[str]) -> None:
        super().__init__(
            f"{method or '?'} is not allowed on {path}; allowed: "
            + ", ".join(allowed),
            remedy=f"Use {' or '.join(allowed)} on {path}.",
        )
        self.allowed = tuple(allowed)


class PhotoStoreError(PhoneBridgeError):
    """The photo could not be written where the vision seam can read it."""

    code = "photo_store_failed"
    status = 500

    def __init__(self, path: Any, cause: BaseException) -> None:
        super().__init__(
            f"could not store the photo at {path} "
            f"({type(cause).__name__}: {cause})",
            remedy=(
                "Make sure the photo directory exists and is writable by the "
                "user running Claire, or point the bridge elsewhere with "
                f"{PHOTO_DIR_ENV} / PhoneBridge(photo_dir=...)."
            ),
            cause=cause,
        )


class SinkUnconfiguredError(PhoneBridgeError):
    """No utterance sink was injected, so the utterance has nowhere to go."""

    code = "sink_unconfigured"
    status = HTTP_SERVICE_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(
            "no utterance sink is wired up, so the text was received but not "
            "delivered to Claire's voice loop",
            remedy=(
                "Construct the bridge with the loop's intake, for example "
                "PhoneBridge(sink=loop.submit_text). Until then /text cannot "
                "do anything honest with an utterance."
            ),
        )


class SinkFailedError(PhoneBridgeError):
    """The sink raised: the utterance did not reach Claire."""

    code = "sink_failed"
    status = 500

    def __init__(self, cause: BaseException) -> None:
        super().__init__(
            f"the utterance sink raised {type(cause).__name__}: {cause}",
            remedy=(
                "The text did not reach the voice loop. Check the sink "
                "implementation and Claire's logs, then send it again."
            ),
            cause=cause,
        )


class QueueUnconfiguredError(PhoneBridgeError):
    """No reply queue is wired up, so there is nowhere to put the reply."""

    code = "queue_unconfigured"
    status = HTTP_SERVICE_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(
            "no reply queue is wired up, so the item was received but not "
            "queued for the phone to drain",
            remedy=(
                "Construct the bridge with the queue the phone drains, for "
                "example PhoneBridge(queue=ReplyQueue()). Until then /say "
                "cannot do anything honest with an item."
            ),
        )


class SayRejectedError(PhoneBridgeError):
    """``POST /say`` named a command the phone does not know.

    The remedy always spells out the whole accepted command vocabulary — a
    caller should never have to read the routing table to recover from a typo.
    """

    code = "say_rejected"
    status = HTTP_BAD_REQUEST

    def __init__(self, message: str, *, remedy: str = "") -> None:
        super().__init__(message, remedy=remedy or DEFAULT_SAY_REMEDY)


class AudioDecodeError(PhoneBridgeError):
    """The audio body was the right media type but not decodable WAV."""

    code = "invalid_audio"
    status = HTTP_BAD_REQUEST

    def __init__(self, message: str, *, remedy: str = "", cause: Any = None) -> None:
        super().__init__(
            message,
            remedy=remedy
            or (
                "Send 16-bit PCM mono WAV at 16000 Hz: one RIFF/WAVE header and "
                "one 'data' chunk. The phone's own ClaireWire.wav() writes exactly "
                "that, and android/tools/WireSelfTest.java checks it byte for byte."
            ),
            cause=cause,
        )


class TranscriptionUnavailableError(PhoneBridgeError):
    """No authoritative recogniser could be built, so the audio cannot be read."""

    code = "transcription_unavailable"
    status = HTTP_SERVICE_UNAVAILABLE

    def __init__(self, message: str, *, remedy: str = DEFAULT_TRANSCRIBE_REMEDY,
                 cause: Any = None) -> None:
        super().__init__(message, remedy=remedy, cause=cause)


class TranscriptionFailedError(PhoneBridgeError):
    """The recogniser was built but failed on this audio."""

    code = "transcription_failed"
    status = HTTP_BAD_GATEWAY

    def __init__(self, cause: BaseException) -> None:
        super().__init__(
            f"the speech recogniser raised {type(cause).__name__}: {cause}",
            remedy=(
                "The audio reached the laptop but was not recognised. Send it "
                "again; if it fails every time, check the recogniser with "
                "'python scripts/check_stt_model.py' and read Claire's logs."
            ),
            cause=cause,
        )


class LocationSinkFailedError(PhoneBridgeError):
    """The location sink raised: the coordinates were recorded but not filed."""

    code = "location_sink_failed"
    status = 500

    def __init__(self, cause: BaseException) -> None:
        super().__init__(
            f"the location sink raised {type(cause).__name__}: {cause}",
            remedy=(
                "The coordinates were recorded on the bridge but the consumer "
                "failed; check the sink and Claire's logs, then send /loc again."
            ),
            cause=cause,
        )


class VisionDescriberError(PhoneBridgeError):
    """The injected vision describer is not usable at all."""

    code = "vision_describer_unusable"
    status = 500

    def __init__(self, describer: Any) -> None:
        super().__init__(
            f"the configured vision describer {type(describer).__name__} is "
            "neither callable nor an object with a describe(path, question) "
            "method",
            remedy=(
                "Pass a function taking (image_path, question), or an object "
                "with a describe(image_path, question) method — the same shape "
                "claire.senses.camera.describe_image accepts."
            ),
        )


class PublicBindRefusedError(PhoneBridgeError):
    """A non-loopback bind was requested without the explicit opt-in."""

    code = "public_bind_refused"
    status = None

    def __init__(self, host: str, port: int) -> None:
        super().__init__(
            f"refusing to bind the phone bridge to {host!r}: that is not a "
            "loopback address",
            remedy=(
                "Traffic on this bridge is unencrypted plain HTTP, so a public "
                "interface exposes your utterances, photos and location - and "
                "the bearer token itself - to anyone on the network. Keep the "
                f"default ({DEFAULT_HOST}) and reach it through a tunnel (for "
                f"example: ssh -N -L {port}:{DEFAULT_HOST}:{port} owner@laptop), "
                "or pass allow_public_bind=True if you have read that and mean "
                "it anyway."
            ),
        )
        self.host = host
        self.port = port


# --------------------------------------------------------------------------- #
# Small, testable helpers
# --------------------------------------------------------------------------- #

def media_type(content_type: Optional[str]) -> str:
    """The bare, lower-cased media type of a Content-Type header.

    ``"Application/JSON; charset=utf-8"`` -> ``"application/json"``.  An absent
    header gives ``""`` so callers refuse rather than guess.
    """
    if not content_type:
        return ""
    return str(content_type).split(";", 1)[0].strip().lower()


#: Control characters that must never reach a log line or a terminal.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: How much of a client-supplied string one log line may carry.
LOG_MAX_CHARS = 200


def sanitise_for_log(text: Any, limit: int = LOG_MAX_CHARS) -> str:
    """One log line's worth of a client-supplied string.

    A path and a query string are attacker-controlled.  Without this, a newline
    in a request path forges a second log entry — which could fake the very
    lines this module promises never contain a token — and a 4 KB path drowns
    the entry it belongs to.  Control characters become ``?`` and the result is
    length-capped.
    """
    cleaned = _CONTROL_CHARS.sub("?", str(text))
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned


def is_loopback_host(host: Any) -> bool:
    """True only for addresses that cannot leave this machine.

    ``"localhost"`` and every address in ``127.0.0.0/8`` or ``::1`` qualify.  A
    name that merely *resolves* to loopback does not: resolution is not a
    security decision, and DNS could answer differently later.
    """
    if not host:
        return False
    text = str(host).strip().strip("[]").lower()
    if not text:
        return False
    if text == "localhost":
        return True
    if text == "::1" or text == "0:0:0:0:0:0:0:1":
        return True
    parts = text.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        octets = [int(part) for part in parts]
        if any(value > 255 for value in octets):
            return False
        return octets[0] == 127
    return False


def check_token(expected: Optional[str], supplied: Optional[str]) -> bool:
    """Constant-time bearer-token comparison.

    :func:`hmac.compare_digest` and nothing else: ``==`` stops at the first
    differing byte, which lets an attacker recover a token one character at a
    time from response timing.  There is no early ``==``-shaped return here, and
    an empty stored token authenticates nobody (it is compared against a
    sentinel that cannot match).
    """
    want = (expected or "").encode("utf-8")
    given = (supplied or "").encode("utf-8")
    if not want:
        # No usable token configured: compare against a sentinel so this path
        # costs the same as a real comparison and can never succeed.
        return hmac.compare_digest(b"\x00", given)
    return hmac.compare_digest(want, given)


def parse_bearer_token(header: Optional[str]) -> Optional[str]:
    """The token from an ``Authorization: Bearer <token>`` header, or ``None``.

    ``None`` means "no credentials were offered" (absent header, wrong scheme,
    empty token) and is answered with 401 without ever loading the stored
    token.
    """
    if not header:
        return None
    text = str(header).strip()
    scheme, separator, value = text.partition(" ")
    if not separator:
        return None
    if scheme.strip().lower() != BEARER_SCHEME.lower():
        return None
    token = value.strip()
    if not token:
        return None
    return token


def default_host(env: Optional[Mapping[str, str]] = None) -> str:
    """The bind host: ``$CLAIRE_PHONE_HOST`` if set, else loopback."""
    source = os.environ if env is None else env
    value = str(source.get(HOST_ENV, "") or "").strip()
    return value or DEFAULT_HOST


def default_port(env: Optional[Mapping[str, str]] = None) -> int:
    """The bind port: ``$CLAIRE_PHONE_PORT`` if set, else :data:`DEFAULT_PORT`.

    A malformed override raises a named :class:`ValueError` rather than being
    silently replaced with the default, because "I set the port and it listened
    somewhere else" is a confusing failure.
    """
    source = os.environ if env is None else env
    raw = str(source.get(PORT_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{PORT_ENV}={raw!r} is not a number; set it to a port between 1 "
            f"and 65535, or unset it to use {DEFAULT_PORT}"
        ) from exc
    if not (1 <= value <= 65535):
        raise ValueError(
            f"{PORT_ENV}={value} is out of range; set it to a port between 1 "
            f"and 65535, or unset it to use {DEFAULT_PORT}"
        )
    return value


def default_photo_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """Where phone photos are written when the caller does not choose.

    ``$CLAIRE_PHONE_PHOTO_DIR``, else ``$XDG_CACHE_HOME/claire/phone``, else
    ``~/.cache/claire/phone`` — beside the camera frames, outside the
    repository, never in it.
    """
    source = os.environ if env is None else env
    override = str(source.get(PHOTO_DIR_ENV, "") or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(source.get("XDG_CACHE_HOME", "") or "").strip() or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / "claire" / "phone"


def resolve_bind_host(
    host: Optional[str],
    port: int,
    allow_public_bind: bool = False,
) -> str:
    """Validate a bind host, refusing a public interface unless opted in.

    Loopback binds pass through silently.  A non-loopback host raises
    :class:`PublicBindRefusedError` — *before* any socket is created — unless
    ``allow_public_bind`` is true, in which case a loud warning in plain words
    says the traffic is unencrypted and a tunnel is the right answer.
    """
    resolved = str(host or "").strip() or DEFAULT_HOST
    if is_loopback_host(resolved):
        return resolved
    if not allow_public_bind:
        raise PublicBindRefusedError(resolved, port)
    LOG.warning(
        "SECURITY: the phone bridge is bound to %s:%d, which is NOT loopback. "
        "Everything the phone sends - utterances, photos, your location - and "
        "the bearer token itself travel as unencrypted plain HTTP, readable and "
        "replayable by anyone on that network. A tunnel is the right answer: "
        "keep the bridge on %s and forward to it (ssh -N -L %d:%s:%d "
        "owner@laptop), or put it behind WireGuard. Continuing only because "
        "allow_public_bind=True was passed explicitly.",
        resolved,
        port,
        DEFAULT_HOST,
        port,
        DEFAULT_HOST,
        port,
    )
    return resolved


def read_request_body(
    content_length: Any,
    stream: Any,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> bytes:
    """Read exactly the declared body, refusing an oversized one up front.

    A ``Content-Length`` over the limit is refused *before* reading, so a
    hostile 10 GB declaration costs nothing; the actual bytes read are then
    capped again, so a lying header cannot get past either.  No socket is
    involved: ``stream`` is anything with ``read(n)``.
    """
    if content_length is None or (isinstance(content_length, str) and not content_length.strip()):
        raise LengthRequiredError(content_length)
    try:
        length = int(str(content_length).strip())
    except (TypeError, ValueError) as exc:
        raise LengthRequiredError(content_length) from exc
    if length < 0:
        raise InvalidPayloadError(
            f"Content-Length is negative ({length})",
            remedy="Send the exact number of body bytes, or 0 for no body.",
        )
    if length > max_body_bytes:
        raise BodyTooLargeError(length, max_body_bytes)
    return _read_exactly(stream, length, max_body_bytes)


def _read_exactly(stream: Any, length: int, max_body_bytes: int) -> bytes:
    """Read ``length`` bytes in bounded chunks, refusing to exceed the cap."""
    if length <= 0:
        return b""
    chunks: List[bytes] = []
    remaining = length
    while remaining > 0:
        block = stream.read(min(remaining, 65536))
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    body = b"".join(chunks)
    if len(body) > max_body_bytes:
        raise BodyTooLargeError(len(body), max_body_bytes)
    return body


def _header(headers: Any, name: str) -> str:
    """A header value from a mapping, case-insensitively, or ``""``."""
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    value = getter(name)
    if value is not None:
        return str(value)
    try:
        items = dict(headers).items()
    except (TypeError, ValueError):
        return ""
    wanted = name.lower()
    for key, item in items:
        if str(key).lower() == wanted:
            return str(item)
    return ""


def _query_value(query: Any, name: str) -> str:
    """One query parameter from a mapping (``parse_qs`` shape) or raw string."""
    if not query:
        return ""
    if isinstance(query, str):
        parsed = parse_qs(query.lstrip("?"), keep_blank_values=True)
        values = parsed.get(name) or []
        return str(values[0]) if values else ""
    getter = getattr(query, "get", None)
    if getter is None:
        return ""
    value = getter(name)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value)


# --------------------------------------------------------------------------- #
# Values passed around inside the bridge
# --------------------------------------------------------------------------- #

@dataclass
class BridgeResponse:
    """One HTTP answer: a status, a JSON payload and any extra headers."""

    status: int
    payload: Dict[str, Any]
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status < 400

    def body(self) -> bytes:
        """The response body: JSON, UTF-8, never containing a raw token."""
        return json.dumps(self.payload, sort_keys=True, default=str).encode("utf-8")

    def content_type(self) -> str:
        return "application/json; charset=utf-8"


@dataclass(frozen=True)
class LocationRecord:
    """One position report from the phone."""

    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None
    received_at: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "received_at": self.received_at,
        }
        if self.accuracy_m is not None:
            payload["accuracy_m"] = self.accuracy_m
        return payload


class ReplyQueue:
    """A thread-safe, drain-once queue of things Claire has to say.

    The two threads this exists for are a request thread (``GET /reply``) and
    the voice loop (which puts a sentence in when Claire answers).  Every
    operation takes the same :class:`threading.RLock`, and :meth:`drain` swaps
    the list out in one step — so an item is handed out **once**, and never
    handed out again.  A queue that replayed the same sentence on every poll
    would make the phone say "yes?" forever.
    """

    def __init__(self, items: Optional[Sequence[Any]] = None) -> None:
        self._items: List[Dict[str, Any]] = [self.as_item(item) for item in (items or ())]
        self._lock = threading.RLock()

    @staticmethod
    def as_item(item: Any) -> Dict[str, Any]:
        """Normalise an item to the JSON object ``GET /reply`` returns."""
        if isinstance(item, dict):
            return dict(item)
        if isinstance(item, str):
            return {"kind": "text", "text": item}
        return {"kind": "object", "text": str(item)}

    @property
    def lock(self) -> threading.RLock:
        """The guard itself, exposed so a test can prove there is one."""
        return self._lock

    def put(self, item: Any) -> Dict[str, Any]:
        """Append one item, returning it as stored."""
        stored = self.as_item(item)
        with self._lock:
            self._items.append(stored)
        return stored

    def put_text(self, text: str) -> Dict[str, Any]:
        """Append a spoken sentence."""
        return self.put({"kind": "text", "text": str(text)})

    def drain(self) -> List[Dict[str, Any]]:
        """Take every queued item, leaving the queue empty."""
        with self._lock:
            items = self._items
            self._items = []
            return items

    def peek(self) -> Tuple[Dict[str, Any], ...]:
        """A copy of the queue, without consuming it."""
        with self._lock:
            return tuple(dict(item) for item in self._items)

    def clear(self) -> None:
        """Drop everything queued."""
        with self._lock:
            self._items = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __repr__(self) -> str:
        return f"ReplyQueue(depth={len(self)})"


@dataclass(frozen=True)
class VisionOutcome:
    """What the vision seam actually gave back — seen words, or a named why-not.

    ``seen`` is only True with a non-empty description, so no caller can speak a
    confident sentence about an image nobody looked at.
    """

    seen: bool
    description: str = ""
    code: str = ""
    message: str = ""
    remedy: str = ""
    status: int = HTTP_BAD_GATEWAY


# --------------------------------------------------------------------------- #
# The bridge itself
# --------------------------------------------------------------------------- #

class PhoneBridge:
    """The whole request path, with every collaborator injected.

    Parameters
    ----------
    store:
        A :class:`~claire.platform.keys.KeyStore`, or a path for one.  ``None``
        builds the default 0600 store lazily, so constructing a bridge with
        injected collaborators never touches ``~/.config``.
    sink:
        Called with one utterance dict per ``POST /text``.  ``None`` means
        nothing is wired up yet and ``/text`` answers with a named 503 instead
        of quietly dropping the words.
    describer:
        :data:`DEFAULT_DESCRIBER` resolves the project's own vision seam
        (:func:`claire.senses.camera.describe_image`); ``None`` means *no
        describer is configured* and ``/see`` says so; anything else is used as
        an object with ``describe(image_path, question)`` or a plain callable
        with that signature.
    queue:
        The :class:`ReplyQueue` ``GET /reply`` drains.  A fresh one by default.
    transcriber:
        The authoritative recogniser ``POST /transcribe`` uses: an object with
        ``transcribe(samples, sample_rate)`` and ``identity()``, which is exactly
        :class:`claire.listening.stt.SttEngine`.  ``None`` resolves the project's
        own engine through :func:`claire.listening.stt.create_stt_engine` from
        :func:`claire.config.load_settings` — the *same* recogniser the voice
        loop uses, so there is one transcription stack in this project and not
        two.  A caller may inject a fake here, which is how the tests cover the
        route without loading a 0.6B model.
    transcriber_factory:
        A zero-argument callable returning such an engine, called at most once,
        for a caller that wants the default resolution but deferred (the model
        takes tens of seconds to load, and importing it at construction time
        would make ``--help`` slow).
    location_sink:
        Optional callable given the :class:`LocationRecord` on ``POST /loc``.
    photo_dir:
        Where photos are stored.  Defaults to :func:`default_photo_dir`, created
        0700 only when a photo actually arrives.
    max_body_bytes:
        The 413 threshold.  Defaults to :data:`MAX_BODY_BYTES` (8 MiB).
    env:
        Mapping consulted for host/port/photo-dir defaults; ``os.environ`` at
        call time by default, so tests can redirect it.
    clock:
        Injectable time source, so a record's timestamp is testable.
    """

    def __init__(
        self,
        *,
        store: Any = None,
        sink: Optional[Callable[[Dict[str, Any]], Any]] = None,
        describer: Any = DEFAULT_DESCRIBER,
        queue: Optional[ReplyQueue] = None,
        location_sink: Optional[Callable[[LocationRecord], Any]] = None,
        photo_dir: Any = None,
        transcriber: Any = None,
        transcriber_factory: Optional[Callable[[], Any]] = None,
        max_body_bytes: int = MAX_BODY_BYTES,
        host: Optional[str] = None,
        port: Optional[int] = None,
        env: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self._env = env
        self._store = store
        self._sink = sink
        self._describer = describer
        self._resolved_describer: Any = None
        self._transcriber = transcriber
        self._transcriber_factory = transcriber_factory
        self._resolved_transcriber: Any = None
        self._transcriber_failed: Optional[str] = None
        self._queue = queue if queue is not None else ReplyQueue()
        self._location_sink = location_sink
        self._photo_dir = photo_dir
        self.max_body_bytes = int(max_body_bytes)
        self._clock = clock
        self.host = str(host).strip() if host else default_host(self._env)
        self.port = int(port) if port else default_port(self._env)
        self._token: Optional[str] = None
        self._token_ready = False
        self._last_location: Optional[LocationRecord] = None
        self._lock = threading.RLock()

    # -- introspection ---------------------------------------------------- #

    def __repr__(self) -> str:
        # Never the token, not even masked: a repr is the most likely thing to
        # be pasted into a bug report.
        return (
            f"PhoneBridge(host={self.host!r}, port={self.port}, "
            f"queue_depth={len(self._queue)}, "
            f"vision={self.vision_status()!r}, "
            f"max_body_bytes={self.max_body_bytes})"
        )

    @property
    def env(self) -> Mapping[str, str]:
        """The environment consulted for defaults (``os.environ`` by default)."""
        return self._env if self._env is not None else os.environ

    @property
    def store(self) -> KeyStore:
        """The key store, built lazily so construction has no side effects."""
        with self._lock:
            if self._store is None:
                self._store = KeyStore()
            elif isinstance(self._store, (str, os.PathLike)):
                self._store = KeyStore(self._store)
            return self._store

    @property
    def sink(self) -> Optional[Callable[[Dict[str, Any]], Any]]:
        return self._sink

    @property
    def queue(self) -> ReplyQueue:
        return self._queue

    @property
    def photo_dir(self) -> Path:
        """Where photos land; created (0700) only when one is stored."""
        with self._lock:
            if self._photo_dir is None:
                self._photo_dir = default_photo_dir(self._env)
            return Path(self._photo_dir)

    @property
    def last_location(self) -> Optional[LocationRecord]:
        """The most recent ``POST /loc``, or ``None`` if none has arrived."""
        with self._lock:
            return self._last_location

    def vision_status(self) -> str:
        """``"configured"``, ``"project_default"`` or ``"not_configured"``."""
        if self._describer is None:
            return "not_configured"
        if self._describer is DEFAULT_DESCRIBER:
            return "project_default"
        return "configured"

    def transcribe_status(self) -> str:
        """``"configured"``, ``"project_default"`` or ``"not_configured"``.

        Mirrors :meth:`vision_status`, with one extra state: once a resolution
        attempt has failed, the truthful answer is not "not_configured" any more
        — something *is* configured and it does not work — so the property
        reports the failure rather than pretending nothing was asked.
        """
        if self._transcriber_failed is not None:
            return "failed:%s" % self._transcriber_failed
        if self._transcriber is not None:
            return "configured"
        if self._resolved_transcriber is not None:
            return "configured"
        return "project_default"

    @property
    def transcriber(self) -> Any:
        """The recogniser, resolved once and then remembered.

        Resolution order, and there is no fourth: an injected engine, then an
        injected factory, then the project's own
        :func:`~claire.listening.stt.create_stt_engine` over
        :func:`~claire.config.load_settings`.  A failure is remembered as a named
        503 rather than retried on every request — reloading a 2.5 GB checkpoint
        per utterance would be a worse bug than the one being reported.
        """
        if self._transcriber is not None:
            return self._transcriber
        if self._resolved_transcriber is not None:
            return self._resolved_transcriber
        if self._transcriber_failed is not None:
            raise TranscriptionUnavailableError(
                "no speech recogniser is available: %s" % self._transcriber_failed
            )
        try:
            if self._transcriber_factory is not None:
                engine = self._transcriber_factory()
            else:
                engine = self._build_project_transcriber()
        except TranscriptionUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - named, remembered, never silent
            self._transcriber_failed = "%s: %s" % (type(exc).__name__, exc)
            LOG.warning(
                "phone bridge could not build a recogniser for /transcribe (%s)",
                type(exc).__name__,
            )
            raise TranscriptionUnavailableError(
                "no speech recogniser could be built (%s: %s)"
                % (type(exc).__name__, exc),
                cause=exc,
            ) from exc
        if engine is None:
            self._transcriber_failed = "the factory returned None"
            raise TranscriptionUnavailableError(
                "the transcriber factory returned None, so there is nothing to "
                "recognise with"
            )
        self._resolved_transcriber = engine
        LOG.info("phone bridge resolved a recogniser for /transcribe (%s)", self._engine_name(engine))
        return engine

    @staticmethod
    def _engine_name(engine: Any) -> str:
        """The engine's own name, or its class name.  Never anything secret."""
        name = getattr(engine, "name", None)
        return str(name) if name else type(engine).__name__

    @staticmethod
    def _build_project_transcriber() -> Any:
        """The voice loop's recogniser, built from the project's configuration."""
        from ..config import load_settings
        from ..listening.stt import create_stt_engine

        return create_stt_engine(load_settings())

    # -- the token -------------------------------------------------------- #

    def token(self) -> str:
        """The bridge token, generating and storing one on first use.

        The raw value leaves this method and is used for one thing only: the
        constant-time comparison in :meth:`authorize`.  Everything else reports
        :meth:`masked_token`.
        """
        with self._lock:
            if self._token_ready and self._token:
                return self._token
            try:
                value = self.store.get(TOKEN_KEY_NAME)
            except KeyNotFoundError:
                value = self._generate_token()
            except (KeyStoreError, OSError, ValueError) as exc:
                # Every way the store can fail to be read is one named,
                # actionable error.  A raw OSError escaping from here would be
                # an unexplained 500 at the phone, which is exactly the silence
                # this project refuses.
                raise TokenUnavailableError(
                    f"could not read {TOKEN_KEY_NAME} from the key store at "
                    f"{self.store.path} ({type(exc).__name__}: {exc})",
                    remedy=(
                        "Check the key store is readable and not corrupt: "
                        f"python -m claire.phone keys list, or move {self.store.path} "
                        f"aside and let Claire create a fresh one; or set a fresh "
                        f"token with: python -m claire.phone keys set "
                        f"{TOKEN_KEY_NAME}"
                    ),
                    cause=exc,
                ) from exc
            if len(value) < MIN_TOKEN_LENGTH:
                raise TokenUnavailableError(
                    f"the stored {TOKEN_KEY_NAME} is shorter than "
                    f"{MIN_TOKEN_LENGTH} characters, so it is too weak to "
                    "authenticate the phone",
                    remedy=(
                        "Replace it with a fresh 32-byte token: python -m "
                        f"claire.phone keys set {TOKEN_KEY_NAME}"
                    ),
                )
            self._token = value
            self._token_ready = True
            LOG.info(
                "phone bridge token ready (%s)", mask(value)
            )
            return value

    def _generate_token(self) -> str:
        """Mint a token and store it 0600, or fail with a named error."""
        value = secrets.token_urlsafe(TOKEN_BYTES)
        try:
            self.store.store(TOKEN_KEY_NAME, value)
        except (KeyStoreError, OSError, ValueError) as exc:
            raise TokenUnavailableError(
                f"could not write {TOKEN_KEY_NAME} to the key store at "
                f"{self.store.path} ({type(exc).__name__}: {exc})",
                remedy=(
                    "Make sure the key store directory is writable by the user "
                    "running Claire, or point the bridge at another store with "
                    "PhoneBridge(store=KeyStore('/some/where/keys.json'))."
                ),
                cause=exc,
            ) from exc
        LOG.info("phone bridge generated a new token (%s)", mask(value))
        return value

    def masked_token(self) -> str:
        """The only rendering of the token this module reports: last-4 + tag."""
        return mask(self.token())

    def token_fingerprint(self) -> str:
        """A short, non-reversible handle for the token, for logs and /health."""
        return fingerprint(self.token())

    def redact(self, text: Any) -> str:
        """Replace a token already in memory with its masked rendering.

        Applied to anything logged after a token exists, so an upstream that
        echoes the Authorization header back cannot smuggle the raw value into a
        log line.  It never *generates* a token just to redact a message.
        """
        rendered = str(text)
        token = self._token
        if token:
            rendered = rendered.replace(token, mask(token))
        return rendered

    def authorize(self, headers: Any) -> None:
        """Raise :class:`AuthenticationError` unless the request is signed.

        Two spellings are accepted, and both are compared with
        :func:`hmac.compare_digest`:

        * ``Authorization: Bearer <token>`` — canonical, per RFC 6750, and the
          only one the spec leads with;
        * ``X-Claire-Token: <token>`` — what the shipped 0.2 phone client sends
          (see :data:`LEGACY_TOKEN_HEADER`).  It exists so the APK already in the
          owner's hand keeps working, and it is not a weaker path: a wrong value
          is answered with the same byte-identical 401 as a missing one, and a
          request carrying *both* headers is accepted only if one of them
          matches.

        Returns ``None`` and says nothing on success, so a caller cannot
        accidentally branch on a truthy token.
        """
        supplied = parse_bearer_token(_header(headers, "Authorization"))
        if supplied is None:
            legacy = _header(headers, LEGACY_TOKEN_HEADER)
            supplied = str(legacy).strip() if legacy is not None else None
        if not supplied:
            LOG.warning(
                "phone bridge refused a request: no bearer token was offered"
            )
            raise AuthenticationError()
        expected = self.token()
        if not check_token(expected, supplied):
            LOG.warning(
                "phone bridge refused a request: bearer token did not match (%s)",
                mask(expected),
            )
            raise AuthenticationError()

    # -- the request path ------------------------------------------------- #

    def handle(
        self,
        method: str,
        path: str,
        *,
        content_type: str = "",
        body: bytes = b"",
        headers: Any = None,
        query: Any = None,
    ) -> BridgeResponse:
        """Answer one request.  No socket, no thread, no network.

        The order is deliberate: authenticate, then route, then check the body
        size, then dispatch.  An unauthenticated request is answered 401 before
        any collaborator is touched, and nothing about the route table leaks to
        a caller without a token.
        """
        verb = str(method or "").strip().upper()
        route = str(path or "").split("?", 1)[0].split("#", 1)[0] or "/"
        try:
            self.authorize(headers)
            if route not in ENDPOINTS:
                raise RouteNotFoundError(route)
            allowed = ENDPOINTS[route]
            if verb not in allowed:
                raise MethodNotAllowedError(route, verb, allowed)
            if len(body) > self.max_body_bytes:
                raise BodyTooLargeError(len(body), self.max_body_bytes)
            response = self._dispatch(route, content_type, body, headers, query)
        except PhoneBridgeError as exc:
            response = self._error_response(exc)
        except Exception as exc:  # noqa: BLE001 - named and logged, never silent
            LOG.exception(
                "phone bridge failed handling %s %s (%s)",
                sanitise_for_log(verb or "?"),
                sanitise_for_log(route),
                type(exc).__name__,
            )
            response = self._error_response(InternalError(exc, redact=self.redact))
        LOG.info(
            "phone bridge %s %s -> %d",
            sanitise_for_log(verb or "?"),
            sanitise_for_log(route),
            response.status,
        )
        return response

    def _dispatch(
        self,
        route: str,
        content_type: str,
        body: bytes,
        headers: Any,
        query: Any,
    ) -> BridgeResponse:
        route = ROUTE_ALIASES.get(route, route)
        if route == "/health":
            return self._health()
        if route == "/reply":
            return self._reply()
        if route == "/text":
            return self._text(content_type, body)
        if route == "/say":
            return self._say(content_type, body)
        if route == "/transcribe":
            return self._transcribe(content_type, body)
        if route == "/loc":
            return self._loc(content_type, body)
        return self._see(content_type, body, headers, query)

    def _error_response(self, exc: PhoneBridgeError) -> BridgeResponse:
        headers: Dict[str, str] = {}
        if exc.status == HTTP_UNAUTHORIZED:
            headers["WWW-Authenticate"] = BEARER_SCHEME
        if isinstance(exc, MethodNotAllowedError):
            headers["Allow"] = ", ".join(exc.allowed)
        return BridgeResponse(
            status=exc.status if exc.status is not None else 500,
            payload={"ok": False, "error": exc.as_payload()},
            headers=headers,
        )

    # -- endpoints -------------------------------------------------------- #

    def _health(self) -> BridgeResponse:
        """Reachability plus the capability verdict.  Authenticated like the rest."""
        location = self.last_location
        return BridgeResponse(
            status=HTTP_OK,
            payload={
                "ok": True,
                "status": "ok",
                "host": self.host,
                "port": self.port,
                "loopback": is_loopback_host(self.host),
                "token": self.masked_token(),
                "token_fingerprint": self.token_fingerprint(),
                "queue_depth": len(self._queue),
                "vision": self.vision_status(),
                "transcribe": self.transcribe_status(),
                "photo_dir": str(self.photo_dir),
                "max_body_bytes": self.max_body_bytes,
                "location": location.as_dict() if location else None,
                "endpoints": {route: list(methods) for route, methods in ENDPOINTS.items()},
            },
        )

    def _reply(self) -> BridgeResponse:
        """Whatever Claire has to say next, handed out exactly once."""
        items = self._queue.drain()
        return BridgeResponse(
            status=HTTP_OK,
            payload={"ok": True, "items": items, "count": len(items)},
        )

    def _text(self, content_type: str, body: bytes) -> BridgeResponse:
        data = self._json_object(content_type, body)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidPayloadError(
                "the 'text' field is missing or empty",
                remedy='POST {"text": "what you want to say to Claire"}.',
            )
        if self._sink is None:
            raise SinkUnconfiguredError()
        if not callable(self._sink):
            raise SinkUnconfiguredError()
        utterance = {
            "kind": "text",
            "text": text,
            "source": "phone",
            "received_at": self._clock(),
        }
        try:
            self._sink(utterance)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise SinkFailedError(exc) from exc
        return BridgeResponse(
            status=HTTP_ACCEPTED,
            payload={
                "ok": True,
                "accepted": True,
                "chars": len(text),
                "received_at": utterance["received_at"],
            },
        )

    def _say(self, content_type: str, body: bytes) -> BridgeResponse:
        """Queue one item for the phone, from Claire's own processes.

        ``/text`` carries something the *phone* said to Claire; this is the
        channel back.  It exists because the bridge is normally its own process
        (``python -m claire.phone serve``), so the voice loop cannot call
        :meth:`ReplyQueue.put_text` on the live queue: it has to speak to the
        bridge over HTTP like any other client.  The item lands in the same
        drain-once queue ``GET /reply`` already serves, so nothing the phone
        polls has to change.

        The body is either ``{"text": "..."}`` — stored exactly as
        :meth:`ReplyQueue.put_text` would store it — or a command naming one of
        :data:`ALLOWED_SAY_COMMANDS`.  ``kind`` is inferred when omitted (a
        ``command`` key means a command, else text), and when given it wins, so
        ``{"text": "...", "kind": "command", "command": "photo"}`` is a command
        that still carries the text.  Every other key in the body is preserved
        verbatim.
        """
        data = self._json_object(content_type, body)
        kind = data.get("kind") if "kind" in data else None
        if kind is None:
            kind = "command" if "command" in data else "text"
        if kind == "command":
            item = self._say_command_item(data)
        elif kind == "text":
            item = self._say_text_item(data)
        else:
            raise SayRejectedError(
                "the 'kind' field must be 'text' or 'command', not "
                f"{kind!r}"
            )
        if self._queue is None:
            raise QueueUnconfiguredError()
        stored = self._queue.put(item)
        return BridgeResponse(
            status=HTTP_OK,
            payload={"ok": True, "queued": stored, "count": len(self._queue)},
        )

    @staticmethod
    def _say_command_item(data: Mapping[str, Any]) -> Dict[str, Any]:
        """The command item for a ``/say`` body, or a named 400."""
        raw = data.get("command")
        command = raw.strip() if isinstance(raw, str) else ""
        if command not in ALLOWED_SAY_COMMANDS:
            raise SayRejectedError(
                "the 'command' field must name one of the commands the phone "
                "knows (" + ", ".join(ALLOWED_SAY_COMMANDS) + f"); got {raw!r}"
            )
        item = dict(data)
        item["kind"] = "command"
        item["command"] = command
        return item

    @staticmethod
    def _say_text_item(data: Mapping[str, Any]) -> Dict[str, Any]:
        """The text item for a ``/say`` body, or a named 400."""
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidPayloadError(
                "the 'text' field is missing or empty",
                remedy=(
                    'POST {"text": "what Claire should say"} to queue a '
                    "sentence, or a command: "
                    + ", ".join(ALLOWED_SAY_COMMANDS)
                    + '.'
                ),
            )
        item = dict(data)
        item["kind"] = "text"
        item["text"] = text
        return item

    def _transcribe(self, content_type: str, body: bytes) -> BridgeResponse:
        """Audio in, authoritative text out, and the utterance on to Claire.

        This is the one endpoint that is not a translation of something the
        phone can already do: an always-listening client has *audio*, and until
        now the bridge only accepted text.  The recogniser is the voice loop's
        own (see :attr:`transcriber`), never a second one, so a transcript this
        route produces is the same text the microphone on the laptop would have
        produced.

        Differs from ``/text`` in exactly one honest way: the transcript is the
        answer, so a caller without a wired sink still gets the words back, with
        ``accepted: false`` and a note saying they were not delivered.  A phone
        whose laptop is not running Claire should still be able to show what it
        heard.
        """
        samples, sample_rate, seconds = decode_wav_audio(body, content_type)
        engine = self.transcriber
        try:
            result = engine.transcribe(samples, sample_rate)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise TranscriptionFailedError(exc) from exc
        text = str(getattr(result, "text", "") or "").strip()
        engine_name = self._engine_name(engine)
        received_at = self._clock()

        delivered = False
        note = ""
        if not text:
            note = (
                "the recogniser heard nothing intelligible in this audio, so "
                "nothing was delivered to Claire"
            )
        elif self._sink is None or not callable(self._sink):
            note = str(SinkUnconfiguredError())
        else:
            utterance = {
                "kind": "transcript",
                "text": text,
                "source": "phone",
                "engine": engine_name,
                "seconds": seconds,
                "sample_rate": sample_rate,
                "received_at": received_at,
            }
            try:
                self._sink(utterance)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                raise SinkFailedError(exc) from exc
            delivered = True

        payload: Dict[str, Any] = {
            "ok": True,
            "text": text,
            "chars": len(text),
            "accepted": delivered,
            "delivered": delivered,
            "engine": engine_name,
            "seconds": seconds,
            "sample_rate": sample_rate,
            "received_at": received_at,
        }
        if note:
            payload["note"] = note
        return BridgeResponse(status=HTTP_OK, payload=payload)

    def _loc(self, content_type: str, body: bytes) -> BridgeResponse:
        data = self._json_object(content_type, body)
        latitude = self._coordinate(data, ("latitude", "lat"), 90.0, "latitude")
        longitude = self._coordinate(
            data, ("longitude", "lon", "lng"), 180.0, "longitude"
        )
        accuracy: Optional[float] = None
        raw_accuracy = data.get("accuracy", data.get("accuracy_m"))
        if raw_accuracy is not None:
            if isinstance(raw_accuracy, bool) or not isinstance(
                raw_accuracy, (int, float)
            ):
                raise InvalidPayloadError(
                    "the 'accuracy' field must be a number of metres",
                    remedy='POST {"latitude": .., "longitude": .., "accuracy": 12.5}.',
                )
            if raw_accuracy < 0:
                raise InvalidPayloadError(
                    f"the 'accuracy' field is negative ({raw_accuracy})",
                    remedy="Send the uncertainty radius in metres, zero or more.",
                )
            accuracy = float(raw_accuracy)
        record = LocationRecord(
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy,
            received_at=self._clock(),
        )
        with self._lock:
            self._last_location = record
        if self._location_sink is not None:
            try:
                self._location_sink(record)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                raise LocationSinkFailedError(exc) from exc
        return BridgeResponse(
            status=HTTP_OK,
            # Acknowledge the fix, do not read it back.  The phone already knows
            # where it is; echoing the latitude and longitude the phone just sent
            # is how the owner ended up being told their own coordinates as if it
            # were news.  What the caller cannot know, and what actually matters
            # for a fix, is that it was accepted and when -- so that is all that
            # comes back.  The position itself goes to the location sink (Claire's
            # context), never into the reply the owner sees.
            payload={
                "ok": True,
                "recorded": True,
                "accuracy_m": record.accuracy_m,
                "received_at": record.received_at,
            },
        )

    def _see(
        self,
        content_type: str,
        body: bytes,
        headers: Any,
        query: Any,
    ) -> BridgeResponse:
        kind = media_type(content_type)
        if kind == CONTENT_TYPE_JSON:
            data = self._json_object(content_type, body)
            encoded = data.get("image_base64")
            if not isinstance(encoded, str) or not encoded.strip():
                raise InvalidPayloadError(
                    "the JSON form of /see needs a non-empty 'image_base64' field",
                    remedy=(
                        'POST {"image_base64": "<base64 image>", "question": "..."}, '
                        "or POST the raw image bytes with Content-Type: image/jpeg."
                    ),
                )
            try:
                image = base64.b64decode(encoded, validate=True)
            except Exception as exc:  # noqa: BLE001 - named, actionable
                raise InvalidPayloadError(
                    f"the 'image_base64' field is not valid base64 "
                    f"({type(exc).__name__})",
                    remedy=(
                        "Re-encode the photo: python -c \"import base64,"
                        "pathlib;print(base64.b64encode(pathlib.Path('photo.jpg')"
                        ".read_bytes()).decode())\""
                    ),
                    cause=exc,
                ) from exc
            question = data.get("question") or ""
            declared = media_type(data.get("content_type"))
            extension = ALLOWED_IMAGE_CONTENT_TYPES.get(declared, ".bin")
        elif kind in ALLOWED_IMAGE_CONTENT_TYPES:
            image = body
            question = _query_value(query, "question") or _header(
                headers, QUESTION_HEADER
            )
            extension = ALLOWED_IMAGE_CONTENT_TYPES[kind]
        else:
            raise UnsupportedContentTypeError(
                content_type, [CONTENT_TYPE_JSON, *sorted(ALLOWED_IMAGE_CONTENT_TYPES)]
            )
        if not isinstance(question, str):
            raise InvalidPayloadError(
                "the 'question' field must be a string",
                remedy='Send {"question": "what is this?"} or omit it entirely.',
            )
        if len(question) > MAX_QUESTION_CHARS:
            raise InvalidPayloadError(
                f"the question is {len(question)} characters, over the "
                f"{MAX_QUESTION_CHARS}-character limit",
                remedy="Ask one short spoken question instead of pasting a document.",
            )
        if not image:
            raise InvalidPayloadError(
                "the photo body is empty",
                remedy=(
                    "Send the image bytes with Content-Type: image/jpeg or "
                    "image/png (curl --data-binary @photo.jpg -H 'Content-Type: "
                    "image/jpeg')."
                ),
            )
        path = self._store_photo(image, extension)
        return self._describe_photo(path, question)

    # -- photos and vision ------------------------------------------------ #

    def _store_photo(self, image: bytes, extension: str) -> Path:
        """Write the photo 0600 into the photo directory, returning its path."""
        directory = self.photo_dir
        try:
            _ensure_private_dir(directory)
        except OSError as exc:
            raise PhotoStoreError(directory, exc) from exc
        name = f"photo-{int(self._clock())}-{secrets.token_hex(4)}{extension}"
        path = directory / name
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(handle, "wb") as stream:
                stream.write(image)
        except OSError as exc:
            raise PhotoStoreError(path, exc) from exc
        LOG.info("phone bridge stored a photo at %s (%d bytes)", path, len(image))
        return path

    def _describe_photo(self, path: Path, question: str) -> BridgeResponse:
        """Hand the stored path to the vision seam, or say honestly why not."""
        describer = self._resolve_describer()
        if describer is None:
            # The photo is safely on disk; nothing here invents a description.
            return BridgeResponse(
                status=HTTP_SERVICE_UNAVAILABLE,
                payload={
                    "ok": False,
                    "seen": False,
                    "image_path": str(path),
                    "question": question,
                    "error": {
                        "type": "VisionNotConfigured",
                        "code": "vision_not_configured",
                        "status": HTTP_SERVICE_UNAVAILABLE,
                        "message": (
                            "no vision describer is configured, so the photo was "
                            "stored and not described"
                        ),
                        "remedy": DEFAULT_VISION_REMEDY,
                    },
                },
            )
        try:
            result = invoke_describer(describer, path, question)
        except PhoneBridgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            outcome = VisionOutcome(
                seen=False,
                code="vision_describer_failed",
                message=f"the vision describer raised {type(exc).__name__}: {exc}",
                remedy=DEFAULT_VISION_REMEDY,
                status=HTTP_BAD_GATEWAY,
            )
        else:
            outcome = interpret_vision_result(result)
        if outcome.seen:
            return BridgeResponse(
                status=HTTP_OK,
                payload={
                    "ok": True,
                    "seen": True,
                    "description": outcome.description,
                    "image_path": str(path),
                    "question": question,
                },
            )
        return BridgeResponse(
            status=outcome.status,
            payload={
                "ok": False,
                "seen": False,
                "image_path": str(path),
                "question": question,
                "error": {
                    "type": "VisionNotSeen",
                    "code": outcome.code or "vision_not_seen",
                    "status": outcome.status,
                    "message": outcome.message
                    or "the vision describer did not report anything",
                    "remedy": outcome.remedy or DEFAULT_VISION_REMEDY,
                },
            },
        )

    def _resolve_describer(self) -> Any:
        """The callable that describes an image, or ``None`` when unconfigured."""
        if self._describer is None:
            return None
        if self._describer is DEFAULT_DESCRIBER:
            with self._lock:
                if self._resolved_describer is None:
                    from ..senses.camera import describe_image

                    self._resolved_describer = describe_image
                return self._resolved_describer
        return self._describer

    # -- helpers ---------------------------------------------------------- #

    def _json_object(self, content_type: str, body: bytes) -> Dict[str, Any]:
        """The body as a JSON object, refusing anything else by name."""
        kind = media_type(content_type)
        if kind != CONTENT_TYPE_JSON:
            raise UnsupportedContentTypeError(content_type, [CONTENT_TYPE_JSON])
        if not body:
            raise InvalidPayloadError(
                "the request body is empty",
                remedy="Send a JSON object body with Content-Type: application/json.",
            )
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidPayloadError(
                f"the body is not valid UTF-8 JSON ({type(exc).__name__})",
                remedy="Send a JSON object with Content-Type: application/json.",
                cause=exc,
            ) from exc
        if not isinstance(data, dict):
            raise InvalidPayloadError(
                f"the JSON body must be an object, not {type(data).__name__}",
                remedy='Wrap the fields in an object, for example {"text": "hello"}.',
            )
        return data

    @staticmethod
    def _coordinate(
        data: Mapping[str, Any],
        names: Sequence[str],
        bound: float,
        label: str,
    ) -> float:
        """One numeric coordinate from any of ``names``, in range, or a 400."""
        for name in names:
            if name in data:
                raw = data[name]
                break
        else:
            raise InvalidPayloadError(
                f"the '{label}' field is missing",
                remedy=(
                    f'POST {{"{names[0]}": 0.0, "longitude": 0.0}} — latitude '
                    "must be between -90 and 90, longitude between -180 and 180."
                ),
            )
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidPayloadError(
                f"the '{label}' field must be a number, not "
                f"{type(raw).__name__}",
                remedy=f'Send {label} as a JSON number, for example {{"{names[0]}": 51.5}}.',
            )
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidPayloadError(
                f"the '{label}' field is not a finite number ({raw!r})",
                remedy=f'Send {label} as a finite JSON number, for example {{"{names[0]}": 51.5}}.',
            )
        if not (-bound <= value <= bound):
            raise InvalidPayloadError(
                f"the '{label}' field is out of range ({value}); it must be "
                f"between {-bound} and {bound}",
                remedy=(
                    "Check the phone's GPS fix. Latitude -90..90, longitude "
                    "-180..180, and mind the order: latitude first."
                ),
            )
        return value

    # -- what the voice loop uses ----------------------------------------- #

    def push_reply(self, text: str) -> Dict[str, Any]:
        """Queue one thing Claire has to say, for the next ``GET /reply``."""
        return self._queue.put_text(text)


def _ensure_private_dir(path: Path) -> None:
    """Create the photo directory 0700, tightening a loose one loudly."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        path.chmod(0o700)
        LOG.warning(
            "phone photo directory %s was mode 0o%o; tightened to 0o700 "
            "(phone photos are private)",
            path,
            mode,
        )


# --------------------------------------------------------------------------- #
# The vision seam, interpreted honestly
# --------------------------------------------------------------------------- #

def decode_wav_audio(
    body: bytes, content_type: str
) -> Tuple[Any, int, float]:
    """Decode one WAV body into mono float32 samples for the recogniser.

    Returns ``(samples, sample_rate, seconds)``.  The ``wave`` module from the
    standard library does the framing — it is the same parser the rest of this
    project would use, and it refuses a truncated header rather than guessing —
    and numpy does the sample conversion, because
    :func:`claire.listening.stt.create_stt_engine` takes a float32 array in
    ``[-1, 1]``.

    Every rejection is a *named* error: a wrong media type is a 415 listing what
    is accepted, and a body that claims to be WAV but is not is a 400 that says
    which part was wrong.
    """
    declared = str(content_type or "").split(";", 1)[0].strip().lower()
    if not body:
        raise AudioDecodeError(
            "the audio body was empty",
            remedy="POST the bytes of a WAV file, not an empty body.",
        )
    if declared not in ALLOWED_AUDIO_CONTENT_TYPES and not _is_riff_wave(body):
        # A client that says application/octet-stream but really sends a RIFF
        # file is not worth an argument; a client that sends an MP3 is.
        raise UnsupportedContentTypeError(
            declared, sorted(ALLOWED_AUDIO_CONTENT_TYPES)
        )
    if not _is_riff_wave(body):
        raise AudioDecodeError(
            f"the body is {len(body)} bytes but does not start with a RIFF/WAVE "
            "header",
            remedy=(
                "Only uncompressed WAV is accepted. If this came from the phone, "
                "ClaireWire.wav() produces the right container; if it came from "
                "elsewhere, re-encode it: ffmpeg -i in.m4a -ac 1 -ar 16000 "
                "-c:a pcm_s16le out.wav"
            ),
        )

    import io
    import wave

    try:
        with wave.open(io.BytesIO(body), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            compression = handle.getcomptype()
            raw = handle.readframes(frames)
    except (wave.Error, EOFError, ValueError) as exc:
        raise AudioDecodeError(
            f"the WAV container could not be read ({type(exc).__name__}: {exc})",
            cause=exc,
        ) from exc

    if compression != "NONE":
        raise AudioDecodeError(
            f"the WAV file is {compression!r}-compressed, which this route does "
            "not decode",
            remedy="Re-encode as uncompressed 16-bit PCM WAV (see the ffmpeg line "
            "in the remedy for a non-RIFF body).",
        )
    if channels < 1 or channels > 8:
        raise AudioDecodeError(f"the WAV file claims {channels} channels")
    if width not in (1, 2, 4):
        raise AudioDecodeError(
            f"the WAV file uses {width * 8}-bit samples; 8, 16 and 32-bit PCM are "
            "supported"
        )
    if sample_rate < 1000 or sample_rate > 384_000:
        raise AudioDecodeError(
            f"the WAV file claims a sample rate of {sample_rate} Hz"
        )
    if not raw:
        raise AudioDecodeError("the WAV file has a header but no audio frames")

    seconds = len(raw) / float(width * channels * sample_rate)
    if seconds < MIN_AUDIO_SECONDS:
        raise AudioDecodeError(
            "the WAV file holds %.3f s of audio, which is too short to recognise "
            "(the minimum is %.2f s)" % (seconds, MIN_AUDIO_SECONDS)
        )
    if seconds > MAX_AUDIO_SECONDS:
        raise AudioDecodeError(
            "the WAV file holds %.1f s of audio; the maximum for one utterance is "
            "%d s" % (seconds, MAX_AUDIO_SECONDS),
            remedy=(
                "Send shorter utterances. The phone's own segmenter closes a "
                "segment after 15 s of continuous speech; a longer file usually "
                "means the client is buffering instead of segmenting."
            ),
        )

    import numpy as np

    if width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    if channels > 1:
        frames_count = samples.size // channels
        samples = samples[: frames_count * channels].reshape(frames_count, channels)
        samples = samples.mean(axis=1)
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    return samples, int(sample_rate), seconds


def _is_riff_wave(body: bytes) -> bool:
    """True when the first 12 bytes are a RIFF/WAVE header."""
    return len(body) >= 12 and body[0:4] == b"RIFF" and body[8:12] == b"WAVE"


def invoke_describer(describer: Any, image_path: Any, question: str) -> Any:
    """Call an injected describer: an object with ``describe`` or a callable.

    The same shape :func:`claire.senses.camera._invoke_describer` accepts, so a
    describer written for the camera sense drops in here unchanged.
    """
    method = getattr(describer, "describe", None)
    if callable(method):
        return method(image_path, question)
    if callable(describer):
        return describer(image_path, question)
    raise VisionDescriberError(describer)


def interpret_vision_result(result: Any) -> VisionOutcome:
    """Turn whatever the describer returned into a seen / not-seen verdict.

    Accepted shapes: an :class:`~claire.senses.camera.ImageDescription` (or
    anything with a boolean ``seen``), a non-empty string, or a mapping with
    ``seen`` / ``description`` / ``error``.  Anything else is a named contract
    failure — never a guessed description.
    """
    if isinstance(result, str):
        text = result.strip()
        if text:
            return VisionOutcome(seen=True, description=text)
        return VisionOutcome(
            seen=False,
            code="vision_empty_answer",
            message="the vision describer answered with an empty description",
            remedy=DEFAULT_VISION_REMEDY,
        )
    seen = getattr(result, "seen", None)
    if isinstance(seen, bool):
        if seen:
            description = str(getattr(result, "description", "") or "").strip()
            if description:
                return VisionOutcome(seen=True, description=description)
            return VisionOutcome(
                seen=False,
                code="vision_empty_answer",
                message=(
                    "the vision describer reported 'seen' with an empty "
                    "description, which is not a description"
                ),
                remedy=DEFAULT_VISION_REMEDY,
            )
        error = getattr(result, "error", None)
        remedy = str(getattr(result, "remedy", "") or "")
        if error is None:
            return VisionOutcome(
                seen=False,
                code="vision_not_seen",
                message=(
                    "the vision describer reported the image was not seen and "
                    "gave no reason"
                ),
                remedy=remedy or DEFAULT_VISION_REMEDY,
            )
        return vision_failure(error, remedy)
    if isinstance(result, Mapping):
        if result.get("seen") is True:
            description = str(result.get("description", "") or "").strip()
            if description:
                return VisionOutcome(seen=True, description=description)
            return VisionOutcome(
                seen=False,
                code="vision_empty_answer",
                message="the vision describer reported 'seen' with no description",
                remedy=DEFAULT_VISION_REMEDY,
            )
        reason = result.get("error") or result.get("message")
        error: BaseException = (
            reason if isinstance(reason, BaseException) else RuntimeError(str(reason or ""))
        )
        return vision_failure(error, str(result.get("remedy", "") or ""))
    return VisionOutcome(
        seen=False,
        code="vision_describer_contract",
        message=(
            f"the vision describer returned {type(result).__name__}, which is "
            "not an ImageDescription, a string or a mapping"
        ),
        remedy=(
            "Return claire.senses.camera.ImageDescription, a non-empty string, "
            "or a mapping with 'seen'/'description'."
        ),
    )


def vision_failure(error: BaseException, remedy: str = "") -> VisionOutcome:
    """Map a describer's failure to a named, honest not-seen outcome."""
    name = type(error).__name__
    message = str(error).strip() or name
    if name in ("VisionUnavailableError", "MissingDependencyError"):
        return VisionOutcome(
            seen=False,
            code="vision_not_configured",
            message=message,
            remedy=remedy or DEFAULT_VISION_REMEDY,
            status=HTTP_SERVICE_UNAVAILABLE,
        )
    if name in ("VisionRequestError", "VisionNoAnswerError"):
        return VisionOutcome(
            seen=False,
            code="vision_route_failed",
            message=message,
            remedy=remedy or DEFAULT_VISION_REMEDY,
            status=HTTP_BAD_GATEWAY,
        )
    return VisionOutcome(
        seen=False,
        code="vision_not_seen",
        message=message,
        remedy=remedy or DEFAULT_VISION_REMEDY,
        status=HTTP_BAD_GATEWAY,
    )


# --------------------------------------------------------------------------- #
# The HTTP adapter: thin, and the only thing that touches a socket
# --------------------------------------------------------------------------- #

class PhoneRequestHandler(BaseHTTPRequestHandler):
    """A thin adapter: read a body, call the bridge, write the response.

    It holds no logic of its own — every decision lives in
    :class:`PhoneBridge` — which is what lets the whole request path be tested
    by calling ``handle`` with no socket at all.  The bridge is injected either
    as the class attribute ``bridge`` (see :meth:`for_bridge`) or on the server
    object as ``server.bridge``.
    """

    server_version = "ClairePhoneBridge/1.0"
    # HTTP/1.0 keeps the adapter simple: every response carries an accurate
    # Content-Length and the connection closes, so there is no keep-alive state
    # to get wrong over a phone's flaky connection.
    protocol_version = "HTTP/1.0"

    #: Injected by :meth:`for_bridge`, or read from ``self.server.bridge``.
    bridge: Optional[PhoneBridge] = None

    @classmethod
    def for_bridge(cls, bridge: PhoneBridge) -> type:
        """A handler class bound to one bridge, for ``ThreadingHTTPServer``."""
        return type("BoundPhoneRequestHandler", (cls,), {"bridge": bridge})

    def bridge_or_none(self) -> Optional[PhoneBridge]:
        """The bridge for this request: class attribute first, then the server."""
        if self.bridge is not None:
            return self.bridge
        return getattr(self.server, "bridge", None)

    # Every method is answered, so an unexpected verb gets a named 405 from the
    # bridge rather than BaseHTTPRequestHandler's bare 501.

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._serve("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._serve("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._serve("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve("HEAD")

    def _serve(self, method: str) -> None:
        bridge = self.bridge_or_none()
        if bridge is None:
            LOG.error("phone bridge request arrived with no bridge attached")
            self._write(
                BridgeResponse(
                    status=500,
                    payload={
                        "ok": False,
                        "error": {
                            "type": "PhoneBridgeError",
                            "code": "bridge_not_attached",
                            "message": "this handler has no PhoneBridge attached",
                            "remedy": (
                                "Start the server with claire.phone.bridge."
                                "create_server(bridge) so the handler is bound."
                            ),
                        },
                    },
                )
            )
            return
        split = urlsplit(self.path)
        content_length = self.headers.get("Content-Length")
        try:
            if method in BODYLESS_METHODS and not str(content_length or "").strip():
                # No body was framed and none is expected: treat it as empty
                # rather than as a client error.  A GET that does carry a
                # Content-Length is still read and validated below.
                body = b""
            else:
                body = read_request_body(
                    content_length,
                    self.rfile,
                    bridge.max_body_bytes,
                )
        except PhoneBridgeError as exc:
            self._write(bridge._error_response(exc))
            return
        response = bridge.handle(
            method,
            split.path,
            content_type=self.headers.get("Content-Type", "") or "",
            body=body,
            headers=self.headers,
            query=parse_qs(split.query, keep_blank_values=True),
        )
        self._write(response)

    def _write(self, response: BridgeResponse) -> None:
        body = response.body()
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type())
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:  # pragma: no cover
            LOG.debug("phone bridge client went away before the reply (%s)", exc)

    # -- logging: request lines only, redacted, never headers ------------- #

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Log one line, with any token redacted and no headers at all.

        ``http.server`` logs the request line (which never contains the
        Authorization header); redaction is belt-and-braces against a path or
        query string that accidentally carries a secret.
        """
        try:
            message = format % args
        except (TypeError, ValueError, KeyError):
            message = str(format)
        bridge = self.bridge_or_none()
        if bridge is not None:
            message = bridge.redact(message)
        LOG.debug("phone bridge http: %s", sanitise_for_log(message))


def create_server(
    bridge: Optional[PhoneBridge] = None,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    allow_public_bind: bool = False,
) -> ThreadingHTTPServer:
    """Bind the bridge and return the (unstarted) server.

    Refuses a non-loopback host unless ``allow_public_bind=True``, and refuses
    it *before* ``ThreadingHTTPServer`` is constructed, so a refusal cannot have
    opened a port.  Call ``server.serve_forever()`` to actually serve.
    """
    bridge = bridge if bridge is not None else PhoneBridge()
    resolved_host = resolve_bind_host(
        host if host is not None else bridge.host,
        port if port is not None else bridge.port,
        allow_public_bind=allow_public_bind,
    )
    resolved_port = int(port if port is not None else bridge.port)
    handler = PhoneRequestHandler.for_bridge(bridge)
    server = ThreadingHTTPServer((resolved_host, resolved_port), handler)
    server.bridge = bridge  # type: ignore[attr-defined]
    LOG.info(
        "phone bridge listening on http://%s:%d (loopback=%s)",
        resolved_host,
        resolved_port,
        is_loopback_host(resolved_host),
    )
    return server


def serve(
    bridge: Optional[PhoneBridge] = None,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    allow_public_bind: bool = False,
) -> None:
    """Create the server and serve until interrupted.

    This is the blocking, real-process entry point (``python -m
    claire.phone.bridge``).  The test suite does not call it: it calls
    :func:`create_server` with ``port=0`` and serves that in a thread, so the
    tests exercise the real HTTP stack on a real socket without ever holding a
    fixed port.  Two past bugs -- a bodyless ``GET`` answered with ``411``, and a
    header map that only broke when a real request carried real headers -- were
    both invisible to in-process dispatch and both caught this way.
    """
    server = create_server(
        bridge, host=host, port=port, allow_public_bind=allow_public_bind
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
