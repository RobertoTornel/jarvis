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
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime as _datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
    "POST_ROUTES",
    "ROUTE_CHAT",
    "ROUTE_HEALTH",
    "ROUTE_INDEX",
    "ROUTE_SELECT",
    "ROUTE_SELF_CHECK",
    # data
    "ActionRecord",
    "ActionsStatus",
    "CalendarEvent",
    "CalendarStatus",
    "ChatResult",
    "ConfigCredential",
    "DiaryEntry",
    "DiaryStatus",
    "KeyStatus",
    "PanelRequest",
    "PanelResponse",
    "ProviderModel",
    "RouterStatus",
    "SelfCheckStatus",
    "SelfCheckView",
    "TranscriptStatus",
    "TurnRecord",
    "WriteResult",
    # errors
    "PanelBadRequest",
    "PanelBindError",
    "PanelError",
    "PanelLLMError",
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
    "TranscriptBuffer",
    # rendering / dispatch
    "dispatch",
    "handle_chat",
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
ROUTE_CHAT = "/chat"
ROUTE_MEMORY = "/memory"
ROUTE_MEMORY_SAVE = "/memory/save"
ROUTE_MEMORY_DELETE = "/memory/delete"

#: Every path that serves a page.  Tests iterate this; nothing else is served.
#: ``ROUTE_MEMORY`` takes query parameters (``?q=``, ``?note=``) and is
#: therefore dispatched by name rather than through ``GET_VIEWS``, exactly like
#: ``ROUTE_INDEX``.
GET_ROUTES: tuple[str, ...] = (
    ROUTE_INDEX,
    ROUTE_SELF_CHECK,
    ROUTE_HEALTH,
    ROUTE_FAVICON,
    ROUTE_MEMORY,
)

#: Every path that accepts a form POST.  A GET to one of these is a 405, not a
#: silent no-op, so a link that should have been a form is caught immediately.
POST_ROUTES: tuple[str, ...] = (
    ROUTE_SELECT,
    ROUTE_CHAT,
    ROUTE_MEMORY_SAVE,
    ROUTE_MEMORY_DELETE,
)

#: The longest message the chat box will forward to the model.  Bounded so a
#: pasted file cannot be pushed into the prompt (or the backend's timeout) by
#: accident; refused by name, never silently truncated.
MAX_CHAT_CHARS = 4000

#: How long the panel waits for one chat completion.
CHAT_TIMEOUT_SECONDS = 90.0

#: How many turns the panel's own transcript ring buffer keeps.
TRANSCRIPT_LIMIT = 50

#: The system prompt for a panel chat turn.  Deliberately short and honest about
#: the channel: this is typed text from the owner's browser, not a spoken turn,
#: and the panel shows the reply verbatim rather than speaking it.
PANEL_CHAT_SYSTEM_PROMPT = (
    "You are Claire, a local voice assistant, answering your owner through the "
    "local web panel instead of by voice. Keep replies short, warm and plain; "
    "they are read in a small browser card. Do not claim to have taken an action "
    "you did not take."
)

#: How many daily diary summaries the conversation section shows.
DIARY_LIMIT = 7

#: How many notes the memories browser lists on one page.  Large enough to see a
#: real vault at a glance, small enough that the page stays a page; the search
#: box is the answer for anything past it.
MEMORY_NOTE_LIMIT = 200

#: How many hits a memory search returns, per store.  Both stores are searched
#: with their own existing index (note search, then the summaries FTS table).
MEMORY_SEARCH_LIMIT = 25

#: How many characters of a note body the browser renders.  A note is meant to
#: be small, but the cap stops one pathological file from rendering a megabyte
#: of HTML, and the truncation is stated on the page.
MEMORY_BODY_CHARS = 20000

#: The largest note body the browser will write back.  Notes are short by
#: design, and the cap is deliberately well under ``MAX_BODY_BYTES`` so a note
#: that is too big is refused with a *note-shaped* explanation rather than the
#: generic "body too large" error for any form.
MEMORY_WRITE_MAX_CHARS = 32 * 1024

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


