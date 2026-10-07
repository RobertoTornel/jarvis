"""Real browser control for Claire, backed by Playwright + chromium.

Why Playwright and not the WebDriver-BiDi route used by the reference project
--------------------------------------------------------------------------
The reference implementation deliberately drove Firefox over WebDriver BiDi
rather than Playwright, and its stated reason was that Node 24 ships a native
``WebSocket`` so no extra dependency was needed. That reason does not transfer
to this Python port: Python's standard library has no built-in WebSocket
client, so the BiDi route would *add* a dependency (``websockets``) instead of
avoiding one, and it would require hand-rolling the whole protocol
(session handshake, browsing-context bookkeeping, script evaluation) on top of
it. Playwright is already installed here, ships the chromium binary at
``~/.cache/ms-playwright/chromium-1243``, and gives real rendering, real
clicks, real form filling and real screenshots through one small API. So
Playwright is the right call for this port -- recorded here because a future
reader who knows the reference project will otherwise "fix" it back.

The browser is PROCESS-WIDE state
---------------------------------
There is exactly ONE chromium process, ONE profile directory and ONE operation
ordering for the whole Claire process. Chromium takes an exclusive lock
(``SingletonLock``) on its user-data directory; a second browser launched
against the same profile can exit immediately having done *nothing at all*,
which is indistinguishable from success if it is not checked. So this module
never lets a caller build a second browser: :func:`get_session` lazily creates
a single :class:`BrowserSession`, guarded by a module lock, and every session
method serialises on the session's own lock so that "one browser, one
ordering" is a property of the module and not of luck.

Missing dependencies are NAMED and carry a fix
---------------------------------------------
If the chromium binary is absent, :func:`ensure_browser_installed` raises
:class:`BrowserUnavailableError` whose message carries the exact command
(``playwright install chromium``) -- never a bare stack trace, never a silent
empty result.

Secrets are never typed into a log
----------------------------------
:meth:`BrowserSession.fill` refuses to put a secret into a log line or into the
value it returns, and the rule does **not** rest on guessing what a credential
looks like:

1. **DOM truth first.** :meth:`BrowserDriver.field_type` asks the browser what
   the element *is*. ``type=password`` makes the value a secret regardless of
   the selector's spelling and regardless of what the value looks like.
2. **Fail closed.** If the type cannot be determined (no capability, element
   missing, any error) the value is a secret unless the caller positively
   identified a harmless field (a reported or declared non-secret input type).
   A redacted debug line is a small cost; a password in a log is not.
3. :func:`secret_field` and :func:`looks_like_secret` are **backstops**. They
   can only add redaction, never remove it.

Heuristics alone were proven wrong here: ``#pw`` -- one of the most common
password-field ids in the world -- was not matched, and a strong password such
as ``Sup3rSecretValue!42`` was not recognised, so both the ActionResult and the
debug log carried the value in plaintext.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # pragma: no cover - import shim, exercised by every test indirectly
    from ..debug import debug_log
except Exception:  # pragma: no cover - stand-alone use (e.g. doctools)
    def debug_log(message: str, category: str = "debug") -> None:  # type: ignore
        pass

try:  # pragma: no cover - import shim
    from ..utils.redact import redact as _redact_text, scrub_secrets as _scrub_secrets
except Exception:  # pragma: no cover - stand-alone use
    def _redact_text(text: str, max_len: int = 8000) -> str:  # type: ignore
        return text[:max_len]

    def _scrub_secrets(text: str) -> str:  # type: ignore
        return text


def _log(message: str, category: str = "senses") -> None:
    """Log a browser action, structurally scrubbing credentials first.

    Every log line in this module goes through here. A page URL is exactly the
    kind of string that carries a credential (``/reset?token=...``), and so is
    an exception message, so scrubbing has to be structural rather than a
    promise made separately at each call site.

    It reads the module-global ``debug_log`` at call time, so tests capture log
    lines by monkeypatching that name.
    """
    try:
        safe = _scrub_secrets(str(message))
    except Exception:  # pragma: no cover - never let logging break an action
        safe = "[browser log line withheld: scrubbing failed]"
    debug_log(safe, category)


__all__ = [
    # errors
    "BrowserError",
    "BrowserUnavailableError",
    "BrowserProfileLockedError",
    "NavigationError",
    "NavigationTimeoutError",
    "SelectorNotFoundError",
    "ActionTimeoutError",
    "EmptyPageError",
    # results
    "PageRead",
    "FindResult",
    "ActionResult",
    # availability
    "CHROMIUM_INSTALL_COMMAND",
    "ensure_browser_installed",
    "browser_available",
    "probe_chromium_binary",
    # secret handling
    "looks_like_secret",
    "secret_field",
    "redact_value",
    # session
    "BrowserSession",
    "get_session",
    "close_session",
    "reset_session",
    "session_is_open",
]

#: The exact command a user must run when the chromium binary is missing.
#: Quoted verbatim in every message that reports the condition, because a fix
#: the user has to guess is not a fix.
CHROMIUM_INSTALL_COMMAND = "playwright install chromium"

#: Default per-operation timeout. Long enough for a slow page, short enough
#: that a hung navigation cannot wedge a voice turn forever.
DEFAULT_TIMEOUT_MS = 15_000

#: Every textual action logged at most this many characters of content.
_LOG_VALUE_MAX = 120


# --------------------------------------------------------------------------
# Named errors
# --------------------------------------------------------------------------


class BrowserError(Exception):
    """Base class for every failure this module reports.

    Callers can catch :class:`BrowserError` for "the browser did not do it",
    and catch the concrete subclasses when the *kind* of failure matters to
    what Claire says out loud.
    """

    def __init__(self, message: str, *, fix: Optional[str] = None) -> None:
        self.fix = fix
        super().__init__(message)

    def user_message(self) -> str:
        """One sentence suitable for speaking, including the fix when known."""
        text = str(self)
        if self.fix:
            text = f"{text} Fix: {self.fix}"
        return text


class BrowserUnavailableError(BrowserError):
    """A browser dependency is absent (binary missing, playwright not installed).

    Always carries :attr:`fix` -- the exact command to run.
    """


class BrowserProfileLockedError(BrowserError):
    """Another chromium already owns the profile directory.

    Distinct from :class:`BrowserUnavailableError` because the fix is
    different: wait for / close the other session rather than install
    anything.
    """


class NavigationError(BrowserError):
    """Navigation was attempted and failed for a reason other than a timeout.

    Covers DNS failures, refused connections and "there is no earlier page in
    this session's history" for :meth:`BrowserSession.back`.
    """


class NavigationTimeoutError(NavigationError):
    """The page did not finish loading inside the timeout."""


class SelectorNotFoundError(BrowserError):
    """The selector (or the visible text) is not on the page.

    Distinct from :class:`ActionTimeoutError`: this means "not there".
    """


class ActionTimeoutError(BrowserError):
    """The element is there but the action did not complete inside the timeout.

    This is the "found it, but it is covered by a cookie banner / still
    animating / not clickable" case, and it is a different thing to say out
    loud than "that button does not exist".
    """


class EmptyPageError(BrowserError):
    """The page navigated fine but has no readable text at all.

    Distinct from a timeout and from a missing selector: it usually means a
    canvas application, a PDF viewer, or a page that is still rendering.
    """


# --------------------------------------------------------------------------
# Secret handling -- the hard rule
# --------------------------------------------------------------------------
#
# Order of authority, and the reason for it:
#
#   (1) DOM truth: BrowserDriver.field_type() asks the browser what the field
#       IS. 'password' is a secret, full stop.
#   (2) Fail closed: a type that cannot be determined is a secret unless the
#       caller positively identified a harmless field.
#   (3) Backstops: secret_field()/looks_like_secret() may ADD redaction; they
#       can never remove it.
#
# Heuristics are not the foundation: '#pw' was missed by the field-name pattern
# and 'Sup3rSecretValue!42' was missed by the value heuristics, and the value
# reached both the ActionResult and the log in plaintext.

#: Keyword patterns that make a *value* a secret regardless of the field it is
#: typed into (e.g. a caller that types "my password is hunter2" into a search
#: box). BACKSTOP ONLY -- see :func:`looks_like_secret`. Fail-safe: a false
#: positive only masks something harmless in a log.
_SECRET_VALUE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(pass(?:word|wd|phrase)?|passwd|pwd|secret|token|api[_-]?key|"
               r"apikey|otp|pin|passcode|credential|bearer)\b"),
)

#: An opaque token blob: no spaces, long, and mixing letters with digits.
#: "hello world" is not a secret; "a8f3k2m9x1q7z4b6" is treated as one.
_OPAQUE_BLOB = re.compile(r"^[A-Za-z0-9+/=_\-]{16,}$")

#: Length at which a whitespace-free, mixed-character-class string counts as a
#: credential by the backstop below. Chosen so ordinary words and short ids do
#: not trip it.
_MIN_MIXED_CLASS_LEN = 8

#: A field whose *name* says "credential" is treated as a secret field even if
#: the value passed in looks innocuous. NOTE: this is a hint, not the guarantee
#: -- the guarantee is DOM truth plus failing closed.
#:
#: ``pw`` carries a word-ish boundary so it matches ``#pw``, ``pw``, ``user_pw``
#: and ``pw-field`` but NOT ``pwned`` or ``power``. The older alternatives are
#: deliberately left unbounded: they over-redact at worst.
_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)(pass(?:word|wd|phrase)?|passwd|pwd|"
    r"(?<![a-z])pw(?![a-z])|"
    r"secret|token|api[_-]?key|apikey|otp|"
    r"pin|passcode|credential|cvv|cc[-_]?num)"
)

#: A ``type=`` declaration written into the selector itself (``input[type=text]``,
#: ``[type='search']``). Stating a harmless type is the caller positively
#: identifying a harmless field when the driver cannot. The lookbehind keeps a
#: *different* attribute such as ``[data-type=text]`` from counting as a
#: declaration about the input's own type.
_SELECTOR_TYPE_ATTR = re.compile(r"(?i)(?<![a-z0-9_-])type\s*=\s*['\"]?([a-z][a-z0-9-]*)")

#: Input types that are positively NOT credential fields. Only a type named here
#: (or a selector that declares one) can make a *known* value visible in a log;
#: anything unrecognised goes down the fail-closed path.
_HARMLESS_FIELD_TYPES = frozenset({
    "text", "search", "email", "url", "tel", "number", "textarea",
    "date", "datetime-local", "month", "week", "time", "color", "range",
    "checkbox", "radio", "file", "submit", "reset", "button", "hidden",
})

#: Reason strings recorded in the log line (and in tests). The *reason* is safe
#: to log: it never contains the value.
_REASON_DOM_PASSWORD = "dom-type=password"
_REASON_FIELD_NAME = "field-name-names-a-credential"
_REASON_VALUE_BACKSTOP = "value-backstop"
_REASON_DOM_HARMLESS = "dom-type-harmless"
_REASON_DECLARED_HARMLESS = "selector-declares-harmless-type"
_REASON_UNPROVEN = "field-type-unproven-fail-closed"


def _character_class_count(text: str) -> int:
    """How many of {lower, upper, digit, symbol} ``text`` mixes."""
    count = 0
    if any(ch.islower() for ch in text):
        count += 1
    if any(ch.isupper() for ch in text):
        count += 1
    if any(ch.isdigit() for ch in text):
        count += 1
    if any(not ch.isalnum() and not ch.isspace() for ch in text):
        count += 1
    return count


def looks_like_secret(value: Optional[str]) -> bool:
    """Return True when ``value`` looks like a credential.

    .. warning::
       This is a **BACKSTOP**, not the guarantee. The guarantee is DOM truth --
       :meth:`BrowserDriver.field_type` reporting ``password`` -- plus failing
       closed when the type cannot be determined (see
       :func:`_judge_fill_secret`). This function only ever ADDS redaction.

    Signals, all cheap and all fail-safe (a false positive only means something
    harmless is masked in a log line):

    * scrubbing the value with the project scrubber changes it, i.e. it
      contains a token/email/key-shaped run;
    * it carries a credential keyword such as ``password``/``token``/``otp``;
    * it is an opaque no-spaces blob mixing letters and digits (a bare key); or
    * it has no whitespace, is at least :data:`_MIN_MIXED_CLASS_LEN` characters
      long, and mixes at least two of lower/upper/digit/symbol -- which is what
      catches a strong password such as ``Sup3rSecretValue!42`` that the
      alnum-blob rule missed because of its punctuation.
    """
    if value is None:
        return False
    text = str(value)
    if not text.strip():
        return False
    try:
        # scrub_secrets is structural only (it does not collapse whitespace),
        # so ordinary prose is never mistaken for a credential.
        if _scrub_secrets(text) != text:
            return True
    except Exception:  # pragma: no cover - scrubber is total in practice
        pass
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(text):
            return True
    if _OPAQUE_BLOB.match(text) and re.search(r"[A-Za-z]", text) and re.search(r"\d", text):
        return True
    # Backstop for punctuation-carrying credentials (mixed case, digits, symbols).
    if len(text) >= _MIN_MIXED_CLASS_LEN and not any(ch.isspace() for ch in text):
        if _character_class_count(text) >= 2:
            return True
    return False


def secret_field(selector: Optional[str]) -> bool:
    """Return True when a selector/field name points at a credential field.

    A hint, not the guarantee: it can only turn a value secret.
    """
    if not selector:
        return False
    return bool(_SECRET_FIELD_PATTERN.search(str(selector)))


def _declared_field_type(selector: Optional[str]) -> Optional[str]:
    """The input ``type`` the selector itself declares, if it declares one."""
    if not selector:
        return None
    match = _SELECTOR_TYPE_ATTR.search(str(selector))
    if match is None:
        return None
    return match.group(1).strip().lower() or None


def _dom_field_type(driver: Any, selector: str) -> Optional[str]:
    """Ask ``driver`` what kind of field ``selector`` is; ``None`` = unknown.

    ``field_type`` is an OPTIONAL capability: a driver that predates it has no
    such attribute, one that cannot see the element returns ``None``, and one
    that is broken raises. All three mean "the type cannot be determined" and
    all three must fail closed, so every failure is swallowed into ``None``.
    """
    probe: Optional[Callable[[str], Optional[str]]] = getattr(driver, "field_type", None)
    if probe is None:
        return None
    try:
        reported = probe(selector)
    except Exception:
        return None
    if reported is None:
        return None
    kind = str(reported).strip().lower()
    return kind or None


def _judge_fill_secret(
    selector: str, value: str, dom_type: Optional[str]
) -> Tuple[bool, str]:
    """Decide whether a fill is a secret, and say why.

    Returns ``(secret, reason)``. The reason is a short safe label, never the
    value, so it can go straight into the log line.

    DOM truth decides first: a reported ``password`` type is a secret whatever
    the selector says and whatever the value looks like. Otherwise the field
    must be *positively* identified as harmless -- by the DOM or by the selector
    declaring a harmless type -- before the value may be shown; a type nobody
    could determine is treated as a secret.
    """
    if dom_type == "password":
        return True, _REASON_DOM_PASSWORD
    if secret_field(selector):
        return True, _REASON_FIELD_NAME
    if looks_like_secret(value):
        return True, _REASON_VALUE_BACKSTOP
    if dom_type is not None and dom_type in _HARMLESS_FIELD_TYPES:
        return False, _REASON_DOM_HARMLESS
    declared = _declared_field_type(selector)
    if declared is not None and declared in _HARMLESS_FIELD_TYPES:
        return False, _REASON_DECLARED_HARMLESS
    # FAIL CLOSED: nothing proved this field harmless.
    return True, _REASON_UNPROVEN


def redact_value(value: Optional[str], *, secret: bool = False) -> str:
    """Return a log-safe rendering of a value that is about to be typed.

    A secret becomes a fixed marker; anything else is passed through the
    project's structural scrubber *and* length-capped, so a non-secret value
    can never smuggle a credential into a log line either.
    """
    if secret or looks_like_secret(value):
        return "[REDACTED]"
    if value is None:
        return ""
    try:
        scrubbed = _redact_text(str(value), max_len=_LOG_VALUE_MAX)
    except Exception:  # pragma: no cover
        return "[REDACTED]"
    if len(scrubbed) > _LOG_VALUE_MAX:
        scrubbed = scrubbed[:_LOG_VALUE_MAX] + "…"
    return scrubbed


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRead:
    """What a page shows right now."""

    url: str
    title: str
    text: str
    frames: Tuple[str, ...] = ()
    char_count: int = 0
    word_count: int = 0
    frame_count: int = 0
    link_count: int = 0

    @property
    def is_empty(self) -> bool:
        """True when the main document has no visible text."""
        return not self.text.strip()

    @property
    def total_char_count(self) -> int:
        return self.char_count + sum(len(f) for f in self.frames)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "frames": list(self.frames),
            "char_count": self.char_count,
            "word_count": self.word_count,
            "frame_count": self.frame_count,
            "link_count": self.link_count,
            "is_empty": self.is_empty,
        }


@dataclass(frozen=True)
class FindResult:
    """Where (or whether) a phrase appears on the current page.

    A miss is a *successful* answer, not an error: :attr:`found` is False and
    :attr:`detail` says so in words. Callers must not have to catch an
    exception to learn "it is not there".
    """

    phrase: str
    found: bool
    count: int = 0
    region: str = ""
    in_frame: Optional[int] = None
    snippet: str = ""
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "phrase": self.phrase,
            "found": self.found,
            "count": self.count,
            "region": self.region,
            "in_frame": self.in_frame,
            "snippet": self.snippet,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ActionResult:
    """The outcome of click/fill/back/screenshot.

    For :meth:`BrowserSession.fill` the typed value is represented only by
    :attr:`value_redacted`, :attr:`value_len` and :attr:`secret`; the raw value
    is deliberately not a field on this structure.
    """

    action: str
    target: str
    ok: bool = True
    detail: str = ""
    value_redacted: str = ""
    value_len: int = 0
    secret: bool = False
    path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "ok": self.ok,
            "detail": self.detail,
            "value_redacted": self.value_redacted,
            "value_len": self.value_len,
            "secret": self.secret,
            "path": self.path,
        }


# --------------------------------------------------------------------------
# Availability probe (cheap: no browser launch, no subprocess)
# --------------------------------------------------------------------------


def _browsers_root() -> Path:
    """Directory playwright keeps downloaded browsers in."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override not in ("0", "1"):
        return Path(override)
    return Path.home() / ".cache" / "ms-playwright"


