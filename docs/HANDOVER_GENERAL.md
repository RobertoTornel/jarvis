# Claire — general handover

**Snapshot: 2026-10-13, at `e222100` on `claire-rewrite` (pushed to `personal`).** Start here. This
page is the *general description*: what Claire is, what the running system is made of, how to operate
it, how to change it without fooling yourself, where everything lives, and what is still open. Each
claim is labelled as **measured**, **verified live**, or **unverified** — the code is right where this
page and the code disagree, and the fix report holds the evidence for anything marked measured.

For depth, in this order: `docs/HANDOVER.md` (the previous, much longer handover — still the best
account of the hardening programme), `STATUS.md` (wave-by-wave build log, including the corrections),
`CLAIRE_FIX_REPORT_2026-10-12.md` in `/home/mana/Documents/Jarvis/` (outside the repository) (the measured latency work and the
subsystem fixes), and the `*.spec.md` file next to whatever code you are about to touch.

---

## 1. What Claire is

A local, always-on personal voice assistant on one Linux desktop, built as a personal fork of
`isair/jarvis`. You say **"Claire"** — a curated set of mishearings of the name is accepted
(`clair`, `clare`, `claira`, `claya`, `klair`, `klare`, `claires`, configurable as `wake_aliases`),
and the name is stripped from the question whether it leads or trails it — and she answers out loud
in a cloned voice. She is not a chatbot in a window: the primary
surface is a room, a microphone and a speaker, and the whole design is aimed at a turn that feels
like conversation rather than a request-response cycle.

- **Voice.** Wake word → parakeet TDT 0.6b on CPU → intent judge (may be skipped) → tool router →
  planner → tool calls → answer → Fish Audio TTS with a cloned voice. `piper`/`chatterbox` remain as
  offline fallback voices.
- **Brain.** All LLM traffic goes through **OmniRouter** on `http://127.0.0.1:20129/v1` (an
  OpenAI-compatible endpoint; `cmd/deepseek/deepseek-v4.1-flash`, plus a `-fast` model for cheap
  classification). Cloud is allowed by design in this fork, unlike upstream.
- **Hands.** Time, weather (Open-Meteo, no key), timers, spoken reminders and a diary, local files,
  web search and page fetch, screenshot and screen reading, meal logging, and window control through
  an MCP server called `linux-desktop`. Tools are discovered, not hard-coded: a tool-search tool and
  a per-turn router decide which handful are offered to the model.
- **Memory.** A SQLite database with a conversation log, a memory graph, periodic summaries and a
  recall gate that decides what is worth remembering. Text chat and voice share one conversation.
- **How you can reach her.** Voice in the room; the desktop app's tray, face window and chat window;
  a web panel on `127.0.0.1:8765`; and an Android phone client through the daemon's phone bridge on
  port `8788`, exposed over your tailnet at `http://100.104.1.69:8788`.

Two of those surfaces are weaker than they look and are called out in §7: the panel's chat is a
separate tool-less path, and the phone bridge is reachable by anything on the tailnet.

---

## 2. The shape of the system

Three systemd **user** units, plus the processes they raise:

```
claire.service          python -m claire            the daemon: ears, brain, mouth, timers,
  │                                                  reminders, phone bridge, control socket
  ├── linux-desktop MCP server (stdio child, DISPLAY=:0)
  └── control socket  ~/.local/share/claire/control.sock  (Unix, mode 0600)

claire-desktop.service  python -m desktop_app       the tray app: face window, chat window,
  │                                                  settings, log viewer; attaches to the daemon
  └── one connected control-socket client  ← this is how its chat box reaches her

claire-panel.service    python -m claire.panel.server   web panel on 127.0.0.1:8765

OmniRouter   127.0.0.1:20129/v1   (external to the repo; every model call goes through it)
```

The desktop app is deliberately a *view*: if a daemon is already running it attaches instead of
starting a second one, follows the unit's journal for logs, and speaks to her over the control
socket. When the app spawns the daemon itself (a bare checkout, or the bundled build) chat travels
over the child's stdin/stdout instead. **Verified live** that both halves work: a message sent to
the running daemon from outside the app came back as a real tool-backed answer.

### The path of one spoken turn, as measured