class PanelLLMError(PanelError):
    """The model was asked for a reply and did not give one.

    This exists because the backend's ``direct()`` signals every failure as
    ``None`` — an unreachable endpoint, a bad model id, a refused key and a
    server-side error are indistinguishable from inside.  The panel refuses to
    render that as an empty reply box: it names the provider, the model and the
    endpoint it actually asked, so the failure can be acted on.
    """

    status = 502
    code = "llm-unavailable"

    def __init__(self, provider: str, model: str, base_url: str, detail: str = "") -> None:
        where = " ".join(
            part
            for part in (
                f"provider {provider!r}" if provider else "",
                f"model {model!r}" if model else "",
                f"at {base_url}" if base_url else "",
            )
            if part
        )
        message = (
            f"the model returned nothing for this message"
            + (f" ({where})." if where else ".")
        )
        if detail:
            message += f" The backend reported: {detail}"
        message += (
            " The LLM backend signals every failure the same way - no reply - so "
            "this covers an unreachable endpoint, an unloaded model, a missing or "
            "refused credential, and a server-side error alike."
        )
        super().__init__(
            message,
            fix=(
                "Check the self-check page for the LLM endpoint, confirm the model "
                "id in the selector, and set the credential out-of-band if it is "
                "missing."
            ),
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


#: The roles a transcript turn may carry.  ``note`` is the panel speaking in its
#: own voice — a named failure, or the fact that no reply came — so a failed turn
#: is never rendered as Claire having said something she did not say.
TURN_ROLES: tuple[str, ...] = ("you", "claire", "note")


@dataclass(frozen=True)
class TurnRecord:
    """One turn of the panel's own conversation, as it will be rendered.

    ``text`` is scrubbed on construction: the owner types into this box, and a
    pasted key must not become a rendered one.  ``at`` is a clock string the
    panel produced, never a value from the request.
    """

    role: str
    text: str = ""
    at: str = ""

    def __post_init__(self) -> None:
        if self.role not in TURN_ROLES:
            object.__setattr__(self, "role", "note")
        object.__setattr__(self, "text", _scrub(self.text))
        object.__setattr__(self, "at", _scrub(self.at))


@dataclass(frozen=True)
class TranscriptStatus:
    """The panel's transcript, with an honest account of where it lives.

    ``durable`` is False and ``source`` says so: these turns are held in this
    process's memory only.  The persistent record is the memory layer's daily
    diary, which is a different (and summarised) thing — see
    :class:`DiaryStatus`.
    """

    turns: tuple[TurnRecord, ...] = ()
    source: str = ""
    durable: bool = False
    note: str = ""
    error: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class DiaryEntry:
    """One persisted daily conversation summary from the memory database."""

    date: str = ""
    recorded_at: str = ""
    summary: str = ""
    topics: str = ""
    source_app: str = ""


@dataclass(frozen=True)
class DiaryStatus:
    """The persisted conversation diary, or the named reason it is unreadable.

    Read-only, and read with a read-only SQLite URI: the panel never creates,
    migrates or writes the owner's memory database.
    """

    path: str = ""
    exists: bool = False
    entries: tuple[DiaryEntry, ...] = ()
    error: str | None = None
    fix: str | None = None
    note: str = ""


# --------------------------------------------------------------------------- #
# The memories browser (notes + the router + the SQLite diary)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NoteSummary:
    """One note as the list shows it: name, title, size, path."""

    name: str
    title: str
    path: str
    size_bytes: int
    #: True when the router body never mentions this note.  Reported, never
    #: hidden: an orphaned note is memory Claire will not read.
    unreferenced: bool = False


@dataclass(frozen=True)
class NoteHit:
    """One ranked note search hit: which note, which line, and that line."""

    name: str
    title: str
    line_no: int
    line: str
    score: float


@dataclass(frozen=True)
class MemoryNotesStatus:
    """The markdown half of memory: the vault, its router and its notes.

    A missing notes directory is a *named* problem with a fix, not an empty
    list: "there are no notes" and "the vault has no ``notes/`` directory yet"
    are different states and the page must not conflate them.
    """

    vault_dir: str = ""
    notes_dir: str = ""
    router_path: str = ""
    obsidian_marked: bool = False
    notes: tuple[NoteSummary, ...] = ()
    total_bytes: int = 0
    unreferenced: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    error: str | None = None
    fix: str | None = None
    note: str = ""


@dataclass(frozen=True)
class MemoryNoteView:
    """One note, opened readably: frontmatter, body and the links it makes.

    ``text`` is the **body without frontmatter**, in full: the page truncates
    its own rendering at :data:`MEMORY_BODY_CHARS`, but the edit box must carry
    the whole thing so saving can never silently drop the tail.
    """

    name: str = ""
    title: str = ""
    path: str = ""
    text: str = ""
    frontmatter: tuple[tuple[str, str], ...] = ()
    links: tuple[tuple[str, bool], ...] = ()
    problems: tuple[str, ...] = ()
    error: str | None = None
    fix: str | None = None


@dataclass(frozen=True)
class MemorySearchStatus:
    """One query across both memory stores, with each store's own misses named.

    ``error`` is only set when *nothing* could be searched.  A vault that
    cannot be read while the diary can still reports the note-side error and
    the diary hits it did find, because half an answer beats a blank page.
    """

    query: str = ""
    note_hits: tuple[NoteHit, ...] = ()
    diary_hits: tuple[DiaryEntry, ...] = ()
    note_error: str | None = None
    note_fix: str | None = None
    diary_error: str | None = None
    diary_fix: str | None = None
    note: str = ""
    #: Why the diary side has no hits, when that is *not* an error (no database
    #: yet).  A named absence, so "nothing matched" and "nothing to search" are
    #: not the same sentence.
    diary_note: str = ""

    @property
    def hit_count(self) -> int:
        return len(self.note_hits) + len(self.diary_hits)


@dataclass(frozen=True)
class ChatResult:
    """One successful chat completion, with the route that produced it."""

    text: str = ""
    provider: str = ""
    model: str = ""
    base_url: str = ""
    elapsed_ms: int = 0


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


def check_write_origin(
    request: PanelRequest,
    *,
    server_port: int | None = None,
    route: str = ROUTE_SELECT,
) -> str:
    """Refuse a write whose ``Origin``/``Host`` do not agree on loopback.

    A browser always sends ``Origin`` on a cross-origin form POST, so a page the
    owner is merely *visiting* cannot make their browser change Claire's
    configuration: that page's Origin is its own, not loopback.  A missing
    ``Origin`` is refused too — silence is not a same-origin proof.  Returns the
    validated origin (useful for a log line) or raises :class:`PanelOriginError`.

    ``route`` names the endpoint in every refusal.  It is a parameter because the
    guard now protects more than one: a refusal on ``/chat`` used to say
    "POST /select was refused", which points the owner at the wrong form.
    """
    where = f"POST {route} was refused: "
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
                where + "no Origin header, so the request is not "
                "provably from the panel page.",
                fix=PanelOriginError.default_fix,
            )
        raise PanelOriginError(
            where + "the Origin is opaque ('null') and no "
            "Sec-Fetch-Site: same-origin accompanies it, so the request is not "
            "provably from the panel page.",
            fix=PanelOriginError.default_fix,
        )
    parsed = urlsplit(str(origin))
    if parsed.scheme.lower() != "http":
        raise PanelOriginError(
            where + f"Origin scheme {parsed.scheme!r} is not "
            "'http' (this panel is plain HTTP).",
            fix=PanelOriginError.default_fix,
        )
    origin_host = (parsed.hostname or "").lower()
    if not is_loopback_host(origin_host):
        raise PanelOriginError(
            where + f"Origin host {origin_host!r} is not "
            "loopback, so this is a cross-origin request.",
            fix=PanelOriginError.default_fix,
        )
    host_header = headers.get("Host")
    if not host_header:
        raise PanelOriginError(
            where + "the request carries no Host header, so "
            "the origin cannot be checked.",
            fix=PanelOriginError.default_fix,
        )
    host_parsed = urlsplit(f"//{host_header}")
    host_host = (host_parsed.hostname or "").lower()
    if host_host != origin_host:
        raise PanelOriginError(
            where + f"Origin host {origin_host!r} does not "
            f"match Host {host_host!r}.",
            fix=PanelOriginError.default_fix,
        )
    expected_port = request.server_port if server_port is None else server_port
    if expected_port:
        for label, port in (("Origin", parsed.port), ("Host", host_parsed.port)):
            if port is not None and int(port) != int(expected_port):
                raise PanelOriginError(
                    where + f"the {label} port {port} is not "
                    f"the port this panel is running on ({expected_port}).",
                    fix=PanelOriginError.default_fix,
                )
    site = headers.get("Sec-Fetch-Site")
    if site is not None and str(site).strip().lower() not in ("same-origin", "none", ""):
        raise PanelOriginError(
            where + f"Sec-Fetch-Site is {site!r}, not "
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


class TranscriptBuffer:
    """A bounded, thread-safe ring buffer of conversation turns.

    Why this exists, and why it is not a database
    --------------------------------------------
    The codebase's verbatim conversation lives in
    :class:`claire.memory.conversation.DialogueMemory` — a plain list held in the
    **daemon process's** memory.  Nothing writes it to disk, and the panel is a
    separate process, so it cannot read it.  The only *persisted* record is the
    memory layer's daily diary (``conversation_summaries``), which is a summary,
    not a transcript.

    So the panel keeps its own turns here and says so on the page.  This is
    deliberately the honest option rather than a fabricated "conversation
    history": it holds only what was actually sent through this panel, it is lost
    on restart, and :class:`TranscriptStatus.durable` is False.
    """

    def __init__(self, limit: int = TRANSCRIPT_LIMIT) -> None:
        self._limit = max(1, int(limit))
        self._turns: deque[TurnRecord] = deque(maxlen=self._limit)
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def record(self, role: str, text: str, at: str = "") -> TurnRecord:
        """Append one turn and return it as recorded."""
        turn = TurnRecord(role=role, text=text, at=at)
        with self._lock:
            self._turns.append(turn)
        return turn

    def turns(self) -> tuple[TurnRecord, ...]:
        """The turns oldest-first.  The renderer reverses them."""
        with self._lock:
            return tuple(self._turns)

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    def __len__(self) -> int:  # pragma: no cover - convenience
        with self._lock:
            return len(self._turns)


def default_chat_call(
    message: str,
    *,
    timeout_sec: float = CHAT_TIMEOUT_SECONDS,
    settings: Any | None = None,
) -> ChatResult:
    """Send one message through **the project's one LLM stack**.

    This is the authoritative path and nothing else: ``claire.config`` resolves
    the selected provider, model, endpoint and credential, and
    :func:`claire.llm.get_llm_backend` returns the very backend object the daemon
    and the reply engine use.  It deliberately does **not** open an HTTP
    connection of its own — a second client calling the provider directly is the
    duplicated-stack mistake this project already made once with transcription.

    Returns a :class:`ChatResult`, or raises :class:`PanelLLMError` when the
    backend reports no reply.  ``settings`` is injectable so a test never reaches
    the network and never depends on the owner's config.
    """
    if settings is None:
        from claire.config import load_settings

        settings = load_settings()
    provider = str(getattr(settings, "llm_provider", "") or "")
    model = str(getattr(settings, "llm_chat_model", "") or "")
    base_url = str(getattr(settings, "llm_base_url", "") or getattr(settings, "ollama_base_url", "") or "")
    started = time.monotonic()
    try:
        from claire.llm import get_llm_backend

        backend = get_llm_backend(settings)
    except Exception as exc:  # noqa: BLE001 - a broken backend is a finding
        raise PanelLLMError(provider, model, base_url, f"{type(exc).__name__}: {_scrub(exc)}") from exc
    try:
        reply = backend.direct(
            model,
            PANEL_CHAT_SYSTEM_PROMPT,
            message,
            timeout_sec=timeout_sec,
            thinking=False,
            max_tokens=None,
        )
    except Exception as exc:  # noqa: BLE001 - report it, never swallow it
        raise PanelLLMError(provider, model, base_url, f"{type(exc).__name__}: {_scrub(exc)}") from exc
    if reply is None or not str(reply).strip():
        raise PanelLLMError(provider, model, base_url)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return ChatResult(
        text=str(reply),
        provider=provider,
        model=model,
        base_url=base_url,
        elapsed_ms=elapsed_ms,
    )


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

    def transcript(self) -> TranscriptStatus:
        """The panel's own turns.  Empty is a valid answer, not an error."""
        return TranscriptStatus(
            turns=(),
            source="this panel's process memory",
            durable=False,
            note=(
                "No turns yet. Claire's live voice conversation is held in memory "
                "inside the daemon process, which a separate process cannot read, "
                "and nothing writes a verbatim transcript to disk."
            ),
        )

    def diary(self) -> DiaryStatus:
        """The persisted daily summaries, or a named reason there are none."""
        return DiaryStatus(
            note=(
                "This state does not read a memory database, so no persisted "
                "conversation diary is available."
            )
        )

    # -- memories --------------------------------------------------------- #
    #
    # A state that cannot answer these says so in a note, exactly as ``diary``
    # does: the memories page must distinguish "no vault here" from "no notes in
    # the vault", and a partial fake must not make the page claim either.

    def memory_notes(self) -> MemoryNotesStatus:
        """The markdown vault's router and notes, or a named reason there are none."""
        return MemoryNotesStatus(
            note=(
                "This state does not read a memory vault, so the notes are not "
                "available here."
            )
        )

    def memory_note(self, name: str) -> MemoryNoteView:
        """One note by name, or a named reason it cannot be opened."""
        raise PanelStateError("memory_note")

    def memory_search(self, query: str) -> MemorySearchStatus:
        """One query across the notes and the persisted diary."""
        return MemorySearchStatus(
            query=str(query or ""),
            note=(
                "This state does not read a memory vault or database, so there "
                "is nothing to search."
            ),
        )

    def save_note(
        self,
        name: str,
        body: str,
        *,
        title: str = "",
        create: bool = True,
    ) -> NoteSummary:
        """Write one note through the same code path Claire reads. Raises to refuse."""
        raise PanelStateError("save_note")

    def delete_note(self, name: str) -> NoteSummary:
        """Delete one note. Raises to refuse, and never reports a no-op as success."""
        raise PanelStateError("delete_note")

    # -- chat ------------------------------------------------------------- #

    def chat_reply(self, message: str, *, timeout_sec: float = CHAT_TIMEOUT_SECONDS) -> ChatResult:
        """Answer one typed message through the one LLM stack. Raises to refuse."""
        raise PanelStateError("chat_reply")

    # -- selector ---------------------------------------------------------- #

    def available_providers(self) -> Sequence[str]:
        return self.PROVIDERS

    def available_models(self) -> Sequence[str]:
        return ()

    def select_model(self, provider: str, model: str) -> WriteResult:
        """Persist a provider/model selection. Raises to refuse."""
        raise PanelStateError("select_model")


# --------------------------------------------------------------------------- #
# The read-only SQLite memory seam
# --------------------------------------------------------------------------- #
#
# These helpers are the panel's *only* access to the persisted memory database,
# and they are shared by the diary and the memories search so both read it the
# same way: ``mode=ro`` (a page view must never create or migrate the owner's
# memory) and the memory layer's own FTS index for keyword search.  No
# embeddings, no LLM, no write, and no second keyword algorithm.

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _open_memory_db(path: Path) -> "sqlite3.Connection":
    """Open *path* read-only. Raises ``sqlite3.Error``; never creates the file."""
    uri = "file:" + quote(str(path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _diary_entries(rows: Any) -> tuple[DiaryEntry, ...]:
    """Map ``conversation_summaries`` rows to :class:`DiaryEntry`."""
    return tuple(
        DiaryEntry(
            date=str(row["date_utc"] or ""),
            recorded_at=str(row["ts_utc"] or ""),
            summary=str(row["summary"] or ""),
            topics=str(row["topics"] or ""),
            source_app=str(row["source_app"] or ""),
        )
        for row in rows
    )


def _fts_summary_rows(
    connection: "sqlite3.Connection", query: str, limit: int
) -> Any | None:
    """Search the summaries FTS index; ``None`` means "fall back to LIKE".

    ``None`` is returned for the two *expected* absences — no FTS table, or a
    query FTS5 will not accept — while a genuine database error propagates so
    the caller can name it.  The MATCH expression is built from Unicode word
    tokens, quoted per token, so punctuation in a query cannot become FTS
    syntax (and no language-specific word list is involved anywhere).
    """
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return None
    match = " OR ".join(f'"{token}"' for token in tokens)
    try:
        return connection.execute(
            "SELECT s.date_utc, s.ts_utc, s.summary, s.topics, s.source_app "
            "FROM summaries_fts JOIN conversation_summaries s "
            "ON s.id = summaries_fts.rowid "
            "WHERE summaries_fts MATCH ? ORDER BY bm25(summaries_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return None


def _like_summary_rows(
    connection: "sqlite3.Connection", query: str, limit: int
) -> Any:
    """Substring fallback over summaries and topics, with LIKE wildcards escaped."""
    escaped = (
        str(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    like = f"%{escaped}%"
    return connection.execute(
        "SELECT date_utc, ts_utc, summary, topics, source_app "
        "FROM conversation_summaries "
        "WHERE summary LIKE ? ESCAPE '\\' OR topics LIKE ? ESCAPE '\\' "
        "ORDER BY date_utc DESC, id DESC LIMIT ?",
        (like, like, limit),
    ).fetchall()


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
        diary_path: "str | os.PathLike[str] | None" = None,
        vault_dir: "str | os.PathLike[str] | None" = None,
        transcript: TranscriptBuffer | None = None,
        chat_call: Callable[..., ChatResult] | None = None,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], Any] | None = None,
        self_check_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._config_path = config_path
        self._key_store = key_store
        self._router_path = router_path
        self._schedule_path = schedule_path
        self._action_log_path = action_log_path
        self._diary_path = diary_path
        #: Explicit vault override.  ``None`` means "resolve it exactly the way
        #: Claire does" — environment first, then the config key — so the panel
        #: and the daemon cannot end up looking at different vaults.
        self._vault_dir = vault_dir
        self._environ = environ
        self._now = now or _date.today
        self._self_check_factory = self_check_factory
        self._self_check_cache: tuple[float, SelfCheckStatus] | None = None
        # The panel's own conversation, held in this process only.  A caller can
        # inject one (tests) or share one; by default it is private to this state.
        self.transcript_buffer = transcript if transcript is not None else TranscriptBuffer()
        #: The one LLM seam.  Defaults to the project's real stack; injectable so
        #: no test ever reaches the network.
        self._chat_call = chat_call
        self._chat_lock = threading.Lock()

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

    def _router_file(self) -> Path | None:
        """The router file to read: an explicit path, else the vault's, else None.

        ``None`` hands resolution to ``claire.memory.router`` — the environment
        then config order Claire uses — so an injected vault keeps the router
        card and the memories browser looking at one file.
        """
        if self._router_path is not None:
            return Path(self._router_path)
        if self._vault_dir is not None:
            from claire.memory.notes import resolve_router_path

            return resolve_router_path(self._vault_dir)
        return None

    def router_status(self) -> RouterStatus:
        from claire.memory import notes as notes_module
        from claire.memory import router as router_module

        explicit_router = self._router_file()
        try:
            router = router_module.load_router(explicit_router, create=False)
        except (router_module.RouterError, notes_module.NotesError) as exc:
            path = str(getattr(exc, "path", "") or explicit_router or "")
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

    def diary_path(self) -> Path:
        """Where ``claire.memory`` keeps its database, per the same config.

        The path is resolved the way ``claire.config`` resolves it (the
        ``db_path`` field, ``~`` expanded, defaulting to
        ``~/.local/share/claire/claire.db``) rather than being invented here.
        """
        if self._diary_path is not None:
            return Path(self._diary_path).expanduser()
        configured = str(self._config().get("db_path", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        from claire.config import get_default_config

        return Path(str(get_default_config()["db_path"])).expanduser()

    def diary(self) -> DiaryStatus:
        """Read the persisted daily summaries — read-only, and never created.

        ``mode=ro`` matters: opening the owner's memory database for writing
        would create it if it were missing and would let a page view migrate a
        schema.  The panel only ever reads.
        """
        path = self.diary_path()
        if not path.exists():
            return DiaryStatus(
                path=str(path),
                exists=False,
                note=(
                    "No memory database yet, so the memory layer has persisted no "
                    "conversation summaries. It is created the first time Claire "
                    "writes to memory; this panel never creates it."
                ),
            )
        try:
            connection = _open_memory_db(path)
        except sqlite3.Error as exc:
            return DiaryStatus(
                path=str(path),
                exists=True,
                error=f"{type(exc).__name__}: {_scrub(exc)}",
                fix=f"Check that {path} is a readable SQLite database.",
            )
        try:
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT date_utc, ts_utc, summary, topics, source_app "
                    "FROM conversation_summaries "
                    "ORDER BY date_utc DESC, id DESC LIMIT ?",
                    (DIARY_LIMIT,),
                ).fetchall()
            except sqlite3.Error as exc:
                return DiaryStatus(
                    path=str(path),
                    exists=True,
                    error=f"{type(exc).__name__}: {_scrub(exc)}",
                    fix=(
                        "The memory database has no readable "
                        "conversation_summaries table; run the daemon once so the "
                        "memory layer can create it."
                    ),
                )
        finally:
            connection.close()
        return DiaryStatus(path=str(path), exists=True, entries=_diary_entries(rows))

    # -- memories ---------------------------------------------------------- #

    def vault_path(self) -> Path:
        """The markdown vault the memories browser reads and writes.

        An injected *vault_dir* wins; otherwise this is
        ``claire.memory.notes.resolve_vault_dir()`` — the environment variable
        and config key in the same order the daemon uses.  Nothing here invents
        a second resolution rule, so the panel and Claire look at one vault.
        """
        from claire.memory import notes as notes_module

        if self._vault_dir is not None:
            return notes_module.resolve_vault_dir(self._vault_dir)
        return notes_module.resolve_vault_dir()

    def memory_notes(self) -> MemoryNotesStatus:
        """List the router and every note, and name what is wrong with the vault.

        Uses :func:`claire.memory.notes.list_notes` and
        :func:`claire.memory.notes.unreferenced_notes` — the same readers Claire
        uses — so the page cannot show a different vault from the one she reads.
        """
        from claire.memory import notes as notes_module
        from claire.memory import obsidian as obsidian_module

        vault = self.vault_path()
        layout = obsidian_module.describe_vault(vault)
        try:
            infos = notes_module.list_notes(vault_dir=vault)
        except notes_module.NotesDirectoryError as exc:
            return MemoryNotesStatus(
                vault_dir=str(layout.vault_dir),
                notes_dir=str(layout.notes_dir),
                router_path=str(layout.router_path),
                obsidian_marked=layout.obsidian_dir_exists,
                error=f"{type(exc).__name__}: {exc}",
                fix=(
                    "Create the notes directory (and the vault around it) with "
                    "`python -c \"from claire.memory.obsidian import ensure_vault; "
                    "ensure_vault()\"` — or add the first note from this page, "
                    "which creates it on demand."
                ),
            )

        router_text = ""
        router_error = ""
        router_fix = ""
        try:
            router_text = layout.router_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            router_error = f"{type(exc).__name__}: {exc}"
            router_fix = (
                f"CLAIRE.md is missing at {layout.router_path}, so no note can be "
                "called referenced. Create it with "
                "`python -c \"from claire.memory.router import ensure_router; "
                "ensure_router()\"`."
            )

        orphaned: tuple[str, ...] = ()
        if not router_error:
            try:
                orphaned = tuple(
                    info.name
                    for info in notes_module.unreferenced_notes(
                        router_text, notes_dir=layout.notes_dir, vault_dir=vault
                    )
                )
            except notes_module.NotesError:  # pragma: no cover - listing succeeded above
                orphaned = ()

        unreferenced_names = {name.casefold() for name in orphaned}
        summaries = tuple(
            NoteSummary(
                name=info.name,
                title=info.title,
                path=str(info.path),
                size_bytes=int(info.size_bytes),
                unreferenced=info.name.casefold() in unreferenced_names,
            )
            for info in infos[:MEMORY_NOTE_LIMIT]
        )
        note = ""
        if len(infos) > MEMORY_NOTE_LIMIT:
            note = (
                f"{len(infos)} notes in the vault; the page lists the first "
                f"{MEMORY_NOTE_LIMIT} by name. Use the search box for the rest."
            )
        if orphaned:
            orphan_line = (
                f"{len(orphaned)} note(s) are never mentioned by CLAIRE.md, so "
                "Claire will not read them: " + ", ".join(orphaned[:12]) + "."
            )
            note = (note + " " + orphan_line).strip()

        return MemoryNotesStatus(
            vault_dir=str(layout.vault_dir),
            notes_dir=str(layout.notes_dir),
            router_path=str(layout.router_path),
            obsidian_marked=layout.obsidian_dir_exists,
            notes=summaries,
            total_bytes=sum(int(info.size_bytes) for info in infos),
            unreferenced=orphaned,
            error=router_error or "",
            fix=router_fix or "",
            note=note,
        )

    def memory_note(self, name: str) -> MemoryNoteView:
        """Open one note by name, with its frontmatter and resolvable links."""
        from claire.memory import notes as notes_module
        from claire.memory import obsidian as obsidian_module

        vault = self.vault_path()
        try:
            note = notes_module.read_note(name, vault_dir=vault)
        except notes_module.NotesError as exc:
            # The name itself may be the problem, so it is never normalised
            # again here: re-raising the note layer's own refusal is the answer.
            return MemoryNoteView(
                name=str(name or ""),
                error=f"{type(exc).__name__}: {exc}",
                fix=(
                    "Pick a note from the list, or create it from the "
                    "'New note' form on this page."
                ),
            )

        known = tuple(info.name for info in notes_module.list_notes(vault_dir=vault))
        links = tuple(
            (target, obsidian_module.resolve_wiki_link(target, known) is not None)
            for target in obsidian_module.extract_wiki_links(note.text)
        )
        problems = tuple(obsidian_module.validate_note_document(note.text))
        return MemoryNoteView(
            name=note.name,
            title=note.title,
            path=str(note.path),
            # The body, not the whole file: the frontmatter is shown as its own
            # table and re-rendered by ``update_note`` from the stored keys, so
            # editing the Title field actually changes the title.
            text=note.body,
            frontmatter=tuple(
                (str(key), str(value)) for key, value in (note.frontmatter or {}).items()
            ),
            links=links,
            problems=problems,
            fix=(
                ""
                if not problems
                else "Rewrite the frontmatter as flat 'key: value' pairs between "
                "two '---' lines so Obsidian shows a Properties table."
            ),
        )

    def memory_search(self, query: str) -> MemorySearchStatus:
        """Search the notes and the persisted diary, reusing each store's own index.

        Notes go through :func:`claire.memory.notes.search_notes` — the one note
        search that exists.  The diary is the SQLite FTS table the memory layer
        already maintains (``summaries_fts``), queried read-only; there is no
        second keyword algorithm here, and no embeddings or LLM are loaded to
        answer a page request.
        """
        from claire.memory import notes as notes_module

        text = str(query or "").strip()
        if not text:
            raise PanelBadRequest(
                "the memory search carried no query.",
                fix="Type a word into the search box and submit it.",
            )
        if len(text) > 200:
            raise PanelBadRequest(
                f"the search query is {len(text)} characters; the box forwards at "
                f"most 200.",
                fix="Shorten the query to 200 characters or fewer.",
            )

        vault = self.vault_path()
        note_hits: tuple[NoteHit, ...] = ()
        note_error = ""
        note_fix = ""
        try:
            note_hits = tuple(
                NoteHit(
                    name=match.name,
                    title=match.title,
                    line_no=int(match.line_no),
                    line=str(match.line),
                    score=float(match.score),
                )
                for match in notes_module.search_notes(
                    text, vault_dir=vault, limit=MEMORY_SEARCH_LIMIT
                )
            )
        except notes_module.NotesDirectoryError as exc:
            note_error = f"{type(exc).__name__}: {exc}"
            note_fix = (
                "The vault has no notes directory yet, so only the diary below "
                "was searched. Add a note from this page to create it."
            )

        diary_hits, diary_error, diary_fix = self._search_diary(text)
        diary_note = ""
        if not diary_hits and not diary_error:
            diary_note = (
                "No persisted summary matched this query. "
                + ("The memory database does not exist yet, so there is nothing "
                   "to search." if not self.diary_path().exists() else
                   "Try a word from a summary, or browse the diary below.")
            )
        return MemorySearchStatus(
            query=text,
            note_hits=note_hits,
            diary_hits=diary_hits,
            note_error=note_error,
            note_fix=note_fix,
            diary_error=diary_error,
            diary_fix=diary_fix,
            diary_note=diary_note,
        )

    def _search_diary(self, query: str) -> tuple[tuple[DiaryEntry, ...], str, str]:
        """Search the persisted summaries read-only; return hits, error and fix."""
        path = self.diary_path()
        if not path.exists():
            return (
                (),
                "",
                "",
            )
        try:
            connection = _open_memory_db(path)
        except sqlite3.Error as exc:
            return ((), f"{type(exc).__name__}: {_scrub(exc)}", f"Check that {path} is readable.")
        try:
            connection.row_factory = sqlite3.Row
            rows = _fts_summary_rows(connection, query, MEMORY_SEARCH_LIMIT)
            if rows is None:
                rows = _like_summary_rows(connection, query, MEMORY_SEARCH_LIMIT)
        except sqlite3.Error as exc:
            return (
                (),
                f"{type(exc).__name__}: {_scrub(exc)}",
                "The memory database has no readable conversation_summaries table; "
                "run the daemon once so the memory layer can create it.",
            )
        finally:
            connection.close()
        return (_diary_entries(rows), "", "")

    def save_note(
        self,
        name: str,
        body: str,
        *,
        title: str = "",
        create: bool = True,
    ) -> NoteSummary:
        """Write one note through :mod:`claire.memory.notes`, then re-read it.

        The panel never formats frontmatter itself: ``create_note`` and
        ``update_note`` do, so what the browser writes is exactly what Claire's
        parser reads back.  The file is re-read after the write so the page shows
        the stored document rather than the submitted one.
        """
        from claire.memory import notes as notes_module
        from claire.memory import obsidian as obsidian_module

        vault = self.vault_path()
        if create:
            obsidian_module.ensure_vault(vault)
            info = notes_module.create_note(
                name,
                body,
                title=title or None,
                vault_dir=vault,
            )
        else:
            info = notes_module.update_note(
                name,
                body,
                title=title or None,
                vault_dir=vault,
            )
        return NoteSummary(
            name=info.name,
            title=info.title,
            path=str(info.path),
            size_bytes=int(info.size_bytes),
        )

    def delete_note(self, name: str) -> NoteSummary:
        """Delete one note through :mod:`claire.memory.notes`."""
        from claire.memory import notes as notes_module

        info = notes_module.delete_note(name, vault_dir=self.vault_path())
        return NoteSummary(
            name=info.name,
            title=info.title,
            path=str(info.path),
            size_bytes=int(info.size_bytes),
        )

    # -- conversation ------------------------------------------------------ #

    def transcript(self) -> TranscriptStatus:
        turns = self.transcript_buffer.turns()
        return TranscriptStatus(
            turns=turns,
            source="this panel's process memory",
            durable=False,
            note=(
                "Turns sent through this page, held in the panel process only — lost "
                "when the panel restarts. Claire's live voice conversation lives in "
                "memory inside the daemon process, which the panel cannot read, and "
                "nothing writes a verbatim transcript to disk. The daily summaries "
                "below are the persisted record, and they are summaries."
            ),
        )

    def chat_reply(self, message: str, *, timeout_sec: float = CHAT_TIMEOUT_SECONDS) -> ChatResult:
        """Answer through the one LLM stack, recording both turns as it goes.

        The owner's turn is recorded *before* the call, so a failure still shows
        what was asked, and a ``note`` turn carries the named failure — a failed
        turn is never rendered as Claire having replied.
        """
        stamp = _datetime.now().strftime("%H:%M:%S")
        self.transcript_buffer.record("you", message, stamp)
        call = self._chat_call or default_chat_call
        # One chat call at a time: this is a single conversational thread, and the
        # ring buffer's ordering must match the order the model saw the turns.
        with self._chat_lock:
            try:
                result = call(message, timeout_sec=timeout_sec)
            except PanelError as exc:
                self.transcript_buffer.record(
                    "note", f"no reply — {exc.code}: {exc.message}", stamp
                )
                raise
            except Exception as exc:  # noqa: BLE001 - a backend failure is a finding
                error = PanelLLMError("", "", "", f"{type(exc).__name__}: {_scrub(exc)}")
                self.transcript_buffer.record(
                    "note", f"no reply — {error.code}: {error.message}", stamp
                )
                raise error from exc
        self.transcript_buffer.record("claire", result.text, stamp)
        return result

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
/* ---- tokens ------------------------------------------------------------ */
:root {
  color-scheme: dark light;
  --bg: #101317;
  --bg-soft: #161a20;
  --card: #1a1e25;
  --card-2: #1f242c;
  --ink: #e8ebef;
  --ink-soft: #a7b0bb;
  --ink-faint: #828b97;
  --line: #2a3039;
  --line-soft: #232931;
  --accent: #6ea8fe;
  --accent-ink: #0b1220;
  --ok: #56d68a;
  --warn: #f0b429;
  --bad: #ff7b72;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 1px 2px rgba(0, 0, 0, .35), 0 8px 24px -18px rgba(0, 0, 0, .8);
  --sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f6f7f9;
    --bg-soft: #eef0f4;
    --card: #ffffff;
    --card-2: #f4f6f9;
    --ink: #171b21;
    --ink-soft: #4d5763;
    --ink-faint: #6b7480;
    --line: #d9dee6;
    --line-soft: #e6eaf0;
    --accent: #1d5fd0;
    --accent-ink: #ffffff;
    --ok: #16794a;
    --warn: #8a5b00;
    --bad: #b4231a;
    --shadow: 0 1px 2px rgba(16, 24, 40, .06), 0 10px 24px -20px rgba(16, 24, 40, .45);
  }
}

/* ---- base -------------------------------------------------------------- */
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: 0 1rem 2.5rem;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.55 var(--sans);
  overflow-wrap: break-word;
}
h1 { font-size: 1.15rem; margin: 0; letter-spacing: -.01em; font-weight: 650; }
h2 { font-size: .95rem; margin: 0; font-weight: 650; letter-spacing: .005em; }
h3 { font-size: .78rem; margin: 0 0 .35rem; font-weight: 650; color: var(--ink-faint);
     text-transform: uppercase; letter-spacing: .06em; }
p { margin: .35rem 0; }
a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 2px; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
code, .mono { font-family: var(--mono); font-size: .86em; }
.muted { color: var(--ink-soft); }
.faint { color: var(--ink-faint); }
.ok { color: var(--ok); }
.warn { color: var(--warn); }
.bad { color: var(--bad); }
.num { font-variant-numeric: tabular-nums; }

/* ---- masthead ---------------------------------------------------------- */
.top { border-bottom: 1px solid var(--line); background: var(--bg-soft);
       margin: 0 -1rem 1rem; padding: .85rem 1rem .9rem; }
.top-inner { max-width: 1180px; margin: 0 auto; }
.brand { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
.brand .dot { width: .55rem; height: .55rem; border-radius: 50%; background: var(--ok);
              box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 22%, transparent); }
.tag { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
       color: var(--ink-faint); border: 1px solid var(--line); border-radius: 999px;
       padding: .1rem .45rem; }
.sub { color: var(--ink-soft); font-size: .82rem; margin: .35rem 0 0; }

/* ---- layout ------------------------------------------------------------ */
main { max-width: 1180px; margin: 0 auto; display: grid; gap: 1rem;
       grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
       align-items: start; }
main > section.wide { grid-column: 1 / -1; }
section.card { background: var(--card); border: 1px solid var(--line);
               border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
.card-head { display: flex; align-items: baseline; justify-content: space-between;
             gap: .5rem; flex-wrap: wrap; padding: .7rem .95rem;
             border-bottom: 1px solid var(--line-soft); background: var(--card-2); }
.card-body { padding: .85rem .95rem 1rem; overflow-x: auto; }
.card-body > :first-child { margin-top: 0; }
.card-body > :last-child { margin-bottom: 0; }

/* ---- tables ------------------------------------------------------------ */
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .38rem .5rem; border-bottom: 1px solid var(--line-soft);
         vertical-align: top; }
thead th { color: var(--ink-faint); font-weight: 600; font-size: .76rem;
           text-transform: uppercase; letter-spacing: .05em; }
tbody tr:last-child th, tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }

/* ---- bits -------------------------------------------------------------- */
.pill { display: inline-block; padding: .08rem .5rem; border-radius: 999px;
        border: 1px solid var(--line); color: var(--ink-soft); font-size: .74rem;
        letter-spacing: .02em; white-space: nowrap; }
.pill.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, var(--line)); }
.pill.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--line)); }
.pill.bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono);
      font-size: .84rem; background: var(--bg); border: 1px solid var(--line);
      border-left: 3px solid var(--bad); border-radius: var(--radius-sm);
      padding: .6rem .7rem; margin: .5rem 0 0; color: var(--ink); }