def _chromium_candidates() -> List[Path]:
    """Every plausible chromium binary path, most preferred first."""
    candidates: List[Path] = []
    override = os.environ.get("CLAIRE_CHROMIUM_EXECUTABLE")
    if override:
        candidates.append(Path(override))
    root = _browsers_root()
    # Real chromium (full rendering, screenshots of real pages).
    candidates.extend(sorted(root.glob("chromium-*/chrome-linux/chrome"), reverse=True))
    candidates.extend(sorted(root.glob("chromium-*/chrome-linux64/chrome"), reverse=True))
    # The headless shell: smaller, and all this module needs day to day.
    candidates.extend(sorted(root.glob("chromium_headless_shell-*/chrome-linux/headless_shell"),
                              reverse=True))
    return candidates


def probe_chromium_binary() -> Optional[str]:
    """Return the chromium executable path, or None when it is not installed.

    Cheap by construction: it only stats a few paths. It never starts
    playwright's node driver and never launches a browser, so it is safe to
    call on every turn and inside unit tests.

    Honest limit: it checks the standard install location (or
    ``CLAIRE_CHROMIUM_EXECUTABLE`` / ``PLAYWRIGHT_BROWSERS_PATH``), so a
    chromium installed somewhere exotic must be pointed at explicitly rather
    than silently assumed.
    """
    for path in _chromium_candidates():
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:  # pragma: no cover - unreadable path
            continue
    return None


