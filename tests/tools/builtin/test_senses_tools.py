"""Tests for the senses tools: readScreen, lookThroughCamera, browseWeb.

Every test uses an injected fake — no real screen capture, no camera, no
network and no real browser. The fakes are the same seams the registry leaves
empty, so a fake that is accepted here is the shape the real module must have.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.claire.senses import browser as browser_sense
from src.claire.senses import camera as camera_sense
from src.claire.senses import screen as screen_sense
from src.claire.tools import registry
from src.claire.tools import builtin as builtin_package
from src.claire.tools.base import Tool, ToolContext
from src.claire.tools.builtin.senses import (
    MAX_SPOKEN_CHARS,
    BrowseWebTool,
    LookThroughCameraTool,
    ReadScreenTool,
)
from src.claire.tools.types import ToolExecutionResult

pytestmark = pytest.mark.unit

SENSES_TOOLS = {
    "readScreen": ReadScreenTool,
    "lookThroughCamera": LookThroughCameraTool,
    "browseWeb": BrowseWebTool,
}


# --------------------------------------------------------------------------- #
# Fixtures and fakes
# --------------------------------------------------------------------------- #

def _context() -> ToolContext:
    return ToolContext(
        db=None,
        cfg=None,
        system_prompt="",
        original_prompt="",
        redacted_text="",
        max_retries=0,
        user_print=lambda _message: None,
    )


def _execute(tool: Tool, args) -> ToolExecutionResult:
    """Call the public execute() path — the one that must never raise."""
    return tool.execute(
        None,
        None,
        args,
        "",
        "",
        "",
        0,
        lambda _message: None,
    )


def _available_verdict() -> SimpleNamespace:
    return SimpleNamespace(
        available=True,
        fix_command="sudo apt-get install -y tesseract-ocr",
        reason="tesseract is installed",
    )


def _missing_verdict() -> SimpleNamespace:
    return SimpleNamespace(
        available=False,
        fix_command="sudo apt-get install -y tesseract-ocr",
        reason="OCR engine missing: the 'tesseract' binary is not installed",
    )


class _FakeLocation:
    """Stand-in for screen.PhraseLocation with its spoken describe()."""

    def __init__(self, found: bool, phrase: str):
        self.found = found
        self.phrase = phrase
        self.line_number = 4
        self.line_text = "Save changes before closing"

    def describe(self) -> str:
        if not self.found:
            return f"{self.phrase!r} was not found on the screen."
        return f"{self.phrase!r} is at line {self.line_number}: {self.line_text!r}"


class _FakeSession:
    """Stand-in for senses.browser.BrowserSession covering its public API."""

    def __init__(
        self,
        page: browser_sense.PageRead | None = None,
        failures: dict | None = None,
    ) -> None:
        self.calls: list = []
        self.page = page or browser_sense.PageRead(
            url="https://example.com/",
            title="Example Domain",
            text="This domain is for use in illustrative examples.",
            char_count=48,
            word_count=8,
        )
        self.failures = failures or {}

    def _maybe_fail(self, key):
        error = self.failures.get(key)
        if error is not None:
            raise error

    def open(self, url, **_kwargs):
        self.calls.append(("open", url))
        self._maybe_fail("open")
        return self.page

    def read(self, **_kwargs):
        self.calls.append(("read",))
        self._maybe_fail("read")
        return self.page

    def find(self, phrase, **_kwargs):
        self.calls.append(("find", phrase))
        self._maybe_fail("find")
        return browser_sense.FindResult(
            phrase=phrase,
            found=True,
            count=1,
            region="main content",
            detail=f'Yes, "{phrase}" is in the main content, once.',
        )

    def click(self, target, **_kwargs):
        self.calls.append(("click", target))
        self._maybe_fail("click")
        return browser_sense.ActionResult(action="click", target=target)

    def fill(self, selector, value, **_kwargs):
        self.calls.append(("fill", selector, value))
        self._maybe_fail("fill")
        return browser_sense.ActionResult(
            action="fill",
            target=selector,
            detail=f"filled {selector!r}",
            value_redacted="[redacted]",
            value_len=len(value),
            secret="pass" in selector.lower(),
        )

    def screenshot(self, path):
        self.calls.append(("screenshot", path))
        self._maybe_fail("screenshot")
        return browser_sense.ActionResult(action="screenshot", target="", path=path)

    def back(self, **_kwargs):
        self.calls.append(("back",))
        self._maybe_fail("back")
        return self.page


def _camera_available() -> camera_sense.CameraAvailability:
    return camera_sense.CameraAvailability(
        available=True,
        devices=(camera_sense.CameraDevice(path="/dev/video0", index=0, name="Fake Webcam"),),
        reason="1 camera detected and ready to capture from.",
    )


def _camera_missing() -> camera_sense.CameraAvailability:
    return camera_sense.CameraAvailability(
        available=False,
        devices=(),
        reason="No camera found: nothing matching /dev/video* exists.",
        actions=(
            "Plug in a USB webcam; it normally appears as /dev/video0 within a "
            "second or two. Check with: ls -l /dev/video*",
        ),
    )


def _spoken(result: ToolExecutionResult) -> str:
    return result.reply_text or ""


def _assert_no_traceback(result: ToolExecutionResult) -> None:
    assert "Traceback" not in _spoken(result)
    assert 'File "' not in _spoken(result)
    assert "\n" not in _spoken(result)


# --------------------------------------------------------------------------- #
# Registration and schema (a forgotten registration must fail here)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,cls", sorted(SENSES_TOOLS.items()))
def test_senses_tool_is_registered_by_name(name, cls):
    assert name in registry.BUILTIN_TOOLS, (
        f"{name} is not in BUILTIN_TOOLS: the tool exists but the model can never "
        "be offered it"
    )
    tool = registry.BUILTIN_TOOLS[name]
    assert isinstance(tool, cls)
    assert isinstance(tool, Tool)
    assert tool.name == name


@pytest.mark.parametrize("name", sorted(SENSES_TOOLS))
def test_senses_tool_is_discoverable_in_the_tool_schema(name):
    names = [entry["function"]["name"] for entry in registry.generate_tools_json_schema([name])]
    assert name in names

    description = registry.generate_tools_description([name])
    assert name in description
    assert registry.BUILTIN_TOOLS[name].description.strip()


@pytest.mark.parametrize("name", sorted(SENSES_TOOLS))
def test_senses_tool_is_exported_from_the_builtin_package(name):
    cls = SENSES_TOOLS[name]
    assert getattr(builtin_package, cls.__name__) is cls
    assert cls.__name__ in builtin_package.__all__


@pytest.mark.parametrize("name", sorted(SENSES_TOOLS))
def test_every_senses_tool_has_a_sane_parameter_schema(name):
    schema = registry.BUILTIN_TOOLS[name].inputSchema
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert schema["properties"], "a tool with no parameters must still document why"
    assert isinstance(schema["required"], list)
    for prop, spec in schema["properties"].items():
        assert spec.get("type") in ("string", "integer", "number", "boolean", "array", "object"), prop
        assert spec.get("description"), f"{name}.{prop} has no description"


def test_browser_schema_documents_every_action_and_requires_one():
    schema = BrowseWebTool().inputSchema
    assert schema["required"] == ["action"]
    assert set(schema["properties"]["action"]["enum"]) == {
        "open", "read", "find", "click", "fill", "screenshot", "back", "close",
    }


def test_screen_schema_only_offers_the_two_modes():
    schema = ReadScreenTool().inputSchema
    assert schema["properties"]["mode"]["enum"] == ["read", "find"]
    assert schema["required"] == []


def test_camera_schema_takes_an_optional_question():
    schema = LookThroughCameraTool().inputSchema
    assert schema["required"] == []
    assert schema["properties"]["question"]["type"] == "string"


# --------------------------------------------------------------------------- #
# Screen: a missing OCR engine is a calm capability report
# --------------------------------------------------------------------------- #

def test_screen_reports_missing_ocr_engine_without_capturing(monkeypatch):
    tool = ReadScreenTool()
    monkeypatch.setattr(screen_sense, "diagnose", lambda *a, **k: _missing_verdict())

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("read_screen must not run when OCR cannot run")

    monkeypatch.setattr(screen_sense, "read_screen", _must_not_run)

    result = _execute(tool, None)

    assert isinstance(result, ToolExecutionResult)
    assert result.success is False
    assert "cannot read your screen" in _spoken(result).lower()
    assert "tesseract-ocr" in _spoken(result)
    assert result.error_message
    _assert_no_traceback(result)


def test_screen_reports_ocr_engine_missing_error_as_a_reported_failure():
    def _raise_missing():
        raise screen_sense.OcrEngineMissingError(
            binary="tesseract",
            package="tesseract-ocr",
            command="sudo apt-get install -y tesseract-ocr",
        )

    tool = ReadScreenTool(read_screen=_raise_missing, diagnose=lambda *a, **k: _available_verdict())
    result = _execute(tool, {"mode": "read"})

    assert result.success is False
    assert "cannot read your screen" in _spoken(result).lower()
    assert "tesseract-ocr" in _spoken(result)
    _assert_no_traceback(result)


def test_screen_reports_capture_failure_as_a_reported_failure(monkeypatch):
    def _raise_capture(*_args, **_kwargs):
        raise screen_sense.ScreenCaptureUnavailableError(
            attempts=[("pillow-imagegrab", "no display"), ("grim", "not installed")],
            fixes=["Install Pillow: pip install pillow"],
        )

    tool = ReadScreenTool()
    monkeypatch.setattr(screen_sense, "diagnose", lambda *a, **k: _available_verdict())
    monkeypatch.setattr(screen_sense, "read_screen", _raise_capture)

    result = _execute(tool, {})

    assert result.success is False
    assert "could not capture your screen" in _spoken(result).lower()
    assert "pip install pillow" in _spoken(result)
    _assert_no_traceback(result)


def test_screen_read_happy_path_is_spoken_plainly():
    tool = ReadScreenTool(
        read_screen=lambda: SimpleNamespace(text="Save changes\nCancel", status="text"),
        diagnose=lambda *a, **k: _available_verdict(),
    )
    result = _execute(tool, {"mode": "read"})

    assert result.success is True
    assert "Save changes Cancel" in _spoken(result)
    assert len(_spoken(result)) <= MAX_SPOKEN_CHARS
    _assert_no_traceback(result)


def test_screen_read_of_a_blank_screen_is_success_not_an_error():
    tool = ReadScreenTool(
        read_screen=lambda: SimpleNamespace(text="", status="empty"),
        diagnose=lambda *a, **k: _available_verdict(),
    )
    result = _execute(tool, {})

    assert result.success is True
    assert "no readable words" in _spoken(result).lower()


def test_screen_find_happy_path_names_the_place():
    calls = []

    def _find(phrase):
        calls.append(phrase)
        return _FakeLocation(found=True, phrase=phrase)

    tool = ReadScreenTool(
        find_phrase_on_screen=_find,
        diagnose=lambda *a, **k: _available_verdict(),
    )
    result = _execute(tool, {"mode": "find", "phrase": "Save changes"})

    assert result.success is True
    assert calls == ["Save changes"]
    assert "Save changes" in _spoken(result)
    assert "line 4" in _spoken(result)


def test_screen_find_miss_is_an_answer_not_a_failure():
    tool = ReadScreenTool(
        find_phrase_on_screen=lambda phrase: _FakeLocation(found=False, phrase=phrase),
        diagnose=lambda *a, **k: _available_verdict(),
    )
    result = _execute(tool, {"mode": "find", "phrase": "Unicorn"})

    assert result.success is True
    assert "cannot see" in _spoken(result).lower()
    assert "Unicorn" in _spoken(result)


def test_screen_find_without_a_phrase_asks_instead_of_capturing(monkeypatch):
    monkeypatch.setattr(screen_sense, "diagnose", lambda *a, **k: _available_verdict())
    result = _execute(ReadScreenTool(), {"mode": "find"})

    assert result.success is False
    assert "phrase" in _spoken(result).lower()


def test_screen_rejects_an_unknown_mode():
    result = _execute(ReadScreenTool(), {"mode": "delete"})

    assert result.success is False
    assert "screen" in _spoken(result).lower()


def test_screen_unexpected_exception_is_reported_not_raised():
    def _explode():
        raise RuntimeError("segfault in the capture helper")

    tool = ReadScreenTool(read_screen=_explode, diagnose=lambda *a, **k: _available_verdict())
    result = _execute(tool, {})

    assert result.success is False
    assert "RuntimeError" in (result.error_message or "")
    _assert_no_traceback(result)


# --------------------------------------------------------------------------- #
# Camera: no camera is a plain capability report
# --------------------------------------------------------------------------- #

def test_camera_reports_no_camera_plainly_and_never_captures(monkeypatch):
    tool = LookThroughCameraTool()
    monkeypatch.setattr(camera_sense, "describe_availability", lambda *a, **k: _camera_missing())

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("capture_frame must not run when there is no camera")

    monkeypatch.setattr(camera_sense, "capture_frame", _must_not_run)

    result = _execute(tool, None)

    assert isinstance(result, ToolExecutionResult)
    assert result.success is False
    assert "do not have a camera" in _spoken(result).lower()
    assert "webcam" in _spoken(result).lower()
    assert "photo from your phone" in _spoken(result).lower()
    assert result.error_message
    _assert_no_traceback(result)


def test_camera_reports_a_camera_that_disappeared_as_no_camera(monkeypatch):
    def _raise_not_found(*_args, **_kwargs):
        raise camera_sense.CameraNotFoundError(available=["/dev/video0"])

    tool = LookThroughCameraTool(
        describe_availability=lambda *a, **k: _camera_available(),
        capture_frame=_raise_not_found,
    )
    result = _execute(tool, {})

    assert result.success is False
    assert "camera" in _spoken(result).lower()
    _assert_no_traceback(result)


def test_camera_capture_failure_is_reported_calmly():
    def _raise_capture(*_args, **_kwargs):
        raise camera_sense.CameraCaptureError(
            "/dev/video0",
            "the driver refused every pixel format Claire can decode",
            fixes=["Close any other program using the camera: sudo fuser -v /dev/video0"],
        )

    tool = LookThroughCameraTool(
        describe_availability=lambda *a, **k: _camera_available(),
        capture_frame=_raise_capture,
    )
    result = _execute(tool, {})

    assert result.success is False
    assert "could not get a picture" in _spoken(result).lower()
    assert "fuser" in _spoken(result)
    _assert_no_traceback(result)


def test_camera_happy_path_describes_the_frame(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"fake-jpeg")
    seen = []

    def _describe(image_path, question=""):
        seen.append((image_path, question))
        return camera_sense.ImageDescription(
            seen=True,
            description="You are sitting at a desk in a blue shirt.",
            image_path=str(image_path),
            question=question,
        )

    tool = LookThroughCameraTool(
        describe_availability=lambda *a, **k: _camera_available(),
        capture_frame=lambda *a, **k: frame,
        describe_image=_describe,
    )
    result = _execute(tool, {"question": "what am I doing?"})

    assert result.success is True
    assert "blue shirt" in _spoken(result)
    assert seen == [(frame, "what am I doing?")]
    _assert_no_traceback(result)


def test_camera_without_a_vision_answer_refuses_to_invent_one():
    error = camera_sense.VisionUnavailableError(
        "no vision model is configured",
        remedy="Export the router key: export OMNIROUTE_API_KEY=<your key>",
    )
    tool = LookThroughCameraTool(
        describe_availability=lambda *a, **k: _camera_available(),
        capture_frame=lambda *a, **k: Path("/tmp/claire_frame_fake.jpg"),
        describe_image=lambda image_path, question="": camera_sense.ImageDescription.not_seen(
            image_path, question, error
        ),
    )
    result = _execute(tool, {"question": "what is this?"})

    assert result.success is False
    assert "will not" in _spoken(result).lower()
    assert "OMNIROUTE_API_KEY" in _spoken(result)
    _assert_no_traceback(result)


def test_camera_unexpected_exception_is_reported_not_raised():
    def _explode(*_args, **_kwargs):
        raise RuntimeError("v4l2 ioctl blew up")

    tool = LookThroughCameraTool(
        describe_availability=lambda *a, **k: _camera_available(),
        capture_frame=_explode,
    )
    result = _execute(tool, {})

    assert result.success is False
    assert "RuntimeError" in (result.error_message or "")
    _assert_no_traceback(result)


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #

def _browser_tool(session, available=True):
    return BrowseWebTool(session_factory=lambda: session, available_fn=lambda: available)


def test_browser_reports_missing_browser_with_the_exact_fix(monkeypatch):
    def _must_not_start(*_args, **_kwargs):
        raise AssertionError("no session may be created when chromium is missing")

    tool = BrowseWebTool(session_factory=_must_not_start, available_fn=lambda: False)
    result = _execute(tool, {"action": "open", "url": "https://example.com"})

    assert result.success is False
    assert browser_sense.CHROMIUM_INSTALL_COMMAND in _spoken(result)
    assert "browser is not installed" in _spoken(result)
    _assert_no_traceback(result)


def test_browser_reports_a_session_that_cannot_start():
    def _raise(*_args, **_kwargs):
        raise browser_sense.BrowserUnavailableError(
            "Claire's browser is unavailable: Playwright's chromium binary is not installed.",
            fix=browser_sense.CHROMIUM_INSTALL_COMMAND,
        )

    tool = BrowseWebTool(session_factory=_raise, available_fn=lambda: True)
    result = _execute(tool, {"action": "read"})

    assert result.success is False
    assert "could not start the browser" in _spoken(result).lower()
    _assert_no_traceback(result)


def test_browser_open_happy_path():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "open", "url": "example.com"})

    assert result.success is True
    assert session.calls == [("open", "example.com")]
    assert "Example Domain" in _spoken(result)
    _assert_no_traceback(result)


def test_browser_open_without_a_url_asks_first():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "open"})

    assert result.success is False
    assert session.calls == []
    assert "web address" in _spoken(result).lower()


def test_browser_read_happy_path_includes_the_page_text():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "read"})

    assert result.success is True
    assert "illustrative examples" in _spoken(result)
    assert "Example Domain" in _spoken(result)


def test_browser_find_reports_the_located_phrase():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "find", "phrase": "domain"})

    assert result.success is True
    assert session.calls == [("find", "domain")]
    assert "domain" in _spoken(result)


def test_browser_click_happy_path():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "click", "target": "#submit"})

    assert result.success is True
    assert session.calls == [("click", "#submit")]
    assert "#submit" in _spoken(result)


def test_browser_screenshot_writes_to_the_requested_path(tmp_path):
    session = _FakeSession()
    target = str(tmp_path / "shot.png")
    result = _execute(_browser_tool(session), {"action": "screenshot", "path": target})

    assert result.success is True
    assert session.calls == [("screenshot", target)]
    assert target in _spoken(result)


def test_browser_screenshot_without_a_path_picks_one():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "screenshot"})

    assert result.success is True
    assert session.calls[0][0] == "screenshot"
    assert session.calls[0][1].endswith(".png")


def test_browser_back_happy_path():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "back"})

    assert result.success is True
    assert session.calls == [("back",)]
    assert "Example Domain" in _spoken(result)


def test_browser_close_happy_path():
    closed = []
    tool = BrowseWebTool(close_fn=lambda: closed.append(True), available_fn=lambda: True)
    result = _execute(tool, {"action": "close"})

    assert result.success is True
    assert closed == [True]
    assert "closed the browser" in _spoken(result)


def test_browser_navigation_failure_is_reported_calmly():
    session = _FakeSession(
        failures={"open": browser_sense.NavigationError("could not open https://nope.invalid")}
    )
    result = _execute(_browser_tool(session), {"action": "open", "url": "nope.invalid"})

    assert result.success is False
    assert "could not open that page" in _spoken(result).lower()
    _assert_no_traceback(result)


def test_browser_unexpected_exception_is_reported_not_raised():
    session = _FakeSession(failures={"read": RuntimeError("driver vanished mid-read")})
    result = _execute(_browser_tool(session), {"action": "read"})

    assert result.success is False
    assert "something went wrong inside the browser" in _spoken(result).lower()
    assert "RuntimeError" in (result.error_message or "")
    _assert_no_traceback(result)


def test_browser_rejects_an_unknown_action():
    session = _FakeSession()
    result = _execute(_browser_tool(session), {"action": "teleport"})

    assert result.success is False
    assert session.calls == []


def test_browser_fill_needs_a_selector_and_a_value():
    session = _FakeSession()
    missing_selector = _execute(_browser_tool(session), {"action": "fill", "value": "hi"})
    missing_value = _execute(_browser_tool(session), {"action": "fill", "selector": "#name"})

    assert missing_selector.success is False
    assert missing_value.success is False
    assert session.calls == []


def test_browser_fill_reports_without_repeating_the_text():
    session = _FakeSession()
    result = _execute(
        _browser_tool(session),
        {"action": "fill", "selector": "#name", "value": "Ada Lovelace"},
    )

    assert result.success is True
    assert session.calls == [("fill", "#name", "Ada Lovelace")]
    assert "Ada Lovelace" not in _spoken(result)
    assert "#name" in _spoken(result)


# --------------------------------------------------------------------------- #
# Credentials: a password must never come back out
# --------------------------------------------------------------------------- #

PASSWORD = "Correct-Horse-Battery-Staple-42"


def test_browser_password_is_never_echoed_back():
    session = _FakeSession()
    result = _execute(
        _browser_tool(session),
        {"action": "fill", "selector": "#password", "value": PASSWORD},
    )

    assert result.success is True
    # It WAS typed into the field...
    assert session.calls == [("fill", "#password", PASSWORD)]
    # ...and it never appears in anything the user or a log could see.
    assert PASSWORD not in (result.reply_text or "")
    assert PASSWORD not in (result.error_message or "")
    assert PASSWORD not in str(result)
    assert "will not read" in _spoken(result).lower()


def test_browser_password_is_scrubbed_out_of_an_error_message():
    session = _FakeSession(
        failures={
            "fill": browser_sense.BrowserError(
                f"could not fill '#password' with value {PASSWORD!r} before timeout"
            )
        }
    )
    result = _execute(
        _browser_tool(session),
        {"action": "fill", "selector": "#password", "value": PASSWORD},
    )

    assert result.success is False
    assert PASSWORD not in (result.reply_text or "")
    assert PASSWORD not in (result.error_message or "")
    assert PASSWORD not in str(result)
    assert "[redacted]" in (result.error_message or "")
    _assert_no_traceback(result)


def test_browser_guard_never_leaks_the_value_on_an_unexpected_failure():
    def _explode_on_startup():
        raise RuntimeError(f"driver startup failed while handling {PASSWORD!r}")

    tool = BrowseWebTool(session_factory=_explode_on_startup, available_fn=lambda: True)
    result = _execute(tool, {"action": "fill", "selector": "#password", "value": PASSWORD})

    assert result.success is False
    assert PASSWORD not in (result.reply_text or "")
    assert PASSWORD not in (result.error_message or "")
    assert PASSWORD not in str(result)
    assert "[redacted]" in (result.error_message or "")
    _assert_no_traceback(result)


def test_browser_password_is_never_logged(monkeypatch):
    from src.claire.tools.builtin import senses as senses_module

    logged: list[str] = []
    monkeypatch.setattr(senses_module, "debug_log", lambda message, category="debug": logged.append(str(message)))

    session = _FakeSession()
    tool = _browser_tool(session)
    _execute(tool, {"action": "fill", "selector": "#password", "value": PASSWORD})
    _execute(tool, {"action": "fill", "selector": "#password", "value": PASSWORD})

    # Nothing the tool logged contains the password, whatever else it logged.
    assert all(PASSWORD not in message for message in logged)


def test_browser_secret_field_names_are_treated_as_credentials():
    for selector in ("#password", "input[name=passwd]", "#api_key", "#otp-code", "#card-number"):
        assert senses_secret(selector) is True
    for selector in ("#username", "#search", "input[name=q]"):
        assert senses_secret(selector) is False


def senses_secret(selector: str) -> bool:
    from src.claire.tools.builtin.senses import _is_secret_field

    return _is_secret_field(selector)