.fix { background: var(--card-2); border: 1px solid var(--line);
       border-left: 3px solid var(--accent); border-radius: var(--radius-sm);
       padding: .55rem .7rem; margin: .5rem 0 0; font-size: .88rem; }
.bar { height: 8px; background: var(--bg); border: 1px solid var(--line);
       border-radius: 999px; overflow: hidden; margin: .45rem 0 .3rem; }
.bar > i { display: block; height: 100%; background: var(--ok); }
.bar.over > i { background: var(--bad); }
.empty { color: var(--ink-faint); font-style: italic; margin: .3rem 0 0; }

/* ---- forms ------------------------------------------------------------- */
form { display: flex; flex-wrap: wrap; gap: .7rem; align-items: flex-end;
       margin: .2rem 0 .6rem; }
label.field { display: flex; flex-direction: column; gap: .25rem; color: var(--ink-soft);
              font-size: .8rem; font-weight: 550; min-width: 0; flex: 1 1 12rem; }
select, input, textarea {
  background: var(--bg); color: var(--ink); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: .42rem .55rem; font: inherit; width: 100%;
  min-width: 0;
}
select:focus, input:focus, textarea:focus { border-color: var(--accent); }
textarea { resize: vertical; min-height: 4.4rem; line-height: 1.5; }
button { background: var(--accent); color: var(--accent-ink); border: 0;
         border-radius: var(--radius-sm); padding: .48rem .95rem; font: inherit;
         font-weight: 600; cursor: pointer; }
