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


def test_plan_char_never_infers_shift_from_altgr_slot_indices():
    """Slots 3/4 are AltGr levels, not Shift levels.

    Parity (`index % 2`) would claim Shift for '@' (slot 3 here) and type
    '"' on this es,us layout, so only the plain and Shift levels of the
    active group — slots 0 and 1 — may become a direct plan.
    """
    from claire.mcp_servers.linux_desktop import plan_char

    class FakeDisplay:
        def __init__(self, mapping):
            self._mapping = mapping

        def keysym_to_keycodes(self, keysym):
            return list(self._mapping.get(keysym, []))

        def keysym_to_keycode(self, keysym):
            # An index-free fallback exists but cannot prove which level
            # of the row the keysym sits on.
            return 26

    display = FakeDisplay(
        {
            0x40: [(11, 3), (11, 4), (24, 4)],  # '@' only at AltGr levels
            0x60: [(49, 2)],  # '`' only at group-2 level
            0x61: [(38, 0)],  # 'a' plain level
            0x41: [(38, 1)],  # 'A' Shift level
        }
    )
    assert plan_char(display, "@") is None
    assert plan_char(display, "`") is None
    assert plan_char(display, "a") == (38, False)
    assert plan_char(display, "A") == (38, True)
    # No slot information at all → no direct plan, caller rebinds instead.
    assert plan_char(display, "ñ") is None


def test_tap_unmapped_char_maps_a_full_row_and_restores_it_when_the_tap_raises(
    monkeypatch,
):
    """The rebind must cover every level of the spare keycode's row.

    A one-element row leaves the other levels stale, and an exception must
    never leave the owner's keyboard remapped — the original row goes back
    in a `finally`, with syncs around the tap so the client sees the rebind
    before the key event and the restore after it.
    """
    from claire.mcp_servers import linux_desktop as mod

    class FakeDisplay:
        def __init__(self):
            self.row = [0] * 10  # ten keysyms per keycode, as on the real map
            self.changes = []  # (keycode, row) in call order
            self.syncs = 0

        def get_keyboard_mapping(self, first, count):
            return [list(self.row)]

        def change_keyboard_mapping(self, first, rows):
            self.changes.append((int(first), list(rows[0])))
            if int(first) == 248:
                self.row = list(rows[0])

        def sync(self):
            self.syncs += 1

    display = FakeDisplay()

    def boom(*_args, **_kwargs):
        raise RuntimeError("tap exploded")

    monkeypatch.setattr(mod, "_tap", boom)
    with pytest.raises(RuntimeError):
        mod._tap_unmapped_char(display, "é", 248)

    assert len(display.changes) == 2, "rebind first, restore second"
    spare, rebound = display.changes[0]
    assert spare == 248
    assert len(rebound) == 10, "a full-length row, like every other keycode's"
    assert rebound == [0xE9] * 10, "the keysym at every level of every group"
    restored_keycode, restored = display.changes[1]
    assert restored_keycode == 248
    assert restored == [0] * 10, "the original row must return even when the tap raises"
    assert display.syncs >= 2, "sync before the tap and again after the restore"


def test_type_text_never_counts_a_character_it_could_not_send(monkeypatch):
    """Control characters cannot be produced by a key press, so they are
    skipped without being counted — the count must match what a client
    actually receives."""
    from claire.mcp_servers import linux_desktop as mod

    class FakeDisplay:
        def keysym_to_keycodes(self, keysym):
            return [(38, 0)] if keysym in (0x61, 0x62, 0x63) else []

        def keysym_to_keycode(self, keysym):
            return 0

        def sync(self):
            pass

    taps = []

    def record(display, keycode, shift=False):
        taps.append((int(keycode), bool(shift)))

    monkeypatch.setattr(mod, "_tap", record)
    typed = mod._type_text(FakeDisplay(), "ab\x07c")
    assert typed == 3
    assert len(taps) == 3, "the bell was skipped, not counted as typed"


def test_type_text_fails_loudly_when_a_character_cannot_be_sent(monkeypatch):
    """A character that cannot be produced raises out of `_type_text` — the
    tool call fails instead of returning a success count that lies."""
    from claire.mcp_servers import linux_desktop as mod

    class FakeDisplay:
        def keysym_to_keycodes(self, keysym):
            return [(38, 0)] if keysym == 0x61 else []

        def keysym_to_keycode(self, keysym):
            return 0

        def get_keyboard_mapping(self, first, count):
            return [[0] * 10]

        def sync(self):
            pass

    def boom(display, ch, spare):
        raise ValueError(f"cannot type the character {ch!r}")

    monkeypatch.setattr(mod, "_tap", lambda display, keycode, shift=False: None)
    monkeypatch.setattr(mod, "_tap_unmapped_char", boom)
    with pytest.raises(ValueError):
        mod._type_text(FakeDisplay(), "aé")


def test_split_launch_command_is_shell_free():
    from claire.mcp_servers.linux_desktop import split_launch

    assert split_launch('gedit "my notes.txt"') == ["gedit", "my notes.txt"]
    assert split_launch("firefox --new-window") == ["firefox", "--new-window"]
    with pytest.raises(ValueError):
        split_launch('echo "unterminated')


