# The face in the panel

The face is the small animated indicator in the web panel
(`src/claire/panel/server.py`) that shows which of Claire's five states she is
in. This note records what it reads, what each state looks like, and how to test
it by hand.

## Why the face is in the panel

The panel is the user interface (the desktop app remains available, see
`src/desktop_app/desktop_app.spec.md`), and a panel that shows everything except
what Claire is doing right now would be missing the one thing the owner glances
at most. The face is a status indicator, not a toy: it answers "is she asleep,
is she hearing me, is she thinking" at a glance, in a page that is otherwise
static text.

## The single channel: one file, and why it is not in /tmp

The daemon publishes the current state by writing the state's lowercase name —
no newline — to one file, every time it changes. The writer is
`ClaireStateManager` in `src/desktop_app/face_widget.py`; the daemon reaches it
through `claire/listening/state_manager.py`. The panel is a separate process
from the daemon, so the file is the only channel: no socket, no service, no
second writer. The panel reads it and never writes it.

The file lives under `$XDG_RUNTIME_DIR` rather than `/tmp` because `/tmp`
**does not work between systemd units**: `claire.service`,
`claire-panel.service` and `claire-desktop.service` all set `PrivateTmp=yes`, so
each service sees its own private `/tmp` and a file written by one unit is
invisible to the others. The state therefore travels through a directory under
`$XDG_RUNTIME_DIR` (`/run/user/<uid>`), which `PrivateTmp` does not isolate and
which all of the user's units share:

| | |
| --- | --- |
| Canonical path | `$XDG_RUNTIME_DIR/claire/claire_state` |
| Writer | `ClaireStateManager` (`desktop_app/face_widget.py`); creates the `claire/` directory if needed |
| Reader | `read_face_state()` in `src/claire/panel/server.py`; read-only, one read per request |
| Fallback | `tempfile.gettempdir()/claire_state`, used only when `XDG_RUNTIME_DIR` is unset (dev runs, non-systemd machines) |

The reader tries the shared path first and falls back to the temp-dir path. The
first candidate that *exists* decides: a newer writer's file is authoritative
even when it holds nothing usable, so a garbage shared file reports `asleep`
rather than resurrecting a stale value from the fallback. The read is total: a
missing, empty, unreadable or unrecognised file means `asleep`. A status
indicator must never guess a busier state than it can prove, and a read failure
must never break the page.

## The five states

The enum is `ClaireState` in `desktop_app/face_widget.py`; the daemon sets
`listening` while collecting speech, `thinking` while a query is answered, and
`speaking` while audio is out. What the panel draws for each:

| State | Eyes | Mouth | Motion (slow, all under 5 s) | Accent |
| --- | --- | --- | --- | --- |
| `asleep` | Closed, drawn as two lines | Flat line | None; the whole face is dimmed to 55% | None |
| `idle` | Open, normal pupils | Gentle smile | Breathing: a 4.5 s scale of 1.00 to 1.025 | None |
| `listening` | Open, pupils 35% larger | Gentle smile | Forward lean (subtle grow) plus a 1.6 s pulse of a thin ring around the head | The ring is the panel's single accent (`var(--accent)`) — this is the "I am hearing you" state |
| `thinking` | Open, pupils 38% smaller and raised | Flat line | A 2.8 s side-to-side pupil scan, a different rhythm from breathing | None |
| `speaking` | Open, normal pupils | Filled ellipse | The mouth opens and closes at 0.55 s — the fastest rhythm on the face — over a 2.8 s breathing | None |

The states are distinguished by *shape* and *rhythm*, never by colour alone:
closed versus open eyes, pupil size, three different mouths, and four different
animation speeds. The accent appears exactly once, on the listening ring.

## How the page stays current

`GET /face/state` is a read-only JSON route declared in `GET_ROUTES` and
dispatched through `GET_VIEWS` like the other reads. It reads the state file on
every request — the point of the route is that it is never stale — and answers:

```json
{"state": "thinking", "ok": true}
```

`ok` is `false` (with a `detail` naming the paths tried) whenever no usable file
was found, with `state` still `asleep` so the poller never has to tell "asleep"
from "could not tell". The response carries `Cache-Control: no-store` so the
browser's cache cannot sit between the poll and the file.

The index page embeds a few lines of inline vanilla JavaScript that fetch the
route every 500 ms (twice a second: responsive behind speech, trivial for a
loopback server), skip the fetch while the tab is hidden, and fail silent: a
refused or malformed reply leaves the last face on show. Polling was chosen
over Server-Sent Events deliberately — it needs nothing from the stdlib server
beyond an ordinary GET and cannot wedge a worker thread.

**No JavaScript, no problem.** The state is read server-side when the page is
rendered, so the face is drawn in the correct state before any script runs. With
JavaScript disabled the script never executes, the static face simply stays as
it was server-rendered, and nothing is logged to the console.

## Testing it by hand

Drive the file the panel can actually see — not `/tmp`, which the units keep
private — and watch the endpoint follow:

```sh
systemctl --user restart claire-panel.service && sleep 5
for s in asleep idle listening thinking speaking; do
  printf "$s" > "$XDG_RUNTIME_DIR/claire/claire_state"
  sleep 0.4
  curl -s http://127.0.0.1:8765/face/state; echo
done
printf banana > "$XDG_RUNTIME_DIR/claire/claire_state"  # must answer asleep
printf idle   > "$XDG_RUNTIME_DIR/claire/claire_state"  # leave it idle
```

Each of the five names must come back as itself and `banana` must answer
`"state": "asleep", "ok": false`. On the page itself, each state's face changes
shape and rhythm without a reload. The unit tests cover the same ground:

```sh
.venv/bin/python -m pytest tests/test_panel_face.py -q -p no:cacheprovider
```

The daemon resolves the shared path when its process starts, so the panel
reports `asleep` when only the shared path is populated until the daemon next
restarts. That is the honest answer, not a silent failure.

## Accessibility and reduced motion

* The face is `role="img"` with an accessible name that states the state in
  words ("Claire is thinking"), and the same words appear visibly — the pill in
  the card header and the sentence under the face. The browser updates all
  three together when the state changes.
* Colour is never the only cue: closed versus open eyes, pupil size, three
  mouths and four rhythms separate the states in a screenshot with the colour
  turned off.
* Every animation declaration sits inside
  `@media (prefers-reduced-motion: no-preference)`, so with reduce requested
  nothing moves at all — and the five faces remain distinguishable, because the
  shape rules live outside the media query. The listening ring is still drawn,
  just still.

## Deliberately not done

* **The desktop app stays.** `claire-desktop.service` and several tests depend
  on it, so it runs; its state-file path resolves through the shared path, which
  makes it work *across* units.
* **No Server-Sent Events, no framework, no build step, no dependency.** The
  panel remains one stdlib `http.server` module and one stylesheet string; the
  poller is inline vanilla JavaScript.
* **No face detail in `panel.spec.md`.** The spec's route list carries
  `GET /face/state`; the face's drawing, its five states and its motion are
  documented here.
* **No state write from the panel.** The panel is a reader only; the daemon
  remains the single writer, and the panel never creates the `claire/`
  directory (a directory that does not exist means no state was ever
  published, and `asleep` is the honest report).
* **No state mirror on error pages.** The face card is on the index page;
  refusal pages stay minimal.