button:hover { filter: brightness(1.07); }
button:active { transform: translateY(1px); }
form.chat { display: grid; gap: .55rem; margin: 0; }
form.chat .row { display: flex; align-items: center; justify-content: space-between;
                 gap: .5rem; flex-wrap: wrap; }
.hint { color: var(--ink-faint); font-size: .78rem; }

/* ---- conversation ------------------------------------------------------ */
.turns { list-style: none; margin: 0; padding: 0; display: flex;
         flex-direction: column; gap: .6rem; }
.turn { border: 1px solid var(--line); border-radius: var(--radius-sm);
        padding: .5rem .65rem; background: var(--card-2); max-width: 62ch; }
.turn header { display: flex; align-items: baseline; gap: .5rem; margin-bottom: .2rem; }
.turn .who { font-size: .74rem; font-weight: 700; letter-spacing: .05em;
             text-transform: uppercase; color: var(--ink-faint); }
.turn .at { font-size: .72rem; color: var(--ink-faint); font-variant-numeric: tabular-nums; }
.turn p { margin: 0; white-space: pre-wrap; }
.turn.you { border-left: 3px solid var(--accent); align-self: flex-end;
            background: color-mix(in srgb, var(--accent) 8%, var(--card-2)); }
.turn.claire { border-left: 3px solid var(--ok); }
.turn.note { border-left: 3px solid var(--warn); background: transparent; }
.turn.note p { color: var(--warn); }
.diary { list-style: none; margin: .8rem 0 0; padding: .7rem 0 0;
         border-top: 1px dashed var(--line); display: grid; gap: .6rem; }
