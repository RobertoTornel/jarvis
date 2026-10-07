"""Claire's senses exposed as tools: screen text, camera, browser.

Spec: ``senses.spec.md`` (this directory).

Three small tools sit on top of the ``claire.senses`` modules. They exist for
one reason: what comes back has to be a sentence Claire can *say out loud*.

The senses modules report honestly and in detail — named errors, capture
mechanisms, OCR verdicts, install commands. Almost none of that belongs in a
spoken answer, so these tools are a translation layer with three rules:

* **A missing capability is not an error to shout about.** No OCR engine and no
  camera are expected states on this machine. They come back as a calm,
  reported failure (``success=False``) whose ``reply_text`` names the fix — the
  exact command from the module's own ``diagnose()`` / availability verdict.
  The user hears "I cannot, and here is what would fix it", never a stack trace.
* **Nothing raises out of** :meth:`Tool.execute`. Every expected condition is
  caught and reported in the :class:`ToolExecutionResult`; a last-resort guard
  converts even an unforeseen ``Exception`` into a calm reply.
* **A typed value is never echoed.** :class:`BrowseWebTool` never puts the text
  the user typed into a field into its reply, its error message or a log line,
  and it scrubs the value out of anything a lower layer hands back. Password
  fields are recognised either by the browser module's own ``secret_field``
  helper or by a local fallback heuristic.

Every reply is collapsed to one spoken line (``MAX_SPOKEN_CHARS``) with no
markdown, no traceback and no raw diagnostic dump.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

__all__ = [
    "BrowseWebTool",
    "LookThroughCameraTool",
    "MAX_SPOKEN_CHARS",
    "ReadScreenTool",
]

#: Longest run of page/screen text this tool will hand back for speech.
#: Speech is linear: a wall of text read aloud is worse than useless.
MAX_SPOKEN_CHARS = 1200

#: A short reason or fix line, long enough to be actionable.
MAX_REASON_CHARS = 240

#: Values shorter than this are not scrubbed from error text: replacing a
#: three-character needle mangles every unrelated word containing it.
_MIN_SCRUB_CHARS = 3

#: Field names that mark a value as a credential. Used only when the browser
#: module's own ``secret_field`` helper is unavailable.
_SECRET_FIELD_RE = re.compile(
    r"pass|passwd|secret|token|otp|pin\b|cvv|card|iban|auth|api[-_]?key|credential",
    re.IGNORECASE,
)

#: Traceback fragments must never appear in a spoken reply.
_TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "')


# --------------------------------------------------------------------------- #
# Spoken-text helpers
# --------------------------------------------------------------------------- #

def _spoken_text(text: Any, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Collapse any text to one spoken paragraph, no markdown, no newlines."""
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    if not collapsed:
        return ""
    for marker in _TRACEBACK_MARKERS:
        if marker in collapsed:
            collapsed = collapsed.split(marker)[0].strip()
    if len(collapsed) <= limit:
        return collapsed
    clipped = collapsed[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{clipped}; there is more after that."


def _first_sentence(text: Any, limit: int = MAX_REASON_CHARS) -> str:
    """The first sentence of a diagnostic message, which is the actionable one."""
    collapsed = _spoken_text(text, limit=10000)
    if not collapsed:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", collapsed, maxsplit=1)
    return _spoken_text(parts[0], limit=limit)


def _scrub(text: Any, *secrets: Optional[str]) -> str:
    """Replace any occurrence of a typed value with ``[redacted]``."""
    cleaned = str(text or "")
    for secret in secrets:
        if not secret or len(secret) < _MIN_SCRUB_CHARS:
            continue
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def _failure(reply: str, error: Optional[str] = None) -> ToolExecutionResult:
    """A reported failure: calm spoken reply, engine detail only in the error."""
    return ToolExecutionResult(
        success=False,
        reply_text=_spoken_text(reply) or "I could not do that on this machine.",
        error_message=_spoken_text(error, limit=MAX_REASON_CHARS) or None,
    )


def _success(reply: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=True, reply_text=_spoken_text(reply), error_message=None)


def _note(context: Any, message: str) -> None:
    """Optional progress line for the UI. Never allowed to fail the tool."""
    printer = getattr(context, "user_print", None)
    if callable(printer):
        try:
            printer(message)
        except Exception:  # pragma: no cover - a broken printer is not the tool's problem
            pass


def _screen_module():
    from ...senses import screen as screen_sense

    return screen_sense


def _camera_module():
    from ...senses import camera as camera_sense

    return camera_sense


def _browser_module():
    from ...senses import browser as browser_sense

    return browser_sense


def _is_secret_field(selector: str) -> bool:
    """True when a field looks like a credential.

    The browser module's own ``secret_field`` rule is consulted first and a
    local name heuristic is OR-ed with it: either rule saying "this is a
    credential" is enough, because over-redacting costs a sentence and
    under-redacting costs the user's password.
    """
    if _SECRET_FIELD_RE.search(selector or ""):
        return True
    try:
        helper = getattr(_browser_module(), "secret_field", None)
        if callable(helper):
            return bool(helper(selector))
    except Exception:
        pass
    return False


def _looks_like_secret_value(value: str) -> bool:
    try:
        helper = getattr(_browser_module(), "looks_like_secret", None)
        if callable(helper):
            return bool(helper(value))
    except Exception:
        pass
    return False


class _SenseTool(Tool):
    """Shared plumbing for the sense tools: nothing escapes :meth:`run`."""

    #: Name used in the last-resort guard's message and log line.
    @property
    def name(self) -> str:  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        try:
            return self._run(args or {}, context)
        except Exception as exc:  # last-resort guard: report, never raise
            debug_log(
                f"{self.name}: unexpected {type(exc).__name__} ({exc})",
                "tools",
            )
            return _failure(
                "I could not finish that: something went wrong inside the tool, so I "
                "stopped rather than guessing. Nothing was changed.",
                f"{type(exc).__name__}: {exc}",
            )

    def _run(self, args: Dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        raise NotImplementedError  # pragma: no cover - overridden everywhere


# --------------------------------------------------------------------------- #
# Screen
# --------------------------------------------------------------------------- #

class ReadScreenTool(_SenseTool):
    """Read the words on the screen, or find where a phrase appears on it."""

    def __init__(
        self,
        read_screen: Optional[Callable[..., Any]] = None,
        find_phrase_on_screen: Optional[Callable[..., Any]] = None,
        diagnose: Optional[Callable[..., Any]] = None,
    ) -> None:
        # Injection seams: tests pass fakes, the registry passes nothing.
        self._read_screen = read_screen
        self._find_phrase_on_screen = find_phrase_on_screen
        self._diagnose = diagnose

    @property
    def name(self) -> str:
        return "readScreen"

    @property
    def description(self) -> str:
        return (
            "Look at the screen: read the words that are on it now, or find where a "
            "phrase appears. Use for 'what is on my screen?', 'read this to me' or "
            "'where is the Save button?'. It only looks; it never clicks or types."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["read", "find"],
                    "description": (
                        "read (the default) reports the words on the screen; find "
                        "locates one phrase on it."
                    ),
                },
                "phrase": {
                    "type": "string",
                    "description": "The phrase to locate. Required when mode is 'find'.",
                },
            },
            "required": [],
        }

    def _run(self, args: Dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        mode = str(args.get("mode") or "read").strip().lower()
        phrase = str(args.get("phrase") or "").strip()
        if mode not in ("read", "find"):
            return _failure(
                "I can either read what is on your screen or look for one phrase on "
                "it; tell me which."
            )
        if mode == "find" and not phrase:
            return _failure("Tell me the word or phrase to look for on your screen.")

        try:
            screen = _screen_module()
        except Exception as exc:
            return _failure(
                "I cannot reach my screen-reading module right now, so I cannot look "
                "at your screen.",
                f"{type(exc).__name__}: {exc}",
            )

        verdict = self._ocr_verdict(screen)
        if verdict is not None and not getattr(verdict, "available", True):
            return _failure(
                self._missing_ocr_reply(verdict),
                getattr(verdict, "reason", "") or "OCR engine unavailable",
            )

        _note(
            context,
            f"Looking for {phrase!r} on the screen…" if mode == "find" else "Reading the screen…",
        )
        try:
            if mode == "find":
                finder = self._find_phrase_on_screen or screen.find_phrase_on_screen
                return self._found_reply(phrase, finder(phrase))
            reader = self._read_screen or screen.read_screen
            return self._read_reply(reader())
        except screen.ScreenError as exc:
            return _failure(self._screen_error_reply(exc), f"{type(exc).__name__}: {exc}")

    def _ocr_verdict(self, screen: Any) -> Any:
        checker = self._diagnose or getattr(screen, "diagnose", None)
        if not callable(checker):
            return None
        try:
            return checker()
        except Exception:
            # The verdict is advisory here: read_screen() still raises a named
            # error, so a failing check must not stop the attempt.
            return None

    @staticmethod
    def _missing_ocr_reply(verdict: Any) -> str:
        fix = _spoken_text(getattr(verdict, "fix_command", ""), limit=160)
        if fix:
            return (
                "I cannot read your screen yet, because the OCR engine is not "
                f"installed. Run exactly: {fix}. Once that is installed I can read "
                "the screen straight away."
            )
        return (
            "I cannot read your screen yet, because the OCR engine is not installed. "
            "Install tesseract and I will be able to read the screen."
        )

    @staticmethod
    def _screen_error_reply(exc: Exception) -> str:
        fixes = getattr(exc, "fixes", None) or ()
        command = getattr(exc, "command", "")
        kind = type(exc).__name__
        fix = _first_sentence(fixes[0]) if fixes else _spoken_text(command, limit=160)

        if "OcrEngineMissing" in kind:
            # The expected state on a machine without tesseract: a capability
            # report with the exact install command, not an alarm.
            reply = "I cannot read your screen yet, because the OCR engine is not installed."
            if fix:
                return f"{reply} Run exactly: {fix}, and then I can read the screen."
            return f"{reply} Install tesseract and I will be able to read the screen."
        if "OcrModuleMissing" in kind:
            reply = "I cannot read your screen yet, because the OCR helper is not installed."
            if fix:
                return f"{reply} Run exactly: {fix}, and then I can read the screen."
            return reply
        if "Capture" in kind or "captur" in _spoken_text(exc).lower():
            reply = "I could not capture your screen, so I have nothing to read."
        else:
            reply = "I could not read your screen."
        if fix:
            reply += f" What would fix it: {fix}"
        return reply

    @staticmethod
    def _read_reply(result: Any) -> ToolExecutionResult:
        text = _spoken_text(getattr(result, "text", ""))
        if text:
            return _success(f"Here is what is on your screen: {text}")
        return _success(
            "I looked at your screen, but there are no readable words on it. It may "
            "be blank, or show a picture rather than text."
        )

    @staticmethod
    def _found_reply(phrase: str, location: Any) -> ToolExecutionResult:
        describe = getattr(location, "describe", None)
        spoken = _spoken_text(describe() if callable(describe) else getattr(location, "detail", ""))
        if getattr(location, "found", False):
            reply = f'Yes, I can see "{phrase}" on your screen.'
            if spoken:
                reply += f" {spoken}"
            return _success(reply)
        return _success(f'No, I cannot see "{phrase}" on your screen right now.')


# --------------------------------------------------------------------------- #
# Camera
# --------------------------------------------------------------------------- #

class LookThroughCameraTool(_SenseTool):
    """Look through the camera and describe what is in view."""

    def __init__(
        self,
        describe_availability: Optional[Callable[..., Any]] = None,
        capture_frame: Optional[Callable[..., Any]] = None,
        describe_image: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._describe_availability = describe_availability
        self._capture_frame = capture_frame
        self._describe_image = describe_image

    @property
    def name(self) -> str:
        return "lookThroughCamera"

    @property
    def description(self) -> str:
        return (
            "Look through the camera and say what is in view: use for 'can you see "
            "me?', 'what am I doing?' or 'what is in front of me?'. If this machine "
            "has no camera it says so plainly and suggests what to do instead."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "What to ask about the picture, e.g. 'is my form right?'. "
                        "Optional; a general description is given without it."
                    ),
                },
            },
            "required": [],
        }

    def _run(self, args: Dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        question = str(args.get("question") or "").strip()
        try:
            camera = _camera_module()
        except Exception as exc:
            return _failure(
                "I cannot reach my camera module right now, so I cannot look.",
                f"{type(exc).__name__}: {exc}",
            )

        try:
            availability = self._availability(camera)
        except camera.CameraError as exc:
            return _failure(self._availability_error_reply(exc), f"{type(exc).__name__}: {exc}")

        if not getattr(availability, "available", False):
            return _failure(self._no_camera_reply(camera, availability), "no camera detected")

        _note(context, "Looking through the camera…")
        try:
            path = self._capture(camera)()
        except camera.CameraNotFoundError as exc:
            return _failure(self._no_camera_reply(camera, availability), f"{type(exc).__name__}: {exc}")
        except camera.CameraError as exc:
            return _failure(self._camera_error_reply(exc), f"{type(exc).__name__}: {exc}")

        try:
            description = self._describe(camera)(path, question)
        except camera.CameraError as exc:
            return _failure(self._describe_error_reply(exc), f"{type(exc).__name__}: {exc}")

        if getattr(description, "seen", False):
            spoken = getattr(description, "spoken_summary", None)
            text = spoken() if callable(spoken) else getattr(description, "description", "")
            return _success(text or "I can see you.")
        return _failure(
            self._not_seen_reply(description),
            _spoken_text(getattr(description, "error", "") or "vision did not return a description"),
        )

    def _availability(self, camera: Any) -> Any:
        checker = self._describe_availability or camera.describe_availability
        return checker()

    def _capture(self, camera: Any) -> Callable[[], Any]:
        return self._capture_frame or camera.capture_frame

    def _describe(self, camera: Any) -> Callable[..., Any]:
        return self._describe_image or camera.describe_image

    @staticmethod
    def _no_camera_reply(camera: Any, availability: Any) -> str:
        spoken = getattr(availability, "spoken_summary", None)
        reply = (
            spoken()
            if callable(spoken)
            else "I do not have a camera on this machine, so I cannot take a picture. "
            "You can plug in a USB webcam, or send me a photo from your phone."
        )
        actions = getattr(availability, "actions", None) or getattr(camera, "NO_CAMERA_FIXES", ())
        if actions:
            first = _first_sentence(actions[0], limit=180)
            if first and first.lower() not in reply.lower():
                reply += f" If you want it here: {first}"
        return reply

    @staticmethod
    def _availability_error_reply(exc: Exception) -> str:
        fixes = getattr(exc, "fixes", None) or ()
        fix = _first_sentence(fixes[0], limit=180) if fixes else ""
        reply = "I could not check whether this machine has a camera, so I will not guess."
        if fix:
            reply += f" What would fix it: {fix}"
        return reply

    @staticmethod
    def _camera_error_reply(exc: Exception) -> str:
        fixes = getattr(exc, "fixes", None) or ()
        fix = _first_sentence(fixes[0], limit=180) if fixes else ""
        reply = "I have a camera, but I could not get a picture from it."
        if fix:
            reply += f" What would fix it: {fix}"
        return reply

    @staticmethod
    def _describe_error_reply(exc: Exception) -> str:
        remedy = getattr(exc, "remedy", "")
        reply = (
            "I took a picture, but the vision model is not reachable, so I will not "
            "guess what was in it."
        )
        if remedy:
            reply += f" What would fix it: {_first_sentence(remedy, limit=200)}"
        return reply

    @staticmethod
    def _not_seen_reply(description: Any) -> str:
        remedy = getattr(description, "remedy", "")
        reply = "I took a picture, but I could not describe it, so I will not make one up."
        if remedy:
            reply += f" What would fix it: {_first_sentence(remedy, limit=200)}"
        return reply


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #

#: Every action the browser tool accepts. Anything else is a calm refusal.
_BROWSER_ACTIONS = ("open", "read", "find", "click", "fill", "screenshot", "back", "close")

#: How each action is named when something goes wrong.
_BROWSER_VERBS = {
    "open": "open that page",
    "read": "read that page",
    "find": "look for that on the page",
    "click": "click that",
    "fill": "type that in",
    "screenshot": "take a screenshot of the page",
    "back": "go back a page",
    "close": "close the browser",
}


class BrowseWebTool(_SenseTool):
    """Drive the real browser: open a page, read it, click, type, screenshot."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        available_fn: Optional[Callable[[], bool]] = None,
        close_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._available_fn = available_fn
        self._close_fn = close_fn

    @property
    def name(self) -> str:
        return "browseWeb"

    @property
    def description(self) -> str:
        return (
            "Use the real web browser on this machine: open a page, read the page "
            "that is open, find a phrase on it, click something, type into a field, "
            "screenshot it, go back, or close it. Text the user types into a field is "
            "never repeated back."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_BROWSER_ACTIONS),
                    "description": (
                        "open a URL, read the current page, find a phrase, click a "
                        "selector or text, fill a field, screenshot, back, or close."
                    ),
                },
                "url": {"type": "string", "description": "Page to open (action 'open')."},
                "phrase": {"type": "string", "description": "Phrase to look for (action 'find')."},
                "target": {
                    "type": "string",
                    "description": "CSS selector or visible text to click (action 'click').",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the field to type into (action 'fill').",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "Text to type into the field (action 'fill'). It is never "
                        "repeated back, so passwords are safe to pass here."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "File to write the screenshot to (action 'screenshot').",
                },
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        """Guard the last-resort path with the credential rule.

        The value is re-read from ``args`` here (no shared state) so that even
        the unexpected-exception guard cannot carry the user's typed text into
        a reply, an error field or a log line.
        """
        typed_value = str((args or {}).get("value") or "")
        try:
            return self._run(args or {}, context)
        except Exception as exc:
            debug_log(f"{self.name}: unexpected {type(exc).__name__}", "tools")
            return _failure(
                "I could not finish that: something went wrong inside the browser, "
                "so I stopped rather than guessing. Nothing was changed.",
                _scrub(f"{type(exc).__name__}: {exc}", typed_value),
            )

    def _run(self, args: Dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        action = str(args.get("action") or "").strip().lower()
        if action not in _BROWSER_ACTIONS:
            return _failure(
                "Tell me what to do in the browser: open a page, read it, find "
                "something on it, click, type into a field, screenshot, go back, or close."
            )

        # The typed value is held in one local and never logged or echoed.
        typed_value = str(args.get("value") or "")

        try:
            browser = _browser_module()
        except Exception as exc:
            return _failure(
                "I cannot reach my browser module right now, so I cannot use the web.",
                _scrub(f"{type(exc).__name__}: {exc}", typed_value),
            )

        if action == "close":
            try:
                closer = self._close_fn or browser.close_session
                closer()
            except Exception as exc:
                return _failure(
                    "I could not close the browser cleanly.",
                    _scrub(f"{type(exc).__name__}: {exc}", typed_value),
                )
            return _success("I closed the browser.")

        try:
            available = self._available_fn or browser.browser_available
            if not available():
                return _failure(
                    self._missing_browser_reply(browser),
                    "chromium is not installed",
                )
        except Exception as exc:
            return _failure(
                self._missing_browser_reply(browser),
                _scrub(f"{type(exc).__name__}: {exc}", typed_value),
            )

        try:
            factory = self._session_factory or browser.get_session
            session = factory()
        except browser.BrowserError as exc:
            return _failure(
                _scrub(
                    f"I could not start the browser. {_first_sentence(exc, limit=200)}",
                    typed_value,
                ),
                _scrub(f"{type(exc).__name__}: {exc}", typed_value),
            )

        verb = _BROWSER_VERBS.get(action, "do that")
        try:
            return self._dispatch(browser, session, action, args, typed_value)
        except browser.BrowserError as exc:
            return _failure(
                _scrub(f"I could not {verb}: {_first_sentence(exc, limit=200)}", typed_value),
                _scrub(f"{type(exc).__name__}: {exc}", typed_value),
            )
        except Exception as exc:  # unexpected: calm reply, detail in the error field
            return _failure(
                f"I could not {verb}: something went wrong inside the browser.",
                _scrub(f"{type(exc).__name__}: {exc}", typed_value),
            )

    def _dispatch(
        self,
        browser: Any,
        session: Any,
        action: str,
        args: Dict[str, Any],
        typed_value: str,
    ) -> ToolExecutionResult:
        if action == "open":
            url = str(args.get("url") or "").strip()
            if not url:
                return _failure("Which page should I open? Give me a web address.")
            page = session.open(url)
            return _success(self._page_reply("I opened", page, with_text=False))

        if action == "read":
            page = session.read()
            return _success(self._page_reply("Here is the page", page, with_text=True))

        if action == "find":
            phrase = str(args.get("phrase") or "").strip()
            if not phrase:
                return _failure("Tell me the word or phrase to look for on the page.")
            found = session.find(phrase)
            detail = _spoken_text(getattr(found, "detail", ""), limit=300)
            if detail:
                return _success(detail)
            if getattr(found, "found", False):
                return _success(f'Yes, "{phrase}" is on the page.')
            return _success(f'No, "{phrase}" is not on the page.')

        if action == "click":
            target = str(args.get("target") or args.get("selector") or "").strip()
            if not target:
                return _failure("What should I click? Give me a CSS selector or the visible text.")
            session.click(target)
            return _success(f"I clicked {target}.")

        if action == "fill":
            selector = str(args.get("selector") or args.get("target") or "").strip()
            if not selector:
                return _failure("Which field should I type into? Give me its CSS selector.")
            if "value" not in args or args.get("value") is None:
                return _failure("Tell me what to type into that field.")
            secret = _is_secret_field(selector) or _looks_like_secret_value(typed_value)
            result = session.fill(selector, typed_value)
            # The value itself is deliberately absent from every branch below.
            if secret or getattr(result, "secret", False):
                return _success(
                    f"I typed it into the {selector} field. I will not read a "
                    "password back out loud."
                )
            return _success(f"I typed your text into the {selector} field.")

        if action == "screenshot":
            path = str(args.get("path") or "").strip() or self._default_screenshot_path()
            result = session.screenshot(path)
            written = getattr(result, "path", None) or path
            return _success(f"I saved a screenshot of the page to {written}.")

        if action == "back":
            page = session.back()
            return _success(self._page_reply("I went back one page; now on", page, with_text=False))

        return _failure("Tell me what to do in the browser.")  # pragma: no cover - guarded above

    @staticmethod
    def _page_reply(lead: str, page: Any, *, with_text: bool) -> str:
        title = _spoken_text(getattr(page, "title", ""), limit=120)
        url = _spoken_text(getattr(page, "url", ""), limit=160)
        named = title or url or "the page"
        reply = f"{lead} {named}."
        if url and title:
            reply = f"{lead} {named} ({url})."
        if not with_text:
            if getattr(page, "is_empty", False):
                reply += " It has no visible text."
            return reply
        text = _spoken_text(getattr(page, "text", ""))
        if text:
            reply += f" It says: {text}"
        else:
            reply += " It has no visible text."
        return reply

    @staticmethod
    def _missing_browser_reply(browser: Any) -> str:
        command = _spoken_text(
            getattr(browser, "CHROMIUM_INSTALL_COMMAND", "playwright install chromium"),
            limit=160,
        )
        return (
            "I cannot open a web page yet, because the browser is not installed on "
            f"this machine. Run exactly: {command}, and then I will be able to browse."
        )

    @staticmethod
    def _default_screenshot_path() -> str:
        directory = Path(tempfile.mkdtemp(prefix="claire_page_"))
        return str(directory / "page.png")
