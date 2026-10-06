"""The Claire web panel — a small, loopback-only window onto Claire's state.

Why this module exists
----------------------
Half of Claire's behaviour is buried in files the owner never opens: which
provider and model are selected, whether ``CLAIRE.md`` has rotted past its byte
budget, what is on the calendar today, whether anything is broken, and what
Claire actually did in the last few actions.  This module serves that as **one
server-rendered page** with the standard library alone — no Flask, no
JavaScript framework, no build step.  ``python -m claire.panel.server`` starts
it, ``http://127.0.0.1:8765/`` shows it.

The five invariants this module is built around
-----------------------------------------------
1. **A raw key value cannot be rendered, structurally.**  The renderer only ever
   interpolates a value that passed through :func:`_esc` (scrub + HTML escape),
   and the one column that shows credentials is typed as
   :class:`KeyStatus`, whose ``render`` field must be *empty or mask-shaped*
   (``...1234#a1b2c3d4``) or construction raises :class:`PanelSecretError`.
   Masked renderings come from :meth:`claire.platform.keys.KeyStore.render` and
   :func:`claire.platform.keys.mask`.  The panel never calls ``KeyStore.get`` —
   the single raw-value accessor in the codebase — and there is deliberately no
   form field anywhere on the page that could carry a key *in*: keys are set
   out-of-band (the key store, or ``OMNIROUTE_API_KEY``), and the page says so.
2. **Loopback only, by default and by guard.**  :data:`DEFAULT_BIND_HOST` is
   ``127.0.0.1``; :func:`guard_bind_host` refuses anything else unless the
   caller passes ``allow_remote=True``, which logs a loud warning naming the
   fact that the panel is **plain HTTP**, with no TLS and no authentication.
3. **The write endpoint is forgery-resistant.**  ``POST /select`` refuses any
   request whose ``Origin``/``Host`` do not agree on a loopback origin
   (:func:`check_write_origin`) — a page on the internet cannot make the owner's
   browser run that POST — and refuses any body that carries a key-shaped field,
   naming the out-of-band route in the fix.
4. **No directory traversal, because no path is served.**  Every route is an
   exact string in :data:`GET_VIEWS`; the request target is decoded and any
   ``..``/backslash/NUL segment is refused by name before dispatch.  Nothing
   reads from a path derived from the request.
5. **Every missing thing is a named error with a fix, and nothing raises past
   the handler.**  A section whose state provider raises renders as a named
   PROBLEM box inside the page instead of a 500 with a traceback; an HTTP-level
   refusal renders a standalone page carrying ``code``, message and ``fix``.

State is **injected** (:class:`PanelState`), so every route is testable without
a socket or a real key store.  See ``panel.spec.md`` next to this file.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date as _date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from claire.platform.keys import DEFAULT_ENV_SOURCES, KeyStoreError
from claire.utils.redact import scrub_secrets

__all__ = [
    # constants
    "ACTION_LOG_ENV_VAR",
    "DEFAULT_BIND_HOST",
    "DEFAULT_PORT",
    "FORBIDDEN_KEY_FIELDS",
    "GET_ROUTES",
    "LOOPBACK_HOSTS",
    "MAX_BODY_BYTES",
    "PANEL_VERSION",
    "PROVIDER_KEY_NAMES",
    "ROUTE_HEALTH",
    "ROUTE_INDEX",
    "ROUTE_SELECT",
    "ROUTE_SELF_CHECK",
    # data
    "ActionRecord",
    "ActionsStatus",
    "CalendarEvent",
    "CalendarStatus",
    "ConfigCredential",
    "KeyStatus",
    "PanelRequest",
    "PanelResponse",
    "ProviderModel",
    "RouterStatus",
    "SelfCheckStatus",
    "SelfCheckView",
    "WriteResult",
    # errors
    "PanelBadRequest",
    "PanelBindError",
    "PanelError",
    "PanelMethodNotAllowed",
    "PanelOriginError",
    "PanelRouteNotFound",
    "PanelSecretError",
    "PanelSourceError",
    "PanelStateError",
    "PanelTooLarge",
    "PanelTraversalError",
    "PanelWriteError",
    # state
    "PanelState",
    "RealPanelState",
    # rendering / dispatch
    "dispatch",
    "render_error_page",
    "render_index",
    "render_health",
    "render_self_check_text",
    # http
    "PanelHandler",
    "PanelServer",
    "create_handler",
    "create_server",
    "guard_bind_host",
    "is_loopback_host",
    "is_mask_shaped",
    "main",
    "parse_form",
    "parse_target",
    "serve",
    "check_write_origin",
]

PANEL_VERSION = "1"

#: The only host the panel binds to unless the owner explicitly says otherwise.
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Host names/addresses that cannot be reached from another machine.  The empty
#: string is deliberately *not* here: ``--host ""`` and an ``Origin`` without a
#: host both mean "somewhere else", never "loopback".
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: A request body larger than this is refused before it is read.
MAX_BODY_BYTES = 64 * 1024

#: Form fields that would carry a credential *into* the page.  The page has no
#: such field, and a POST that tries to add one is refused by name.
FORBIDDEN_KEY_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "keys",
        "llm_api_key",
        "embedding_api_key",
        "brave_search_api_key",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "bearer",
        "authorization",
    }
)

ROUTE_INDEX = "/"
ROUTE_SELF_CHECK = "/self-check"
ROUTE_HEALTH = "/healthz"
ROUTE_FAVICON = "/favicon.ico"
ROUTE_SELECT = "/select"

#: Every path that serves a page.  Tests iterate this; nothing else is served.
GET_ROUTES: tuple[str, ...] = (ROUTE_INDEX, ROUTE_SELF_CHECK, ROUTE_HEALTH, ROUTE_FAVICON)

#: Where Claire appends one JSON object per completed action.
ACTION_LOG_ENV_VAR = "CLAIRE_ACTION_LOG"
DEFAULT_ACTION_LOG = "~/.config/claire/actions.jsonl"

HTML_CONTENT_TYPE = "text/html; charset=utf-8"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"

_LOGGER = logging.getLogger(__name__)

#: A masked rendering, exactly as :func:`claire.platform.keys.mask` produces it:
#: last-four (``...1234``) or fully hidden (``****``), a ``#`` and an 8-hex
#: fingerprint.  ``KeyStatus`` refuses any non-empty ``render`` that does not
#: match, which is the tripwire that makes "a raw key cannot be rendered" true
#: by construction rather than by review.  The four ``.`` are deliberately not a
#: character class: a secret's last four characters may be anything (``mask()``
#: reveals them by the project's policy) and rejecting one would break the page.
_MASK_SHAPE = re.compile(r"^(?:\.{3}.{4}|\*{4})#[0-9a-f]{8}$")

#: The only source labels a key row may carry.  The second displayable field on
#: a credential row, so it is shape-checked for the same reason ``render`` is.
_SOURCE_SHAPE = re.compile(r"^(?:store|env:[A-Za-z_][A-Za-z0-9_]*|config\.json)$")


def is_mask_shaped(value: Any) -> bool:
    """True when ``value`` is exactly the mask :func:`claire.platform.keys.mask`
    produces — last-four or fully hidden, then a fingerprint, and nothing else.

    This is the panel's tripwire for "a raw key cannot be rendered": the only
    credential-bearing field on a page (:attr:`KeyStatus.render`) must satisfy
    it, and a raw value does not.
    """
    if not value:
        return False
    return bool(_MASK_SHAPE.match(str(value)))

#: A model id: conservative, because it is written into ``config.json``.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,127}$")

#: A provider name: same shape, no slashes needed but harmless.
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}$")

_PATH_LOG_LIMIT = 200


# --------------------------------------------------------------------------- #
# Named errors
#
# Every refusal names what happened and what to do about it.  ``fix`` is not
# optional in spirit: an error the owner cannot act on is noise.
# --------------------------------------------------------------------------- #


class PanelError(Exception):
    """Base class for every refusal the panel makes.

    ``code`` is a stable, greppable token; ``fix`` is the actionable sentence
    the owner reads.  ``status`` is the HTTP status the refusal renders as.
    """

    status = 400
    code = "panel-error"
    default_fix = "Re-open http://127.0.0.1:%d/ and try again." % DEFAULT_PORT

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        self.fix = fix or self.default_fix

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "fix": self.fix,
        }


class PanelBadRequest(PanelError):
    """The request target or body could not be understood."""

    status = 400
    code = "bad-request"
    default_fix = "Reload the panel page and use the form it renders."


class PanelTooLarge(PanelBadRequest):
    """The request body is larger than the panel will read."""

    status = 413
    code = "body-too-large"
    default_fix = (
        f"The panel form is a handful of fields; it refuses bodies over "
        f"{MAX_BODY_BYTES} bytes. Submit the page's own form."
    )


class PanelTraversalError(PanelBadRequest):
    """The request target tried to walk out of the served routes."""

    status = 400
    code = "traversal-refused"
    default_fix = (
        "The panel serves a fixed set of routes and no files; use the links on "
        "http://127.0.0.1:%d/ instead of a path of your own." % DEFAULT_PORT
    )


class PanelRouteNotFound(PanelError):
    """No route is registered under that path."""

    status = 404
    code = "route-not-found"

    def __init__(self, path: str) -> None:
        known = ", ".join(GET_ROUTES)
        super().__init__(
            f"no panel route is registered under {path!r}.",
            fix=f"Use one of the registered routes: {known}.",
        )


class PanelMethodNotAllowed(PanelError):
    """A write verb was sent where only a read is served (or the reverse)."""

    status = 405
    code = "method-not-allowed"

    def __init__(self, method: str, path: str, *, allowed: Sequence[str]) -> None:
        super().__init__(
            f"{method} is not served on {path!r}.",
            fix=f"Use one of: {', '.join(allowed)}.",
        )
        self.allowed = tuple(allowed)


class PanelOriginError(PanelError):
    """The write endpoint refused a cross-origin / forged request."""

    status = 403
    code = "origin-refused"
    default_fix = (
        "POST /select from the panel page itself (a browser sends a matching "
        "Origin). From a terminal, send -H 'Origin: http://127.0.0.1:%d'."
        % DEFAULT_PORT
    )


class PanelWriteError(PanelError):
    """The selection could not be written to ``config.json``."""

    status = 500
    code = "write-failed"
    default_fix = (
        "Check that the Claire config directory exists and is writable, then "
        "retry; nothing was changed if this page says so."
    )


class PanelBindError(PanelError):
    """The panel refuses to bind that host without an explicit opt-in."""

    status = 500
    code = "bind-refused"

    def __init__(self, host: str) -> None:
        super().__init__(
            f"refusing to bind {host!r}: the panel is loopback-only by default.",
            fix=(
                "Keep the default host 127.0.0.1, or start it with "
                "--allow-remote-binding to accept that the panel would be plain "
                "HTTP with no TLS and no authentication."
            ),
        )


class PanelStateError(PanelError):
    """A state provider has no implementation for this section."""

    status = 500
    code = "state-missing"

    def __init__(self, section: str) -> None:
        super().__init__(
            f"the panel state has no source for {section!r}.",
            fix=(
                "Inject a PanelState that implements "
                f"{section}() (see panel.spec.md), or use RealPanelState."
            ),
        )


class PanelSecretError(PanelError):
    """A value shaped like a raw secret was about to be rendered."""

    status = 500
    code = "raw-secret-refused"

    def __init__(self, field_name: str) -> None:
        super().__init__(
            f"refusing to render {field_name!r}: the panel renders masks only.",
            fix=(
                "Pass the value through claire.platform.keys.mask() (or "
                "KeyStore.render()) before handing it to KeyStatus."
            ),
        )


class PanelSourceError(PanelError):
    """A configured source exists but could not be read.

    Distinct from "there is nothing there": a corrupt key store or a damaged
    schedule is a finding the owner must act on, never an empty section.
    """

    status = 500
    code = "source-unreadable"

    def __init__(self, what: str, error: Any, *, fix: str | None = None) -> None:
        super().__init__(
            f"{what} could not be read: {type(error).__name__}: {_scrub(error)}",
            fix=fix or f"Repair or move the file behind {what}, then reload the panel.",
        )


# --------------------------------------------------------------------------- #
# Escaping and scrubbing
# --------------------------------------------------------------------------- #


def _esc(value: Any) -> str:
    """Scrub and HTML-escape one interpolated value.

    Every value the renderer puts into the page goes through here: this page
    shows titles, notes, model ids and error text that came from the internet or
    from a config file, and HTML is data, never markup.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return html.escape(scrub_secrets(text), quote=True)


