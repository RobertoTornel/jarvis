"""Tests for local files tool."""

import pytest
from unittest.mock import Mock, patch, mock_open
import tempfile
import os
from pathlib import Path

from src.claire.tools.builtin.local_files import LocalFilesTool
from src.claire.tools.base import ToolContext
from src.claire.tools.types import ToolExecutionResult

# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`


class TestLocalFilesTool:
    """Test local files tool functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = LocalFilesTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    def test_tool_properties(self):
        """Test tool metadata properties."""
        assert self.tool.name == "localFiles"
        assert "file" in self.tool.description.lower()
        assert self.tool.inputSchema["type"] == "object"
        assert "operation" in self.tool.inputSchema["required"]
        assert "path" in self.tool.inputSchema["required"]

    def test_run_no_args(self):
        """Test local files with no arguments."""
        result = self.tool.run(None, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "requires a JSON object" in result.reply_text

    def test_run_missing_operation(self):
        """Test local files with missing operation."""
        args = {"path": "test.txt"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "requires 'operation'" in result.reply_text

    def test_run_missing_path(self):
        """Test local files with missing path."""
        args = {"operation": "read"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "requires 'operation' and 'path'" in result.reply_text

    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.is_file')
    @patch('pathlib.Path.read_text')
    def test_run_read_success(self, mock_read_text, mock_is_file, mock_exists):
        """Test successful file read."""
        mock_exists.return_value = True
        mock_is_file.return_value = True
        mock_read_text.return_value = "Test content"

        args = {"operation": "read", "path": "~/test.txt"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "Test content" in result.reply_text

    @patch('pathlib.Path.exists')
    def test_run_read_not_found(self, mock_exists):
        """Test file read when file doesn't exist."""
        mock_exists.return_value = False

        args = {"operation": "read", "path": "~/nonexistent.txt"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "not found" in result.reply_text.lower()

    @patch('pathlib.Path.write_text')
    @patch('pathlib.Path.mkdir')
    def test_run_write_success(self, mock_mkdir, mock_write_text):
        """Test successful file write."""
        args = {"operation": "write", "path": "~/test.txt", "content": "Test content"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "Wrote" in result.reply_text

    def test_run_write_no_content(self):
        """Test file write without content."""
        args = {"operation": "write", "path": "~/test.txt"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "requires string 'content'" in result.reply_text

    def test_run_unsafe_path(self):
        """Test with path outside home directory."""
        args = {"operation": "read", "path": "/etc/passwd"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "not allowed" in result.reply_text.lower()

    def test_run_unknown_operation(self):
        """Test with unknown operation."""
        args = {"operation": "invalid", "path": "~/test.txt"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "Unknown localFiles operation" in result.reply_text


class TestRelativePathResolution:
    """`path` is documented in the schema as "relative to home directory".

    run() must therefore resolve a relative path against $HOME — not against
    the process working directory, which is what ``Path.resolve()`` does on a
    relative input. The daemon runs with ``WorkingDirectory=.../jarvis_cloud``,
    so a CWD-based resolve broke every documented relative path."""

    def setup_method(self):
        self.tool = LocalFilesTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    def test_relative_path_resolves_under_home(self, tmp_path, monkeypatch):
        """A relative path resolves under $HOME even when a decoy file of the
        same relative name exists under the current working directory."""
        home = tmp_path / "home"
        (home / "docs").mkdir(parents=True)
        (home / "docs" / "note.txt").write_text("hello from home", encoding="utf-8")
        cwd = tmp_path / "cwd"
        (cwd / "docs").mkdir(parents=True)
        (cwd / "docs" / "note.txt").write_text("decoy from cwd", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(cwd)

        result = self.tool.run(
            {"operation": "read", "path": "docs/note.txt"}, self.context
        )

        assert result.success is True, result.reply_text
        assert result.reply_text == "hello from home"

    def test_tilde_path_resolves_under_home(self, tmp_path, monkeypatch):
        """`~`-prefixed paths keep resolving against $HOME exactly as before."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "welcome.txt").write_text("tilde works", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(tmp_path)

        result = self.tool.run(
            {"operation": "read", "path": "~/welcome.txt"}, self.context
        )

        assert result.success is True, result.reply_text
        assert result.reply_text == "tilde works"

    def test_absolute_path_unchanged(self, tmp_path, monkeypatch):
        """An absolute path is never re-rooted under $HOME: it resolves to
        itself and containment is judged against home as before."""
        home = tmp_path / "home"
        home.mkdir()
        target = home / "abs.txt"
        target.write_text("absolute stays put", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(tmp_path)

        result = self.tool.run(
            {"operation": "read", "path": str(target)}, self.context
        )

        assert result.success is True, result.reply_text
        assert result.reply_text == "absolute stays put"

        # The write reply echoes the resolved path back — it must be the
        # absolute path itself, never $HOME joined onto it.
        result = self.tool.run(
            {"operation": "write", "path": str(home / "out.txt"), "content": "x"},
            self.context,
        )
        assert result.success is True, result.reply_text
        assert f"to {home / 'out.txt'}" in result.reply_text
