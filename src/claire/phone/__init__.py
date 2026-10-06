"""Claire's phone package: the owner's phone as a channel and as a sensor.

One module so far — :mod:`claire.phone.bridge` (R-IO-1, R-LOC-1, R-VISION-1) —
a small, authenticated, loopback-by-default HTTP surface that lets the phone
send Claire a typed utterance, a photo and a position, and lets it ask what she
has to say.  The public names are re-exported here so callers write
``from claire.phone import PhoneBridge`` rather than reaching into the module.

The token, the bind host and the naming rules live in the spec:
``src/claire/phone/bridge.spec.md``.
"""

from __future__ import annotations

from .bridge import (
    ALLOWED_IMAGE_CONTENT_TYPES,
    DEFAULT_DESCRIBER,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ENDPOINTS,
    MAX_BODY_BYTES,
    TOKEN_KEY_NAME,
    AuthenticationError,
    BodyTooLargeError,
    BridgeResponse,
    InternalError,
    InvalidPayloadError,
    LengthRequiredError,
    LocationRecord,
    MethodNotAllowedError,
    PhoneBridge,
    PhoneBridgeError,
    PhoneRequestHandler,
    PhotoStoreError,
    PublicBindRefusedError,
    ReplyQueue,
    RouteNotFoundError,
    SinkFailedError,
    SinkUnconfiguredError,
    TokenUnavailableError,
    UnsupportedContentTypeError,
    VisionDescriberError,
    check_token,
    create_server,
    default_host,
    default_photo_dir,
    default_port,
    is_loopback_host,
    media_type,
    parse_bearer_token,
    read_request_body,
    resolve_bind_host,
    sanitise_for_log,
    serve,
)

__all__ = [
    "ALLOWED_IMAGE_CONTENT_TYPES",
    "AuthenticationError",
    "BodyTooLargeError",
    "BridgeResponse",
    "DEFAULT_DESCRIBER",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENDPOINTS",
    "InternalError",
    "InvalidPayloadError",
    "LengthRequiredError",
    "LocationRecord",
    "MAX_BODY_BYTES",
    "MethodNotAllowedError",
    "PhoneBridge",
    "PhoneBridgeError",
    "PhoneRequestHandler",
    "PhotoStoreError",
    "PublicBindRefusedError",
    "ReplyQueue",
    "RouteNotFoundError",
    "SinkFailedError",
    "SinkUnconfiguredError",
    "TOKEN_KEY_NAME",
    "TokenUnavailableError",
    "UnsupportedContentTypeError",
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