def _scrub(text: str) -> str:
    """Scrub a string for a log line or an exception message."""
    return scrub_secrets(str(text))


# --------------------------------------------------------------------------- #
# The data the page renders
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderModel:
    """Which provider and model are selected, and where the answer came from."""

    provider: str = ""
    model: str = ""
    base_url: str = ""
    source: str = ""
    error: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class KeyStatus:
    """One credential, as the panel is allowed to show it.

    Both displayable fields are shape-checked in :meth:`__post_init__`:
    ``render`` must be *empty or exactly* the mask :func:`claire.platform.keys.mask`
    produces, and ``source`` must be a known label.  A raw value cannot be put in
    either — that is the tripwire which makes "the panel cannot render a key"
    true by construction rather than by review.  ``present`` is shown for
    credentials the panel can only see the existence of (a value in
    ``config.json``), and then ``render`` stays empty: the panel never reads a
    raw value in order to build a mask of it.
    """

    name: str
    present: bool
    render: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if self.render and not is_mask_shaped(self.render):
            raise PanelSecretError(self.name)
        if self.source and not _SOURCE_SHAPE.match(str(self.source)):
            raise PanelSecretError(f"{self.name}.source")

    @property
    def label(self) -> str:
        if self.render:
            return self.render
        return "present" if self.present else "absent"


@dataclass(frozen=True)
class ConfigCredential:
    """A credential-shaped field noticed in ``config.json`` (presence only)."""

    name: str
    present: bool


@dataclass(frozen=True)
class RouterStatus:
    """``CLAIRE.md`` — path, bytes against the budget, and the loud framing."""

    path: str = ""
    exists: bool = False
    size_bytes: int = 0
    budget_bytes: int = 0
    over_budget: bool = False
    dropped_bytes: int = 0
    created_from_template: bool = False
    warning: str = ""
    error: str | None = None
    fix: str | None = None

    @property
    def fraction(self) -> float:
        if self.budget_bytes <= 0:
            return 0.0
        return self.size_bytes / float(self.budget_bytes)


@dataclass(frozen=True)
class CalendarEvent:
    """One occurrence today.

    ``start``/``end`` are clock strings (``16:00``) produced by the calendar
    module's own formatter; ``spoken`` is the full human phrase it gives, kept so
    the panel can show what Claire would say.
    """

    title: str = ""
    date: str = ""
    start: str = ""
    end: str = ""
    duration_minutes: int = 0
    notes: str = ""
    is_recurring: bool = False
    event_id: str = ""
    spoken: str = ""


@dataclass(frozen=True)
class CalendarStatus:
    """Today's calendar, or the named reason it could not be read."""

    date: str = ""
    path: str = ""
    existed: bool = False
    events: tuple[CalendarEvent, ...] = ()
    error: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class SelfCheckView:
    """One self-check verdict, flattened for rendering."""

    name: str
    title: str
    status: str
    detail: str = ""
    fix: str | None = None


@dataclass(frozen=True)
class SelfCheckStatus:
    """The self-check report, or the named reason it could not be produced."""

    ok: bool = False
    summary_line: str = ""
    generated_at: str = ""
    checks: tuple[SelfCheckView, ...] = ()
    error: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class ActionRecord:
    """One thing Claire did, with the outcome it reported.

    ``detail`` is scrubbed on construction, so an action journal that embedded a
    credential cannot smuggle it into the page.
    """

    at: str = ""
    action: str = ""
    outcome: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _scrub(self.detail))


@dataclass(frozen=True)
class ActionsStatus:
    """The tail of the action journal, or the named reason it is empty."""

    path: str = ""
    records: tuple[ActionRecord, ...] = ()
    skipped_lines: int = 0
    error: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class WriteResult:
    """What a successful selection write actually changed."""

    provider: str
    model: str
    config_path: str
    changed: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Requests and responses
# --------------------------------------------------------------------------- #