# ---------------------------------------------------------------------------
# Targeted typing: `window=` raises and focuses before any input lands
# ---------------------------------------------------------------------------

_WIN_A = {"id": "0x01a00001", "title": "CLAIRE_TARGET_A"}
_WIN_B = {"id": "0x01b00002", "title": "Discord — general"}


def _xid(window):
    return int(window["id"], 16)


class _DesktopStub:
    """A fake X/wmctrl world: window rows, focus state, recorded input.

    Installed the way the other unit tests here stub the X layer — every
    piece of the module's display access is a module global — so no display
    is needed. `events` records what happened in order, which is how the
    tests prove focus happens *before* typing.
    """

    def __init__(self, windows, active):
        self.windows = list(windows)
        self.active = active  # int xid currently focused
        self.refuse_focus = False  # wmctrl -a returns, focus never moves
        self.events = []  # ("focus", id) / ("type", text) / ("key", tokens)

    # --- installed as mod._wmctrl -------------------------------------
    def wmctrl(self, args, timeout=10.0):
        if args[:1] == ["-lG"]:
            lines = [
                f"{w['id']} 0 0 0 640 480 host {w['title']}"
                for w in self.windows
            ]
            return _Completed("\n".join(lines))
        if args[:2] == ["-i", "-a"]:
            target = args[2]
            self.events.append(("focus", target))
            if not self.refuse_focus:
                self.active = int(target, 16)
            return _Completed("")
        raise AssertionError(f"unexpected wmctrl call: {args!r}")

    # --- installed as mod._active_window_id / _window_title -----------
    def active_window_id(self, display):
        return self.active

    def window_title(self, display, window_id):
        for w in self.windows:
            if _xid(w) == window_id:
                return w["title"]
        return None

    # --- installed as mod._type_text / mod._press_tokens ---------------
    def type_text(self, display, text):
        self.events.append(("type", text))
        return len(text)

    def press_tokens(self, display, tokens):
        self.events.append(("key", list(tokens)))


class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class _FakeX:
    """Stands in for the module's X11 singleton: runs the closure on None."""

    def run(self, fn):
        return fn(None)


def _stub_desktop(monkeypatch, windows, active):
    from claire.mcp_servers import linux_desktop as mod

    stub = _DesktopStub(windows, active)
    monkeypatch.setattr(mod, "_wmctrl", stub.wmctrl)
    monkeypatch.setattr(mod, "_X", _FakeX())
    monkeypatch.setattr(mod, "_active_window_id", stub.active_window_id)
    monkeypatch.setattr(mod, "_window_title", stub.window_title)
    monkeypatch.setattr(mod, "_type_text", stub.type_text)
    monkeypatch.setattr(mod, "_press_tokens", stub.press_tokens)
    return stub


def test_type_text_into_named_window_focuses_first_and_names_it(monkeypatch):
    """The named window is raised and confirmed focused *before* a character
    is typed, and the result says where the text went."""
    from claire.mcp_servers import linux_desktop as mod

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_A))
    result = json.loads(mod.type_text("hello there", window="Discord"))
    assert result["ok"] is True
    assert [e[0] for e in stub.events] == ["focus", "type"], (
        "focus must happen before typing"
    )
    assert stub.events[0][1] == _WIN_B["id"]
    assert stub.active == _xid(_WIN_B), "the target really took focus"
    assert result["window"]["id"] == _WIN_B["id"]
    assert result["window"]["title"] == _WIN_B["title"]
    assert _WIN_B["title"] in result["detail"]


def test_type_text_unknown_window_lists_candidates_and_types_nothing(monkeypatch):
    from claire.mcp_servers import linux_desktop as mod
    from claire.mcp_servers.linux_desktop import WindowError

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_A))
    with pytest.raises(WindowError) as exc:
        mod.type_text("should never land", window="calculator")
    message = str(exc.value)
    assert _WIN_A["title"] in message, "the error must list the candidates"
    assert _WIN_B["title"] in message, "the error must list the candidates"
    assert stub.events == [], "nothing may be typed (or focused) on a miss"


def test_type_text_ambiguous_window_lists_candidates_and_types_nothing(monkeypatch):
    from claire.mcp_servers import linux_desktop as mod
    from claire.mcp_servers.linux_desktop import WindowError

    voice = {"id": "0x01c00003", "title": "Discord — voice"}
    afk = {"id": "0x01d00004", "title": "Discord — afk"}
    stub = _stub_desktop(
        monkeypatch, [_WIN_A, voice, afk], active=_xid(_WIN_A)
    )
    with pytest.raises(WindowError) as exc:
        mod.type_text("should never land", window="discord")
    message = str(exc.value)
    assert voice["title"] in message, "the ambiguity must list candidates"
    assert afk["title"] in message, "the ambiguity must list candidates"
    assert stub.events == [], "nothing may be typed on an ambiguous name"