.diary li { border-left: 3px solid var(--line); padding-left: .6rem; }
.diary .meta { font-size: .76rem; color: var(--ink-faint); display: flex; gap: .5rem;
               flex-wrap: wrap; }
.diary p { margin: .15rem 0 0; }

/* ---- memories browser -------------------------------------------------- */
form.memory { margin: .9rem 0 0; }
form.memory .field { display: block; font-size: .82rem; color: var(--ink-soft);
                     margin: .55rem 0 .2rem; }
form.memory input[type="text"], form.memory input:not([type]),
form.memory input[name="name"], form.memory input[name="title"],
form.memory input[name="q"] {
  width: 100%; box-sizing: border-box; padding: .5rem .6rem;
  border: 1px solid var(--line); border-radius: 8px;
  background: var(--card-2); color: var(--ink); font: inherit; }
form.memory textarea { width: 100%; box-sizing: border-box; padding: .55rem .6rem;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card-2);
  color: var(--ink); font: inherit; resize: vertical; }
form.memory .row { display: flex; align-items: center; gap: .7rem;
                   flex-wrap: wrap; margin-top: .7rem; }
form.memory.danger { border-top: 1px dashed var(--line); padding-top: .7rem;
                     margin-top: 1rem; }
form.memory.danger button { background: transparent; border: 1px solid var(--bad);
                            color: var(--bad); }
pre.note-body { max-height: 30rem; overflow: auto; white-space: pre-wrap;
                word-break: break-word; }

/* ---- footer ------------------------------------------------------------ */
footer { max-width: 1180px; margin: 1.25rem auto 0; padding-top: .8rem;
         border-top: 1px solid var(--line); color: var(--ink-faint); font-size: .82rem; }

/* ---- narrow ------------------------------------------------------------ */
@media (max-width: 560px) {
  body { padding: 0 .7rem 2rem; font-size: 14.5px; }
  main { grid-template-columns: minmax(0, 1fr); gap: .8rem; }
  .top { margin: 0 -.7rem 1rem; padding: .7rem; }
  th, td { padding: .34rem .35rem; }
  table { font-size: .85rem; }
  .turn { max-width: 100%; }
  form.chat button { width: 100%; }
  button { padding: .62rem .95rem; }
}
@media (prefers-reduced-motion: no-preference) {
  a, button { transition: filter .12s ease, background-color .12s ease; }
}
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


def _section(
    title: str,
    inner: str,
    *,
    anchor: str = "",
    badge: str = "",
    wide: bool = False,
) -> str:
    """One card: a heading, an optional status pill, and its body.

    ``wide`` makes the card span the whole grid; ``anchor`` gives it a fragment
    id so a form POST can redirect straight to it.  The layout grid keys off the
    ``card`` class, so that name is load-bearing.
    """
    attrs = ' class="card wide"' if wide else ' class="card"'
    if anchor:
        attrs += f' id="{_esc(anchor)}"'
    pill = f'<span class="pill">{_esc(badge)}</span>' if badge else ""
    return (
        f"<section{attrs}>\n"
        f'<div class="card-head"><h2>{_esc(title)}</h2>{pill}</div>\n'
        f'<div class="card-body">\n{inner}\n</div>\n'
        "</section>"
    )


def _masthead(bind_host: str, bind_port: int) -> str:
    """The one-line banner: what this is, where it lives, and what it will not do."""
    return (
        '<header class="top">\n<div class="top-inner">\n'
        '<div class="brand">\n<span class="dot" aria-hidden="true"></span>\n'
        "<h1>Claire</h1>\n"
        '<span class="tag">local panel</span>\n'
        "</div>\n"
        '<p class="sub">Bound to <span class="mono">'
        f"{_esc(bind_host)}:{_esc(bind_port)}</span> &middot; loopback only &middot; "
        "plain HTTP, no login &middot; never shows a key value.</p>\n"
        "</div>\n</header>"
    )


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
        f'<form method="post" action="{ROUTE_SELECT}">\n'
        '<label class="field" for="provider">Provider'
        f'<select id="provider" name="provider">'
        f"{options(providers, info.provider)}</select></label>\n"
        '<label class="field" for="model">Model'
        '<input id="model" name="model" list="known-models" '
        f'value="{_esc(info.model)}" required></label>\n'
        "<datalist id=\"known-models\">"
        + "".join(f'<option value="{_esc(m)}"></option>' for m in models)
        + "</datalist>\n"
        '<button type="submit">Save selection</button>\n'
        "</form>\n"
        '<p class="hint">The selector writes <span class="mono">llm_provider</span> and the '
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


def _turn_html(turn: TurnRecord) -> str:
    label = {"you": "You", "claire": "Claire", "note": "Panel"}.get(turn.role, "Panel")
    stamp = f'<span class="at mono">{_esc(turn.at)}</span>' if turn.at else ""
    return (
        f'<li class="turn {_esc(turn.role)}">\n'
        f'<header><span class="who">{_esc(label)}</span>{stamp}</header>\n'
        f"<p>{_esc(turn.text)}</p>\n"
        "</li>"
    )


