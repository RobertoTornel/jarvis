"""Tests for screenshot tool."""

import pytest
from unittest.mock import Mock

from src.claire.tools.builtin.screenshot import ScreenshotTool
from src.claire.tools.base import ToolContext

# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`


class TestScreenshotTool:
    """Test screenshot tool functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = ScreenshotTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    def test_tool_properties(self):
        """Test tool metadata properties."""
        assert self.tool.name == "screenshot"
        assert "capture" in self.tool.description.lower()
        assert self.tool.inputSchema["type"] == "object"
        assert self.tool.inputSchema["required"] == []

    # The run() behaviour used to be tested here against an inlined macOS
    # `screencapture` + `tesseract` pipeline that existed only on macOS, and one
    # of those tests asserted `success is True` alongside an empty reply, which
    # is the silent-empty defect itself. `run()` now delegates to the screen
    # sense (`claire.senses.screen.read_screen`), and its behaviour including
    # every failure mode is covered by `tests/test_screenshot_tool.py`, which
    # injects a fake sense instead of patching deleted internals.
