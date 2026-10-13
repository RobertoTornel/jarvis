# MCP servers

[← Back to Claire](../README.md)

Claire can drive external tools that speak the **Model Context Protocol (MCP)**.
An MCP server is a small program that advertises tools over a defined wire
protocol; Claire launches it, discovers what it offers, and calls those tools
during a reply. This page is the reference for how that mechanism works, how to
build a server from the repository template, and what to check when one does not
come up.

Worked configurations for existing servers (Home Assistant, Google Workspace,
GitHub and others) live in [CONFIGURATION.md](CONFIGURATION.md#mcp-integrations).

## How Claire connects to a server

Everything below is implemented in `src/claire/tools/external/mcp_client.py`
(the connection and config handling), `src/claire/tools/external/mcp_runtime.py`
(the long-lived session), `src/claire/tools/registry.py` (discovery and
dispatch) and `src/claire/daemon.py` (startup discovery).

**Transport is stdio only.** Claire spawns the server as a subprocess and talks
to it over stdin/stdout. `transport` defaults to `"stdio"`; any other value is
rejected with `Unsupported MCP transport`. There is no HTTP/SSE path.

**Configuration is the `mcps` key.** `config.json` carries one object per
server, keyed by the name Claire will use for it:

```json
{
  "mcps": {
    "server_name": {
      "transport": "stdio",
      "command": "python3",
      "args": ["/absolute/path/to/server.py"],
      "env": { "SOME_TOKEN": "..." },
      "timeout_sec": 120,
      "idle_timeout_sec": 300
    }
  }
}
```

| Key | Required | Meaning |
|---|---|---|
| `transport` | no | Defaults to `"stdio"`. The only accepted value. |
| `command` | yes | Executable to launch. |
| `args` | no | Argument list; a leading `~` in a string argument is expanded. |
| `env` | no | Extra environment variables, merged over the daemon's own environment. |
| `timeout_sec` | no | Bounds one tool call. Default **120** seconds. |
| `idle_timeout_sec` | no | Frees the server subprocess after that long without activity. Unset keeps it resident for the daemon's lifetime. |

There is no `cwd` key: the server inherits the daemon's working directory. Use
absolute paths, or paths relative to the daemon's directory.

**The launch command is resolved, not just exec'd.** `command` is looked up on
`PATH`, then in the common install locations a GUI-launched app tends to miss
(Homebrew, `~/.volta/bin`, `~/.local/bin`, nvm and fnm version directories), and
finally through your login shell. An absolute path is used as-is and must exist.
The resolved command's directory is prepended to the child's `PATH` so
`#!/usr/bin/env node` shebangs find siblings.

**One session per server, kept open.** On first use Claire starts the
subprocess and completes the MCP handshake once; the session stays open for the
daemon's lifetime, and tool calls and re-discovery share it. This is what lets a
stateful server — browser automation holding a Chrome process, for example —
survive between calls. A config change (for that server) replaces the worker; a
crashed worker is replaced and the call retried once. Calls to a single server
are serialised, calls to different servers run in parallel. Set
`idle_timeout_sec` for a stateless server you would rather not keep resident.

**Tools are namespaced and discovered at startup.** At daemon start, every
configured server is asked for its tool list, and each tool is registered as
`<server>__<tool>` — a tool named `echo` on a server named `example` reaches the
model as `example__echo`. The daemon prints a per-server count:

```
📡 Discovering MCP tools from 2 server(s)...
  ✅ example: 1 tools available
```

A server that fails is reported by name and does not stop the others. To pick up
a server you added, or tools it started offering, restart the daemon or ask for
`refreshMCPTools`, which re-runs discovery against the cached config.

**Tool names enter a reply through the normal route.** Discovered MCP tools join
the tool catalogue with everything else. For a given turn the router and planner
choose the tools the model is allowed to use, and `toolSearchTool` can widen that
set mid-loop. When the model emits `example__echo`, Claire splits the name on the
first `__`, calls that server, and passes the flattened text of the result back
to the model as the tool's reply. A result carrying `isError` comes back as a
tool error with its text as the message.

## Ships with Claire: the `linux-desktop` server

One server does not need to be built or installed: `linux-desktop` lives in
`src/claire/mcp_servers/linux_desktop.py` and ships inside the `claire`
package, so it runs on the daemon's own interpreter. It is what lets a spoken
request become an action on this computer — moving the pointer, clicking,
typing, pressing keys, focusing or closing windows, launching applications.

Add it from **Settings → MCP Servers → Add from catalogue → 🖱️ Linux Desktop
Control**, or let the setup wizard add it (it needs no API key). The config it
writes is:

```json
{
  "mcps": {
    "linux-desktop": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "claire.mcp_servers.linux_desktop"],
      "env": { "DISPLAY": ":0" }
    }
  }
}
```

`env` matters here even though the server needs no secrets: the MCP SDK spawns
servers with a safe environment subset that has no `DISPLAY`, and Xlib cannot
connect without one. Passing any `env` value makes Claire hand the full daemon
environment through, and `DISPLAY` is set explicitly as well.

**Requirements.** An X11 session — Xwayland counts, a native Wayland session
without it does not. Input needs no external binary (XTEST over python-xlib
does the work), but the window tools need `wmctrl`:

```bash
sudo apt install wmctrl        # or the equivalent for your distribution
```

**Tools.** The model sees them prefixed as `linux-desktop__<name>`:

| Tool | What it does |
|---|---|
| `get_screen_info` | Screen size, pointer position, focused window |
| `list_windows` | Open windows with ids, titles and geometry |
| `focus_window`, `close_window`, `move_resize_window` | Window management by title or id |
| `click`, `double_click`, `move_mouse`, `scroll` | Pointer actions at screen coordinates |
| `type_text` | Types literal text, layout-aware; optional `window` raises and focuses the destination first |
| `press_key` | One key or a chord such as `ctrl+shift+t`; optional `window` raises and focuses the destination first |
| `launch_app` | A desktop entry, a program on PATH, a file or a URL |
| `wait` | Pause up to 10 s so the interface can settle |

Screenshots are deliberately **not** part of this server: the built-in
`screenshotOCR` tool already captures the screen, and this server only adds
the ability to act on what it shows.

**Safety.** The server only does something when a tool is called, and the tool
call comes from a reply in the conversation — there is no background input, no
shell and no file access. The config entry is the switch: remove it and Claire
has no hands.

## Build a server from the template

`templates/mcp_server/` is a runnable starting point: one tool, one resource,
one prompt, no imports from Claire. Build a server from it in a few steps.

1. **Copy the directory** somewhere outside the repository, or to wherever you
   keep local integrations. Nothing in it depends on this checkout.

   ```bash
   cp -r templates/mcp_server ~/mcp-servers/my_server
   ```

2. **Choose the server name.** Open `server.py`, set `SERVER_NAME` to the name
   you will use as the `mcps` key. It prefixes the tools the model sees.

3. **Fill in the tool.** Replace the `echo` body with the real work. The
   signature is the tool's input schema: type each parameter, give it a
   docstring line, and return a string or a JSON-serialisable object. Raise an
   exception to report failure — the model sees the error text.

4. **Fill in the resource and prompt, or delete them.** Claire speaks protocol
   `2025-06-18`, which carries tools, resources and prompts, and the template
   declares all three. Claire's client currently discovers and calls **tools
   only**: a resource or prompt you keep is reachable from any other MCP client
   but not yet from a spoken reply. Keep them only if something else will read
   them.

5. **Check it starts**, before Claire ever sees it:

   ```bash
   python server.py
   ```

   It waits silently for protocol frames on stdin, which is correct. All
   diagnostics go to stderr: stdout is reserved for protocol frames, and a stray
   `print()` there corrupts the stream.

6. **Register it with Claire.** Add a `mcps` entry to
   `~/.config/claire/config.json`, merging with any servers already there. Point
   `command` at the interpreter that has your dependencies, and `args` at the
   server file:

   ```json
   {
     "mcps": {
       "my_server": {
         "transport": "stdio",
         "command": "/home/you/mcp-servers/my_server/.venv/bin/python",
         "args": ["/home/you/mcp-servers/my_server/server.py"],
         "env": {}
       }
     }
   }
   ```

   `templates/mcp_server/config.example.json` is the same shape with
   placeholders. When you run the server with its own virtual environment, use
   that environment's interpreter here — the daemon does not activate it for
   you. A server that needs no dependencies beyond `mcp` can use the daemon's
   own interpreter.

7. **Restart Claire**, or say **"refresh MCP tools"**, and watch the startup
   discovery line list your server with its tool count.

8. **Call it in a real reply.** Ask for something that needs the tool, in the
   words you would naturally use. A correct result appears in Claire's answer;
   the daemon log shows the discovery count and any tool error.

### Scaffolding one on demand

Because the template is self-contained and its placeholders are explicit, a new
server can be scaffolded by copying `templates/mcp_server/` and working through
the `FILL IN` blocks in `server.py` — no other file in the repository needs to
change. Its README lists the fill-in points and the rules a copy must keep
(stdio transport, protocol-only stdout, no postponed annotations, no paths or
secrets in the code). A capability that is really a memory search, a lookup or a
wrapper around an existing CLI is a good candidate for this: write the tool
function, register the config entry, restart.

## Test a server

Three levels, cheapest first.

**Run it by hand.** Feed it an `initialize` frame and a `tools/list` frame; see
the [MCP specification](https://modelcontextprotocol.io) for the frames, or just
start it and confirm it stays alive without writing anything to stdout.

**Run the template test.** `tests/test_mcp_server_template.py` launches the
template against Claire's real MCP client, asserts the tool is discovered and
that a call round-trips, and separately confirms the negotiated capabilities and
the declared tool/resource/prompt. It is the pattern to copy for a new server:

```bash
.venv/bin/python -m pytest tests/test_mcp_server_template.py -q
```

**Check it from Claire's side.** The unit test proves the client contract; only
the daemon proves the wiring. Restart Claire and read the discovery lines — your
server should appear with a non-zero tool count. If the count is zero or the
server is named in an error line, the entry did not launch. Then call the tool
out loud; the tool name the model uses is `<server>__<tool>`. If discovery is
right but the tool never runs, the problem is routing rather than transport: the
router decides which tools a turn may use, so a tool that does not fit the
request is simply not offered to the model.

## Failure modes

| Symptom | Likely cause | What to check |
|---|---|---|
| Server named in an error line at startup; count 0 | The launch command did not run. | `command` and `args` are absolute and exist; the interpreter can `import mcp`; the file has no syntax error. Run the exact command line from the config by hand — Claire resolves `command` on `PATH` and through your login shell, but it does not expand `~` in `command`, only in `args`. |
| `Unsupported MCP transport` | `transport` is not `"stdio"`. | Remove the key or set it to `"stdio"`. HTTP and SSE servers need a stdio proxy. |
| The server starts but its tool never appears | The tool is not declared, or discovery is stale. | `tools/list` returns it (see the template test); then restart the daemon or ask for `refreshMCPTools`. A tool returns only after a daemon restart when you add a new server to the config. |
| The tool appears but a call times out | The tool runs longer than `timeout_sec`. | Raise `timeout_sec` on that server's entry. The default is 120 seconds. |
| A call fails once and succeeds on retry | The server subprocess died mid-call; the session was re-established. | Look at the server's own logging — Claire discards the child's stderr. A server that exits between calls needs to be fixed, not retried around. |
| Server not in the daemon's `PATH` when launched from the desktop app | GUI-launched processes get a minimal environment. | Use an absolute `command`. |
| Everything else is fine but the model never chooses the tool | Routing, not MCP. | Phrase the request so the tool matches it, or check that the tool's description says what it does and takes. |

Treat an MCP server as code you are choosing to run: it executes with the
daemon's privileges, so only configure servers you trust and keep secrets in
`config.json`, never in the server's source.