| stage | measured | note |
|---|---|---|
| trailing silence before dispatch | fixed 2.0 s | `voice_collect_seconds`; the "you stopped talking" wait |
| parakeet transcription | p50 0.96 s, p90 1.40 s | the one listening model; no fallback engine |
| intent judge | p50 3.22 s | an LLM call, skipped on the wake-word fast path |
| tool router | p50 1.93 s | an LLM call, skipped for small talk |
| planner | p50 1.87 s | skipped when the router names only system tools |
| answer | p50 2.79 s | written in full before speech starts; not streamed |
| TTS first audio | ~0.39 s | already per-sentence and streamed |
| **warm turn, total** | **~9.3 s** | silence 2.1 + stt 1.0 + reply 7.2 |

The floor is the gateway's time-to-first-byte (1.27–1.44 s per call) multiplied by the number of
calls a turn makes. The reliable wins were *removing* calls (small-talk fast path, skipping the
planner, skipping the judge) rather than making any one call faster. Streamed answers were designed,
measured and **retired** — see the dead ends in §7.

### Storage

| what | where |
|---|---|
| configuration | `~/.config/claire/config.json`, mode 0600 |
| secrets | OS keychain (`keyring`) or a 0600 env file — never in the repo, never in a log |
| database (conversation, meals, memory) | `~/.local/share/claire/claire.db` |
| speech model | `~/.local/share/claire/models/parakeet-tdt-0.6b-v2/` |
| control socket | `~/.local/share/claire/control.sock` (runtime dir first, see below) |
| caches | `~/.cache/claire`, `~/.cache/huggingface`, `~/.cache/numba` |
| schedule / diary | resolved by `claire.schedule.store` (`$CLAIRE_SCHEDULE_PATH` overrides) |
| logs | the journal: `journalctl --user -u claire -f` |

### The control channel

The newest piece of plumbing, and the answer to "why does the chat box in her own window do
nothing". With systemd running the daemon, the app owns neither its stdin (`/dev/null`) nor its
stdout (the journal's socket), so a typed message had nowhere to go and failed silently. The daemon
now serves a Unix socket — no port, no network, mode 0600 inside a 0700 directory — that carries the
**same line-framed protocol as stdin**, by calling the same handlers, so the two transports cannot
drift. Both ends look for it in the same two places, in this order:

1. `$XDG_RUNTIME_DIR/claire/control.sock` — per-user, cleared on logout.
2. `~/.local/share/claire/control.sock` — the fallback, and the one in use on this machine, because
   `claire.service` sets `ProtectSystem=strict` with writable paths only under the home directory,
   and binding a socket under `/run/user/<uid>` is refused there with `EROFS`.

`CLAIRE_CONTROL_SOCKET` overrides the list **exclusively**, which is what lets a test instance run
beside the real one without reaching it. `CLAIRE_CONTROL_CHANNEL=0` switches the channel off.
The full rules and the reasoning are in `src/claire/control/control.spec.md`.

---

## 3. Operating it

Everything is a user unit, so no `sudo` is involved.

```bash
systemctl --user status claire claire-desktop claire-panel    # what is running
systemctl --user restart claire                               # restart the brain (voice drops ~20 s)
journalctl --user -u claire -f                                # watch her think, live
journalctl --user -u claire-desktop -f                        # app-side: attach, chat link, journal tail
```

| I want to… | do this |
|---|---|
| check the whole install | `.venv/bin/python -m claire.platform.service --self-check` (add `--json` to script it) |
| talk to her in text | the desktop app's chat window (it reaches the real brain now), or the panel's box |
| open the panel | `http://127.0.0.1:8765/` |
| use the phone | `http://100.104.1.69:8788` from a device on the tailnet |
| change a setting | the app's Settings window, or edit `config.json` and restart the daemon |
| stop her talking / stop the turn | say "stop", or the tray's Stop Listening (`systemctl --user stop claire` when systemd owns her) |
| see what the model was told | `debug_log` output in the journal; the answer prompt is assembled in `src/claire/reply/` |
| test a change without touching her | §4's isolated instance recipe |

### Two things that will confuse you

- **"Listening" is claimed early.** `python -m claire` prints `🎙️ Listening!` about 2 s after start,
  while parakeet is still restoring (~18–19 s on this host). Speech in that window is heard but not
  transcribed. **Measured**, still unfixed, and the honest fix is to delay or reword the banner.
- **A restart is not invisible to the audio device.** The daemon holds the microphone; restarting it
  makes PulseAudio re-negotiate, so a turn in flight is lost. Restart between turns, not during one.

### Going further: the isolated instance

To exercise the real pipeline without touching the owner's config, database or audio devices (this is
what the sessions behind the fix report used):