def _diary_html(status: DiaryStatus) -> str:
    """The persisted daily summaries, rendered as the memory layer's own record."""
    if status.error:
        return _problem(status.error, status.fix)
    if not status.entries:
        note = status.note or (
            "The memory database holds no conversation summaries yet."
        )
        return f'<p class="empty">{_esc(note)}</p>'
    items = []
    for entry in status.entries:
        topics = (
            f'<span>topics: {_esc(entry.topics)}</span>' if entry.topics else ""
        )
        source = f"<span>{_esc(entry.source_app)}</span>" if entry.source_app else ""
        items.append(
            "<li>\n"
            f'<div class="meta"><span class="mono">{_esc(entry.date)}</span>'
            f"{source}{topics}</div>\n"
            f"<p>{_esc(entry.summary)}</p>\n"
            "</li>"
        )
    return (
        f'<ul class="diary">{"".join(items)}</ul>'
        f'<p class="hint mono">{_esc(status.path)}</p>'
    )


def _conversation_section(state: PanelState) -> str:
    seen: list[TranscriptStatus] = []

    def build() -> str:
        status = state.transcript()
        seen.append(status)
        turns = tuple(status.turns)
        parts: list[str] = []
        if turns:
            # Newest visible: the reply the owner just asked for is at the top.
            parts.append(
                f'<ul class="turns">{"".join(_turn_html(t) for t in reversed(turns))}</ul>'
            )
        else:
            parts.append('<p class="empty">No turns yet.</p>')
        if status.note:
            # Always, not only when the transcript is empty: the disclaimer that
            # these turns are not a durable record matters *most* once the page is
            # showing turns, because that is when it starts to look like an archive.
            parts.append(f'<p class="hint">{_esc(status.note)}</p>')
        if status.error:
            parts.append(_problem(status.error, status.fix))
        return "\n".join(parts)

    inner = _guard("transcript", build)
    # The badge reports what the source claimed rather than asserting one and
    # hoping. A source that says it is durable is badged as durable; a transcript
    # that could not be read is badged as the weaker of the two.
    durable = seen[0].durable if seen else False
    badge = "on disk" if durable else "in memory"
    inner += (
        "\n<h3>Persisted diary</h3>\n"
        '<p class="hint">The memory layer writes one summarised entry per day. '
        "These are summaries, not a verbatim transcript.</p>\n"
        + _guard("conversation diary", lambda: _diary_html(state.diary()))
    )
    return _section("Conversation", inner, anchor="conversation", badge=badge, wide=True)


def _chat_section(state: PanelState) -> str:
    """The chat form. It posts to the same guard the selector does."""
    inner = (
        f'<form class="chat" method="post" action="{ROUTE_CHAT}">\n'
        '<label class="field" for="message">Message to Claire\'s model</label>\n'
        f'<textarea id="message" name="message" rows="3" maxlength="{MAX_CHAT_CHARS}" '
        'placeholder="Type here and press Send - the reply appears above." '
        "required></textarea>\n"
        '<div class="row">\n'
        '<span class="hint">Answered through the same provider, model and backend '
        "the daemon uses. Nothing is spoken, and nothing is stored on disk. "
        "Type no secrets here: this text is sent to the model and shown back "
        "on this page.</span>\n"
        '<button type="submit">Send</button>\n'
        "</div>\n"
        "</form>"
    )
    # Wide, like the transcript it feeds: the box is the page's main verb, and the
    # reply the owner is about to read will be rendered at this width above it.
    return _section("Chat", inner, anchor="chat", wide=True)


def _note_link(name: str) -> str:
    """A link that opens one note in the memories browser."""
    return f"{ROUTE_MEMORY}?note={quote(str(name), safe='')}"


def _memory_section(state: PanelState) -> str:
    """The index-page card: how much memory is in the vault, and how to browse it."""

    def build() -> str:
        status = state.memory_notes()
        if status.error:
            # The one case where the owner most needs the browser is the one where
            # there is nothing to list yet, so the card still offers it: the first
            # note written from the page creates the directory.
            return "\n".join(
                [
                    _problem(status.error, status.fix),
                    f'<p class="hint">The browser still opens: '
                    f'<a href="{ROUTE_MEMORY}">read, search and write memory</a>.</p>',
                ]
            )
        count = len(status.notes)
        parts = [
            '<p class="hint">The markdown half of memory: the always-loaded '
            "router plus one file per note. It lives outside the repository.</p>",
            '<p class="mono">'
            f"vault: {_esc(status.vault_dir)}<br>"
            f"notes: {_esc(status.notes_dir)}<br>"
            f"router: {_esc(status.router_path)}</p>",
        ]
        if status.note:
            parts.append(f'<p class="warn">{_esc(status.note)}</p>')
        if status.notes:
            rows = []
            for summary in status.notes[:8]:
                flag = (
                    '<span class="warn"> not in CLAIRE.md</span>'
                    if summary.unreferenced
                    else ""
                )
                rows.append(
                    "<tr>"
                    f'<td><a href="{_esc(_note_link(summary.name))}">'
                    f"{_esc(summary.name)}</a></td>"
                    f"<td>{_esc(summary.title)}{flag}</td>"
                    f'<td class="mono">{summary.size_bytes} B</td>'
                    "</tr>"
                )
            more = (
                f'<p class="hint">and {count - 8} more.</p>' if count > 8 else ""
            )
            parts.append(
                "<table><thead><tr><th>Note</th><th>Title</th><th>Size</th>"
                f"</tr></thead><tbody>{''.join(rows)}</tbody></table>{more}"
            )
        else:
            parts.append(
                '<p class="empty">No notes yet. The router holds the pointers; '
                "the detail belongs in notes.</p>"
            )
        mark = (
            '<span class="ok">marked as an Obsidian vault</span>'
            if status.obsidian_marked
            else '<span class="muted">not yet opened by Obsidian</span>'
        )
        parts.append(
            f'<p class="hint">{count} note(s), {status.total_bytes} bytes of detail '
            f"&middot; {mark} &middot; "
            f'<a href="{ROUTE_MEMORY}">open the memories browser</a></p>'
        )
        return "\n".join(parts)

    return _section("Memories", _guard("memories", build), anchor="memories")


