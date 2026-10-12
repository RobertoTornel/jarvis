"""Screenshot tool implementation for OCR capture."""

from typing import Dict, Any, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


def _screen_module():
    """Lazy import of the screen sense.

    The screen module pulls in capture and OCR machinery; like the sibling
    tools in ``tools/builtin/senses.py``, it must not be imported at module
    import time (the registry imports every builtin tool eagerly).
    """
    from ...senses import screen as screen_sense

    return screen_sense


class ScreenshotTool(Tool):
    """Tool for capturing screenshots and performing OCR."""

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return "Capture a selected screen region and OCR the text. Use only if the OCR will materially help."

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": []
        }

    @staticmethod
    def _failure(context: ToolContext, reply: str, error: str) -> ToolExecutionResult:
        """An honest failure: the user hears the reason, the log gets the detail."""
        debug_log(f"screenshot: failed: {error}", "screenshot")
        try:
            context.user_print(reply)
        except Exception:  # pragma: no cover - a broken printer is not the tool's problem
            pass
        return ToolExecutionResult(success=False, reply_text=reply, error_message=error)

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        """Execute the screenshot tool.

        Capture and OCR are delegated to ``claire.senses.screen.read_screen``,
        which is platform-aware and raises named errors instead of returning a
        silent empty string. Those errors are converted here into a reported
        failure: this tool never claims success it did not have.
        """
        context.user_print("📸 Capturing a screenshot for OCR…")
        debug_log("screenshot: capturing OCR...", "screenshot")

        try:
            screen = _screen_module()
        except Exception as exc:
            return self._failure(
                context,
                "❌ Screenshot failed: my screen-reading module is unavailable, so "
                "I cannot capture the screen.",
                f"{type(exc).__name__}: {exc}",
            )

        try:
            result = screen.read_screen()
        except Exception as exc:
            # ScreenError subclasses (ScreenCaptureUnavailableError,
            # OcrEngineMissingError, …) name the fault and the fix in their
            # message; anything else still gets reported honestly.
            detail = " ".join(str(exc).split()) or type(exc).__name__
            reply = f"❌ Screenshot failed: {detail}"
            return self._failure(context, reply, f"{type(exc).__name__}: {detail}")

        text = str(getattr(result, "text", "") or "")
        if not text.strip():
            if not getattr(result, "ocr_ran", False):
                return self._failure(
                    context,
                    "❌ Screenshot failed: the screen was captured but no text was "
                    "read from it, so I have nothing to report.",
                    "read_screen returned no text and OCR did not run",
                )
            # Capture and OCR both happened; the screen simply holds no words.
            debug_log("screenshot: ocr_chars=0 (blank screen)", "screenshot")
            blank = "Screenshot taken, but there are no readable words on the screen."
            context.user_print(f"✅ {blank}")
            return ToolExecutionResult(success=True, reply_text=blank)

        ocr_text = text.strip()
        debug_log(f"screenshot: ocr_chars={len(ocr_text)}", "screenshot")
        context.user_print("✅ Screenshot processed.")
        # Return raw OCR text as tool result (no LLM processing here)
        return ToolExecutionResult(success=True, reply_text=ocr_text)
