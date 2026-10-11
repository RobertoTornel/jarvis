"""The linux-desktop MCP server: pure units, contract, and a live round trip.

The integration tests launch the real server (``python -m
claire.mcp_servers.linux_desktop``) as a stdio subprocess and drive it
through Claire's own MCP client, mirroring ``test_mcp_server_template.py``.
Only read-only tools run by default; injecting input into the developer's
desktop is opt-in through ``CLAIRE_MCP_DESKTOP_INJECT=1``.
"""

import json
import os
import re
import sys

import pytest

pytestmark = pytest.mark.unit

SERVER_NAME = "linux-desktop"

EXPECTED_TOOLS = {
    "get_screen_info",
    "list_windows",
    "focus_window",
    "close_window",
    "move_resize_window",
    "click",
    "double_click",
    "move_mouse",
    "scroll",
    "type_text",
    "press_key",
    "launch_app",
    "wait",
}


@pytest.fixture
def shutdown_persistent_runtime():
    """Tear down the persistent MCP runtime singleton after each test."""
    yield
    try:
        from claire.tools.external.mcp_runtime import shutdown_runtime

        shutdown_runtime()
    except Exception:  # noqa: BLE001 - teardown must never mask the test
        pass


def _has_display() -> bool:
    if not os.environ.get("DISPLAY"):
        return False
    try:
        import Xlib.display

        d = Xlib.display.Display()
        d.close()
        return True
    except Exception:
        return False


HAS_DISPLAY = _has_display()
needs_display = pytest.mark.skipif(
    not HAS_DISPLAY, reason="needs a live X11 display (DISPLAY)"
)


def _server_config():
    return {
        SERVER_NAME: {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "claire.mcp_servers.linux_desktop"],
            "env": {"DISPLAY": os.environ.get("DISPLAY", ":0")},
            "timeout_sec": 30,
        }
    }


# ---------------------------------------------------------------------------
# Contract (no display needed)
# ---------------------------------------------------------------------------

def test_server_module_exists_without_postponed_annotations():
    """FastMCP reads parameter annotations at import time, so the module
    must not postpone them with ``from __future__ import annotations``."""
    import claire.mcp_servers.linux_desktop as mod

    assert mod.SERVER_NAME == SERVER_NAME
    source = open(mod.__file__, encoding="utf-8").read()
    assert not re.search(
        r"(?m)^\s*from __future__ import annotations\s*$", source
    ), "the server must not postpone annotations"


def test_catalogue_entry_converts_to_a_stdio_config():
    from desktop_app.mcp_catalogue import CATALOGUE_BY_NAME

    entry = CATALOGUE_BY_NAME[SERVER_NAME]
    cfg = entry.to_config()
    assert cfg["transport"] == "stdio"
    assert cfg["command"]
    assert "-m" in cfg["args"]
    assert "claire.mcp_servers.linux_desktop" in cfg["args"]
    # DISPLAY is what lets Xlib connect inside the spawned subprocess.
    assert "DISPLAY" in cfg["env"]


# ---------------------------------------------------------------------------
# Pure helpers (no display needed)
# ---------------------------------------------------------------------------

def test_parse_combo_splits_and_validates():
    from claire.mcp_servers.linux_desktop import parse_combo

    assert parse_combo("ctrl+shift+t") == ["ctrl", "shift", "t"]
    assert parse_combo("Return") == ["Return"]
    assert parse_combo(" alt+tab ") == ["alt", "tab"]
    assert parse_combo("super") == ["super"]
    with pytest.raises(ValueError):
        parse_combo("")          # empty
    with pytest.raises(ValueError):
        parse_combo("ctrl++")    # literal '+' must be spelled "plus"
    with pytest.raises(ValueError):
        parse_combo("ctrl+")     # dangling separator


def test_keysym_for_resolves_aliases_and_chars():
    from Xlib import XK

    from claire.mcp_servers.linux_desktop import keysym_for

    assert keysym_for("enter") == XK.string_to_keysym("Return")
    assert keysym_for("esc") == XK.string_to_keysym("Escape")
    assert keysym_for("pageup") == XK.string_to_keysym("Page_Up")
    assert keysym_for("f5") == XK.string_to_keysym("F5")
    assert keysym_for("win") == XK.string_to_keysym("Super_L")
    assert keysym_for("Return") == XK.string_to_keysym("Return")
    assert keysym_for("a") == XK.string_to_keysym("a")
    assert keysym_for("A") == XK.string_to_keysym("A")
    with pytest.raises(ValueError):
        keysym_for("banana")


def test_button_number_mapping():
    from claire.mcp_servers.linux_desktop import (
        button_number,
        scroll_button_number,
    )

    assert button_number("left") == 1
    assert button_number("right") == 3
    assert button_number("middle") == 2
    assert button_number("2") == 2
    with pytest.raises(ValueError):
        button_number("banana")
    assert scroll_button_number("up") == 4
    assert scroll_button_number("down") == 5
    assert scroll_button_number("left") == 6
    with pytest.raises(ValueError):
        scroll_button_number("diagonal")