class HeaderMap(Mapping[str, str]):
    """A case-insensitive, read-only view over the request headers."""

    def __init__(self, headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> None:
        # Duck-type on .items() rather than isinstance(Mapping). The live server
        # hands us http.client's HTTPMessage (from email.message), which provides
        # .items() but is NOT a registered Mapping - so the isinstance check used
        # to fail and fall through to treating it as an iterable of pairs. An
        # HTTPMessage iterates to its header NAMES, so unpacking "Host" as (k, v)
        # raised ValueError and every request over a real socket died before
        # routing, while every in-process test passed.
        getter = getattr(headers, "items", None)
        items = getter() if callable(getter) else headers
        self._data = {str(k).lower(): str(v) for k, v in items}

    def __getitem__(self, key: str) -> str:
        return self._data[key.lower()]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._data.get(str(key).lower(), default)


@dataclass(frozen=True)
class PanelRequest:
    """One request, in a form that needs no socket to build.

    Tests construct this directly, which is why no test has to start a server.
    """

    method: str = "GET"
    path: str = "/"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    client: str = "127.0.0.1"
    server_port: int = DEFAULT_PORT
    server_host: str = DEFAULT_BIND_HOST
    declared_length: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", str(self.method).upper())
        object.__setattr__(self, "path", str(self.path))
        if not isinstance(self.headers, HeaderMap):
            object.__setattr__(self, "headers", HeaderMap(self.headers))
        if isinstance(self.body, str):  # convenience for tests
            object.__setattr__(self, "body", self.body.encode("utf-8"))

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    def form(self) -> dict[str, str]:
        """Parse a urlencoded body. Raises :class:`PanelBadRequest`."""
        return parse_form(self)


@dataclass(frozen=True)
class PanelResponse:
    """A rendered response. ``body`` is bytes, ready for the socket."""

    status: int = 200
    body: bytes = b""
    content_type: str = HTML_CONTENT_TYPE
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Guards: bind host, request target, origin
# --------------------------------------------------------------------------- #


def is_loopback_host(host: str) -> bool:
    """True when ``host`` cannot be reached from another machine."""
    if host is None:
        return False
    text = str(host).strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text in LOOPBACK_HOSTS:
        return True
    if text.startswith("127."):
        parts = text.split(".")
        if len(parts) == 4:
            try:
                return all(0 <= int(part) <= 255 for part in parts)
            except ValueError:
                return False
    return False


def guard_bind_host(host: str, *, allow_remote: bool = False) -> str:
    """Return the host to bind, or refuse it by name.

    Loopback needs nothing.  Anything else — ``0.0.0.0``, a LAN address, a
    hostname — needs ``allow_remote=True`` and produces a loud warning, because
    the panel is plain HTTP: no TLS, no authentication, and it shows the owner's
    configuration.
    """
    text = str(host).strip()
    if is_loopback_host(text):
        return text or DEFAULT_BIND_HOST
    if not allow_remote:
        raise PanelBindError(text)
    _LOGGER.warning(
        "Claire panel: binding %s at the explicit request of the operator. "
        "This panel is PLAIN HTTP - no TLS, no authentication - and anyone who "
        "can reach %s can read this machine's Claire configuration. Use it only "
        "on a network you control.",
        text,
        text,
    )
    return text


def parse_target(target: str) -> tuple[str, dict[str, list[str]]]:
    """Split a request target into a normalised path and its query.

    Refuses, by name, everything that is not a plain absolute path under the
    panel: an absolute-form URL, a backslash, an embedded NUL, and any ``..``
    segment (before or after percent-decoding).  No filesystem path is ever
    built from the result — the routes are exact strings — so this is a second
    wall, not the only one.
    """
    raw = "" if target is None else str(target)
    if not raw:
        raise PanelBadRequest("the request target is empty.")
    if "\x00" in raw:
        raise PanelTraversalError("the request target contains a NUL byte.")
    if "\\" in raw:
        raise PanelTraversalError("the request target contains a backslash.")
    split = urlsplit(raw)
    if split.scheme or split.netloc:
        raise PanelBadRequest(
            f"the request target {raw!r} is an absolute URL; the panel serves "
            "origin-form paths only."
        )
    decoded = unquote(split.path)
    if "\x00" in decoded or "\\" in decoded:
        raise PanelTraversalError("the decoded request target contains a NUL or a backslash.")
    segments = [segment for segment in decoded.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise PanelTraversalError(
            f"the request target {decoded!r} contains a traversal segment."
        )
    path = "/" + "/".join(segments)
    query = parse_qs(split.query, keep_blank_values=True)
    return path, query


def check_write_origin(request: PanelRequest, *, server_port: int | None = None) -> str:
    """Refuse a write whose ``Origin``/``Host`` do not agree on loopback.

    A browser always sends ``Origin`` on a cross-origin form POST, so a page the
    owner is merely *visiting* cannot make their browser change Claire's
    configuration: that page's Origin is its own, not loopback.  A missing
    ``Origin`` is refused too — silence is not a same-origin proof.  Returns the
    validated origin (useful for a log line) or raises :class:`PanelOriginError`.
    """
    headers = request.headers
    origin = headers.get("Origin")
    site_header = headers.get("Sec-Fetch-Site")
    site = str(site_header).strip().lower() if site_header is not None else None

    # An absent or OPAQUE origin ("Origin: null") proves nothing on its own: a
    # cross-site attacker gets exactly the same value, so accepting it blindly
    # would be no CSRF protection at all. It is therefore accepted only alongside
    # Sec-Fetch-Site: same-origin, which a foreign page cannot forge because the
    # browser sets it from the request's real initiator.
    #
    # This path exists because the panel used to answer its own form with
    # "Origin: null": its Referrer-Policy was no-referrer, and that policy makes
    # the browser send an opaque Origin. The guard then refused every genuine
    # write, so the panel could not save its own settings. The policy is now
    # same-origin, and this fallback covers the other opaque-origin contexts.
    if not origin or str(origin).strip().lower() == "null":
        if site == "same-origin":
            return "same-origin"
        if not origin:
            raise PanelOriginError(
                "POST /select was refused: no Origin header, so the request is not "
                "provably from the panel page.",
                fix=PanelOriginError.default_fix,
            )
        raise PanelOriginError(
            "POST /select was refused: the Origin is opaque ('null') and no "
            "Sec-Fetch-Site: same-origin accompanies it, so the request is not "
            "provably from the panel page.",
            fix=PanelOriginError.default_fix,
        )
    parsed = urlsplit(str(origin))
    if parsed.scheme.lower() != "http":
        raise PanelOriginError(
            f"POST /select was refused: Origin scheme {parsed.scheme!r} is not "
            "'http' (this panel is plain HTTP).",
            fix=PanelOriginError.default_fix,
        )
    origin_host = (parsed.hostname or "").lower()
    if not is_loopback_host(origin_host):
        raise PanelOriginError(
            f"POST /select was refused: Origin host {origin_host!r} is not "
            "loopback, so this is a cross-origin request.",
            fix=PanelOriginError.default_fix,
        )
    host_header = headers.get("Host")
    if not host_header:
        raise PanelOriginError(
            "POST /select was refused: the request carries no Host header, so "
            "the origin cannot be checked.",
            fix=PanelOriginError.default_fix,
        )
    host_parsed = urlsplit(f"//{host_header}")
    host_host = (host_parsed.hostname or "").lower()
    if host_host != origin_host:
        raise PanelOriginError(
            f"POST /select was refused: Origin host {origin_host!r} does not "
            f"match Host {host_host!r}.",
            fix=PanelOriginError.default_fix,
        )
    expected_port = request.server_port if server_port is None else server_port
    if expected_port:
        for label, port in (("Origin", parsed.port), ("Host", host_parsed.port)):
            if port is not None and int(port) != int(expected_port):
                raise PanelOriginError(
                    f"POST /select was refused: the {label} port {port} is not "
                    f"the port this panel is running on ({expected_port}).",
                    fix=PanelOriginError.default_fix,
                )
    site = headers.get("Sec-Fetch-Site")
    if site is not None and str(site).strip().lower() not in ("same-origin", "none", ""):
        raise PanelOriginError(
            f"POST /select was refused: Sec-Fetch-Site is {site!r}, not "
            "same-origin.",
            fix=PanelOriginError.default_fix,
        )
    authority = f"{origin_host}:{parsed.port}" if parsed.port else origin_host
    return f"{parsed.scheme.lower()}://{authority}"


def parse_form(request: PanelRequest) -> dict[str, str]:
    """Parse a urlencoded POST body, refusing anything else by name."""
    body = request.body or b""
    if request.declared_length is not None and request.declared_length > MAX_BODY_BYTES:
        raise PanelTooLarge(
            f"the request declares a {request.declared_length}-byte body; the "
            f"panel refuses anything over {MAX_BODY_BYTES}."
        )
    if len(body) > MAX_BODY_BYTES:
        raise PanelTooLarge(
            f"the request body is {len(body)} bytes; the panel refuses anything "
            f"over {MAX_BODY_BYTES}."
        )
    if not body:
        return {}
    declared = str(request.headers.get("Content-Type") or "")
    media_type = declared.split(";", 1)[0].strip().lower()
    if declared and media_type != "application/x-www-form-urlencoded":
        raise PanelBadRequest(
            f"content type {declared!r} is not accepted on a panel form.",
            fix="Post the selector form as application/x-www-form-urlencoded.",
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PanelBadRequest(f"the request body is not valid UTF-8 ({exc}).") from exc
    try:
        raw = parse_qs(text, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:  # pragma: no cover - parse_qs is permissive
        raise PanelBadRequest(f"the request body could not be parsed ({exc}).") from exc
    return {key: values[-1] for key, values in raw.items()}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class PanelState:
    """Everything the page shows, as an injectable source.

    The base class deliberately does **not** implement the sections: a state
    object that cannot answer a section raises :class:`PanelStateError`, which
    the renderer turns into a named PROBLEM box.  Partial fakes are therefore
    safe, and "I could not read this" is never rendered as "there is nothing
    here".
    """

    #: Provider names offered in the selector, canonical spelling.
    PROVIDERS: tuple[str, ...] = ("openai_compatible", "ollama")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__}>"

    # -- reads ------------------------------------------------------------ #

    def provider_model(self) -> ProviderModel:
        raise PanelStateError("provider_model")

    def key_status(self) -> Sequence[KeyStatus]:
        raise PanelStateError("key_status")

    def config_credentials(self) -> Sequence[ConfigCredential]:
        return ()

    def router_status(self) -> RouterStatus:
        raise PanelStateError("router_status")

    def calendar_today(self) -> CalendarStatus:
        raise PanelStateError("calendar_today")

    def self_check(self) -> SelfCheckStatus:
        raise PanelStateError("self_check")

    def last_actions(self) -> ActionsStatus:
        raise PanelStateError("last_actions")

    # -- selector ---------------------------------------------------------- #

    def available_providers(self) -> Sequence[str]:
        return self.PROVIDERS

    def available_models(self) -> Sequence[str]:
        return ()

    def select_model(self, provider: str, model: str) -> WriteResult:
        """Persist a provider/model selection. Raises to refuse."""
        raise PanelStateError("select_model")


class RealPanelState(PanelState):
    """The real sources: ``config.json``, the key store, ``CLAIRE.md``, the
    schedule, the self-check and the action journal.

    Every path and collaborator is injectable so tests never touch the owner's
    real files.  Nothing here calls ``KeyStore.get`` — the codebase's one
    raw-value accessor — and the panel has no way to show a value it never read.
    """

    #: How long a self-check report is reused before probes are run again.
    SELF_CHECK_TTL_SECONDS = 30.0

    #: How many journal lines the panel shows.
    ACTION_LIMIT = 8

    #: The credential-shaped fields noticed (presence only) in config.json.
    CONFIG_CREDENTIAL_FIELDS = ("llm_api_key", "embedding_api_key", "brave_search_api_key")

    def __init__(
        self,
        *,
        config_path: "str | os.PathLike[str] | None" = None,
        key_store: Any | None = None,
        router_path: "str | os.PathLike[str] | None" = None,
        schedule_path: "str | os.PathLike[str] | None" = None,
        action_log_path: "str | os.PathLike[str] | None" = None,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], Any] | None = None,
        self_check_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._config_path = config_path
        self._key_store = key_store
        self._router_path = router_path
        self._schedule_path = schedule_path
        self._action_log_path = action_log_path
        self._environ = environ
        self._now = now or _date.today
        self._self_check_factory = self_check_factory
        self._self_check_cache: tuple[float, SelfCheckStatus] | None = None

    # -- helpers ---------------------------------------------------------- #

    @property
    def env(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    def _now_date(self) -> _date:
        value = self._now()
        if isinstance(value, _date):
            return value
        try:
            return _date.fromisoformat(str(value))
        except ValueError as exc:
            raise PanelStateError("now") from exc

    def _config(self) -> dict[str, Any]:
        from claire.config import default_config_path, get_default_config

        path = Path(self._config_path) if self._config_path else default_config_path()
        raw: dict[str, Any] = {}
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    raw = loaded
        except (OSError, ValueError):
            raw = {}
        merged = dict(get_default_config())
        merged.update(raw)
        return merged

    def _store(self) -> Any:
        from claire.platform.keys import KeyStore, default_store_path

        if self._key_store is not None:
            return self._key_store
        return KeyStore(
            Path(self._config_path).parent / "keys.json"
            if self._config_path
            else default_store_path(),
            env=self.env,
        )

    # -- reads ------------------------------------------------------------ #

    def provider_model(self) -> ProviderModel:
        from claire.config import (
            OMNIROUTE_BASE_URL,
            OMNIROUTE_DEFAULT_CHAT_MODEL,
            OMNIROUTE_PROVIDER_ALIAS,
        )

        config = self._config()
        raw_provider = str(config.get("llm_provider", "") or "").strip().lower()
        provider = "openai_compatible" if raw_provider == OMNIROUTE_PROVIDER_ALIAS else raw_provider
        if provider not in ("ollama", "openai_compatible"):
            provider = "ollama"
        base_url = str(config.get("llm_base_url", "") or "").strip()
        if not base_url:
            base_url = OMNIROUTE_BASE_URL if provider == "openai_compatible" else str(
                config.get("ollama_base_url", "") or ""
            )
        explicit = str(config.get("llm_chat_model", "") or "").strip()
        ollama_model = str(config.get("ollama_chat_model", "") or "")
        # Mirrors claire.config.load_settings exactly: on the Ollama path the
        # ``ollama_chat_model`` picker is authoritative, so a stale
        # ``llm_chat_model`` can never shadow it.
        if provider == "openai_compatible":
            if explicit:
                model = explicit
            elif base_url == OMNIROUTE_BASE_URL:
                model = OMNIROUTE_DEFAULT_CHAT_MODEL
            else:
                model = ollama_model
        else:
            model = ollama_model
        return ProviderModel(
            provider=provider,
            model=model,
            base_url=base_url,
            source="config.json",
        )

    def config_credentials(self) -> Sequence[ConfigCredential]:
        config = self._config()
        found = []
        for name in self.CONFIG_CREDENTIAL_FIELDS:
            value = config.get(name)
            present = bool(str(value or "").strip())
            if present:
                found.append(ConfigCredential(name=name, present=present))
        return tuple(found)

    def key_status(self) -> Sequence[KeyStatus]:
        from claire.platform.keys import KeyNotFoundError

        store = self._store()
        names: set[str] = set()
        try:
            names.update(str(name) for name in store.list_names())
        except (KeyStoreError, OSError) as exc:
            raise PanelSourceError(
                "the key store",
                exc,
                fix=(
                    f"{exc}" if isinstance(exc, KeyStoreError) else
                    f"Check that the key store file is readable by the Claire process."
                ),
            ) from exc
        names.update(str(name) for name in DEFAULT_ENV_SOURCES)
        statuses = []
        for name in sorted(names):
            try:
                rendered = store.render(name)
            except KeyNotFoundError:
                statuses.append(KeyStatus(name=name, present=False))
                continue
            except (KeyStoreError, OSError) as exc:
                raise PanelSourceError("the key store", exc) from exc
            # ``render()`` returns the mask plus "(<source>)" so a log reader
            # knows which source answered.  The panel splits them: the mask goes
            # in the value column, the label in the source column.  Nothing here
            # ever touches the value itself.
            masked, marker, label = str(rendered).rpartition(" (")
            if not marker:
                masked, label = str(rendered), "store"
            statuses.append(
                KeyStatus(
                    name=name,
                    present=True,
                    render=masked,
                    source=label.rstrip(")") or "store",
                )
            )
        return tuple(statuses)

    def router_status(self) -> RouterStatus:
        from claire.memory import notes as notes_module
        from claire.memory import router as router_module

        try:
            router = router_module.load_router(self._router_path, create=False)
        except (router_module.RouterError, notes_module.NotesError) as exc:
            path = str(getattr(exc, "path", "") or self._router_path or "")
            return RouterStatus(
                path=path,
                error=f"{type(exc).__name__}: {exc}",
                fix=getattr(exc, "fix", None)
                or (
                    "Create CLAIRE.md from Claire's template: "
                    "python -c \"from claire.memory.router import ensure_router; "
                    "ensure_router()\" - the panel reads the file and never "
                    "creates it behind your back."
                ),
            )
        warning = ""
        if router.over_budget:
            rendered = router.render_for_prompt()
            marker = router_module.MEMORY_HEADER
            if marker in rendered:
                warning = rendered.split(marker, 1)[0].strip()
        return RouterStatus(
            path=str(router.path),
            exists=True,
            size_bytes=int(router.size_bytes),
            budget_bytes=int(router.budget_bytes),
            over_budget=bool(router.over_budget),
            dropped_bytes=int(getattr(router, "dropped_bytes", 0) or 0),
            created_from_template=bool(getattr(router, "created_from_template", False)),
            warning=warning,
        )

    def calendar_today(self) -> CalendarStatus:
        from claire.schedule import calendar as calendar_module
        from claire.schedule import store as store_module

        day = self._now_date().isoformat()
        try:
            schedule = store_module.load_schedule(self._schedule_path)
        except store_module.ScheduleStoreError as exc:
            return CalendarStatus(
                date=day,
                error=f"{type(exc).__name__}: {exc}",
                fix=(
                    "Repair or move the calendar file, then reload; the panel "
                    "will not guess at a damaged schedule."
                ),
            )
        listed = calendar_module.list_events(schedule, day, day)
        events = tuple(
            self._calendar_event(calendar_module, item)
            for item in listed.get("occurrences", [])
        )
        return CalendarStatus(
            date=day,
            path=str(getattr(schedule, "path", "") or self._schedule_path or ""),
            existed=bool(getattr(schedule, "existed", False)),
            events=events,
        )

    @staticmethod
    def _calendar_event(calendar_module: Any, item: Mapping[str, Any]) -> CalendarEvent:
        """Flatten one occurrence, formatting its clock through the calendar.

        Occurrence ``start``/``end`` are minutes from midnight, and
        ``format_spoken`` is the calendar module's own formatter — the panel does
        not re-implement the clock, it asks the owner of the schema.
        """
        text_date = str(item.get("date", ""))
        start_minutes = item.get("start")
        end_minutes = item.get("end")
        spoken = ""
        try:
            spoken = calendar_module.format_spoken(text_date, start_minutes, end_minutes)
        except calendar_module.ScheduleError:
            try:
                spoken = calendar_module.format_spoken(text_date, start_minutes)
            except calendar_module.ScheduleError:
                spoken = text_date
        clock = spoken.split(" at ", 1)[1] if " at " in spoken else ""
        start_text, _, end_text = clock.partition(" to ")
        return CalendarEvent(
            title=str(item.get("title", "")),
            date=text_date,
            start=start_text,
            end=end_text,
            duration_minutes=int(item.get("duration", 0) or 0),
            notes=str(item.get("notes", "") or ""),
            is_recurring=bool(item.get("is_recurring")),
            event_id=str(item.get("event_id", "")),
            spoken=spoken,
        )

    def self_check(self) -> SelfCheckStatus:
        import time as _time

        cached = self._self_check_cache
        if cached is not None and (_time.monotonic() - cached[0]) < self.SELF_CHECK_TTL_SECONDS:
            return cached[1]
        try:
            if self._self_check_factory is not None:
                report = self._self_check_factory().run()
            else:
                from claire.platform.service import SelfCheck

                report = SelfCheck().run()
        except Exception as exc:  # noqa: BLE001 - a broken check is a finding
            status = SelfCheckStatus(
                error=f"{type(exc).__name__}: {_scrub(exc)}",
                fix="Run `python -m claire.platform.service` in a terminal to see the traceback.",
            )
            self._self_check_cache = (_time.monotonic(), status)
            return status
        checks = tuple(
            SelfCheckView(
                name=str(getattr(check, "name", "")),
                title=str(getattr(check, "title", "")),
                status=str(getattr(check, "status", "")),
                detail=str(getattr(check, "detail", "") or ""),
                fix=getattr(check, "fix", None),
            )
            for check in getattr(report, "checks", ())
        )
        status = SelfCheckStatus(
            ok=bool(getattr(report, "ok", False)),
            summary_line=str(getattr(report, "summary_line", "")),
            generated_at=str(getattr(report, "generated_at", "") or ""),
            checks=checks,
        )
        self._self_check_cache = (_time.monotonic(), status)
        return status

    def action_log_path(self) -> Path:
        if self._action_log_path is not None:
            return Path(self._action_log_path)
        override = self.env.get(ACTION_LOG_ENV_VAR)
        if override and str(override).strip():
            return Path(str(override)).expanduser()
        return Path(DEFAULT_ACTION_LOG).expanduser()

    def last_actions(self) -> ActionsStatus:
        path = self.action_log_path()
        if not path.exists():
            return ActionsStatus(
                path=str(path),
                error=f"no action journal at {path}.",
                fix=(
                    f"Point Claire's action journal somewhere with "
                    f"{ACTION_LOG_ENV_VAR}=<path> (one JSON object per line: "
                    "at, action, outcome, detail); until then the panel cannot "
                    "say what Claire did."
                ),
            )
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ActionsStatus(
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
                fix=f"Check that {path} is readable by the Claire process.",
            )
        records: list[ActionRecord] = []
        skipped = 0
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(item, dict):
                skipped += 1
                continue
            records.append(
                ActionRecord(
                    at=str(item.get("at", "") or ""),
                    action=str(item.get("action", "") or ""),
                    outcome=str(item.get("outcome", "") or ""),
                    detail=str(item.get("detail", "") or ""),
                )
            )
        tail = tuple(records[-self.ACTION_LIMIT :])
        return ActionsStatus(
            path=str(path),
            records=tail,
            skipped_lines=skipped,
            error=None
            if tail or not skipped
            else "the action journal holds no readable records.",
            fix=None if tail else f"Rewrite {path} as one JSON object per line.",
        )

    # -- selector ---------------------------------------------------------- #

    def available_models(self) -> Sequence[str]:
        from claire.config import SUPPORTED_CHAT_MODELS

        models = sorted(SUPPORTED_CHAT_MODELS)
        current = self.provider_model().model
        if current and current not in models:
            models.insert(0, current)
        return tuple(models)

    def select_model(self, provider: str, model: str) -> WriteResult:
        from claire.config import OMNIROUTE_PROVIDER_ALIAS, _save_json, default_config_path

        path = Path(self._config_path) if self._config_path else default_config_path()
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, ValueError):
                raw = {}
        changed: list[str] = []
        if provider == "ollama":
            if raw.get("llm_provider") != "ollama":
                raw["llm_provider"] = "ollama"
                changed.append("llm_provider")
            if raw.get("ollama_chat_model") != model:
                raw["ollama_chat_model"] = model
                changed.append("ollama_chat_model")
        else:
            if raw.get("llm_provider") != "openai_compatible":
                raw["llm_provider"] = "openai_compatible"
                changed.append("llm_provider")
            if raw.get("llm_chat_model") != model:
                raw["llm_chat_model"] = model
                changed.append("llm_chat_model")
        if not changed:
            return WriteResult(
                provider=provider, model=model, config_path=str(path), changed=()
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PanelWriteError(
                f"could not create the config directory {path.parent}: {exc}",
                fix=f"Create {path.parent} yourself (mode 0700) and retry.",
            ) from exc
        if not _save_json(path, raw):
            raise PanelWriteError(
                f"writing {path} failed; the selection was not changed.",
                fix=f"Check that {path} and {path.parent} are writable, then retry.",
            )
        canonical = provider
        if provider == OMNIROUTE_PROVIDER_ALIAS:
            canonical = "openai_compatible"
        return WriteResult(
            provider=canonical,
            model=model,
            config_path=str(path),
            changed=tuple(changed),
        )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 1.25rem; background: #14161a; color: #e6e8eb;
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 0 0 .6rem; letter-spacing: .02em; }
a { color: #7cc4ff; }
.sub { color: #9aa3ad; margin: 0 0 1rem; }
section { background: #1b1e24; border: 1px solid #2a2f38; border-radius: 8px;
          padding: .9rem 1rem; margin: 0 0 1rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid #262b33;
         vertical-align: top; }
th { color: #9aa3ad; font-weight: 600; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88em; }
.ok { color: #5fd08a; } .warn { color: #f0b429; } .bad { color: #ff6b6b; }
.muted { color: #9aa3ad; }
.pill { display: inline-block; padding: .05rem .45rem; border-radius: 999px;
        border: 1px solid #39404b; font-size: .8rem; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #101216;
      border: 1px solid #33261a; border-left: 4px solid #ff6b6b; border-radius: 6px;
      padding: .7rem .8rem; margin: .5rem 0 0; color: #ffd7d7; }
.fix { background: #101216; border: 1px solid #2a2f38; border-left: 4px solid #7cc4ff;
       border-radius: 6px; padding: .6rem .8rem; margin: .5rem 0 0; }
form { display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; margin-top: .6rem; }
label { display: flex; flex-direction: column; gap: .2rem; color: #9aa3ad; font-size: .85rem; }
select, input { background: #101216; color: #e6e8eb; border: 1px solid #39404b;
                border-radius: 6px; padding: .35rem .5rem; font: inherit; }
button { background: #2a6df4; color: #fff; border: 0; border-radius: 6px;
         padding: .45rem .9rem; font: inherit; cursor: pointer; }
footer { color: #6f7883; font-size: .85rem; margin-top: .5rem; }
.bar { height: 8px; background: #101216; border: 1px solid #39404b; border-radius: 999px;
       overflow: hidden; margin: .35rem 0; }
.bar > i { display: block; height: 100%; background: #5fd08a; }
.bar.over > i { background: #ff6b6b; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="same-origin">\n'
        f"<title>{_esc(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f"{body}\n</body>\n</html>\n"
    )


def _section(title: str, inner: str) -> str:
    return f"<section>\n<h2>{_esc(title)}</h2>\n{inner}\n</section>"


def _problem(message: str, fix: str | None) -> str:
    parts = [f'<pre class="mono">! {_esc(message)}</pre>']
    if fix:
        parts.append(f'<div class="fix"><strong>Fix:</strong> {_esc(fix)}</div>')
    else:
        parts.append(
            '<div class="fix"><strong>Fix:</strong> nothing further is known; '
            "run the self-check for a named check.</div>"
        )
    return "\n".join(parts)


def _guard(name: str, thunk: Callable[[], str]) -> str:
    """Run one section; a raise becomes a named PROBLEM box, never a 500."""
    try:
        return thunk()
    except PanelError as exc:
        return _problem(f"{exc.code}: {exc.message}", exc.fix)
    except Exception as exc:  # noqa: BLE001 - a broken source is a finding
        detail = _scrub(exc)
        message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return _problem(
            f"{name} could not be read - {message}",
            "Run `python -m claire.platform.service` for the named check behind "
            "this section; report it with that output.",
        )


#: Which environment variable carries each provider's credential.  Used to
#: answer "does the provider I selected have a key stored?" without the panel
#: ever holding a value.
PROVIDER_KEY_NAMES: dict[str, str] = {
    "openai_compatible": "OMNIROUTE_API_KEY",
    "omniroute": "OMNIROUTE_API_KEY",
}


def _provider_section(state: PanelState, *, changed: bool) -> str:
    def build() -> str:
        info = state.provider_model()
        if info.error:
            return _problem(info.error, info.fix)
        rows = [
            ("Provider", f'<span class="mono">{_esc(info.provider)}</span>'),
            ("Model", f'<span class="mono">{_esc(info.model)}</span>'),
            ("Endpoint", f'<span class="mono">{_esc(info.base_url)}</span>'),
            ("Read from", _esc(info.source)),
            ("Key", _key_for_provider(state, info.provider)),
        ]
        table = "<table><tbody>" + "".join(
            f"<tr><th>{_esc(label)}</th><td>{value}</td></tr>" for label, value in rows
        ) + "</tbody></table>"
        return table + _selector_form(state, info)

    inner = _guard("provider and model", build)
    if changed:
        inner = '<p class="ok">Selection saved.</p>\n' + inner
    return _section("Provider and model", inner)


def _key_for_provider(state: PanelState, provider: str) -> str:
    """Whether the selected provider has a key stored — as a mask or 'absent'."""
    name = PROVIDER_KEY_NAMES.get((provider or "").lower())
    if name is None:
        return '<span class="muted">not needed for this provider</span>'
    try:
        statuses = list(state.key_status())
    except Exception:  # noqa: BLE001 - the row must not blank the section
        return '<span class="muted">unknown (the key store could not be read)</span>'
    for status in statuses:
        if status.name == name:
            css = "ok" if status.present else "warn"
            return (
                f'<span class="mono {css}">{_esc(status.label)}</span> '
                f'<span class="muted">{_esc(status.source)}</span>'
            )
    return f'<span class="warn">absent</span> <span class="muted">({_esc(name)})</span>'


def _selector_form(state: PanelState, info: ProviderModel) -> str:
    def options(values: Iterable[str], current: str) -> str:
        seen: list[str] = []
        for value in values:
            text = str(value)
            if text and text not in seen:
                seen.append(text)
        if current and current not in seen:
            seen.insert(0, current)
        return "".join(
            f'<option value="{_esc(value)}"{" selected" if value == current else ""}>'
            f"{_esc(value)}</option>"
            for value in seen
        )

    def safe(call: Callable[[], Sequence[str]], fallback: Sequence[str]) -> Sequence[str]:
        try:
            return call()
        except Exception:  # noqa: BLE001 - the selector must still render
            return fallback

    providers = safe(state.available_providers, PanelState.PROVIDERS)
    models = safe(state.available_models, (info.model,))
    return (
        '<form method="post" action="/select">\n'
        '<label>Provider<select name="provider">'
        f"{options(providers, info.provider)}</select></label>\n"
        '<label>Model<input name="model" list="known-models" size="34" '
        f'value="{_esc(info.model)}" required></label>\n'
        "<datalist id=\"known-models\">"
        + "".join(f'<option value="{_esc(m)}"></option>' for m in models)
        + "</datalist>\n"
        '<button type="submit">Save selection</button>\n'
        "</form>\n"
        '<p class="muted">The selector writes <span class="mono">llm_provider</span> and the '
        "matching model field to Claire's config. Keys are <strong>not</strong> set here: there "
        "is no key field on this page, and a POST that carries one is refused. Set a key "
        "out-of-band in the key store, or export the provider's environment variable before "
        "starting Claire.</p>"
    )


def _keys_section(state: PanelState) -> str:
    def build() -> str:
        statuses = list(state.key_status())
        extra = list(state.config_credentials())
        rows = []
        for status in statuses:
            css = "ok" if status.present else "muted"
            rows.append(
                "<tr>"
                f"<td class=\"mono\">{_esc(status.name)}</td>"
                f"<td class=\"{css}\">{_esc(status.label)}</td>"
                f"<td>{_esc(status.source)}</td>"
                "</tr>"
            )
        for item in extra:
            rows.append(
                "<tr>"
                f"<td class=\"mono\">{_esc(item.name)}</td>"
                f"<td class=\"warn\">present in config.json (value not shown)</td>"
                f"<td>{_esc('config.json')}</td>"
                "</tr>"
            )
        if not rows:
            rows.append(
                '<tr><td colspan="3" class="muted">No keys are known to the key '
                "store and no key environment variable is set.</td></tr>"
            )
        table = (
            "<table><thead><tr><th>Name</th><th>Value</th><th>Source</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
        return table + (
            '<p class="muted">Masks only: last four characters plus a fingerprint. '
            "A raw value is never read for this page and cannot be rendered by it.</p>"
        )

    return _section("Providers and keys", _guard("key store", build))


def _router_section(state: PanelState) -> str:
    def build() -> str:
        status = state.router_status()
        if status.error:
            return _problem(status.error, status.fix)
        fraction = max(0.0, min(status.fraction, 1.0))
        css = "over" if status.over_budget else ""
        state_word = (
            '<span class="bad">OVER BUDGET</span>'
            if status.over_budget
            else '<span class="ok">within budget</span>'
        )
        parts = [
            f'<p class="mono">{_esc(status.path)}</p>',
            f'<div class="bar {css}"><i style="width:{fraction * 100:.1f}%"></i></div>',
            f'<p>{status.size_bytes} bytes of {status.budget_bytes} bytes - {state_word}.</p>',
        ]
        if status.created_from_template:
            parts.append(
                '<p class="warn">This file did not exist; Claire created it from '
                "its built-in template and reported that it did.</p>"
            )
        if status.over_budget:
            warning = status.warning or (
                "!! WARNING: YOUR ALWAYS-LOADED MEMORY (CLAIRE.md) IS OVER BUDGET "
                "AND WAS TRUNCATED\n"
                f"!! Real size: {status.size_bytes} bytes. Budget: "
                f"{status.budget_bytes} bytes. Dropped: {status.dropped_bytes} bytes.\n"
                "!! Those dropped bytes are NOT being loaded: the end of the file "
                "is invisible to me.\n"
                "!! Fix it by moving detail into notes/ and keeping only one-line "
                "pointers here."
            )
            parts.append(f'<pre class="mono">{_esc(warning)}</pre>')
            parts.append(
                '<div class="fix"><strong>Fix:</strong> move detail into '
                "<span class=\"mono\">notes/</span> and keep one-line pointers in "
                "CLAIRE.md; the warning above is exactly what Claire prepends to "
                "your system prompt.</div>"
            )
        return "\n".join(parts)

    return _section("Memory router (CLAIRE.md)", _guard("memory router", build))


def _calendar_section(state: PanelState) -> str:
    def build() -> str:
        status = state.calendar_today()
        if status.error:
            return _problem(status.error, status.fix)
        heading = f'<p class="mono">{_esc(status.date)}</p>'
        if not status.existed:
            return heading + (
                '<p class="muted">No calendar file yet at '
                f'<span class="mono">{_esc(status.path)}</span> - nothing is '
                "booked because there is nothing to book into.</p>"
            )
        if not status.events:
            return heading + '<p class="muted">Nothing booked today.</p>'
        rows = []
        for event in status.events:
            recurring = ' <span class="pill">recurring</span>' if event.is_recurring else ""
            notes = f'<div class="muted">{_esc(event.notes)}</div>' if event.notes else ""
            when = f"{event.start}-{event.end}" if event.end else (event.start or event.date)
            rows.append(
                "<tr>"
                f'<td class="mono">{_esc(when)}</td>'
                f"<td>{_esc(event.title)}{recurring}{notes}</td>"
                f'<td class="muted">{event.duration_minutes} min</td>'
                "</tr>"
            )
        return (
            heading
            + "<table><thead><tr><th>When</th><th>What</th><th>Length</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    return _section("Today's calendar", _guard("calendar", build))


def _self_check_section(state: PanelState) -> str:
    def build() -> str:
        report = state.self_check()
        if report.error:
            return _problem(report.error, report.fix)
        css = "ok" if report.ok else "bad"
        parts = [
            f'<p class="{css}">{_esc(report.summary_line or "no summary")}</p>',
            f'<p class="muted">checked at {_esc(report.generated_at or "unknown time")} - '
            f'<a href="{ROUTE_SELF_CHECK}">full text report</a></p>',
        ]
        rows = []
        for check in report.checks:
            if check.status == "ready":
                continue
            rows.append(
                "<tr>"
                f"<td class=\"{'warn' if check.status == 'missing' else 'bad'}\">"
                f"{_esc(check.status.upper())}</td>"
                f"<td>{_esc(check.title or check.name)}<div class=\"muted\">"
                f"{_esc(check.detail)}</div></td>"
                f"<td>{_esc(check.fix or '')}</td>"
                "</tr>"
            )
        if rows:
            parts.append(
                "<table><thead><tr><th>Status</th><th>Check</th><th>Fix</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
            )
        else:
            parts.append('<p class="ok">Every check is ready.</p>')
        return "\n".join(parts)

    return _section("Self-check", _guard("self-check", build))


def _actions_section(state: PanelState) -> str:
    def build() -> str:
        status = state.last_actions()
        if status.error and not status.records:
            return _problem(status.error, status.fix)
        rows = []
        for record in status.records:
            css = "ok" if record.outcome.lower() in ("ok", "sent", "success", "done") else ""
            rows.append(
                "<tr>"
                f"<td class=\"mono\">{_esc(record.at)}</td>"
                f"<td>{_esc(record.action)}</td>"
                f"<td class=\"{css}\">{_esc(record.outcome)}</td>"
                f"<td class=\"muted\">{_esc(record.detail)}</td>"
                "</tr>"
            )
        if not rows:
            rows.append('<tr><td colspan="4" class="muted">No actions recorded yet.</td></tr>')
        parts = [
            "<table><thead><tr><th>When</th><th>Action</th><th>Outcome</th>"
            f"<th>Detail</th></tr></thead><tbody>{''.join(rows)}</tbody></table>",
            f'<p class="muted mono">{_esc(status.path)}</p>',
        ]
        if status.skipped_lines:
            parts.append(
                f'<p class="warn">{status.skipped_lines} journal line(s) were not '
                "readable JSON and were skipped.</p>"
            )
        if status.error:
            parts.append(f'<p class="warn">{_esc(status.error)}</p>')
        return "\n".join(parts)

    return _section("Last actions", _guard("action journal", build))


def render_index(
    state: PanelState,
    *,
    changed: bool = False,
    bind_host: str = DEFAULT_BIND_HOST,
    bind_port: int = DEFAULT_PORT,
) -> PanelResponse:
    """The one page: everything the owner came for, server-rendered."""
    body = "\n".join(
        [
            "<h1>Claire panel</h1>",
            f'<p class="sub">Local only - bound to <span class="mono">'
            f"{_esc(bind_host)}:{_esc(bind_port)}</span>, plain HTTP, no login. "
            "It never shows a key value.</p>",
            _provider_section(state, changed=changed),
            _keys_section(state),
            _router_section(state),
            _calendar_section(state),
            _self_check_section(state),
            _actions_section(state),
            f'<footer>Refresh for current state. Text report: '
            f'<a href="{ROUTE_SELF_CHECK}">{ROUTE_SELF_CHECK}</a>.</footer>',
        ]
    )
    return PanelResponse(status=200, body=_page("Claire panel", body).encode("utf-8"))


def render_self_check_text(state: PanelState) -> PanelResponse:
    """The self-check as plain text, so 'is anything broken' is one click."""
    try:
        report = state.self_check()
    except PanelError as exc:
        text = f"{exc.code}: {exc.message}\nfix: {exc.fix}\n"
        return PanelResponse(status=200, body=text.encode("utf-8"), content_type=TEXT_CONTENT_TYPE)
    except Exception as exc:  # noqa: BLE001
        detail = _scrub(exc)
        text = f"self-check-unavailable: {type(exc).__name__}: {detail}\n"
        return PanelResponse(status=200, body=text.encode("utf-8"), content_type=TEXT_CONTENT_TYPE)
    if report.error:
        text = f"self-check-unavailable: {report.error}\nfix: {report.fix or ''}\n"
        return PanelResponse(status=200, body=text.encode("utf-8"), content_type=TEXT_CONTENT_TYPE)
    lines = [f"Claire self-check - {report.generated_at or 'unknown time'}"]
    for check in report.checks:
        lines.append(f"[{check.status.upper()}] {check.title or check.name}: {check.detail}")
        if check.fix:
            lines.append(f"    fix: {check.fix}")
    lines.append(f"Summary: {report.summary_line}")
    return PanelResponse(
        status=200,
        body=_scrub("\n".join(lines)).encode("utf-8") + b"\n",
        content_type=TEXT_CONTENT_TYPE,
        headers=(("X-Content-Type-Options", "nosniff"),),
    )


def render_health(state: PanelState) -> PanelResponse:
    """A tiny JSON document: is the panel up, and is anything broken?"""
    payload: dict[str, Any] = {"panel": PANEL_VERSION, "ok": False}
    try:
        report = state.self_check()
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {_scrub(exc)}"
    else:
        payload["ok"] = bool(report.ok) and not report.error
        payload["summary"] = report.summary_line
        payload["generated_at"] = report.generated_at
        if report.error:
            payload["error"] = report.error
    body = json.dumps(payload, indent=2, sort_keys=True).replace("<", "\\u003c") + "\n"
    return PanelResponse(
        status=200,
        body=body.encode("utf-8"),
        content_type=JSON_CONTENT_TYPE,
        headers=(("X-Content-Type-Options", "nosniff"),),
    )


def render_error_page(exc: PanelError) -> PanelResponse:
    """A standalone refusal page.

    It deliberately does **not** consult the state: a refused request must not
    cause reads of the owner's files, so a traversal attempt reads nothing.
    """
    body = [
        "<h1>Claire panel</h1>",
        f'<p class="sub">Refused - <span class="mono">{_esc(exc.code)}</span></p>',
        _section("Request refused", _problem(f"{exc.code}: {exc.message}", exc.fix)),
        f'<footer><a href="{ROUTE_INDEX}">Back to the panel</a></footer>',
    ]
    return PanelResponse(status=exc.status, body=_page("Claire panel - refused", "\n".join(body)).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

#: The read routes.  Exact strings only: no path from the request is ever used
#: to open a file, which is why directory traversal cannot succeed.
GET_VIEWS: dict[str, Callable[[PanelState], PanelResponse]] = {
    ROUTE_INDEX: render_index,
    ROUTE_SELF_CHECK: render_self_check_text,
    ROUTE_HEALTH: render_health,
}


def _redirect(location: str) -> PanelResponse:
    return PanelResponse(
        status=303,
        body=b"",
        content_type=HTML_CONTENT_TYPE,
        headers=(("Location", location),),
    )


def handle_select(state: PanelState, request: PanelRequest) -> PanelResponse:
    """The write endpoint: validate origin, refuse key fields, write, redirect."""
    check_write_origin(request)
    form = parse_form(request)
    forbidden = sorted(name for name in form if name.strip().lower() in FORBIDDEN_KEY_FIELDS)
    if forbidden:
        raise PanelOriginError(
            "POST /select was refused: the body carries "
            f"{', '.join(repr(name) for name in forbidden)}, and this page never "
            "accepts a key value.",
            fix=(
                "Set a key out-of-band instead: store it with "
                "`python -m claire.platform.service --help` style tooling or "
                "KeyStore.store(), or export the provider's environment variable. "
                "The panel form has no key field by design."
            ),
        )
    provider_raw = str(form.get("provider", "") or "").strip()
    model = str(form.get("model", "") or "").strip()
    if not provider_raw:
        raise PanelBadRequest(
            "the form carried no provider.",
            fix="Reload the panel and submit the provider selector.",
        )
    try:
        providers = [str(name) for name in state.available_providers()]
    except Exception:  # noqa: BLE001 - fall back to the documented set
        providers = list(PanelState.PROVIDERS)
    lookup = {name.lower(): name for name in providers}
    provider = lookup.get(provider_raw.lower())
    if provider is None:
        raise PanelBadRequest(
            f"unknown provider {provider_raw!r}.",
            fix=f"Pick one of: {', '.join(providers)}.",
        )
    if not _PROVIDER_RE.match(provider):
        raise PanelBadRequest(f"provider {provider!r} is not a usable name.")
    if not model:
        raise PanelBadRequest(
            "the form carried no model.",
            fix="Type a model id into the selector (for OmniRouter, an id such as "
            "cmd/deepseek/deepseek-v4.1-flash).",
        )
    if not _MODEL_RE.match(model):
        raise PanelBadRequest(
            f"model {model!r} is not a usable model id.",
            fix="Use letters, digits and . _ / : @ + - only (max 128 characters).",
        )
    try:
        state.select_model(provider, model)
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed write is a named refusal
        raise PanelWriteError(
            f"the selection was not saved: {type(exc).__name__}: {_scrub(exc)}",
            fix="Fix the cause above and submit the form again.",
        ) from exc
    return _redirect(f"{ROUTE_INDEX}?changed=1")


def dispatch(state: PanelState, request: PanelRequest) -> PanelResponse:
    """Route one request. Never raises: every refusal is a rendered page."""
    try:
        path, query = parse_target(request.path)
    except PanelError as exc:
        return render_error_page(exc)
    if path == ROUTE_SELECT:
        if request.method != "POST":
            return render_error_page(
                PanelMethodNotAllowed(request.method, path, allowed=("POST",))
            )
        try:
            return handle_select(state, request)
        except PanelError as exc:
            return render_error_page(exc)
    if path == ROUTE_FAVICON:
        if request.method not in ("GET", "HEAD"):
            return render_error_page(
                PanelMethodNotAllowed(request.method, path, allowed=("GET",))
            )
        return PanelResponse(status=204, body=b"", content_type="image/x-icon")
    view = GET_VIEWS.get(path)
    if view is None:
        return render_error_page(PanelRouteNotFound(path))
    if request.method not in ("GET", "HEAD"):
        return render_error_page(PanelMethodNotAllowed(request.method, path, allowed=("GET",)))
    try:
        if path == ROUTE_INDEX:
            return render_index(
                state,
                changed=query.get("changed", [""])[-1] == "1",
                bind_host=request.server_host,
                bind_port=request.server_port,
            )
        return view(state)
    except PanelError as exc:  # pragma: no cover - sections guard themselves
        return render_error_page(exc)


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #


def _request_from_handler(handler: BaseHTTPRequestHandler) -> PanelRequest:
    declared: int | None = None
    length = handler.headers.get("Content-Length")
    if length:
        try:
            declared = int(length)
        except ValueError:
            declared = None
    body = b""
    if declared is not None and 0 < declared <= MAX_BODY_BYTES:
        body = handler.rfile.read(declared)
    elif declared is not None and declared > MAX_BODY_BYTES:
        handler.close_connection = True
    host_header = handler.headers.get("Host", "") or ""
    port = DEFAULT_PORT
    host = DEFAULT_BIND_HOST
    if handler.server is not None:
        try:
            host = str(handler.server.server_address[0]) or DEFAULT_BIND_HOST
            port = int(handler.server.server_address[1])
        except (IndexError, TypeError, ValueError):
            port = DEFAULT_PORT
    if host_header and host_header.startswith("["):
        host = host_header.split("]")[0].lstrip("[") or host
    elif ":" in host_header:
        host = host_header.rsplit(":", 1)[0] or host
    return PanelRequest(
        method=handler.command or "GET",
        path=handler.path or "/",
        headers=handler.headers,
        body=body,
        client=str(handler.client_address[0]) if handler.client_address else "127.0.0.1",
        server_port=port,
        server_host=host,
        declared_length=declared,
    )


class PanelHandler(BaseHTTPRequestHandler):
    """Serves the panel on one injected state.

    ``state`` is a class attribute so :func:`create_handler` can bind it without
    a closure, and tests can build a handler-free request with
    :func:`dispatch` instead of starting a socket.
    """

    state: PanelState = PanelState()
    server_version = "ClairePanel/" + PANEL_VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Never write a request line to stderr unscrubbed.

        ``BaseHTTPRequestHandler`` logs the raw request line, which can carry a
        credential in the query string.  The panel logs the method and the
        *path* only, scrubbed.
        """
        path = self.path or "/"
        try:
            path = urlsplit(path).path
        except ValueError:  # pragma: no cover - urlsplit is permissive
            path = "/"
        _LOGGER.info("panel %s %s", self.command, _scrub(path)[:_PATH_LOG_LIMIT])

    def _respond(self, response: PanelResponse) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # NOT "no-referrer": that policy makes the browser send an opaque
        # "Origin: null" on this panel's own form POST, which the write guard
        # then correctly refuses as unprovable - so the panel could never
        # accept its own form. "same-origin" keeps the referrer away from
        # other sites (the actual intent) while letting our own requests
        # carry a real Origin.
        self.send_header("Referrer-Policy", "same-origin")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and response.body:
            self.wfile.write(response.body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._respond(dispatch(self.state, _request_from_handler(self)))

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond(dispatch(self.state, _request_from_handler(self)))

    def do_POST(self) -> None:  # noqa: N802
        self._respond(dispatch(self.state, _request_from_handler(self)))

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._respond(
            render_error_page(
                PanelMethodNotAllowed(self.command, self.path or "/", allowed=("GET", "POST"))
            )
        )


def create_handler(state: PanelState) -> type[PanelHandler]:
    """Return a handler class bound to ``state`` (no server started)."""
    return type("BoundPanelHandler", (PanelHandler,), {"state": state})


class PanelServer(ThreadingHTTPServer):
    """A loopback HTTP server; ``daemon_threads`` so Ctrl-C always exits."""

    daemon_threads = True
    allow_reuse_address = True


def create_server(
    state: PanelState,
    host: str = DEFAULT_BIND_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_remote: bool = False,
) -> PanelServer:
    """Build (but do not start) the server. Loopback unless explicitly overridden."""
    bound = guard_bind_host(host, allow_remote=allow_remote)
    return PanelServer((bound, int(port)), create_handler(state))


def serve(
    state: PanelState | None = None,
    host: str = DEFAULT_BIND_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_remote: bool = False,
) -> None:
    """Serve until interrupted. The only place a socket is opened."""
    server = create_server(state or RealPanelState(), host, port, allow_remote=allow_remote)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    _LOGGER.info("Claire panel on http://%s:%s/ (plain HTTP, loopback)", bound_host, bound_port)
    print(f"Claire panel: http://{bound_host}:{bound_port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``python -m claire.panel.server [--host H] [--port N]``."""
    parser = argparse.ArgumentParser(
        prog="python -m claire.panel.server",
        description="Claire's local web panel - loopback only, server-rendered, no key values.",
    )
    parser.add_argument("--host", default=DEFAULT_BIND_HOST, help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port (default 8765)")
    parser.add_argument(
        "--allow-remote-binding",
        action="store_true",
        help="accept a non-loopback bind (plain HTTP, no TLS, no authentication)",
    )
    parser.add_argument("--action-log", default=None, help=f"action journal path (or ${ACTION_LOG_ENV_VAR})")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    state = RealPanelState(action_log_path=args.action_log)
    serve(
        state,
        host=args.host,
        port=args.port,
        allow_remote=args.allow_remote_binding,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