def _memory_notes_table(status: MemoryNotesStatus, selected: str = "") -> str:
    """The vault listing: every note, with size, title and orphan marker."""
    if status.error:
        return _problem(status.error, status.fix)
    if not status.notes:
        return (
            '<p class="empty">No notes yet. CLAIRE.md holds the pointers; write '
            "the first note with the form below.</p>"
        )
    rows = []
    for summary in status.notes:
        marker = (
            '<span class="warn" title="CLAIRE.md never mentions this note">'
            " orphan</span>"
            if summary.unreferenced
            else ""
        )
        current = ' <span class="pill">open</span>' if summary.name == selected else ""
        rows.append(
            "<tr>"
            f'<td><a href="{_esc(_note_link(summary.name))}">'
            f"{_esc(summary.name)}</a>{current}</td>"
            f"<td>{_esc(summary.title)}{marker}</td>"
            f'<td class="mono">{summary.size_bytes} B</td>'
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Note</th><th>Title</th><th>Size</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    if status.note:
        table += f'<p class="warn">{_esc(status.note)}</p>'
    return table


def _memory_note_html(view: MemoryNoteView) -> str:
    """One open note: frontmatter, links, body, and any format problems."""
    if view.error:
        return _problem(view.error, view.fix)

    parts: list[str] = [
        f'<p class="mono">{_esc(view.path)}</p>',
    ]
    if view.frontmatter:
        rows = "".join(
            f"<tr><td>{_esc(key)}</td><td>{_esc(value)}</td></tr>"
            for key, value in view.frontmatter
        )
        parts.append(
            "<table><thead><tr><th>Frontmatter</th><th>Value</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        parts.append(
            '<p class="muted">No frontmatter. Add a title in the editor below so '
            "Obsidian shows this note's properties.</p>"
        )
    if view.problems:
        parts.append(_problem("; ".join(view.problems), view.fix))
    if view.links:
        rendered = []
        for target, resolved in view.links:
            if resolved:
                rendered.append(
                    f'<a href="{_esc(_note_link(target))}">[[{_esc(target)}]]</a>'
                )
            else:
                rendered.append(
                    f'<span class="warn" title="no note with this name">'
                    f"[[{_esc(target)}]]</span>"
                )
        parts.append('<p class="hint">Links: ' + ", ".join(rendered) + "</p>")
    shown = view.text
    if len(shown) > MEMORY_BODY_CHARS:
        parts.append(
            f'<p class="warn">Showing the first {MEMORY_BODY_CHARS} characters; the '
            "edit box below holds the whole body.</p>"
        )
        shown = shown[:MEMORY_BODY_CHARS]
    parts.append(f'<pre class="mono note-body">{_esc(shown)}</pre>')
    return "\n".join(parts)


def _memory_editor_html(view: MemoryNoteView) -> str:
    """The edit form for one note, posting to the same writer Claire uses."""
    return (
        f'<form class="memory" method="post" action="{ROUTE_MEMORY_SAVE}">\n'
        '<input type="hidden" name="mode" value="edit">\n'
        f'<input type="hidden" name="original" value="{_esc(view.name)}">\n'
        '<label class="field" for="edit-title">Title</label>\n'
        f'<input id="edit-title" name="title" maxlength="200" value="'
        f'{_esc(view.title)}">\n'
        '<label class="field" for="edit-body">Body (markdown; [[wiki links]] '
        "work as they do in Obsidian)</label>\n"
        f'<textarea id="edit-body" name="body" rows="12" '
        f'maxlength="{MEMORY_WRITE_MAX_CHARS}">{_esc(view.text)}</textarea>\n'
        '<div class="row"><button type="submit">Save note</button>\n'
        f'<span class="mono">{_esc(view.name)}.md</span></div>\n'
        "</form>\n"
        f'<form class="memory danger" method="post" action="{ROUTE_MEMORY_DELETE}">\n'
        f'<input type="hidden" name="name" value="{_esc(view.name)}">\n'
        '<div class="row"><button type="submit">Delete this note</button>\n'
        "<span class=\"hint\">There is no undo and no trash: the file is removed "
        "from the vault.</span></div>\n"
        "</form>"
    )


def _memory_new_note_html() -> str:
    """The create form: a name (the filename, hence the link target) and a body."""
    return (
        f'<form class="memory" method="post" action="{ROUTE_MEMORY_SAVE}">\n'
        '<input type="hidden" name="mode" value="create">\n'
        '<label class="field" for="new-name">Note name (the filename; '
        "[[this name]] is how it is linked)</label>\n"
        '<input id="new-name" name="name" maxlength="120" '
        'placeholder="dentist" required>\n'
        '<label class="field" for="new-title">Title (optional; defaults to the '
        "name)</label>\n"
        '<input id="new-title" name="title" maxlength="200">\n'
        '<label class="field" for="new-body">Body</label>\n'
        f'<textarea id="new-body" name="body" rows="8" '
        f'maxlength="{MEMORY_WRITE_MAX_CHARS}" '
        'placeholder="Dr. Ruiz, Wednesday 16:00, Calle Mayor 4."></textarea>\n'
        '<div class="row"><button type="submit">Create note</button>\n'
        '<span class="hint">Saved as markdown with title/created/updated '
        "frontmatter, at mode 0600, outside the repository.</span></div>\n"
        "</form>"
    )


def _memory_search_html(status: MemorySearchStatus) -> str:
    """The search results: note hits first, then persisted summaries."""
    parts: list[str] = []
    if status.note_error:
        parts.append(_problem(status.note_error, status.note_fix))
    elif status.note_hits:
        items = []
        for hit in status.note_hits:
            items.append(
                "<li>"
                f'<div class="meta"><a href="{_esc(_note_link(hit.name))}">'
                f"{_esc(hit.name)}</a> &middot; {_esc(hit.title)} "
                f'<span class="mono">line {hit.line_no}</span></div>'
                f"<p class=\"mono\">{_esc(hit.line)}</p>"
                "</li>"
            )
        parts.append(
            f'<p class="hint">{len(status.note_hits)} note hit(s) for '
            f"&ldquo;{_esc(status.query)}&rdquo;.</p>"
            f'<ul class="diary">{"".join(items)}</ul>'
        )
    else:
        parts.append(
            f'<p class="empty">No note matched &ldquo;{_esc(status.query)}'
            "&rdquo;.</p>"
        )

    if status.diary_error:
        parts.append(_problem(status.diary_error, status.diary_fix))
    elif status.diary_hits:
        items = []
        for entry in status.diary_hits:
            topics = (
                f'<span>topics: {_esc(entry.topics)}</span>' if entry.topics else ""
            )
            items.append(
                "<li>"
                f'<div class="meta"><span class="mono">{_esc(entry.date)}</span>'
                f"{topics}</div>"
                f"<p>{_esc(entry.summary)}</p>"
                "</li>"
            )
        parts.append(
            f'<p class="hint">{len(status.diary_hits)} persisted summary hit(s).</p>'
            f'<ul class="diary">{"".join(items)}</ul>'
        )
    elif status.diary_note:
        parts.append(f'<p class="muted">{_esc(status.diary_note)}</p>')
    return "\n".join(parts)


def render_memory(
    state: PanelState,
    *,
    query: str = "",
    note: str = "",
    changed: bool = False,
    bind_host: str = DEFAULT_BIND_HOST,
    bind_port: int = DEFAULT_PORT,
) -> PanelResponse:
    """The memories browser: the router, the notes, and the persisted diary.

    One page, server-rendered and read-only except for the note forms.  Search
    and the open note live in the query string (``?q=``, ``?note=``) so every
    view is a link the owner can bookmark, and no path from a request is ever
    used to open a file: a note is addressed by *name*, through
    :mod:`claire.memory.notes`, which refuses anything that is not one filename
    inside the notes directory.
    """
    cards: list[str] = [
        _masthead(bind_host, bind_port),
        "<main>",
    ]

    def vault_card() -> str:
        info = state.memory_notes()
        parts = [
            '<p class="hint">Memory is a markdown vault: <span class="mono">'
            "CLAIRE.md</span> is the always-loaded router and holds one-line "
            "pointers; each note under <span class=\"mono\">notes/</span> holds "
            "the detail. Point Obsidian (or any markdown tool) at the vault "
            "directory to read the same files.</p>",
            '<p class="mono">'
            f"vault: {_esc(info.vault_dir)}<br>"
            f"notes: {_esc(info.notes_dir)}<br>"
            f"router: {_esc(info.router_path)}</p>",
            '<p class="hint">'
            + (
                '<span class="ok">Obsidian has opened this folder as a vault</span>'
                if info.obsidian_marked
                else '<span class="muted">No .obsidian/ folder yet; opening this '
                "folder in Obsidian creates one.</span>"
            )
            + "</p>",
            '<form class="memory search" method="get" action="'
            + ROUTE_MEMORY
            + '">\n'
            '<label class="field" for="q">Search notes and the persisted '
            "diary</label>\n"
            f'<input id="q" name="q" value="{_esc(query)}" maxlength="200" '
            'placeholder="dentist">\n'
            '<div class="row"><button type="submit">Search</button>'
            + (
                f' <a href="{ROUTE_MEMORY}">clear</a>'
                if query
                else ""
            )
            + "</div>\n</form>",
            "<h3>Router (CLAIRE.md)</h3>",
            _guard("memory router text", lambda: _router_body_html(state)),
            "<h3>Notes</h3>",
            _memory_notes_table(info, selected=note),
        ]
        if changed:
            parts.insert(0, '<p class="ok">The vault was written.</p>')
        return "\n".join(parts)

    cards.append(_section("Memory vault", _guard("memory vault", vault_card), wide=True))

    if query:
        cards.append(
            _section(
                f"Search results for \u201c{query}\u201d",
                _guard("memory search", lambda: _memory_search_html(state.memory_search(query))),
                anchor="search",
                wide=True,
            )
        )

    if note:
        view = _memory_note_view(state, note)
        cards.append(
            _section(
                f"Note: {view.name or note}",
                _guard("note", lambda: _memory_note_html(view)),
                anchor="note",
                badge="note",
                wide=True,
            )
        )
        cards.append(
            _section(
                "Edit this note",
                _guard("note editor", lambda: _memory_editor_html(view)),
                anchor="edit",
                wide=True,
            )
        )

    cards.append(
        _section("New note", _memory_new_note_html(), anchor="new-note", wide=True)
    )
    cards.append(
        _section(
            "Persisted diary (SQLite)",
            _guard("memory diary", lambda: _memory_diary_text(state)),
            anchor="diary",
            badge="on disk",
            wide=True,
        )
    )
    cards.append("</main>")
    cards.append(
        '<footer><a href="/">Back to the panel</a> &middot; memory is Claire\'s '
        "instructions to herself and lives outside the repository.</footer>"
    )
    body = "\n".join(cards)
    return PanelResponse(status=200, body=_page("Claire memories", body).encode("utf-8"))


def _memory_note_view(state: PanelState, name: str) -> MemoryNoteView:
    """Open one note, turning any failure into a view that renders a PROBLEM box.

    The section is built from a value, not from inside a ``_guard`` closure, so
    this is where the failure is caught: a state that cannot open notes still
    gets its two cards, each naming what is wrong.
    """
    try:
        return state.memory_note(name)
    except PanelError as exc:
        return MemoryNoteView(
            name=name, error=f"{exc.code}: {exc.message}", fix=exc.fix
        )
    except Exception as exc:  # noqa: BLE001 - a broken source is a finding
        detail = _scrub(exc)
        return MemoryNoteView(
            name=name,
            error=f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__,
            fix=(
                "Run `python -m claire.platform.service` for the named check "
                "behind the memory vault, and report it with that output."
            ),
        )


def _router_body_html(state: PanelState) -> str:
    """The router's text, read straight from the file, plus its pointers."""
    status = state.router_status()
    if status.error:
        return _problem(status.error, status.fix)
    try:
        text = Path(status.path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _problem(
            f"{type(exc).__name__}: {exc}",
            f"Check that {status.path} is readable by the panel's user.",
        )
    from claire.memory import obsidian as obsidian_module

    pointers = obsidian_module.link_targets_in_text(text)
    pointer_line = (
        '<p class="hint">Pointers found: '
        + ", ".join(
            (
                f'<a href="{_esc(_note_link(target))}">[[{_esc(target)}]]</a>'
            )
            for target in pointers
        )
        + "</p>"
        if pointers
        else '<p class="muted">This router names no note yet.</p>'
    )
    return (
        f'<p class="mono">{_esc(status.path)} &middot; {status.size_bytes} / '
        f"{status.budget_bytes} bytes"
        + (" &middot; <span class=\"bad\">over budget</span>" if status.over_budget else "")
        + "</p>"
        + pointer_line
        + f'<pre class="mono note-body">{_esc(text)}</pre>'
    )


def _memory_diary_text(state: PanelState) -> str:
    """The recent persisted summaries, with the search box above them."""
    status = state.diary()
    html = _diary_html(status)
    return (
        '<p class="hint">The memory layer writes one summarised entry per day '
        "into SQLite. These are summaries, not a verbatim transcript; the "
        "search box above queries this table too.</p>" + html
    )


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
            _masthead(bind_host, bind_port),
            "<main>",
            _conversation_section(state),
            _chat_section(state),
            _provider_section(state, changed=changed),
            _keys_section(state),
            _router_section(state),
            _memory_section(state),
            _calendar_section(state),
            _self_check_section(state),
            _actions_section(state),
            "</main>",
            f'<footer>Refresh for current state. Text report: '
            f'<a href="{ROUTE_SELF_CHECK}">{ROUTE_SELF_CHECK}</a> '
            f'&middot; memories: <a href="{ROUTE_MEMORY}">{ROUTE_MEMORY}</a> '
            f'&middot; machine-readable: <a href="{ROUTE_HEALTH}">{ROUTE_HEALTH}</a>.</footer>',
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
        '<header class="top">\n<div class="top-inner">\n'
        '<div class="brand">\n<span class="dot" aria-hidden="true"></span>\n'
        "<h1>Claire</h1>\n"
        f'<span class="tag">refused: {_esc(exc.code)}</span>\n'
        "</div>\n"
        '<p class="sub">The panel refused this request and read nothing to do it.</p>\n'
        "</div>\n</header>",
        "<main>",
        _section(
            "Request refused",
            _problem(f"{exc.code}: {exc.message}", exc.fix),
            badge=exc.code,
            wide=True,
        ),
        "</main>",
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


def refuse_key_fields(form: Mapping[str, str], route: str) -> None:
    """Refuse any posted field whose name is credential-shaped.

    Shared by every write endpoint on purpose: the panel has no key field
    anywhere, and a POST that carries one is refused by name rather than ignored,
    so "the panel never accepts a key" holds on routes that do not exist yet too.
    """
    forbidden = sorted(name for name in form if name.strip().lower() in FORBIDDEN_KEY_FIELDS)
    if not forbidden:
        return
    raise PanelOriginError(
        f"POST {route} was refused: the body carries "
        f"{', '.join(repr(name) for name in forbidden)}, and this page never "
        "accepts a key value.",
        fix=(
            "Set a key out-of-band instead: store it with "
            "`python -m claire.platform.service --help` style tooling or "
            "KeyStore.store(), or export the provider's environment variable. "
            "The panel form has no key field by design."
        ),
    )


def handle_chat(state: PanelState, request: PanelRequest) -> PanelResponse:
    """The chat endpoint: the same guards as the selector, then one model turn.

    On success it answers 303 back to the panel, so a refresh cannot re-send the
    message, and the new turns are the first thing on the page.  A model that
    gives no reply is a named 502, never an empty box.
    """
    check_write_origin(request, route=ROUTE_CHAT)
    form = parse_form(request)
    refuse_key_fields(form, ROUTE_CHAT)
    message = str(form.get("message", "") or "").strip()
    if not message:
        raise PanelBadRequest(
            "the chat form carried no message.",
            fix="Type something in the chat box and send it again.",
        )
    if len(message) > MAX_CHAT_CHARS:
        raise PanelBadRequest(
            f"the message is {len(message)} characters; the chat box forwards at "
            f"most {MAX_CHAT_CHARS}.",
            fix=f"Shorten the message to {MAX_CHAT_CHARS} characters or fewer.",
        )
    state.chat_reply(message, timeout_sec=CHAT_TIMEOUT_SECONDS)
    return _redirect(f"{ROUTE_INDEX}#conversation")


def handle_select(state: PanelState, request: PanelRequest) -> PanelResponse:
    """The write endpoint: validate origin, refuse key fields, write, redirect."""
    check_write_origin(request, route=ROUTE_SELECT)
    form = parse_form(request)
    refuse_key_fields(form, ROUTE_SELECT)

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


def _memory_notes_error(exc: Exception, *, name: str, mode: str) -> PanelError:
    """Turn a named :mod:`claire.memory.notes` failure into a panel refusal.

    The note layer already says exactly what is wrong ("note already exists",
    "not a safe single filename"); the only thing missing is the *fix*, and it
    differs per failure.  Nothing here is caught generically: an unexpected
    exception stays unexpected and is rendered with its own type name.
    """
    from claire.memory import notes as notes_module

    detail = _scrub(exc)
    if isinstance(exc, notes_module.NoteExistsError):
        return PanelBadRequest(
            f"note {name!r} already exists; nothing was changed. {detail}",
            fix=(
                f"Open {name} from the note list and edit it, or create the new "
                "note under a different name."
            ),
        )
    if isinstance(exc, notes_module.NoteNotFoundError):
        return PanelBadRequest(
            f"note {name!r} was not found, so the {mode} changed nothing. {detail}",
            fix=(
                "The note may have been deleted or renamed since this page was "
                "loaded; reload the memories page and try again."
            ),
        )
    if isinstance(exc, notes_module.InvalidNoteNameError):
        return PanelBadRequest(
            f"{name!r} is not a usable note name. {detail}",
            fix=(
                "A note name is one filename: use letters, digits, spaces, '-' "
                "or '_' (at most 120 characters), with no '/' or '..'."
            ),
        )
    if isinstance(exc, notes_module.NotesDirectoryError):
        return PanelWriteError(
            f"the notes directory is missing, so the {mode} wrote nothing. {detail}",
            fix="Add the note again; the browser creates the vault and its "
            "notes/ directory on demand.",
        )
    return PanelWriteError(
        f"the {mode} failed: {type(exc).__name__}: {detail}",
        fix="Fix the cause above and submit the form again.",
    )


def handle_memory_save(state: PanelState, request: PanelRequest) -> PanelResponse:
    """Create or replace one note, through :mod:`claire.memory.notes`.

    The same origin guard, key-field refusal and body cap as every other write
    endpoint on this page.  ``mode`` is explicit rather than inferred from the
    note's existence: "create" must refuse to clobber an existing note, and
    "edit" must refuse to silently create a new one.
    """
    check_write_origin(request, route=ROUTE_MEMORY_SAVE)
    form = parse_form(request)
    refuse_key_fields(form, ROUTE_MEMORY_SAVE)

    mode = str(form.get("mode", "") or "").strip().lower()
    if mode not in ("create", "edit"):
        raise PanelBadRequest(
            f"the note form carried mode {mode!r}; expected 'create' or 'edit'.",
            fix="Reload the memories page and use one of its note forms.",
        )
    name = str(form.get("name", "") or "").strip()
    if not name:
        name = str(form.get("original", "") or "").strip()
    if not name:
        raise PanelBadRequest(
            "the note form carried no note name.",
            fix="Type a note name (one filename, no '/') and submit the form.",
        )
    body = str(form.get("body", "") or "")
    if len(body) > MEMORY_WRITE_MAX_CHARS:
        raise PanelTooLarge(
            f"the note body is {len(body)} characters; the editor writes at most "
            f"{MEMORY_WRITE_MAX_CHARS}.",
            fix=(
                f"Shorten the body to {MEMORY_WRITE_MAX_CHARS} characters or "
                "fewer. A note is meant to be small; put detail in its own note "
                "and keep a pointer in CLAIRE.md."
            ),
        )
    title = str(form.get("title", "") or "").strip()[:200]

    try:
        state.save_note(name, body, title=title, create=(mode == "create"))
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed write is a named refusal
        raise _memory_notes_error(exc, name=name, mode=mode) from exc
    return _redirect(f"{ROUTE_MEMORY}?note={quote(name, safe='')}&changed=1")


def handle_memory_delete(state: PanelState, request: PanelRequest) -> PanelResponse:
    """Delete one note, reporting a no-op as a refusal rather than success."""
    check_write_origin(request, route=ROUTE_MEMORY_DELETE)
    form = parse_form(request)
    refuse_key_fields(form, ROUTE_MEMORY_DELETE)

    name = str(form.get("name", "") or "").strip()
    if not name:
        raise PanelBadRequest(
            "the delete form carried no note name.",
            fix="Reload the memories page, open the note and use its Delete button.",
        )
    try:
        state.delete_note(name)
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed delete is a named refusal
        raise _memory_notes_error(exc, name=name, mode="delete") from exc
    return _redirect(f"{ROUTE_MEMORY}?changed=1")


def dispatch(state: PanelState, request: PanelRequest) -> PanelResponse:
    """Route one request. Never raises: every refusal is a rendered page."""
    try:
        path, query = parse_target(request.path)
    except PanelError as exc:
        return render_error_page(exc)
    if path in POST_ROUTES:
        if request.method != "POST":
            return render_error_page(
                PanelMethodNotAllowed(request.method, path, allowed=("POST",))
            )
        try:
            if path == ROUTE_CHAT:
                return handle_chat(state, request)
            if path == ROUTE_MEMORY_SAVE:
                return handle_memory_save(state, request)
            if path == ROUTE_MEMORY_DELETE:
                return handle_memory_delete(state, request)
            return handle_select(state, request)
        except PanelError as exc:
            return render_error_page(exc)
    if path == ROUTE_FAVICON:
        if request.method not in ("GET", "HEAD"):
            return render_error_page(
                PanelMethodNotAllowed(request.method, path, allowed=("GET",))
            )
        return PanelResponse(status=204, body=b"", content_type="image/x-icon")
    if path == ROUTE_MEMORY:
        # Dispatched by name, not through GET_VIEWS, because its view takes the
        # query string (?q= search, ?note= which note to open).
        if request.method not in ("GET", "HEAD"):
            return render_error_page(
                PanelMethodNotAllowed(request.method, path, allowed=("GET",))
            )
        try:
            return render_memory(
                state,
                query=query.get("q", [""])[-1].strip(),
                note=query.get("note", [""])[-1].strip(),
                changed=query.get("changed", [""])[-1] == "1",
                bind_host=request.server_host,
                bind_port=request.server_port,
            )
        except PanelError as exc:
            return render_error_page(exc)
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
    if handler.headers.get("Transfer-Encoding"):
        # The panel does not de-chunk.  A chunked body has no Content-Length, so
        # nothing would be read and the body bytes would be parsed as the *next*
        # request on this keep-alive connection - request smuggling inside the
        # panel.  Answering and closing is the only safe pair.
        handler.close_connection = True
    elif length and declared is None:
        # A Content-Length we cannot parse means the body's end is unknown: read
        # nothing, and end the connection rather than guess where it stops.
        handler.close_connection = True
    elif declared is not None and 0 < declared <= MAX_BODY_BYTES:
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
    # Say "loopback" only when it is true.  Hard-coding it meant the one line an
    # operator reads at startup assured them the panel was loopback-only at the
    # exact moment they had opened it to the network.
    reach = "loopback" if is_loopback_host(str(bound_host)) else "REACHABLE FROM THE NETWORK"
    _LOGGER.info(
        "Claire panel on http://%s:%s/ (plain HTTP, %s)", bound_host, bound_port, reach
    )
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