def test_match_window_prefers_exact_then_unique_substring():
    from claire.mcp_servers.linux_desktop import WindowError, match_window

    windows = [
        {"id": "0x100", "title": "Firefox — docs"},
        {"id": "0x200", "title": "Firefox — mail"},
        {"id": "0x300", "title": "Terminal"},
    ]
    # Exact (case-insensitive) title beats an ambiguous substring.
    assert match_window(windows, "terminal")["id"] == "0x300"
    # Unique substring.
    assert match_window(windows, "mail")["id"] == "0x200"
    # Window id passes straight through.
    assert match_window(windows, "0x200")["id"] == "0x200"
    # Ambiguous substring lists candidates.
    with pytest.raises(WindowError) as exc:
        match_window(windows, "firefox")
    assert "Firefox — docs" in str(exc.value)
    # Nothing matches.
    with pytest.raises(WindowError):
        match_window(windows, "calculator")


def test_plan_char_detects_shift_and_unmapped_characters():
    from claire.mcp_servers.linux_desktop import plan_char

    class FakeDisplay:
        def __init__(self, mapping):
            self._mapping = mapping

        def keysym_to_keycodes(self, keysym):
            return list(self._mapping.get(keysym, []))

        def keysym_to_keycode(self, keysym):
            pairs = self._mapping.get(keysym)
            return pairs[0][0] if pairs else 0

    # Slot 0 → no shift, slot 1 → shift (the live keymap's convention).
    display = FakeDisplay({0x61: [(38, 0)], 0x41: [(38, 1)]})
    assert plan_char(display, "a") == (38, False)
    assert plan_char(display, "A") == (38, True)
    # Not on the layout at all → None, caller falls back to a scratch keycode.
    assert plan_char(display, "ñ") is None


def test_split_launch_command_is_shell_free():
    from claire.mcp_servers.linux_desktop import split_launch

    assert split_launch('gedit "my notes.txt"') == ["gedit", "my notes.txt"]
    assert split_launch("firefox --new-window") == ["firefox", "--new-window"]
    with pytest.raises(ValueError):
        split_launch('echo "unterminated')


# ---------------------------------------------------------------------------
# Live round trip through Claire's own MCP client
# ---------------------------------------------------------------------------

@needs_display
def test_round_trip_through_claires_mcp_client(shutdown_persistent_runtime):
    from claire.tools.external.mcp_client import MCPClient

    client = MCPClient(_server_config())

    tools = client.list_tools(SERVER_NAME)
    names = {t.get("name") for t in tools}
    assert names == EXPECTED_TOOLS, f"unexpected tool surface: {names!r}"

    # Every advertised schema is an object schema.
    for t in tools:
        assert t.get("inputSchema", {}).get("type") == "object", t.get("name")

    # Read-only grounding works against this real screen.
    info = client.invoke_tool(SERVER_NAME, "get_screen_info", {})
    assert info["isError"] is False, info
    payload = json.loads(info["text"])
    assert payload["width"] > 0 and payload["height"] > 0
    assert "pointer" in payload and "focused_window" in payload

    windows = client.invoke_tool(SERVER_NAME, "list_windows", {})
    assert windows["isError"] is False, windows
    assert isinstance(json.loads(windows["text"])["windows"], list)

    # Invalid input is refused before anything is injected.
    bad_button = client.invoke_tool(
        SERVER_NAME, "click", {"x": 5, "y": 5, "button": "banana"}
    )
    assert bad_button["isError"] is True
    assert "button" in bad_button["text"].lower()

    bad_key = client.invoke_tool(
        SERVER_NAME, "press_key", {"key": "not-a-key-at-all"}
    )
    assert bad_key["isError"] is True

    out_of_bounds = client.invoke_tool(
        SERVER_NAME, "click", {"x": 10 ** 6, "y": 5}
    )
    assert out_of_bounds["isError"] is True


@needs_display
def test_injection_round_trip_opt_in(shutdown_persistent_runtime):
    """Opt-in: moves the real pointer, so it is off unless
    ``CLAIRE_MCP_DESKTOP_INJECT=1`` is set in the environment."""
    if os.environ.get("CLAIRE_MCP_DESKTOP_INJECT") != "1":
        pytest.skip("set CLAIRE_MCP_DESKTOP_INJECT=1 to inject real input")

    from claire.tools.external.mcp_client import MCPClient

    client = MCPClient(_server_config())
    before = json.loads(
        client.invoke_tool(SERVER_NAME, "get_screen_info", {})["text"]
    )
    x, y = 120, 140
    try:
        moved = client.invoke_tool(SERVER_NAME, "move_mouse", {"x": x, "y": y})
        assert moved["isError"] is False, moved
        after = json.loads(
            client.invoke_tool(SERVER_NAME, "get_screen_info", {})["text"]
        )
        assert (after["pointer"]["x"], after["pointer"]["y"]) == (x, y)
    finally:
        client.invoke_tool(
            SERVER_NAME,
            "move_mouse",
            {"x": before["pointer"]["x"], "y": before["pointer"]["y"]},
        )
