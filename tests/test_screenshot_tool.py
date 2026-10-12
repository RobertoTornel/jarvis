"""Tests for the ``screenshot`` tool: honest success, honest failure.

Every test injects a fake screen sense through the tool's own lazy-import
seam — no real screen capture and no real OCR. The fake is the shape the real
``claire.senses.screen`` module must have, so anything accepted here works
against the real module; anything the real module raises (a named
``ScreenError`` subclass) must come back as a reported failure, never as a
silent empty success.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.claire.senses import screen as screen_sense
from src.claire.tools.base import ToolContext
from src.claire.tools.builtin import screenshot as screenshot_module
from src.claire.tools.builtin.screenshot import ScreenshotTool
from src.claire.tools.types import ToolExecutionResult

pytestmark = pytest.mark.unit


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


def _screen_result(text: str, ocr_ran: bool = True) -> SimpleNamespace:
    """Stand-in for screen.ScreenReadResult — the tool reads ``text``/``ocr_ran``."""

    return SimpleNamespace(
        text=text,
        ocr_ran=ocr_ran,
        status="ocr_text" if text else "ocr_empty",
    )


def _fake_screen(read_screen) -> SimpleNamespace:
    return SimpleNamespace(
        read_screen=read_screen,
        ScreenError=screen_sense.ScreenError,
    )


def _run(monkeypatch, read_screen) -> ToolExecutionResult:
    """Point the tool's lazy-import seam at a fake sense and run it."""
    monkeypatch.setattr(
        screenshot_module,
        "_screen_module",
        lambda: _fake_screen(read_screen),
        raising=False,
    )
    return ScreenshotTool().run(None, _context())


def _combined_message(result: ToolExecutionResult) -> str:
    return f"{result.error_message or ''} {result.reply_text or ''}"


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

def test_success_when_sense_returns_text(monkeypatch):
    """Text from the sense comes back as a successful result."""

    result = _run(monkeypatch, lambda: _screen_result("Meeting at 14:00 in room 3B"))
    assert result.success is True
    assert "Meeting at 14:00 in room 3B" in (result.reply_text or "")


def test_capture_unavailable_is_a_named_failure(monkeypatch):
    """ScreenCaptureUnavailableError -> success=False with a non-empty,
    problem-naming message (the exact bug: an empty 'success')."""

    def _raise():
        raise screen_sense.ScreenCaptureUnavailableError(
            [("pillow-imagegrab", "DISPLAY is not set")],
            ["sudo apt-get install -y scrot"],
        )

    result = _run(monkeypatch, _raise)
    assert result.success is False
    message = _combined_message(result)
    assert message.strip(), "a failure must carry a message"
    assert "capture" in message.lower()
    # The message must point at the fix, not just the fault.
    assert "scrot" in message


def test_never_success_with_an_empty_result(monkeypatch):
    """Whatever the sense does, success=True implies a non-empty reply_text."""

    def _boom():
        raise screen_sense.ScreenCaptureUnavailableError(
            [("pillow-imagegrab", "DISPLAY is not set")],
        )

    fakes = [
        lambda: _screen_result("some words"),          # the happy path
        lambda: _screen_result("", ocr_ran=True),      # blank screen, OCR ran
        lambda: _screen_result("", ocr_ran=False),     # OCR never ran
        lambda: None,                                  # sense returned nothing
        _boom,                                         # sense raised
    ]
    for fake in fakes:
        result = _run(monkeypatch, fake)
        assert not (result.success and not (result.reply_text or "").strip()), (
            f"silent empty success for fake {fake!r}: {result!r}"
        )
