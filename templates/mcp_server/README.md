# MCP server template

A runnable, minimal [MCP](https://modelcontextprotocol.io) server that Claire
launches over stdio. Copy it, fill in the placeholders, point Claire at the
copy — full instructions are in
[docs/MCP_SERVERS.md](../../docs/MCP_SERVERS.md).

```bash
# Start it by hand (it speaks the protocol on stdin/stdout):
python server.py
```

## What is in here

| File | Purpose |
|---|---|
| `server.py` | The server: one tool, one resource, one prompt. |
| `config.example.json` | The `mcps` entry that registers a copy with Claire. |

Only dependency is `mcp`, which Claire already installs
(`requirements.txt`). Nothing here imports Claire, so a copy stays
standalone.

## Fill-in points

Search `server.py` for `FILL IN`:

1. **`SERVER_NAME`** — pick a name and use the same key under `mcps` in
   `config.json`. Claire namespaces tools as `<server>__<tool>`.
2. **The tool** — this is the part Claire actually calls. Type the
   parameters, docstring each one, and return a string or a
   JSON-serialisable object. Raise an exception to report failure.
3. **The resource** — read-only data at a URI. The protocol negotiates
   it; Claire's client does not read resources yet.
4. **The prompt** — a reusable instruction template. Also negotiated but
   not consumed by Claire's client yet.

Then copy `config.example.json` into the `mcps` object of
`~/.config/claire/config.json` and replace the placeholder path.

## Rules the template already follows

- **Transport is stdio.** It is the only transport Claire speaks.
  Do not add HTTP/SSE.
- **stdout is protocol-only.** Diagnostics go to stderr, which Claire
  discards, or to a file.
- **The process is long-lived.** Claire holds one session open for the
  daemon's lifetime, so module-level state persists across calls. Set
  `"idle_timeout_sec"` on the config entry if a copy should be freed
  when idle instead.
- **No `from __future__ import annotations`.** FastMCP reads parameter
  annotations at import time; postponed evaluation turns them into
  strings and the server fails to start.
- **No absolute paths, ports, or secrets in the code.** Paths and keys
  belong in the `mcps` config entry, not in the server.

## Checking a copy

```bash
python -m pytest tests/test_mcp_server_template.py -q
```

That test runs this template against Claire's real MCP client, so a
green run means a copy of the same shape will be discovered and callable.
