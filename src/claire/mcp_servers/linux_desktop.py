"""linux-desktop — an MCP server that gives Claire hands on this computer.

Claire's built-in tools read the machine (screenshot/OCR, local files, time,
web) but nothing built-in can *act* on it. This server closes that gap: it is
an ordinary stdio MCP server, launched by Claire like any other, that injects
mouse and keyboard input, manages windows and launches applications.

Backends, chosen for what this kind of machine actually runs (X11/Xwayland):

* **Input** — XTEST fake events through python-xlib. No external binary and no
  shell. Characters are resolved against the *live* keymap, so the right
  physical key is hit on any layout; a character the layout does not carry is
  typed through a temporarily rebound spare keycode (the trick xdotool uses).
* **Windows** — ``wmctrl`` (EWMH). The one external binary; when it is missing
  the tool call fails with an actionable message rather than crashing.
* **Launching** — ``gtk-launch``, then PATH lookup, then ``xdg-open``. Never a
  shell.

stdout carries protocol frames only. Diagnostics go to ``debug_log`` (stderr,
which Claire's client discards); typed text and window titles are never logged.

Do NOT add ``from __future__ import annotations`` to this file: FastMCP reads
tool parameter annotations at import time, and postponed evaluation turns them
into strings, which breaks schema generation.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from Xlib import X, XK
from Xlib.ext import xtest
from mcp.server.fastmcp import FastMCP

try:  # pragma: no cover - the fallback only matters if Claire's config breaks
    from claire.debug import debug_log
except Exception:  # noqa: BLE001

    def debug_log(message: str, category: str = "debug") -> None:
        try:
            print(f"[{category:^10}] {message}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


# Must match the ``mcps`` key in config.json: Claire namespaces every tool as
# "<server>__<tool>", so the model calls these as "linux-desktop__click".
SERVER_NAME = "linux-desktop"

mcp = FastMCP(SERVER_NAME)

# ---------------------------------------------------------------------------
# Limits and lookup tables
# ---------------------------------------------------------------------------

MAX_TEXT_CHARS = 4096
MAX_WAIT_SECONDS = 10.0
MAX_SCROLL_AMOUNT = 20

_BUTTONS: Dict[str, int] = {
    "left": 1,
    "1": 1,
    "middle": 2,
    "2": 2,
    "right": 3,
    "3": 3,
}

_SCROLL_BUTTONS: Dict[str, int] = {
    "up": 4,
    "down": 5,
    "left": 6,
    "right": 7,
}

# Everything a model is likely to say, mapped to the canonical X keysym name.
_KEY_ALIASES: Dict[str, str] = {
    "enter": "Return",
    "return": "Return",
    "cr": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "space": "space",
    "spacebar": "space",
    "tab": "Tab",
    "backspace": "BackSpace",
    "back": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pgup": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "pgdn": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "prtsc": "Print",
    "printscreen": "Print",
    "print": "Print",
    "pause": "Pause",
    "menu": "Menu",
    "appmenu": "Menu",
    "capslock": "Caps_Lock",
    "numlock": "Num_Lock",
    "scrolllock": "Scroll_Lock",
    "plus": "plus",
    "minus": "minus",
    "comma": "comma",
    "period": "period",
    "dot": "period",
    "slash": "slash",
    "backslash": "backslash",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "quote": "apostrophe",
    "grave": "grave",
    "equal": "equal",
    "lbracket": "bracketleft",
    "rbracket": "bracketright",
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "win": "Super_L",
    "windows": "Super_L",
    "cmd": "Super_L",
    "command": "Super_L",
    "meta": "Super_L",
    "mod4": "Super_L",
    "lwin": "Super_L",
    "rwin": "Super_R",
}

_FKEYS: Dict[str, str] = {"f%d" % n: "F%d" % n for n in range(1, 25)}

_SHIFT_KEYSYM = XK.string_to_keysym("Shift_L")


class WindowError(RuntimeError):
    """A window could not be found, matched or manipulated."""


class X11Error(RuntimeError):
    """The X display could not be reached or used."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a display)
# ---------------------------------------------------------------------------


