# The Control Channel (R-CTRL-1)

A local Unix socket the daemon serves so that an attached UI can reach her brain, carrying
the **same line-framed protocol she already speaks on stdin and stdout** — because when
systemd runs the daemon, the desktop app owns neither of those streams and the chat box
had no channel at all.

## Scope

- Files: `src/claire/control/server.py` (transport), `src/claire/control/client.py` (the
  other end), re-exported by `src/claire/control/__init__.py`.
- Daemon wiring: `_handle_control_line`, `_build_control_socket_server`,
  `_start_control_socket_server`, `_stop_control_socket_server` in `src/claire/daemon.py`,
  plus one `broadcast` call inside `_emit_ipc_event`.
- Tests: `tests/test_control_socket.py` (transport), `tests/test_daemon_control_socket.py`
  (the daemon's half).
- Stdlib only: no Qt, no network, no config file, nothing persisted. The client is
  importable from GUI code, so it imports the protocol prefixes *inside* its methods rather
  than at module import, and it never connects at import time.
- Container-isolation note: `tests/conftest.py::_isolate_control_socket_path` points
  `CLAIRE_CONTROL_SOCKET` at a per-session tempfile, so a test that boots `main()` cannot
  create or replace the socket of the Claire the owner is actually running.

## Where the socket lives

A list, walked in the same order by both ends: `$XDG_RUNTIME_DIR/claire/control.sock` first,
then `~/.local/share/claire/control.sock`. The daemon keeps the first path it can bind; the
client connects to the first one that answers, and its `path` afterwards names that one.
`$CLAIRE_CONTROL_SOCKET` overrides the whole list *exclusively* — an explicitly named path
never falls back, which is what lets an isolated instance run beside the real one and stops
a second Claire from reaching a daemon that is not hers. `CLAIRE_CONTROL_CHANNEL=0` switches
the channel off, mirroring `CLAIRE_PHONE_BRIDGE=0`, and says so on stdout rather than going
quiet.

The fallback is not decoration: on this machine `claire.service` sets
`ProtectSystem=strict` with the user's home directories as the only writable paths, and
under that sandbox binding a Unix socket under `/run/user/<uid>` fails with `EROFS` while
the data directory succeeds. Verified directly under the same sandbox properties. The
runtime directory is still preferred, because it is cleared on logout and a stale socket
cannot outlive the session there.

## The failure it closes

The desktop app can spawn the daemon itself, in which case chat travels over stdin/stdout.
When systemd runs the daemon instead — the owner's configuration — its stdin is `/dev/null`
and its stdout is the journal's socket, owned by systemd. The app still marked chat as
available, so a typed message ran the "bundled" branch of `chat_window._send` in the *app's*
process, where no daemon was ever booted. That emitted `complete(None)` immediately, and
`_on_complete` appends nothing for a falsy reply: the owner watched "Claire is thinking…"
appear and vanish, and got no answer and no error. Silence was the defect.

## The protocol

One message per line: `PREFIX + json.dumps(payload) + "\n"`, using the daemon's own
constants, so `chat_window`'s existing parser reads socket lines unchanged.

- UI → daemon: `__CHAT_QUERY__:{"text": "..."}`, bare `__CHAT_CANCEL__`, bare
  `__CHAT_NEW_SESSION__`, `__CHAT_REWIND__:…`, `__CHAT_RESTORE__:…`,
  `__TURN_TRACE__:{"last": N, "stage": "router"}`
- daemon → UI: `__CHAT__:{"type": ..., "data": ...}`, `__VOICE__:…`, diary chunks,
  `__TURN_TRACE__:{"type": "records", "data": {"records": [...], "path": ..., "enabled": ...}}`

`__TURN_TRACE__:` returns the last N per-turn latency records (work item 7.1) so
the panel can show why a turn was slow without reading
`~/.local/share/claire/turns.jsonl` itself; `"stage"` ranks by one stage's
duration, matching `scripts/turn_trace.py`. The records are the file's own JSON
objects and carry no question or answer text unless `voice_debug` put it there
(`src/claire/turn_trace.spec.md`).

`_handle_control_line` tries the daemon's own `handle_*_stdin_line` handlers in order.
Nothing new is accepted, which is the point: there is no second protocol to keep in step.

## The rules, and the bug each one prevents

1. **Byte-identical framing.** The socket writes what stdout writes, prefix included. A
   "cleaner" protocol of its own would need a second parser in the app, and the two would
   drift — the event stream is the contract, not the transport.
2. **Mode 0600, in a directory 0700, before `listen()`.** A Unix socket has no network
   surface: there is no address to bind by mistake, no firewall to get wrong, and access is
   the filesystem's job. `chmod` runs between `bind` and `listen` on purpose — a file
   created under the process umask would otherwise be connectable by another local account
   for that instant.
3. **A path that cannot be bound is skipped; a path another Claire serves is not.** The
   sandbox case above is an environment fact, not a misconfiguration to fail on, so the
   server moves to its next candidate and logs why it did. A path that *answers* is
   different in kind: falling through there would leave two daemons serving two paths,
   with each client reaching whichever one its own list prefers — the exact confusion this
   channel removes. `ControlSocketInUseError` therefore stops the walk.

4. **A stale socket file is replaced; a live one is not stolen.** A crash leaves the file
   behind, and `bind` refuses an existing path, so the next start must clear it. But a file
   that *answers* means another Claire is serving, and stealing it would leave two daemons
   fighting over one UI. The difference is decided by connecting, not by looking at the
   file's age.
5. **Never delete what is not a socket.** If the path holds a regular file, refuse and say
   so. "Remove whatever is in the way" is how a typo in the path becomes data loss.
6. **`broadcast` never raises, and drops only the client that failed.** It runs on the voice
   thread and on chat worker threads: one UI window closing, or a socket failing mid-write,
   must cost that window and nothing else. The event is not retried — an event stream is a
   live view, and a client that was not listening seeds itself from the dialogue memory or
   the journal.
7. **One bad line never drops a connection, and one bad handler never kills the daemon.**
   Malformed JSON, a non-string `text`, or an unknown prefix is logged and ignored; a
   handler that raises is contained and the line is treated as recognised, because trying
   the remaining handlers on a line that was already ours produces a second, more confusing
   failure. A non-string `text` must never reach `submit_text_query`'s `str` API.
8. **`ControlClient.connect()` answers `False` instead of raising.** A UI has to ask "is
   there a daemon?" on a timer; every failure mode has the same answer and none of them
   deserve an exception in a paint loop.
9. **`read_line` returning `None` is not proof of a closed channel.** Check `connected`:
   `None` means "nothing arrived in time" *or* "the other end closed", and a reconnect loop
   that cannot tell those apart either spins or gives up.
10. **The channel announces itself at startup, and announces its own absence.** A silent
   control channel is exactly the bug being fixed: `🔌 Control socket ready at <path>` on
   success, `⚠️ Control socket unavailable: <reason>` on failure, and a named line when it
   is switched off.

## Honest limits

- A broadcast emitted in the microseconds between a client's `connect()` returning and the
  server's `accept()` adopting it goes nowhere. In the real flow that window cannot matter —
  a UI's own query is what makes the daemon answer it, and the daemon has necessarily read
  from that connection before it replies — but an *unsolicited* event (a timer, a voice
  rejection) racing a just-opened window can be missed. The client is expected to resync
  from dialogue memory.
- The socket is not a general RPC surface: it accepts exactly the stdin vocabulary. Anything
  that needs a new command needs a stdin command first.
- Nothing survives a restart. Stop removes the socket file; the daemon rebuilds it next
  start, and a leftover file from a crash is replaced (rule 4).
- Two clients is the tested case, not a crowd: `broadcast` is a plain loop over a set, with
  no per-client queue, so a UI that stops reading blocks on its own thread and nowhere else.