```bash
pactl load-module module-null-sink sink_name=claire_test_in
pactl load-module module-null-sink sink_name=claire_test_out
# then boot the daemon with:
#   PULSE_SOURCE=claire_test_in.monitor  PULSE_SINK=claire_test_out
#   XDG_CONFIG_HOME=/tmp/claire_test_cfg  CLAIRE_CONFIG_PATH=/tmp/claire_test_cfg/claire/config.json
#   CLAIRE_CONTROL_SOCKET=/tmp/claire_test_data/control.sock  CLAIRE_SCHEDULE_PATH=/tmp/...
#   CLAIRE_PHONE_BRIDGE=0
```

That rig is how a whole turn was driven, over the control socket, with no microphone and no display.

---

## 4. Changing it without fooling yourself

```bash
cd /home/mana/Documents/jarvis_cloud
env -u DISPLAY nice -n 19 .venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider
```

Takes about five minutes and is the only gate that counts. As of this snapshot: **5914 passed, 4
skipped, 25 deselected, 0 failed** (`e222100`). Conventions and traps that cost real time:

- **A test module without `pytestmark = pytest.mark.unit` is silently *deselected*, not failed.**
  `tests/test_test_files_are_marked.py` exists to catch this; the gate spent weeks quietly skipping
  the fork's own tests before anyone noticed.
- **Run one full gate at a time.** Two concurrent runs compete for CPU and produce failures that are
  purely load, and every one of them costs more time to investigate than it saved.
- **Qt tests are headless** (`QT_QPA_PLATFORM=offscreen`, set in `tests/conftest.py`), and nothing in
  the suite may create or replace the owner's real control socket — an autouse fixture points
  `CLAIRE_CONTROL_SOCKET` at a per-session temp file.
- **Spec files are contracts.** Every subsystem has a `*.spec.md` next to it; change the behaviour and
  the spec together, or ask. `AGENTS.md` holds the house rules: never print or log a secret, never
  claim an action you did not perform, emoji-led lines with indentation for command output, and
  `debug_log` at the points that matter rather than everywhere.
- **Break it on purpose before you believe the test.** The working method in this repository is:
  reproduce the bug first (one command, quoted in the commit), write the test, then break the fix
  deliberately and watch the test fail. Several tests in this tree exist because the first version
  caught nothing at all.
- **Never trust a report, including this one.** Every measurement in the fix report names the command
  or the probe behind it; a claim without one is a lead, not a fact. This includes subagent reports:
  in the round that produced the control socket, two delegated halves ran for over an hour and wrote
  nothing while reporting progress.
- **Commit often and push to `personal`.** Upstream is `origin` (`isair/jarvis`); this fork's work
  lives on `claire-rewrite` and is pushed to `personal` (`RobertoTornel/jarvis`). Write Conventional
  Commit messages that say *why*, quoting the reproduction.

---

## 5. Where everything lives

| path | what |
|---|---|
| `src/claire/` | the daemon and every subsystem (`listening/`, `reply/`, `tools/`, `memory/`, `schedule/`, `senses/`, `phone/`, `panel/`, `outreach/`, `platform/`, `control/`) |
| `src/desktop_app/` | the tray application: `app.py` (tray, lifecycle, IPC), `chat_window.py`, `face_window.py`, `settings_window.py`, `control_link.py` |
| `src/claire/mcp_servers/linux_desktop.py` | the window-control MCP server (needs `DISPLAY=:0`) |
| `android/` | the phone client (Gradle project) |
| `deploy/` | the unit files (`claire.service`, `claire-desktop.service`, `claire-panel.service`) and a Dockerfile |
| `start.sh` | installer: substitutes the checkout path into the units and enables them |
| `scripts/`, `evals/`, `templates/`, `examples/` | operational scripts, behavioural evals, prompt/config templates |
| `docs/` | architecture, configuration, memory, phone client, panel, MCP servers, audio routing, cloud policy, this handover and the long one |
| `tests/` | ~5.9 k unit tests, plus `evals/` for end-to-end behaviour |
| `.venv/` | the virtualenv everything runs from (the units use it directly; the package is not pip-installed) |

---

## 6. Current state

- **Branch `claire-rewrite` at `e222100`**, working tree clean, `personal` up to date, 143 commits
  ahead of `origin/develop`. Everything described here is committed and pushed.
- **Live on this host** (after the 09:21 restart): `claire` PID 1477, `claire-desktop` PID 2043,
  `claire-panel` PID 1471, all `active`; the control socket bound at
  `~/.local/share/claire/control.sock` (mode 0600) with the desktop app connected to it.