def test_type_text_refuses_when_focus_does_not_land(monkeypatch):
    """Focus that stays elsewhere must *raise* naming both windows, and no
    character may be typed. A returned ok:false body would reach the harness
    as a successful tool call — a success this is not."""
    from claire.mcp_servers import linux_desktop as mod
    from claire.mcp_servers.linux_desktop import WindowError

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_A))
    stub.refuse_focus = True
    with pytest.raises(WindowError) as exc:
        mod.type_text("should never land", window="Discord")
    message = str(exc.value)
    assert _WIN_B["title"] in message, "names the window aimed at"
    assert _WIN_A["title"] in message, "names the window focused"
    assert "nothing was sent" in message
    assert [e[0] for e in stub.events] == ["focus"], "typing must not happen"


def test_type_text_without_window_reports_the_active_window(monkeypatch):
    from claire.mcp_servers import linux_desktop as mod

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_B))
    result = json.loads(mod.type_text("hi"))
    assert result["ok"] is True and result["typed"] == 2
    assert [e[0] for e in stub.events] == ["type"], "no focus request needed"
    assert result["window"]["id"] == _WIN_B["id"], (
        "even without `window` the result names where the text went"
    )
    assert result["window"]["title"] == _WIN_B["title"]
    assert _WIN_B["title"] in result["detail"]


def test_press_key_into_named_window_focuses_first_and_names_it(monkeypatch):
    from claire.mcp_servers import linux_desktop as mod

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_A))
    result = json.loads(mod.press_key("Return", window="Discord"))
    assert result["ok"] is True
    assert [e[0] for e in stub.events] == ["focus", "key"], (
        "focus must happen before the key is pressed"
    )
    assert stub.events[0][1] == _WIN_B["id"]
    assert stub.events[1][1] == ["Return"]
    assert result["window"]["id"] == _WIN_B["id"]
    assert result["window"]["title"] == _WIN_B["title"]
    assert _WIN_B["title"] in result["detail"]


def test_press_key_unknown_window_lists_candidates_and_presses_nothing(monkeypatch):
    from claire.mcp_servers import linux_desktop as mod
    from claire.mcp_servers.linux_desktop import WindowError

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_A))
    with pytest.raises(WindowError) as exc:
        mod.press_key("Return", window="calculator")
    message = str(exc.value)
    assert _WIN_A["title"] in message
    assert _WIN_B["title"] in message
    assert stub.events == [], "no key may be pressed on a missed name"


def test_press_key_ambiguous_window_lists_candidates_and_presses_nothing(monkeypatch):
    from claire.mcp_servers import linux_desktop as mod
    from claire.mcp_servers.linux_desktop import WindowError

    voice = {"id": "0x01c00003", "title": "Discord — voice"}
    afk = {"id": "0x01d00004", "title": "Discord — afk"}
    stub = _stub_desktop(
        monkeypatch, [_WIN_A, voice, afk], active=_xid(_WIN_A)
    )
    with pytest.raises(WindowError) as exc:
        mod.press_key("Return", window="discord")
    message = str(exc.value)
    assert voice["title"] in message
    assert afk["title"] in message
    assert stub.events == [], "no key may be pressed on an ambiguous name"


def test_press_key_refuses_when_focus_does_not_land(monkeypatch):
    """Same contract for press_key: a non-landing focus raises, both windows
    are named, and the key is never sent."""
    from claire.mcp_servers import linux_desktop as mod
    from claire.mcp_servers.linux_desktop import WindowError

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_A))
    stub.refuse_focus = True
    with pytest.raises(WindowError) as exc:
        mod.press_key("Return", window="Discord")
    message = str(exc.value)
    assert _WIN_B["title"] in message, "names the window aimed at"
    assert _WIN_A["title"] in message, "names the window focused"
    assert "nothing was sent" in message
    assert [e[0] for e in stub.events] == ["focus"], "the key must not be sent"


def test_press_key_without_window_reports_the_active_window(monkeypatch):
    from claire.mcp_servers import linux_desktop as mod

    stub = _stub_desktop(monkeypatch, [_WIN_A, _WIN_B], active=_xid(_WIN_A))
    result = json.loads(mod.press_key("ctrl+c"))
    assert result["ok"] is True
    assert [e[0] for e in stub.events] == ["key"], "no focus request needed"
    assert result["window"]["id"] == _WIN_A["id"]
    assert result["window"]["title"] == _WIN_A["title"]
    assert _WIN_A["title"] in result["detail"]


def test_window_parameter_is_visible_in_signature_and_schema():
    """The planner only learns about `window` from the schema and the tool
    description, so both must carry it."""
    import asyncio
    import inspect

    from claire.mcp_servers import linux_desktop as mod

    for name in ("type_text", "press_key"):
        params = inspect.signature(getattr(mod, name)).parameters
        assert "window" in params, f"{name} must accept window="
        assert params["window"].default is None, "window must be optional"

    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    for name in ("type_text", "press_key"):
        schema = tools[name].inputSchema
        assert "window" in schema["properties"], (
            f"{name}'s schema must expose window"
        )
        assert "window" not in schema.get("required", []), (
            f"window must stay optional in {name}'s schema"
        )
        description = tools[name].description or ""
        assert "window" in description, "the docstring must explain window"
        assert "focused" in description, (
            "the description must tell the planner the window is focused first"
        )


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
