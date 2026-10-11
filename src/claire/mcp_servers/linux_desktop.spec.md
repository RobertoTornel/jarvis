## linux-desktop MCP server Spec

### Purpose

Give Claire hands on the computer she runs on: a first-party MCP server that
injects mouse and keyboard input, manages windows and launches applications on
X11, so a spoken request can be turned into action instead of advice.

### Problem

Claire's built-in tools read the machine (screenshot/OCR, local files, time,
web) but nothing built-in can *act* on it — the closest catalogue entry,
`macos`, only exists on Apple hardware. On this Linux fork a user asking
"click play" or "switch to the terminal" gets a description of what to do
rather than it being done.

### Design

The server lives at `src/claire/mcp_servers/linux_desktop.py` and ships inside
the `claire` package, so it is present wherever the daemon is. It is an
ordinary stdio MCP server started on the daemon's own interpreter:

```json
{ "mcps": { "linux-desktop": {
    "transport": "stdio",
    "command": "/path/to/.venv/bin/python",
    "args": ["-m", "claire.mcp_servers.linux_desktop"],
    "env": { "DISPLAY": ":0" } } } }
```

**The `env` block is load-bearing.** The MCP SDK spawns servers with a safe
env subset (`HOME`, `PATH`, `TERM`, …) that has no `DISPLAY`, and Xlib refuses
to connect without one. Setting any `env` key makes Claire's client pass the
full daemon environment merged with it; setting `DISPLAY` explicitly covers
both paths. As a last resort the server tries `:0` before failing.

Backends, chosen for what this machine actually runs (X11 / Mutter):

- **Input — XTEST via python-xlib.** No external binary and no shell.
  Characters resolve against the *live* keymap, so the correct physical key is
  hit on any layout: `keysym_to_keycodes` gives the (keycode, index) slot and
  an odd index means Shift. A character the layout does not carry (e.g. `ñ`
  on a plain `us` map) is typed by temporarily binding an unused keycode to
  its keysym and restoring the mapping afterwards — the same trick `xdotool`
  uses.
- **Windows — `wmctrl` (EWMH).** The single external binary; when it is
  missing the tool call fails with an actionable message rather than the
  server crashing. The active window comes from the `_NET_ACTIVE_WINDOW`
  root property over Xlib, so read-only inspection needs no binary at all.
- **Launching — `gtk-launch`, PATH lookup, `xdg-open`, in that order.**
  Arguments are split with `shlex`; never `shell=True`.

One lazy `Display` connection and one `threading.Lock` serialise every X
call: FastMCP may run tools concurrently and Xlib displays are not
thread-safe. A failed connection is dropped and retried once (suspend/resume
invalidates it). Diagnostics go through `debug_log` (stderr — Claire
discards the child's stderr). stdout carries protocol frames only, and the
module must not use `from __future__ import annotations`: FastMCP reads the
annotations at import time.

Screenshots stay with the built-in `screenshotOCR` tool; this server does not
duplicate it. The setup wizard shows the entry as a quick pick (it needs no
API key), mirroring the `macos` entry.

### Contract

Config key / server name: `linux-desktop`, so tools reach the model as
`linux-desktop__<tool>`. Protocol `2025-06-18`, tools only. Every tool
returns a JSON object as text or raises; a raised error arrives as a tool
error. All coordinates are root-relative screen pixels, `0 <= x < width`,
`0 <= y < height`; out-of-range or non-integer input is rejected before
anything is injected.

| Tool | Arguments | Behaviour |
|---|---|---|
| `get_screen_info` | — | `{"display", "width", "height", "pointer": {"x","y"}, "focused_window": {"id","title"} \| null}` |
| `list_windows` | — | `{"windows": [{"id","title","x","y","width","height","focused"}]}` from `wmctrl -lG`, focused flagged against `_NET_ACTIVE_WINDOW` |
| `focus_window` | `title` | Raise and focus via `wmctrl -i -a` |
| `close_window` | `title` | Request close via `wmctrl -ic` (the app may prompt) |
| `move_resize_window` | `title`, `x`, `y`, `width`, `height` | `wmctrl -i -r -e` |
| `click` | `x`, `y`, `button="left"`, `double=False` | Absolute motion then press/release; `button` is `left`/`middle`/`right` or `1`/`2`/`3` |
| `double_click` | `x`, `y` | `click(..., double=True)` |
| `move_mouse` | `x`, `y` | Absolute motion only |
| `scroll` | `x`, `y`, `amount=3`, `direction="up"` | Moves to `(x, y)` first, then presses buttons 4/5/6/7 (up/down/left/right); `amount` is 1–20 |
| `type_text` | `text` | Types literally, layout-aware; `\n` → Return, `\t` → Tab; text longer than 4096 chars is rejected |
| `press_key` | `key` | One key or a `+` combo: `Return`, `ctrl+shift+t`, `alt+tab`, `super`. A literal `+` must be written `plus`. Aliases: `enter`, `esc`, `space`, `backspace`, `delete`, arrows, `pageup`/`pagedown`, `f1`–`f12`, `win`/`cmd` → super |
| `launch_app` | `command` | `.desktop` id → `gtk-launch`; PATH binary → `Popen` (own session, no shell); existing file/URI → `xdg-open`; anything else is an error |
| `wait` | `seconds` | Sleep at most 10 s so the model can let a GUI settle |

**Window matching** (for the three window tools) is pure and shared:
case-insensitive **exact** title wins outright; otherwise a case-insensitive
substring must match exactly one window — several matches raise an ambiguity
error listing candidates, none raise a not-found error listing the visible
titles. Window ids are accepted as `0x…` hex and bypass title matching.

**Boundaries:** X11 only (Xwayland counts; native Wayland sessions must run
XWayland or fail with a message naming `DISPLAY`). The server injects only
when asked — no background input, no shell, no screenshot duplication. The
config entry itself is the consent switch: no `mcps` entry, no server.

### Testing

`tests/test_linux_desktop_mcp.py`:

- pure units with no display: combo parsing (`ctrl+shift+t` → tokens, `+`
  rejected, `plus` accepted), key name/alias → keysym, button and scroll
  button mapping, window matching (exact beats substring, ambiguity and
  not-found errors), char planning against a *fake* keymap (shift detection,
  unmapped character → `None`), launch command splitting;
- contract: the module exists, carries no postponed annotations, the
  catalogue entry converts to a valid `stdio` config with `DISPLAY`;
- integration (skipped without `DISPLAY`): the real server is launched
  through Claire's own `MCPClient`, all thirteen tools are discovered,
  `get_screen_info` reports this screen, `list_windows` parses, and invalid
  input (`button="banana"`, unknown key) comes back as a tool error without
  moving anything;
- injection (opt-in via `CLAIRE_MCP_DESKTOP_INJECT=1`, off by default): a
  `move_mouse` round trip verified through `get_screen_info`.