- **Verified live this round:** a chat message sent to the running daemon from outside the app
  returned a real answer produced by a `getWeather` tool call.
- **Recent rounds, newest first:** the control channel and the chat fix (chat over a socket, the
  attach path marking her as listening, the two-path socket resolution); spoken reminders and
  targeted typing; timers; the API-key repr leak; the native-rate test flake; the measured latency
  programme (fast paths, planner skipping, configurable follow-up window); tool truthfulness
  (`getWeather` on Open-Meteo, honest web-search degradation, working screenshots).
- **Only tested, not exercised end to end:** the camera sense, the outreach channels (no credentials
  are configured), and the Android client against the current daemon.

---

## 7. What is still open

**Latency, and what not to try again.** The warm turn is ~9.3 s and the gateway's own TTFB is
1.27–1.44 s per call. These were measured and **failed**; do not spend more time on them: other LLM
providers (the gateway is the fastest route available here), "reduce reasoning" instructions,
router concurrency, shrinking the prompts, skipping the planner on tool turns, and streaming the
answer into speech (streaming made the first sentence *later* in practice, and the design was retired
with its measurement rather than left in the tree).

**Open items, with the owner's decision or action attached:**

1. `🎙️ Listening!` is printed ~17 s before she can actually transcribe (parakeet restore). Reword or
   delay the banner.
2. The panel's chat box is a separate tool-less path — it cannot reach the brain. The control socket
   is now the obvious way to fix that; it is not wired.
3. Web search needs a **Brave API key**; until then it answers from Wikipedia and says so.
4. The microphone chain is quiet end to end (source at 47 %, USB capture at 64 %): the *hardware*
   gain is the fix, not a threshold.
5. The gateway API key reached the journal once and should be rotated; the phone bridge on port 8788
   is reachable by anything on the tailnet and deserves an ACL or a rotated token.
6. Meal logging needs one product decision (how much detail to ask for) before its ~26 s flow is
   worth shortening.
7. Outreach (proactive messages) is built but switched off: it needs a phone number or a webhook, and
   a decision about when Claire may interrupt.
8. `tests/test_panel_chat.py::test_the_startup_line_does_not_claim_loopback_when_it_is_not` fails
   roughly once in a full run while passing alone — known cross-test interference, deliberately not
   papered over.

---

## 8. Document map

| read this | for |
|---|---|
| `README.md` | what the project is, install and first run |
| this file | the general picture, operating commands, open items |
| `docs/HANDOVER.md` | the long handover: the hardening programme, the gate's history, the measured wins |
| `STATUS.md` | the build log wave by wave, including its own corrections and retractions |
| `CLAIRE_FIX_REPORT_2026-10-12.md` (outside the repo) | measured latency budget, the tool fixes, and this round's verification |
| `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/MEMORY.md`, `docs/AUDIO_ROUTING.md`, `docs/PHONE_CLIENT.md`, `docs/MCP_SERVERS.md`, `docs/PANEL_CONVERSATION.md` | one subsystem each |
| `AGENTS.md` | the house rules for anyone (human or agent) changing this repository |
| `src/**/*.spec.md` | the contract for the file next to it — the most reliable description of behaviour |

---

## 9. If you are an agent taking over

The owner runs this machine and plays games on it; the rules below exist because breaking them cost
him real time.

- **Do not open windows or type on `DISPLAY=:0` while he is using it.** GUI verification goes through
  Xvfb or Qt's offscreen platform. Never start a second daemon, panel or web server: the units own
  those ports and the microphone, and a duplicate fights them for it.
- **Use the isolated-instance recipe** (§3) for anything that talks to audio, the database, or the
  control socket, so his live data and his microphone are never in the blast radius.
- **Verify with evidence, not assertion:** reproduce first, then test, then break the fix on purpose.
  Quote the command and its output in the commit. If you cannot verify something, say that plainly
  instead of implying success.
- **One full gate at a time, and re-run it after your last edit** — not before it.
- **Commit in small, explained steps and push to `personal`.** Freeze the conclusion in the message:
  what was broken, how it was proved, what was rejected and why.
- **When a brief's reading load is most of the work, do the work yourself or narrow the brief.** Two
  subagents once spent 75 minutes reading `daemon.py` and a 169 KB `app.py` and wrote nothing.

*Revision: written 2026-10-13 at `e222100`, replacing no existing document — `docs/HANDOVER.md`
remains the historical record. Update this page in the same commit as any change that makes it
wrong, and keep the labels (measured / verified live / unverified) honest.*