def browser_available() -> bool:
    """True when a chromium binary is present. Never raises."""
    return probe_chromium_binary() is not None


def ensure_browser_installed() -> str:
    """Return the chromium path, or raise :class:`BrowserUnavailableError`.

    The message names the missing thing and the exact command that fixes it,
    because "the browser did not work" is not an actionable sentence.
    """
    path = probe_chromium_binary()
    if path:
        return path
    looked_in = ", ".join(str(p) for p in _browsers_root().glob("chromium*")) or str(_browsers_root())
    raise BrowserUnavailableError(
        "Claire's browser is unavailable: Playwright's chromium binary is not "
        "installed, so no web page can be opened or rendered. "
        f"Run exactly: {CHROMIUM_INSTALL_COMMAND}  "
        f"(or `{sys.executable} -m playwright install chromium`). "
        f"Expected it under: {looked_in}",
        fix=CHROMIUM_INSTALL_COMMAND,
    )


# --------------------------------------------------------------------------
# Driver seam: the only place that touches playwright
# --------------------------------------------------------------------------


class BrowserDriver:
    """The primitives :class:`BrowserSession` needs, and nothing else.

    Keeping the seam this small is what makes the session logic unit-testable
    without a browser: tests substitute an object with these methods. The real
    implementation is :class:`PlaywrightDriver`; both follow one contract:

    * ``ElementNotFoundError`` (internal) is raised when the element/selector
      is genuinely absent, so "not there" never gets confused with "too slow".
    * Playwright's own ``TimeoutError`` propagates for the too-slow case.
    * :meth:`field_type` is an OPTIONAL capability used only to decide whether a
      value about to be typed is a secret. A driver without it (or one that
      cannot see the element, or one that raises) reports "unknown", and the
      session then fails closed. Never make this method raise to signal
      "harmless".
    """

    #: methods the session calls; documented here so a fake stays honest
    page_methods = (
        "navigate", "get_url", "get_title", "get_body_text", "get_frame_texts",
        "get_link_count", "click_selector", "click_text", "fill_field",
        "save_screenshot", "go_back", "close", "field_type",
    )

    def field_type(self, selector: str) -> Optional[str]:  # pragma: no cover
        """What kind of field is ``selector``? ``None`` when it cannot be told.

        Optional capability: the base implementation knows nothing, so it
        reports unknown and :meth:`BrowserSession.fill` fails closed. Concrete
        drivers answer from the DOM. Returning ``None`` (or raising) is always
        safe; incorrectly returning a harmless type is not.
        """
        return None

    def navigate(self, url: str, timeout_ms: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_url(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_title(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_body_text(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_frame_texts(self) -> List[str]:  # pragma: no cover
        raise NotImplementedError

    def get_link_count(self) -> int:  # pragma: no cover
        raise NotImplementedError

    def click_selector(self, selector: str, timeout_ms: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def click_text(self, text: str, timeout_ms: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def fill_field(self, selector: str, value: str, timeout_ms: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def save_screenshot(self, path: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def go_back(self, timeout_ms: int) -> bool:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


class _ElementNotFound(Exception):
    """Internal: the element is absent. Raised by the driver, translated by us.

    Not part of the public error surface -- :class:`SelectorNotFoundError` is
    what a caller sees; this exists so the driver can say "definitely not
    there" without importing the session's vocabulary.
    """


class PlaywrightDriver(BrowserDriver):
    """Real chromium via Playwright. One persistent context, one profile."""

    def __init__(self, *, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 profile_dir: Optional[str] = None) -> None:
        # Cheap probe first: a missing binary must surface as the named error
        # with the install command, not as an ImportError/stack trace from
        # deep inside playwright's node handshake.
        executable = ensure_browser_installed()
        self._timeout_ms = timeout_ms
        self._profile_dir = profile_dir or str(_profile_dir())
        self._closed = False
        self._context = None
        self._playwright = None
        self._pw_cm = None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - playwright is installed here
            raise BrowserUnavailableError(
                "Claire's browser is unavailable: the `playwright` Python package "
                f"is not importable ({exc}). Run exactly: "
                f"{sys.executable} -m pip install playwright && {CHROMIUM_INSTALL_COMMAND}",
                fix=f"{sys.executable} -m pip install playwright && {CHROMIUM_INSTALL_COMMAND}",
            ) from exc

        self._pw_cm = sync_playwright()
        self._playwright = self._pw_cm.start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self._profile_dir,
                headless=headless,
                executable_path=executable,
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception as exc:  # pragma: no cover - needs a real browser to hit
            # Tear down the half-built driver so a failed launch does not leave
            # a playwright dispatcher (and its event loop) alive.
            self.close()
            raise _translate_launch_failure(exc, self._profile_dir) from exc
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(timeout_ms)

    # -- primitives -------------------------------------------------------

    def navigate(self, url: str, timeout_ms: int) -> None:
        self._page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

    def get_url(self) -> str:
        return str(self._page.url)

    def get_title(self) -> str:
        return str(self._page.title())

    def get_body_text(self) -> str:
        try:
            return str(self._page.inner_text("body"))
        except Exception:
            return ""

    def get_frame_texts(self) -> List[str]:
        texts: List[str] = []
        # page.frames[0] is the main document, already read as body text.
        for frame in list(self._page.frames)[1:]:
            try:
                frame_text = frame.inner_text("body")
            except Exception:
                continue
            if frame_text and frame_text.strip():
                texts.append(str(frame_text))
        return texts

    def get_link_count(self) -> int:
        try:
            return int(self._page.eval_on_selector_all("a", "els => els.length"))
        except Exception:
            return 0

    def click_selector(self, selector: str, timeout_ms: int) -> None:
        if self._page.query_selector(selector) is None:
            raise _ElementNotFound(f"no element matches selector {selector!r}")
        self._page.click(selector, timeout=timeout_ms)

    def click_text(self, text: str, timeout_ms: int) -> None:
        locator = self._page.get_by_text(text, exact=False)
        try:
            if locator.count() == 0:
                raise _ElementNotFound(f"no element shows the text {text!r}")
        except _ElementNotFound:
            raise
        except Exception as exc:
            raise _ElementNotFound(f"no element shows the text {text!r}") from exc
        locator.first.click(timeout=timeout_ms)

    def field_type(self, selector: str) -> Optional[str]:
        """The element's ``type`` PROPERTY, lower-cased; ``None`` if unknown.

        The property, not the attribute: an ``<input>`` with no ``type=``
        attribute still reports ``"text"`` as a property, which is exactly the
        positive evidence that lets an ordinary field be logged in the clear.
        Every failure -- element gone, page closed, driver detached -- becomes
        ``None`` so the caller fails closed rather than assuming safety.
        """
        try:
            element = self._page.query_selector(selector)
            if element is None:
                return None
            reported = element.evaluate("el => el.type")
        except Exception:
            return None
        if reported is None:
            return None
        kind = str(reported).strip().lower()
        return kind or None

    def fill_field(self, selector: str, value: str, timeout_ms: int) -> None:
        if self._page.query_selector(selector) is None:
            raise _ElementNotFound(f"no input matches selector {selector!r}")
        self._page.fill(selector, value, timeout=timeout_ms)

    def save_screenshot(self, path: str) -> None:
        self._page.screenshot(path=path, full_page=True)

    def go_back(self, timeout_ms: int) -> bool:
        before = str(self._page.url)
        response = self._page.go_back(timeout=timeout_ms)
        if response is not None:
            return True
        # Playwright returns None both when there is no history entry AND when
        # the entry it moved to produced no resource response (data:/about:
        # URLs do not). An actual URL change means the move happened; an
        # unchanged URL means we genuinely cannot tell, and we say "no" rather
        # than claiming a navigation that may not have occurred.
        return str(self._page.url) != before

    def close(self) -> None:
        """Tear down context AND playwright, in that order. Idempotent.

        ``PlaywrightContextManager`` has no ``close()`` -- its teardown is
        ``__exit__`` (which ``SyncPlaywright.stop`` is bound to). Skipping it
        leaves the sync dispatcher greenlet with a *running* event loop, so the
        next ``sync_playwright().start()`` in this process dies with "Sync API
        inside the asyncio loop". Hence both halves are done here, and both
        are idempotent.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True

        context, self._context = getattr(self, "_context", None), None
        if context is not None:
            try:
                context.close()
            except Exception as exc:  # pragma: no cover - teardown is best effort
                _log(f"browser: context close reported {exc!r}", "senses")

        playwright, self._playwright = getattr(self, "_playwright", None), None
        pw_cm, self._pw_cm = getattr(self, "_pw_cm", None), None
        stopped = False
        if playwright is not None and callable(getattr(playwright, "stop", None)):
            try:
                # SyncPlaywright.stop IS PlaywrightContextManager.__exit__.
                playwright.stop()
                stopped = True
            except Exception as exc:  # pragma: no cover - teardown is best effort
                _log(f"browser: playwright stop reported {exc!r}", "senses")
        if not stopped and pw_cm is not None:
            try:
                pw_cm.__exit__(None, None, None)
            except Exception as exc:  # pragma: no cover - teardown is best effort
                _log(f"browser: playwright exit reported {exc!r}", "senses")


def _translate_launch_failure(exc: BaseException, profile_dir: str) -> BrowserError:
    """Turn a raw launch failure into a named, actionable error."""
    text = str(exc)
    lowered = text.lower()
    if "singletonlock" in lowered or "singleton" in lowered or "profile appears to be in use" in lowered:
        return BrowserProfileLockedError(
            "Claire's browser profile is locked by another chromium process, so a "
            f"second browser would exit having done nothing. Profile: {profile_dir}",
            fix=f"close the other chromium using {profile_dir}, or set "
                f"CLAIRE_BROWSER_PROFILE_DIR to a free directory",
        )
    if ("executable doesn't exist" in lowered or "playwright install" in lowered
            or "browser is not installed" in lowered or "failed to launch" in lowered):
        return BrowserUnavailableError(
            f"Claire's browser could not start: {text}. "
            f"Run exactly: {CHROMIUM_INSTALL_COMMAND}",
            fix=CHROMIUM_INSTALL_COMMAND,
        )
    return BrowserError(f"Claire's browser could not start: {text}")


def _profile_dir() -> Path:
    """The one profile directory this process uses."""
    override = os.environ.get("CLAIRE_BROWSER_PROFILE_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base = Path(data_home) / "claire" / "browser-profile"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A read-only home must not be a silent failure: the launch will
        # report the real problem, and this path is only a hint.
        pass
    return base


def _launch_driver(*, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                   profile_dir: Optional[str] = None) -> BrowserDriver:
    """Build the real driver.

    This is the module's browser launcher and the single seam unit tests
    monkeypatch -- no unit test may reach chromium through it.
    """
    return PlaywrightDriver(headless=headless, timeout_ms=timeout_ms, profile_dir=profile_dir)


# --------------------------------------------------------------------------
# Timeout / not-found classification
# --------------------------------------------------------------------------


def _is_timeout(exc: BaseException) -> bool:
    """True for playwright's timeout, matched by name to avoid importing it.

    The MRO is checked, not just the leaf class: playwright raises bare
    ``TimeoutError`` in some paths and a subclass in others, and callers (and
    tests) legitimately subclass it too.
    """
    if isinstance(exc, TimeoutError):
        return True
    return any(
        klass.__name__ in ("TimeoutError", "PlaywrightTimeoutError")
        for klass in type(exc).__mro__
    )


# --------------------------------------------------------------------------
# Selector vs text heuristics
# --------------------------------------------------------------------------

_SELECTOR_PREFIXES = ("#", ".", "[", "css=", "xpath=", "//", "text=", "*")
_SELECTOR_SHAPE = re.compile(r"^[A-Za-z][\w-]*(\s*[>+~]\s*|\s*\[|\s*\.|\s*#|:)")

#: Schemes that must not get "https://" glued in front. Anything with "://"
#: counts; the rest are the useful scheme-with-no-slash URLs.
_BARE_SCHEMES = ("about:", "data:", "file:", "blob:", "javascript:", "mailto:", "chrome:")


def _has_explicit_scheme(target: str) -> bool:
    """True when ``target`` already says how it should be fetched.

    Deliberately conservative: ``example.com:8080/x`` is a host:port, not a
    scheme, so it still gets ``https://`` prepended.
    """
    lowered = target.lower()
    return "://" in target or lowered.startswith(_BARE_SCHEMES)


def looks_like_selector(target: str) -> bool:
    """True when ``target`` reads as a CSS/XPath selector rather than prose.

    Used only by ``click(..., by="auto")``. Being wrong here is recoverable:
    the click reports what it tried, in both forms.
    """
    text = (target or "").strip()
    if not text:
        return False
    if text.startswith(_SELECTOR_PREFIXES):
        return True
    return bool(_SELECTOR_SHAPE.match(text))


def _describe_region(offset: int, total: int) -> str:
    if total <= 0:
        return "on the page"
    fraction = offset / float(total)
    if fraction < 0.33:
        return "near the top of the page"
    if fraction < 0.66:
        return "around the middle of the page"
    return "towards the bottom of the page"


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------


class BrowserSession:
    """One browser, driven step by step.

    Own this through :func:`get_session` (or as a context manager) so the
    process keeps a single chromium and a single profile lock. Every public
    method serialises on ``self._lock``: one browser, one ordering.
    """

    def __init__(self, driver: BrowserDriver, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        if driver is None:  # pragma: no cover - programmer error
            raise BrowserError("BrowserSession needs a driver")
        self._driver = driver
        self._timeout_ms = timeout_ms
        self._lock = threading.RLock()
        self._closed = False

    # -- lifecycle --------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Always close: a crashed turn must not leave an orphan chromium
        # holding the profile lock, because the *next* launch would then exit
        # having done nothing and look like success.
        self.close()

    def close(self) -> None:
        """Close the browser. Safe to call more than once (idempotent)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._driver.close()
            except Exception as exc:  # pragma: no cover - teardown is best effort
                _log(f"browser: close() reported {exc!r}", "senses")
            _log("browser: closed", "senses")

    def _ensure_open(self) -> None:
        if self._closed:
            raise BrowserError(
                "this browser session is already closed; open a new one with get_session()"
            )

    def _timeout(self, override: Optional[int]) -> int:
        return int(override) if override else self._timeout_ms

    # -- actions ----------------------------------------------------------

    def open(self, url: str, *, timeout_ms: Optional[int] = None,
             allow_empty: bool = False) -> PageRead:
        """Navigate to ``url`` and report what is there.

        Raises :class:`NavigationTimeoutError` if the page never loads,
        :class:`NavigationError` for a hard failure (bad host, refused
        connection), :class:`EmptyPageError` if it loads with no text.
        """
        target = (url or "").strip()
        if not target:
            raise NavigationError("open() needs a URL")
        if not _has_explicit_scheme(target):
            target = "https://" + target
        timeout = self._timeout(timeout_ms)
        with self._lock:
            self._ensure_open()
            _log(f"browser: open {target}", "senses")
            try:
                self._driver.navigate(target, timeout)
            except _ElementNotFound as exc:  # pragma: no cover - not raised by navigate
                raise NavigationError(str(exc)) from exc
            except Exception as exc:
                if _is_timeout(exc):
                    raise NavigationTimeoutError(
                        f"the page at {target} did not finish loading within "
                        f"{timeout} ms",
                        fix="raise the timeout, or open a lighter page",
                    ) from exc
                raise NavigationError(
                    f"could not open {target}: {exc}",
                    fix="check the address and that this machine has network access",
                ) from exc
            return self._read_locked(target, allow_empty=allow_empty)

    def read(self, *, allow_empty: bool = False) -> PageRead:
        """Report the visible text, title, URL and frame text of the page.

        :class:`EmptyPageError` when there is nothing readable, unless
        ``allow_empty`` asks for the raw answer.
        """
        with self._lock:
            self._ensure_open()
            return self._read_locked(None, allow_empty=allow_empty)

    def _read_locked(self, url_if_known: Optional[str], *, allow_empty: bool) -> PageRead:
        url = self._safe(self._driver.get_url, "")
        title = self._safe(self._driver.get_title, "")
        text = self._safe(self._driver.get_body_text, "") or ""
        frames = tuple(
            frame.strip() for frame in (self._safe(self._driver.get_frame_texts, []) or [])
            if frame and frame.strip()
        )
        links = self._safe(self._driver.get_link_count, 0) or 0
        page = PageRead(
            url=url or (url_if_known or ""),
            title=title,
            text=text,
            frames=frames,
            char_count=len(text),
            word_count=len(text.split()),
            frame_count=len(frames),
            link_count=int(links),
        )
        _log(
            f"browser: read {page.url!r} title={page.title!r} "
            f"chars={page.char_count} frames={page.frame_count} links={page.link_count}",
            "senses",
        )
        if page.is_empty and not allow_empty:
            raise EmptyPageError(
                f"the page at {page.url or url_if_known or 'about:blank'} rendered but "
                "has no visible text (it may be a canvas application, a PDF viewer, "
                "or still rendering)",
                fix="wait and read again, or pass allow_empty=True to inspect it",
            )
        return page

    def find(self, phrase: str, *, context_chars: int = 80) -> FindResult:
        """Say whether ``phrase`` is on the page, and roughly where.

        A miss is an answer, not an error: ``found`` is False and ``detail``
        explains. "Where" is the phrase's position in the document text
        (top/middle/bottom), plus the embedded frame index when it only appears
        inside a frame -- cheap, and honest about being approximate.
        """
        needle = (phrase or "").strip()
        if not needle:
            raise BrowserError("find() needs a non-empty phrase to look for")
        with self._lock:
            self._ensure_open()
            text = self._safe(self._driver.get_body_text, "") or ""
            frames = [f for f in (self._safe(self._driver.get_frame_texts, []) or []) if f]

            lowered = needle.lower()
            main_count = text.lower().count(lowered)
            if main_count:
                offset = text.lower().find(lowered)
                result = FindResult(
                    phrase=needle,
                    found=True,
                    count=main_count,
                    region=_describe_region(offset, len(text)),
                    in_frame=None,
                    snippet=_snippet(text, offset, len(needle), context_chars),
                    detail=(
                        f"Yes -- {needle!r} appears {main_count} time"
                        f"{'s' if main_count != 1 else ''} on the page, "
                        f"{_describe_region(offset, len(text))}."
                    ),
                )
            else:
                result = FindResult(
                    phrase=needle,
                    found=False,
                    detail=f"No -- {needle!r} is not on the current page.",
                )
                for index, frame_text in enumerate(frames, start=1):
                    frame_count = frame_text.lower().count(lowered)
                    if not frame_count:
                        continue
                    offset = frame_text.lower().find(lowered)
                    result = FindResult(
                        phrase=needle,
                        found=True,
                        count=frame_count,
                        region=_describe_region(offset, len(frame_text)),
                        in_frame=index,
                        snippet=_snippet(frame_text, offset, len(needle), context_chars),
                        detail=(
                            f"Yes -- {needle!r} appears {frame_count} time"
                            f"{'s' if frame_count != 1 else ''} inside embedded frame "
                            f"{index}, {_describe_region(offset, len(frame_text))}."
                        ),
                    )
                    break
            _log(
                f"browser: find {needle!r} -> found={result.found} count={result.count} "
                f"region={result.region or '-'}",
                "senses",
            )
            return result

    def click(self, target: str, *, by: str = "auto",
              timeout_ms: Optional[int] = None) -> ActionResult:
        """Click a selector or a piece of visible text.

        ``by`` is ``"auto"`` (guess), ``"selector"`` or ``"text"``. A target
        that is not there raises :class:`SelectorNotFoundError`; one that is
        there but will not respond in time raises :class:`ActionTimeoutError`.
        """
        wanted = (target or "").strip()
        if not wanted:
            raise BrowserError("click() needs a selector or some visible text")
        mode = by
        if mode not in ("auto", "selector", "text"):
            raise BrowserError(f"click(by=...) must be 'auto', 'selector' or 'text', not {by!r}")
        if mode == "auto":
            mode = "selector" if looks_like_selector(wanted) else "text"
        timeout = self._timeout(timeout_ms)
        with self._lock:
            self._ensure_open()
            _log(f"browser: click ({mode}) {wanted!r}", "senses")
            try:
                if mode == "selector":
                    self._driver.click_selector(wanted, timeout)
                else:
                    self._driver.click_text(wanted, timeout)
            except _ElementNotFound as exc:
                raise SelectorNotFoundError(_not_found_message(mode, wanted, exc)) from exc
            except Exception as exc:
                if _is_timeout(exc):
                    raise ActionTimeoutError(
                        f"the {mode} {wanted!r} is on the page but did not become "
                        f"clickable within {timeout} ms",
                        fix="it may be covered by an overlay or still animating; "
                            "scroll to it or retry",
                    ) from exc
                raise BrowserError(f"clicking the {mode} {wanted!r} failed: {exc}") from exc
            return ActionResult(
                action="click",
                target=wanted,
                detail=f"clicked the {mode} {wanted!r}",
            )

    def fill(self, selector: str, value: str, *, timeout_ms: Optional[int] = None) -> ActionResult:
        """Type ``value`` into ``selector``.

        HARD RULE: a value that is a secret is never logged and never returned.
        The decision is made DOM-first -- :meth:`BrowserDriver.field_type` is
        asked what the field IS, and a ``password`` field is a secret whatever
        the selector says and whatever the value looks like. When the type
        cannot be determined the value is treated as a secret unless the field
        was positively identified as harmless (see :func:`_judge_fill_secret`).

        Both the returned :class:`ActionResult` and the log line are built from
        the single redacted rendering, so neither can carry the raw value.
        """
        field_selector = (selector or "").strip()
        if not field_selector:
            raise BrowserError("fill() needs a selector for the input")
        raw = "" if value is None else str(value)
        timeout = self._timeout(timeout_ms)
        with self._lock:
            self._ensure_open()
            # (1) + (2): ask the DOM, then fail closed on anything unproven.
            dom_type = _dom_field_type(self._driver, field_selector)
            secret, reason = _judge_fill_secret(field_selector, raw, dom_type)
            shown = redact_value(raw, secret=secret)
            kind = dom_type or "-"
            # The logged line is built ONLY from `shown` and the safe labels;
            # `raw` never touches it.
            _log(
                f"browser: fill {field_selector!r} with {shown!r} "
                f"(redacted={'yes' if secret else 'no'}, why={reason}, "
                f"field_type={kind}, chars={len(raw)})",
                "senses",
            )
            try:
                self._driver.fill_field(field_selector, raw, timeout)
            except _ElementNotFound as exc:
                raise SelectorNotFoundError(
                    _not_found_message("selector", field_selector, exc)
                ) from exc
            except Exception as exc:
                if _is_timeout(exc):
                    raise ActionTimeoutError(
                        f"the input {field_selector!r} is on the page but did not "
                        f"accept text within {timeout} ms",
                        fix="it may be hidden or disabled; check the selector",
                    ) from exc
                raise BrowserError(f"filling {field_selector!r} failed: {exc}") from exc
            return ActionResult(
                action="fill",
                target=field_selector,
                detail=f"filled {field_selector!r} with {shown!r}",
                value_redacted=shown,
                value_len=len(raw),
                secret=secret,
            )

    def screenshot(self, path: str) -> ActionResult:
        """Write a full-page screenshot to ``path`` (parent dirs created)."""
        if not path:
            raise BrowserError("screenshot() needs a path to write to")
        destination = Path(path).expanduser()
        if destination.is_dir():
            raise BrowserError(f"screenshot() needs a file path, but {destination} is a directory")
        with self._lock:
            self._ensure_open()
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise BrowserError(
                    f"could not create the screenshot directory {destination.parent}: {exc}"
                ) from exc
            target = str(destination)
            try:
                self._driver.save_screenshot(target)
            except Exception as exc:
                if _is_timeout(exc):
                    raise ActionTimeoutError(
                        f"the screenshot of {self._safe(self._driver.get_url, 'the page')!r} "
                        f"did not finish within {self._timeout_ms} ms"
                    ) from exc
                raise BrowserError(f"could not write the screenshot to {target}: {exc}") from exc
            _log(f"browser: screenshot -> {target}", "senses")
            return ActionResult(action="screenshot", target=target,
                                detail=f"wrote a screenshot to {target}", path=target)

    def back(self, *, timeout_ms: Optional[int] = None) -> PageRead:
        """Go back one page and report what is there now."""
        timeout = self._timeout(timeout_ms)
        with self._lock:
            self._ensure_open()
            try:
                moved = self._driver.go_back(timeout)
            except Exception as exc:
                if _is_timeout(exc):
                    raise NavigationTimeoutError(
                        f"going back did not finish within {timeout} ms",
                        fix="retry, or open the page directly",
                    ) from exc
                raise NavigationError(f"could not go back: {exc}") from exc
            if not moved:
                raise NavigationError(
                    "there is no earlier page in this session's history to go back to",
                    fix="open a page first",
                )
            return self._read_locked(None, allow_empty=True)

    # -- internals --------------------------------------------------------

    def _safe(self, call: Callable[..., Any], default: Any) -> Any:
        """Call a driver reader, degrading to ``default`` rather than failing.

        read() reports what it can see; one unreadable field (a detached
        frame, a page mid-navigation) must not lose the whole reading.
        """
        try:
            return call()
        except Exception as exc:
            _log(f"browser: {getattr(call, '__name__', 'reader')} failed: {exc!r}", "senses")
            return default


def _not_found_message(mode: str, wanted: str, exc: BaseException) -> str:
    return (
        f"no element on the page matches the {mode} {wanted!r} "
        f"(the page may not have loaded that part yet, or it may be inside a "
        f"frame): {exc}"
    )


def _snippet(text: str, offset: int, length: int, context_chars: int) -> str:
    if offset < 0:
        return ""
    start = max(0, offset - context_chars)
    end = min(len(text), offset + length + context_chars)
    fragment = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + fragment + ("…" if end < len(text) else "")


# --------------------------------------------------------------------------
# Process-wide single session
# --------------------------------------------------------------------------

# ONE browser, ONE profile, ONE ordering. Chromium holds an exclusive
# SingletonLock on its user-data directory: a second browser pointed at it can
# exit immediately having done nothing, which looks exactly like success. So
# the session is created lazily, once, behind a module lock, and this is the
# only supported way to get one.
_SESSION: Optional[BrowserSession] = None
_SESSION_LOCK = threading.RLock()


def get_session(*, headless: bool = True, timeout_ms: Optional[int] = None,
                profile_dir: Optional[str] = None) -> BrowserSession:
    """Return the process-wide browser session, creating it on first use.

    Raises :class:`BrowserUnavailableError` (carrying
    ``playwright install chromium``) when chromium is not installed.
    """
    global _SESSION
    timeout = int(timeout_ms) if timeout_ms else DEFAULT_TIMEOUT_MS
    with _SESSION_LOCK:
        if _SESSION is not None and not _SESSION.closed:
            return _SESSION
        driver = _launch_driver(headless=headless, timeout_ms=timeout, profile_dir=profile_dir)
        _SESSION = BrowserSession(driver, timeout_ms=timeout)
        return _SESSION


def session_is_open() -> bool:
    """True when this process already owns a live browser."""
    with _SESSION_LOCK:
        return _SESSION is not None and not _SESSION.closed


def close_session() -> None:
    """Close and forget the process-wide session. Idempotent."""
    global _SESSION
    with _SESSION_LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.close()


def reset_session() -> None:
    """Alias for :func:`close_session`, named for test fixtures."""
    close_session()