def parse_combo(key: str) -> List[str]:
    """Split ``"ctrl+shift+t"`` into ``["ctrl", "shift", "t"]``.

    A literal plus sign must be written ``plus``; an empty segment is an
    error rather than being guessed at.
    """
    raw = (key or "").strip()
    if not raw:
        raise ValueError("key must not be empty")
    tokens = [token.strip() for token in raw.split("+")]
    if any(not token for token in tokens):
        raise ValueError(
            f"{key!r} has an empty key segment — write a literal plus as 'plus'"
        )
    return tokens


def _keysym_for_char(ch: str) -> int:
    """Keysym for a single character, or 0 when there is no mapping."""
    if not ch:
        return 0
    keysym = XK.string_to_keysym(ch)
    if keysym:
        return keysym
    codepoint = ord(ch)
    if codepoint < 0x100:
        # Latin-1 keysyms are the code point itself.
        return codepoint
    if codepoint < 0x10000:
        # The X convention for everything else.
        return 0x01000000 | codepoint
    return 0


def keysym_for(name: str) -> int:
    """Resolve a key name (or single character) to an X keysym.

    Single characters keep their case — ``"A"`` is the shifted A, which is
    what makes ``press_key("A")`` type a capital rather than a lower-case a.
    """
    token = (name or "").strip()
    if not token:
        raise ValueError("key name must not be empty")
    if len(token) == 1:
        keysym = _keysym_for_char(token)
        if keysym:
            return keysym
        raise ValueError(f"cannot map the character {token!r} to a keysym")
    lowered = token.lower()
    alias = _KEY_ALIASES.get(lowered) or _FKEYS.get(lowered)
    if alias:
        token = alias
    keysym = XK.string_to_keysym(token)
    if not keysym:
        raise ValueError(f"unknown key {name!r}")
    return keysym


def button_number(button: str) -> int:
    """Map ``left``/``middle``/``right`` (or ``1``/``2``/``3``) to an X button."""
    key = str(button or "").strip().lower()
    if key in _BUTTONS:
        return _BUTTONS[key]
    raise ValueError(
        f"unknown mouse button {button!r} — use left, middle or right"
    )


def scroll_button_number(direction: str) -> int:
    """Map a scroll direction to its X wheel button (4 up, 5 down, 6/7 across)."""
    key = str(direction or "").strip().lower()
    if key in _SCROLL_BUTTONS:
        return _SCROLL_BUTTONS[key]
    raise ValueError(
        f"unknown scroll direction {direction!r} — use up, down, left or right"
    )


def _titles(windows: List[Dict[str, Any]], limit: int = 10) -> str:
    titles = [str(w.get("title", "")) for w in windows][:limit]
    return ", ".join(repr(t) for t in titles) if titles else "(none)"


def match_window(windows: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    """Pick the window the user means, or explain why that is impossible.

    A window id passes straight through; otherwise a case-insensitive exact
    title wins, and failing that a case-insensitive substring must be unique.
    """
    wanted = (query or "").strip()
    if not wanted:
        raise WindowError("the window query must not be empty")
    lowered = wanted.lower()
    for window in windows:
        if str(window.get("id", "")).lower() == lowered:
            return window
    folded = wanted.casefold()
    exact = [w for w in windows if str(w.get("title", "")).casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise WindowError(
            f"{len(exact)} windows are titled exactly {query!r}: {_titles(exact)} — "
            "use a window id instead"
        )
    partial = [w for w in windows if folded in str(w.get("title", "")).casefold()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise WindowError(
            f"{query!r} matches {len(partial)} windows: {_titles(partial)} — "
            "use a longer, unique part of the title or the window id"
        )
    raise WindowError(
        f"no window matches {query!r}; open windows: {_titles(windows)}"
    )


def plan_char(display: Any, ch: str) -> Optional[Tuple[int, bool]]:
    """Return ``(keycode, needs_shift)`` for typing ``ch``, or None.

    ``None`` means the current layout has no key for this character; the
    caller falls back to a temporarily rebound spare keycode. The keymap's
    slot index is what identifies Shift: odd slots are the shifted level.
    """
    keysym = _keysym_for_char(ch)
    if not keysym:
        return None
    try:
        pairs = list(display.keysym_to_keycodes(keysym))
    except Exception:  # noqa: BLE001 - fall through to the single lookup
        pairs = []
    if pairs:
        keycode, index = pairs[0]
        return int(keycode), bool(index % 2)
    keycode = display.keysym_to_keycode(keysym)
    if keycode:
        return int(keycode), False
    return None


def split_launch(command: str) -> List[str]:
    """Split a launch command the way a shell would, but without a shell."""
    try:
        argv = shlex.split(command or "")
    except ValueError as exc:
        raise ValueError(f"cannot parse the command {command!r}: {exc}") from exc
    if not argv:
        raise ValueError("the command must not be empty")
    return argv


# ---------------------------------------------------------------------------
# X11 backend: one lazy, reconnecting, serialised connection
# ---------------------------------------------------------------------------


class _X11:
    """A single X display shared by every tool.

    FastMCP may run tools concurrently and an Xlib display is not
    thread-safe, so every use is serialised. A dropped connection (suspend,
    server restart) is retried once with a fresh display.
    """

    def __init__(self) -> None:
        self._display: Any = None
        self._lock = threading.RLock()

    def _connect(self) -> Any:
        if self._display is None:
            if not os.environ.get("DISPLAY"):
                # Claire's client may spawn us with a sanitised environment;
                # the overwhelmingly common X display is :0.
                os.environ["DISPLAY"] = ":0"
                debug_log("DISPLAY was unset — trying ':0'", "mcp")
            try:
                from Xlib import display as xdisplay

                self._display = xdisplay.Display()
            except Exception as exc:  # noqa: BLE001
                raise X11Error(
                    "cannot open the X display "
                    f"(DISPLAY={os.environ.get('DISPLAY', 'unset')}): {exc} — "
                    "linux-desktop needs an X11 session (Xwayland counts)"
                ) from exc
        return self._display

    def _reset(self) -> None:
        display, self._display = self._display, None
        if display is not None:
            try:
                display.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self, fn: Callable[[Any], Any]) -> Any:
        """Run ``fn(display)`` under the lock, retrying once on failure."""
        with self._lock:
            last: Optional[BaseException] = None
            for attempt in (0, 1):
                try:
                    return fn(self._connect())
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    debug_log(
                        f"X operation failed (attempt {attempt + 1}): {exc}", "mcp"
                    )
                    self._reset()
            assert last is not None
            raise last


_X = _X11()


def _screen_size(display: Any) -> Tuple[int, int]:
    screen = display.screen()
    return int(screen.width_in_pixels), int(screen.height_in_pixels)


def _pointer(display: Any) -> Tuple[int, int]:
    pointer = display.screen().root.query_pointer()
    return int(pointer.root_x), int(pointer.root_y)


def _active_window_id(display: Any) -> Optional[int]:
    """X id of the focused window from ``_NET_ACTIVE_WINDOW`` (EWMH), if any."""
    try:
        root = display.screen().root
        atom = display.intern_atom("_NET_ACTIVE_WINDOW")
        prop = root.get_full_property(atom, X.AnyPropertyType)
        if prop is None or not prop.value:
            return None
        window_id = int(prop.value[0])
        return window_id or None
    except Exception:  # noqa: BLE001 - a missing property is not an error
        return None


def _decode_property(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value:
        if all(isinstance(item, int) for item in value):
            try:
                return bytes(value).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return None
    return None


def _window_title(display: Any, window_id: int) -> Optional[str]:
    """Title of a window by id, preferring the UTF-8 EWMH property."""
    try:
        window = display.create_resource_object("window", window_id)
        for prop_name in ("_NET_WM_NAME", "WM_NAME"):
            try:
                prop = window.get_full_property(
                    display.intern_atom(prop_name), X.AnyPropertyType
                )
            except Exception:  # noqa: BLE001
                continue
            title = _decode_property(getattr(prop, "value", None))
            if title:
                return title
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# Input injection
# ---------------------------------------------------------------------------


def _checked_point(x: int, y: int) -> Tuple[int, int]:
    """Validate a screen coordinate before anything is injected."""
    width, height = _X.run(_screen_size)
    try:
        px, py = int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise ValueError("x and y must be integers") from exc
    if not (0 <= px < width and 0 <= py < height):
        raise ValueError(
            f"({px}, {py}) is outside the {width}x{height} screen"
        )
    return px, py


def _move_pointer(display: Any, x: int, y: int) -> None:
    xtest.fake_input(display, X.MotionNotify, x=x, y=y, root=display.screen().root)


def _inject_click(display: Any, x: int, y: int, button: int, double: bool) -> None:
    root = display.screen().root
    _move_pointer(display, x, y)
    for _ in range(2 if double else 1):
        xtest.fake_input(display, X.ButtonPress, detail=button, root=root)
        xtest.fake_input(display, X.ButtonRelease, detail=button, root=root)
    display.sync()


def _inject_scroll(display: Any, x: int, y: int, button: int, amount: int) -> None:
    root = display.screen().root
    _move_pointer(display, x, y)
    for _ in range(amount):
        xtest.fake_input(display, X.ButtonPress, detail=button, root=root)
        xtest.fake_input(display, X.ButtonRelease, detail=button, root=root)
    display.sync()


def _tap(display: Any, keycode: int, shift: bool = False) -> None:
    """Press and release one key, optionally holding Shift around it."""
    shift_keycode = 0
    if shift:
        shift_keycode = int(display.keysym_to_keycode(_SHIFT_KEYSYM) or 0)
        if shift_keycode:
            xtest.fake_input(display, X.KeyPress, detail=shift_keycode)
    xtest.fake_input(display, X.KeyPress, detail=keycode)
    xtest.fake_input(display, X.KeyRelease, detail=keycode)
    if shift_keycode:
        xtest.fake_input(display, X.KeyRelease, detail=shift_keycode)


def _spare_keycode(display: Any) -> Optional[int]:
    """A keycode the layout does not use, scanning down from the top."""
    try:
        info = display.display.info
        lowest, highest = int(info.min_keycode), int(info.max_keycode)
    except Exception:  # noqa: BLE001
        lowest, highest = 8, 255
    for keycode in range(highest, lowest - 1, -1):
        try:
            mapping = display.get_keyboard_mapping(keycode, 1)[0]
        except Exception:  # noqa: BLE001
            break
        if not any(mapping):
            return keycode
    return None


def _tap_unmapped_char(display: Any, ch: str, spare: int) -> None:
    """Type a character the layout lacks via a temporarily rebound keycode.

    The mapping is changed and restored on one X connection, so the server
    (and every client watching MappingNotify) sees the rebind *before* the
    key event and the restore *after* it — the same ordering xdotool relies
    on.
    """
    keysym = _keysym_for_char(ch)
    if not keysym:
        raise ValueError(f"cannot type the character {ch!r}")
    original = display.get_keyboard_mapping(spare, 1)[0]
    display.change_keyboard_mapping(spare, [(keysym,)])
    try:
        _tap(display, spare)
    finally:
        display.change_keyboard_mapping(spare, [tuple(original)])


def _type_text(display: Any, text: str) -> int:
    """Type ``text``; returns how many characters were actually injected."""
    return_keysym = XK.string_to_keysym("Return")
    tab_keysym = XK.string_to_keysym("Tab")
    spare: Optional[int] = None
    typed = 0
    for ch in text:
        if ch == "\n":
            keycode = int(display.keysym_to_keycode(return_keysym) or 0)
            if not keycode:
                raise ValueError("this layout has no Return key")
            _tap(display, keycode)
            typed += 1
            continue
        if ch == "\t":
            keycode = int(display.keysym_to_keycode(tab_keysym) or 0)
            if not keycode:
                raise ValueError("this layout has no Tab key")
            _tap(display, keycode)
            typed += 1
            continue
        if ord(ch) < 32:
            # Other control characters are not something Claire should aim at
            # a window; skip them rather than inventing a key.
            continue
        plan = plan_char(display, ch)
        if plan is not None:
            keycode, shift = plan
            _tap(display, keycode, shift)
            typed += 1
            continue
        if spare is None:
            spare = _spare_keycode(display)
        if spare is None:
            raise ValueError(
                f"this keyboard layout has no spare keycode to type {ch!r} through"
            )
        _tap_unmapped_char(display, ch, spare)
        typed += 1
    display.sync()
    return typed


def _press_tokens(display: Any, tokens: List[str]) -> None:
    """Press a chord: every token down in order, then up in reverse."""
    planned: List[Tuple[int, bool]] = []
    for index, token in enumerate(tokens):
        if len(token) == 1 and index == len(tokens) - 1:
            plan = plan_char(display, token)
            if plan is None:
                raise ValueError(
                    f"the character {token!r} is not on this keyboard layout — "
                    "use type_text for literal characters"
                )
            planned.append(plan)
            continue
        keycode = int(display.keysym_to_keycode(keysym_for(token)) or 0)
        if not keycode:
            raise ValueError(f"key {token!r} is not on this keyboard layout")
        planned.append((keycode, False))

    pressed: List[int] = []
    shift_keycode = int(display.keysym_to_keycode(_SHIFT_KEYSYM) or 0)
    for keycode, shift in planned:
        if shift and shift_keycode and shift_keycode not in pressed:
            xtest.fake_input(display, X.KeyPress, detail=shift_keycode)
            pressed.append(shift_keycode)
        xtest.fake_input(display, X.KeyPress, detail=keycode)
        pressed.append(keycode)
    for keycode in reversed(pressed):
        xtest.fake_input(display, X.KeyRelease, detail=keycode)
    display.sync()


# ---------------------------------------------------------------------------
# Window management (wmctrl)
# ---------------------------------------------------------------------------


def _wmctrl(args: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    executable = shutil.which("wmctrl")
    if executable is None:
        raise WindowError(
            "wmctrl is not installed, so Claire cannot manage windows — "
            "install it (for example `sudo apt install wmctrl`) and try again"
        )
    try:
        proc = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise WindowError(f"wmctrl {' '.join(args)} timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WindowError(
            f"wmctrl {' '.join(args)} failed: {detail or f'exit code {proc.returncode}'}"
        )
    return proc


def _window_rows() -> List[Dict[str, Any]]:
    """Every managed window from ``wmctrl -lG``, minus the focused flag."""
    proc = _wmctrl(["-lG"])
    rows: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        window_id, _desktop, x, y, width, height, _host, title = parts
        try:
            rows.append(
                {
                    "id": window_id.lower(),
                    "title": title,
                    "x": int(x),
                    "y": int(y),
                    "width": int(width),
                    "height": int(height),
                }
            )
        except ValueError:
            continue
    return rows


def _windows_with_focus() -> List[Dict[str, Any]]:
    rows = _window_rows()
    active = _X.run(_active_window_id)
    active_text = f"0x{active:08x}" if active else None
    for row in rows:
        row["focused"] = row["id"] == active_text
    return rows


def _require_window(title: str) -> Dict[str, Any]:
    return match_window(_window_rows(), title)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_screen_info() -> str:
    """Report the screen: size, pointer position and the focused window.

    Call this before clicking anywhere so the coordinates are grounded in the
    real screen, and call it again after an action to confirm where the
    pointer ended up. Screenshots come from the built-in screenshotOCR tool.
    """
    width, height, px, py, active = _X.run(
        lambda display: (
            *_screen_size(display),
            *_pointer(display),
            _active_window_id(display),
        )
    )
    focused = None
    if active:
        focused = {
            "id": f"0x{active:08x}",
            "title": _X.run(lambda display: _window_title(display, active)),
        }
    return json.dumps(
        {
            "display": os.environ.get("DISPLAY"),
            "width": width,
            "height": height,
            "pointer": {"x": px, "y": py},
            "focused_window": focused,
        }
    )


@mcp.tool()
def list_windows() -> str:
    """List the open top-level windows with their ids, titles and geometry.

    Use it to find what is open and where it sits on screen; pass a title (or
    an id from here) to focus_window, close_window or move_resize_window.
    """
    windows = _windows_with_focus()
    return json.dumps({"windows": windows})


@mcp.tool()
def focus_window(title: str) -> str:
    """Raise and focus the window whose title contains `title`.

    `title` is matched case-insensitively; it must identify exactly one window
    (the error lists the candidates when it does not). A window id from
    list_windows also works.
    """
    window = _require_window(title)
    _wmctrl(["-i", "-a", window["id"]])
    return json.dumps(
        {
            "ok": True,
            "detail": f"focused {window['title']!r}",
            "window": window,
        }
    )


@mcp.tool()
def close_window(title: str) -> str:
    """Ask the window whose title contains `title` to close.

    This is the polite close (the same as clicking the X): an application with
    unsaved work may open a confirmation dialog instead of closing.
    """
    window = _require_window(title)
    _wmctrl(["-i", "-c", window["id"]])
    return json.dumps(
        {
            "ok": True,
            "detail": f"asked {window['title']!r} to close",
            "window": window,
        }
    )


@mcp.tool()
def move_resize_window(title: str, x: int, y: int, width: int, height: int) -> str:
    """Move and resize the window whose title contains `title`.

    Args:
        title: Part of the window title, or a window id.
        x: New left edge in screen pixels.
        y: New top edge in screen pixels.
        width: New width in pixels.
        height: New height in pixels.
    """
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    window = _require_window(title)
    _wmctrl(
        ["-i", "-r", window["id"], "-e", f"0,{int(x)},{int(y)},{int(width)},{int(height)}"]
    )
    return json.dumps(
        {
            "ok": True,
            "detail": (
                f"moved {window['title']!r} to ({int(x)}, {int(y)}) "
                f"at {int(width)}x{int(height)}"
            ),
        }
    )


@mcp.tool()
def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Move the pointer to (x, y) and click there.

    Args:
        x: Screen x coordinate in pixels.
        y: Screen y coordinate in pixels.
        button: "left", "middle" or "right".
        double: Send a double click instead of a single one.
    """
    number = button_number(button)
    px, py = _checked_point(x, y)
    debug_log(f"click button={number} at ({px}, {py}) double={double}", "mcp")
    _X.run(lambda display: _inject_click(display, px, py, number, double))
    return json.dumps(
        {
            "ok": True,
            "detail": (
                f"{'double-' if double else ''}clicked {button} at ({px}, {py})"
            ),
        }
    )


@mcp.tool()
def double_click(x: int, y: int) -> str:
    """Move the pointer to (x, y) and double-click with the left button."""
    px, py = _checked_point(x, y)
    _X.run(lambda display: _inject_click(display, px, py, 1, True))
    return json.dumps({"ok": True, "detail": f"double-clicked at ({px}, {py})"})


@mcp.tool()
def move_mouse(x: int, y: int) -> str:
    """Move the pointer to (x, y) without clicking."""
    px, py = _checked_point(x, y)
    _X.run(lambda display: (_move_pointer(display, px, py), display.sync()))
    return json.dumps({"ok": True, "detail": f"pointer moved to ({px}, {py})"})


@mcp.tool()
def scroll(x: int, y: int, amount: int = 3, direction: str = "up") -> str:
    """Move the pointer to (x, y) and scroll the wheel there.

    Args:
        x: Screen x coordinate in pixels.
        y: Screen y coordinate in pixels.
        amount: How many wheel steps, 1 to 20.
        direction: "up", "down", "left" or "right".
    """
    button = scroll_button_number(direction)
    if not 1 <= int(amount) <= MAX_SCROLL_AMOUNT:
        raise ValueError(f"amount must be between 1 and {MAX_SCROLL_AMOUNT}")
    px, py = _checked_point(x, y)
    _X.run(lambda display: _inject_scroll(display, px, py, button, int(amount)))
    return json.dumps(
        {
            "ok": True,
            "detail": f"scrolled {direction} {int(amount)} at ({px}, {py})",
        }
    )


@mcp.tool()
def type_text(text: str) -> str:
    """Type `text` into whatever window has focus, exactly as written.

    Newlines become Return and tabs become Tab. Up to 4096 characters at a
    time; characters the keyboard layout cannot produce are typed through a
    temporary keymap binding, so they still arrive correctly.
    """
    if not isinstance(text, str) or not text.strip():
        return json.dumps({"ok": True, "typed": 0, "detail": "nothing to type"})
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            f"text is {len(text)} characters long; the limit is {MAX_TEXT_CHARS} — "
            "send it in smaller pieces"
        )
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    typed = _X.run(lambda display: _type_text(display, normalised))
    debug_log(f"typed {typed} characters", "mcp")
    return json.dumps(
        {"ok": True, "typed": typed, "detail": f"typed {typed} characters"}
    )


@mcp.tool()
def press_key(key: str) -> str:
    """Press one key or a chord: "Return", "Tab", "ctrl+c", "ctrl+shift+t".

    Modifiers are held while the final key is pressed and released after it.
    Write a literal plus sign as "plus". Aliases such as enter, esc, space,
    pageup/pagedown and the arrow names all work.
    """
    tokens = parse_combo(key)
    # Resolve every token before touching the display: an unknown key must
    # fail without half-pressing a chord.
    for token in tokens:
        keysym_for(token)
    _X.run(lambda display: _press_tokens(display, tokens))
    debug_log(f"pressed {key}", "mcp")
    return json.dumps({"ok": True, "detail": f"pressed {key}"})


@mcp.tool()
def launch_app(command: str) -> str:
    """Launch an application, or open a file or URL.

    Accepts a desktop-entry id ("org.gnome.Calculator"), a program on PATH
    ("firefox --new-window"), or a file/URL path. Arguments are split without a
    shell, so quoting works but shell operators do not.
    """
    argv = split_launch(command)
    target = argv[0]

    if "://" in target:
        return _open_with_xdg(target)

    if "/" in target:
        path = os.path.expanduser(target)
        if not os.path.exists(path):
            raise ValueError(f"no such file or directory: {target}")
        if os.access(path, os.X_OK) and os.path.isfile(path):
            return _spawn([path, *argv[1:]], target)
        return _open_with_xdg(path)

    gtk_launch = shutil.which("gtk-launch")
    desktop_error = ""
    if gtk_launch:
        entry = target[: -len(".desktop")] if target.endswith(".desktop") else target
        try:
            proc = subprocess.run(
                [gtk_launch, entry], capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            desktop_error = str(exc)
        else:
            if proc.returncode == 0:
                return json.dumps(
                    {
                        "ok": True,
                        "detail": f"launched the desktop entry {entry!r}",
                    }
                )
            desktop_error = (proc.stderr or proc.stdout or "").strip()

    executable = shutil.which(target)
    if executable:
        return _spawn([executable, *argv[1:]], target)

    expanded = os.path.expanduser(target)
    if os.path.exists(expanded):
        return _open_with_xdg(expanded)

    hint = f" (gtk-launch said: {desktop_error})" if desktop_error else ""
    raise ValueError(
        f"cannot launch {command!r}: no desktop entry, no program on PATH and "
        f"no such file or URI{hint}"
    )


def _spawn(argv: List[str], label: str) -> str:
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise ValueError(f"could not start {label!r}: {exc}") from exc
    return json.dumps(
        {"ok": True, "detail": f"started {label!r} (pid {proc.pid})"}
    )


def _open_with_xdg(target: str) -> str:
    xdg_open = shutil.which("xdg-open")
    if xdg_open is None:
        raise ValueError(
            "xdg-open is not installed, so Claire cannot open files or URLs"
        )
    try:
        subprocess.Popen(
            [xdg_open, target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise ValueError(f"could not open {target!r}: {exc}") from exc
    return json.dumps({"ok": True, "detail": f"opened {target!r}"})


@mcp.tool()
def wait(seconds: float = 1.0) -> str:
    """Pause for up to 10 seconds so a window or page can settle.

    Use it between an action and the check that follows it, instead of
    guessing that the interface is ready.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("seconds must be a number") from exc
    if not 0.0 <= value <= MAX_WAIT_SECONDS:
        raise ValueError(f"seconds must be between 0 and {MAX_WAIT_SECONDS:g}")
    time.sleep(value)
    return json.dumps({"ok": True, "waited": value})


def main() -> None:
    debug_log(
        f"linux-desktop MCP server starting (DISPLAY={os.environ.get('DISPLAY', 'unset')})",
        "mcp",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
