# Claire: general handover

**Snapshot: 2026-10-14, at `2c4c9d4` on `main` (pushed to `personal`).** Start here. This
page is the *general description*: what Claire is, what the running system is made of, how to operate
it, how to change it without fooling yourself, where everything lives, what is still open, and what to
build next. Each claim is labelled as **measured**, **verified live**, or **unverified**: the code is
right where this page and the code disagree, and the fix report holds the evidence for anything marked
measured.

For depth, in this order: `docs/HANDOVER.md` (the long handover, still the best account of the
hardening programme), `STATUS.md` (wave-by-wave build log, including the corrections),
`CLAIRE_FIX_REPORT_2026-10-12.md` in `/home/mana/Documents/Jarvis/` (outside the repository) (the
measured latency work and the subsystem fixes), and the `*.spec.md` file next to whatever code you are
about to touch. The measurement programme lives outside the repository in
`/home/mana/Documents/Jarvis/optimizations/`: `latencyreport.md` (the current end-to-end verdict),
`latency_voice_path.md` (the voice path and the streaming timelines), `latency_reasoning.md` (the
reasoning investigation), `omniroute-latency-forensics.md` (the gateway) and `stage2_seeds.md`.

---

## 1. What Claire is

A local, always-on personal voice assistant on one Linux desktop, built as a personal fork of
`isair/jarvis`. You say **"Claire"**: a curated set of mishearings of the name is accepted
(`clair`, `clare`, `claira`, `claya`, `klair`, `klare`, `claires`, configurable as `wake_aliases`).
The name is stripped from the question whether it leads or trails it, and she answers out loud in a
cloned voice. She is not a chatbot in a window: the primary
surface is a room, a microphone and a speaker, and the whole design is aimed at a turn that feels
like conversation rather than a request-response cycle.

- **Voice.** Wake word → parakeet TDT 0.6b on CPU → intent judge (may be skipped) → tool router →
  planner → tool calls → answer → Fish Audio TTS with a cloned voice. `piper` and `chatterbox` remain
  as offline fallbacks, and **Fish is the default engine**: `tts_engine` defaults to `"fish"`
  (`src/claire/config.py` line 731 and line 1108), and the live config sets `fish_model` to a free
  model with no separate fast tier. **Verified** that the live `config.json` carries no `tts_engine`
  key, so config load resolves the Fish default.
- **Brain.** All LLM traffic goes through **OmniRouter** on `http://127.0.0.1:20129/v1` (an
  OpenAI-compatible endpoint). Both tiers resolve to the same model,
  `cmd/deepseek/deepseek-v4.1-flash`: there is no separate fast model, the owner's requirement is
  Command Code for both tiers, and the live config carries no `fast_model` key. On an
  OpenAI-compatible provider an absent fast model resolves to the chat model
  (`src/claire/llm/tiers.py` line 45, `src/claire/config.py` lines 1258 to 1261). **Verified live**
  that `resolve_model(cfg, Tier.FAST)` and `resolve_model(cfg, Tier.CHAT)` both return
  `cmd/deepseek/deepseek-v4.1-flash`. Cloud is allowed by design in this fork, unlike upstream.
- **Hands.** Time, weather (Open-Meteo, no key), timers, spoken reminders and a diary, local files,
  web search and page fetch, screenshot and screen reading, meal logging, and window control through
  an MCP server called `linux-desktop`. Tools are discovered, not hard-coded: a tool-search tool and
  a per-turn router decide which handful are offered to the model.
- **Memory.** Long-term memory lives in **Postgres with pgvector**: the always-loaded router document
  and the notes are rows in `memory_documents` and `memory_chunks`, and a search fuses vector search
  with full-text search by reciprocal rank (**verified** in `src/claire/memory/pg.py` lines 106 to
  107 and 762 to 779). Postgres is 17.11 with pgvector 0.8.7 and an HNSW index over `vector(768)`
  (**measured live** in the container). The conversation log, the diary, the memory graph, periodic
  summaries and the recall gate remain SQLite at `~/.local/share/claire/claire.db` (the graph uses
  `sqlite3` in `src/claire/memory/graph.py`). The Postgres pieces are
  `src/claire/memory/pg.py`, `pg_router.py`, `embedder.py` and `migrate_md_to_pg.py`, with
  `deploy/pgvector/` for the container. The DSN comes from `CLAIRE_PG_DSN`, in the environment or in
  `~/.config/claire/pg.env`, or from an explicit `pg_dsn` setting
  (`src/claire/memory/pg.py` lines 35 and 155). `docs/PGVECTOR.md` is the runbook and
  `docs/PGVECTOR_DESIGN.md` the design; **verified live** that `memory_backend` is `postgres`.
- **How you can reach her.** Voice in the room; the web panel on `127.0.0.1:8765`, which is the main
  user interface and where the face now lives; and an Android phone client through the daemon's phone
  bridge on port `8788`, bound to `100.104.1.69` on the `tailscale0` interface (**verified live**:
  `ss -ltnp` shows `100.104.1.69:8788` owned by the daemon, and `tailscale0` holds that address). The
  Qt desktop app (`src/desktop_app/`) is deprecated in favour of the panel (`docs/PANEL.md` line 15);
  its one load-bearing part is `ClaireStateManager` in `face_widget.py`, the single writer of the
  face-state file the panel reads.

Two of those surfaces are weaker than they look and are called out in §8: the panel's chat is a
separate tool-less path (`handle_chat` in `src/claire/panel/server.py` line 4459 reaches
`backend.direct` at line 1422), and the phone bridge's application layer is plain HTTP with a bearer
token, travelling inside the Tailscale WireGuard tunnel.

---

## 2. The shape of the system

Three systemd **user** units, plus the processes they raise:

```
claire.service          python -m claire            the daemon: ears, brain, mouth, timers,
  │                                                  reminders, phone bridge, control socket
  ├── linux-desktop MCP server (stdio child, DISPLAY=:0)
  └── control socket  ~/.local/share/claire/control.sock  (Unix, mode 0600)

claire-panel.service    python -m claire.panel.server   the web panel on 127.0.0.1:8765: pages,
  │                                                  the memory editor, and the face
claire-desktop.service  python -m desktop_app       the deprecated tray app; still runs, still
                                                     attaches to the daemon (see docs/PANEL.md)

OmniRouter   127.0.0.1:20129/v1   (external to the repo; every model call goes through it)
```

The desktop app is deliberately a *view*: if a daemon is already running it attaches instead of
starting a second one, follows the unit's journal for logs, and speaks to her over the control
socket. When the app spawns the daemon itself (a bare checkout, or the bundled build) chat travels
over the child's stdin/stdout instead. **Verified live** that both halves work: a message sent to
the running daemon from outside the app came back as a real tool-backed answer. New work belongs in
the panel, not here: `docs/PANEL.md` records the deprecation, and the panel reads the same face-state
file through `read_face_state()`.

### The path of one spoken turn, as measured

| stage | measured | note |
|---|---|---|
| trailing silence before dispatch | fixed 2.0 s | `voice_collect_seconds`; the "you stopped talking" wait |
| parakeet transcription | p50 0.96 s, p90 1.40 s | the one listening model; no fallback engine |
| intent judge | p50 3.22 s | an LLM call, skipped on the wake-word fast path |
| tool router | p50 1.93 s, cold 1.27–1.54 s | an LLM call, skipped for small talk |
| planner | p50 1.87 s, cold 1.04–2.55 s | almost free with a warm prefix (0.015 s); skipped when the router names only system tools |
| answer | p50 2.79 s | written in full before speech starts; the answer call is not streamed |
| TTS first audio | 0.717 s | Fish first audio byte, median of three (0.717, 0.682, 1.067), flat in sentence length and network-bound; the engine splits and pipelines sentences only after the whole reply arrives |
| **warm turn, to the answer starting** | **3.98–8.56 s** | the live-route range in `latencyreport.md`; the full end-to-end total including speech is **unverified** (the earlier ~9.3 s figure is not reproduced here) |

The floor is the gateway's time-to-first-byte (1.27–1.44 s per call) multiplied by the number of
calls a turn makes. The reliable wins were *removing* calls (small-talk fast path, skipping the
planner, skipping the judge) rather than making any one call faster. Streaming the answer into speech
is revisited in §7.3, which records the measurement that refuted it.

### Storage

| what | where |
|---|---|
| configuration | `~/.config/claire/config.json`, mode 0600 |
| secrets | OS keychain (`keyring`) or a 0600 env file: `~/.config/claire/claire.env`, and the Postgres DSN in `~/.config/claire/pg.env` (8 keys). Never in the repo, never in a log. |
| database (conversation, diary, memory graph, summaries) | `~/.local/share/claire/claire.db` (SQLite) |
| long-term memory | Postgres 17.11 + pgvector 0.8.7, container `claire-pg`, `127.0.0.1:5432`: the router document and the notes |
| speech model | `~/.local/share/claire/models/parakeet-tdt-0.6b-v2/` |
| acknowledgement clip cache | `~/.cache/claire/clips` (8 rendered clips) |
| control socket | `~/.local/share/claire/control.sock` (runtime dir first, see below) |
| caches | `~/.cache/claire`, `~/.cache/huggingface`, `~/.cache/numba` |
| schedule / diary | resolved by `claire.schedule.store` (`$CLAIRE_SCHEDULE_PATH` overrides) |
| logs | the journal: `journalctl --user -u claire -f` |

### The control channel

The answer to "why does the chat box in her own window do nothing". With systemd running the daemon,
the app owns neither its stdin (`/dev/null`) nor its stdout (the journal's socket), so a typed message
had nowhere to go and failed silently. The daemon
now serves a Unix socket (no port, no network, mode 0600 inside a 0700 directory) that carries
messages between surfaces; both ends look for it in the same two places, in this order:

1. `$XDG_RUNTIME_DIR/claire/control.sock`, per-user and cleared on logout.
2. `~/.local/share/claire/control.sock`, the fallback, and the one in use on this machine, because
   `claire.service` sets `ProtectSystem=strict` with writable paths only under the home directory,
   and binding a socket under `/run/user/<uid>` is refused there with `EROFS`.

`CLAIRE_CONTROL_SOCKET` overrides the list **exclusively**, which is what lets a test instance run
beside the real one without reaching it. `CLAIRE_CONTROL_CHANNEL=0` switches the channel off.
The full rules and the reasoning are in `src/claire/control/control.spec.md`.

---

## 3. Operating it

Everything is a user unit, so no `sudo` is involved.

```bash
systemctl --user status claire claire-panel claire-desktop   # what is running
systemctl --user restart claire                               # restart the brain (voice drops ~20 s)
journalctl --user -u claire -f                                # watch her think, live
journalctl --user -u claire-panel -f                          # panel-side: pages, memory editor, face
```

| I want to… | do this |
|---|---|
| check the whole install | `.venv/bin/python -m claire.platform.service --self-check` (add `--json` to script it) |
| talk to her in text | the panel's chat box at `http://127.0.0.1:8765/` (a direct, tool-less call), or the deprecated desktop app's chat window (it reaches the real brain) |
| open the panel | `http://127.0.0.1:8765/` |
| see what she is doing | the face in the panel: `docs/PANEL_FACE.md` has the five states and the state file |
| use the phone | `http://100.104.1.69:8788` from a device on the tailnet |
| change a setting | edit `config.json` and restart the daemon, or use the panel |
| stop her talking / stop the turn | say "stop", or the tray's Stop Listening (`systemctl --user stop claire` also works) |
| check readiness | `.venv/bin/python scripts/acceptance_check.py` (20 capabilities; see §6) |

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
env -u DISPLAY nice -n 19 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

Takes about eight minutes and is the only gate that counts. As of this snapshot: **6148 passed, 3
skipped, 2 deselected, 0 failed**. The 2 deselected are the `performance` tests, excluded by
`pytest.ini`'s `addopts`; an independent full run on the same day reported 6147 passed, 4 skipped and
0 failed, so treat the exact tallies as run-to-run rather than fixed. The older `-m unit` filter is now
close to redundant: `tests/test_test_files_are_marked.py` fails the gate if a test module is unmarked,
which is how the gate spent weeks quietly skipping the fork's own tests before anyone noticed.
Conventions and traps that cost real time:

- **A test module without `pytestmark = pytest.mark.unit` is silently *deselected*, not failed.**
  `tests/test_test_files_are_marked.py` exists to catch this.
- **Run one full gate at a time.** Two concurrent runs compete for CPU and produce failures that are
  purely load, and every one of them costs more time to investigate than it saved.
- **Qt tests are headless** (`QT_QPA_PLATFORM=offscreen`, set in `tests/conftest.py`), and nothing in
  the suite may create or replace the owner's real control socket: an autouse fixture points
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
  lives on `main` and is pushed to `personal` (`RobertoTornel/jarvis`). Never push to `origin`. Write
  Conventional Commit messages that say *why*, quoting the reproduction.

---

## 5. Where everything lives

| path | what |
|---|---|
| `src/claire/` | the daemon and every subsystem (`listening/`, `reply/`, `tools/`, `memory/`, `schedule/`, `senses/`, `phone/`, `panel/`, `outreach/`, `platform/`, `control/`) |
| `src/desktop_app/` | the deprecated tray application: `app.py` (tray, lifecycle, IPC), `chat_window.py`, `face_window.py`, `settings_window.py`, `control_link.py`; `ClaireStateManager` in `face_widget.py` is its load-bearing part (the panel's face-state writer) |
| `src/claire/memory/pg.py`, `pg_router.py`, `embedder.py`, `migrate_md_to_pg.py` | the Postgres + pgvector memory store and the one-way markdown export |
| `src/claire/mcp_servers/linux_desktop.py` | the window-control MCP server (needs `DISPLAY=:0`) |
| `src/claire/output/clip_player.py`, `clip_cache.py` | the pre-recorded acknowledgement clip player and its on-disk cache (`~/.cache/claire/clips`) |
| `src/claire/listening/ack_continuations.py` | the 8, 18 and 30 second continuation clips |
| `android/` | the phone client (Gradle project) |
| `deploy/` | the unit files (`claire.service`, `claire-panel.service`, `claire-desktop.service`), a `Dockerfile`, and `deploy/pgvector/` (the Postgres container, compose file, setup script and README) |
| `start.sh` | installer: substitutes the checkout path into the units and enables them |
| `scripts/`, `evals/`, `templates/`, `examples/` | operational scripts (including `measure_ack_latency.py` and `run_evals.sh`), the behavioural eval suite, prompt/config templates |
| `docs/` | architecture, configuration, memory, PGVector, panel, Android, UI/UX, phone client, MCP servers, audio routing, cloud policy, this handover and the long one |
| `tests/` | about 6.1 k tests (6148 passed in the latest full run), plus `evals/` for end-to-end behaviour |
| `.venv/` | the virtualenv everything runs from (the units use it directly; the package is not pip-installed) |

---

## 6. Current state

- **Branch `main` at `2c4c9d4`**, working tree clean. `main` and `personal/main` are both at
  `2c4c9d4`, 158 commits ahead of `origin/develop` (**verified** with `git rev-parse` and
  `git rev-list --count`). The `claire-rewrite` branch is deleted locally and on `personal` (only
  `main` remains there). The two safety refs are local: the branch
  `backup/claire-rewrite-20261013-231328` and the tag `backup/pre-merge-20261013-231328`, both at
  `cc97a9c`. `origin` (`isair/jarvis`, upstream) was deliberately never touched: `origin/main` is
  still `94c5449`.
- **Live on this host** (**verified** with `systemctl --user show -p MainPID --value`): `claire` PID
  441606, `claire-panel` PID 202892, `claire-desktop` PID 202900, `omniroute` PID 1472, all `active`.
  The phone bridge listens on `100.104.1.69:8788` as PID 441606, and the control socket is bound at
  `~/.local/share/claire/control.sock` (mode 0600).
- **Readiness** (`scripts/acceptance_check.py`): **16 READY, 1 BLOCKED, 3 UNMEASURED** of 20
  capabilities. The blocked one is `backups` (0 of 4 rotating slots); the unmeasured ones are
  `wake_word`, `tts_synthesis` and `outreach_live`. **Unverified here** (reported from the owner's run
  on 2026-10-14); the script exits 1 on the blocked item.
- **Recent rounds, newest first:** the pre-recorded acknowledgement clip player
  (`src/claire/output/clip_player.py` and `clip_cache.py`, `src/claire/listening/ack_continuations.py`,
  wired in `src/claire/listening/listener.py` around line 713, measured by
  `scripts/measure_ack_latency.py`); the Postgres and pgvector memory store that now holds long-term
  memory (`src/claire/memory/pg*.py`, `embedder.py`, `migrate_md_to_pg.py`, `deploy/pgvector/`); the
  panel's memory editor and the face moving into the panel with the desktop app deprecated
  (`docs/PANEL.md`, `docs/PANEL_FACE.md`, `docs/MEMORY_EDITOR_UX.md`); the shared colour law applied
  across the panel, the desktop app and the Android client (`docs/UI_UX_GUIDE.md`, whose token
  authority is §4 of `docs/MEMORY_EDITOR_UX.md`).
- **Only tested, not exercised end to end:** the camera sense, the outreach channels (no credentials
  are configured), and the Android client against the current daemon.

---

## 7. What to build next

Four pieces of work, in dependency order, and one documented negative result that should not be
retried without the condition it names. Each gives the case, the exact places to work, the
approach, the verification, and the risk. Every number attributed as measured was measured on this
machine; anything not checked for this page is marked unverified.

### 7.1 Per-turn latency tracing (do this first)

**What and why.** Answering "why was that turn slow?" currently means writing a bespoke probe script,
and every measurement in this project's latency history was produced that way. Build one structured
record per turn instead: a monotonic (`time.monotonic()`) timestamp at each stage, plus the facts
that make the number interpretable. The stages are transcription end, router start and end, planner
start and end, first LLM token, last token, first audio byte, and speech start. Each record also
carries which route actually served the turn (the model that answered, not only the one requested),
whether it was a **cache hit** (§7.4), and whether reasoning was present and how many characters.

**Why first.** It is a prerequisite rather than a feature. It turns every future optimisation from
archaeology into a query, and it is what would have made the semantic-cache trap visible immediately.

**Where.** `src/claire/reply/engine.py` owns the turn (`run_reply_engine` at line 908) and the answer
call (`chat_with_messages` at line 2309); `src/claire/tools/selection.py` `select_tools` at line 431
is the router; `src/claire/reply/planner.py` `plan_query` at line 424 is the planner; the TTS modules
(`src/claire/output/fish_tts.py`, `src/claire/output/tts.py`) and the listener's speak call
(`src/claire/listening/listener.py` line 1969) are where audio begins; transcription end is in
`src/claire/listening/listener.py`.

**Approach.** A small `TurnTrace` dataclass with a `mark(name)` method, created once per turn in
`run_reply_engine` and passed (or attached to a context object) to the router, the planner and the
speaker. Stamp the existing points rather than restructuring them; where a stage runs in a thread,
stamp inside the thread. Write one JSON object per turn to a single bounded append-only file (for
example `~/.local/share/claire/turns.jsonl`) on a queue or at end of turn, and mirror a one-line
summary through `debug_log` under a `trace` category. Expose the last N records on the control socket
so the panel can show them. Then add a reader, `scripts/turn_trace.py` (for example `--last 20`,
`--slowest`, `--stage router`), so the answer to "why was that turn slow" is a command.

**Verify.** Drive one voice turn and one text turn, then assert every expected stage is present and
strictly ordered in the record; break one stamp deliberately (remove the planner end) and watch the
checker fail. Compare the record's total against a wall clock once, to catch a broken scale.

**Risk.** Instrumentation drift (a field that stops being written) and privacy: the record must not
contain the question or the answer by default, only lengths and flags, and `voice_debug` may be
required to add text. The write must never block a turn: one bounded write per turn, on a queue.

### 7.2 An answer-quality eval harness

**What and why.** There is no way to know whether a change made Claire **dumber**, and the problem is
not hypothetical. When reasoning was investigated, first-token latency improved from **3.015 s to
0.668 s**, which looks like a clear win, while quality on multi-step date arithmetic went from **4/4
correct to 1/4** (**measured**, `/home/mana/Documents/Jarvis/optimizations/latency_reasoning.md`). That
was found by luck, not by tooling.

**Why it gates the other work.** Every routing, prompt or tool-selection change trades quality for
speed, and without this the trade is invisible. Several items in this document cannot be attempted
responsibly before it exists.

**Where.** `evals/` already holds 44 modules and a pass-rate reporter (`evals/conftest.py` lines 300,
434 and 443 to 483), driven by `scripts/run_evals.sh` with `scripts/merge_eval_reports.py`; its output,
`EVALS.md`, is a two-model comparison dated 2026-05-04 rather than a graded regression gate, and it
defaults to local Ollama models with an LLM judge. Work there, not in `tests/`: the gate must stay
hermetic and fast.

**Approach.** A small graded set of real tasks, grouped by what they exercise: arithmetic and date
arithmetic, single- and multi-step tool selection, reminders and timers, multi-step planning, and
follow-ups that need earlier context. Each case has the prompt, the expected observable outcome (a
tool name and its arguments, or an exact digit, or a required substring) and a weight, and the runner
reports **pass rates**, never latency. Grade facts without an LLM judge wherever the observable is
checkable; keep the judge for tone and helpfulness only. Keep the whole set small enough to run in a
few minutes and to be read in full by one person.

**Verify.** Run the set on the current tree to fix a baseline, then make a deliberate regression (for
example disable the planner, or strip reasoning) and confirm the pass rate moves in the direction you
expect. A harness that never fails is not a gate.

**Risk.** Eval drift: cases overfit to the current prompt, or the set grows into a second test suite
that nobody runs. Pin each case to an observable outcome, keep the set small, and review it whenever
the system prompt changes. Each run spends gateway tokens, which is why it is on demand and never
inside `pytest`.

### 7.3 Streaming the answer into speech: a documented negative result

**The result.** An earlier round proposed speaking the answer while it is still being written and
refuted it by measurement. `docs/ANSWER_STREAMING_DESIGN.md` is the record, marked **refuted by
measurement, not implemented**, and it is the document to read before anyone reopens this. Its
conclusions:

- about **a second of fixed gateway overhead** precedes any token, and it does not depend on prompt
  size (**measured**: 48 tokens at 0.92 s, 3,257 tokens at 1.25 s);
- prompt size is therefore not worth optimising either, and the gateway already caches the large
  system prompt (3,200 of 3,257 tokens);
- time to first content is dominated by **variable reasoning** (8 to 195 reasoning tokens across
  runs), so only roughly the last half second of the call is spent producing speakable words.

The speakable window is therefore about half a second, while the change needed to reach it is large:
a streaming entry point that carries the message history and the tool schema, an incremental sentence
splitter, and new bookkeeping for echo cancellation, the stop command and barge-in. The cost and the
risk outweigh a few hundred milliseconds.

**The current interest, and why it does not change the result.** The design has been raised again
because two pieces look as if they already exist. Checked against the tree, neither closes the gap:

- The transport exists: `OpenAICompatibleBackend.streaming` at
  `src/claire/llm/openai_compatible.py` line 207, with `"stream": True` at line 223.
- **The streaming entry point does not exist for the answer call.** `streaming()` takes
  `chat_model, system_prompt, user_content, on_token, timeout_sec, thinking`, builds exactly two
  messages (a system string and a user string), carries no `tools`, and keeps only `delta.content`;
  its only consumer is `src/claire/memory/conversation.py` line 68. The answer call needs the full
  message history and the tool schema, and `src/claire/reply/engine.py` line 2309 calls
  `chat_with_messages`, which calls `LLMBackend.chat`. A tool-bearing, history-bearing streaming entry
  point would have to be written from scratch.
- The Fish engine already pipelines sentences, but only after the fact: `_split_sentences` at
  `src/claire/output/fish_tts.py` line 453, the boundary rule `_SENTENCE_BOUNDARY_RE` at line 172,
  and `speak` at line 785, which takes the whole text. Synthesis of sentence N+1 overlaps playback of
  sentence N through a queue with a byte ceiling and a barge-in `flush()` at line 827; nothing
  overlaps generation.
- The live speaker is the listener, not the engine: both live paths call `run_reply_engine` with
  `tts=None` (`src/claire/listening/listener.py` line 1918, `src/claire/daemon.py` line 879), and the
  listener speaks at `src/claire/listening/listener.py` line 1969. The engine's own
  `tts.speak(safe_reply)` at `src/claire/reply/engine.py` line 2833 is dead on live paths, so any
  future attempt has to reach the listener, not the engine.

One correction to this page's own earlier revision, which is why the result is worth restating rather
than only citing. It said streaming "made the first sentence *later* in practice". That is not what
any measurement shows: the recorded streaming timelines have speech starting **earlier** by **0.028 s**
on a 202-character reply, **0.314 s** on a 204-character one (two tool calls), **0.151 s** on a
518-character one, and **2.087 s** on a 1,299-character one (**measured**,
`latency_probes/streaming_engine_timeline.py`, recorded in
`/home/mana/Documents/Jarvis/optimizations/latency_voice_path.md` lines 185 to 188). The design was set
aside because the win is small against the size of the change, not because it regressed anything. The
two findings are consistent: the win is real, and for ordinary replies it is hundreds of milliseconds.

**The condition to revisit.** Reopen this only if the balance changes in one of two measurable ways:
a route whose fixed overhead before the first token is genuinely lower (the roughly one second floor
is the gateway and the model being scheduled, not prompt processing), or a genuinely long generation
where the speakable window stops being half a second and becomes several seconds (a long read-aloud,
a summary). If that happens, the seam to build is the one the design note already names: a streaming
entry point that takes the message list and the tool schema; an `on_sentence` callback carried from
the engine through `run_reply_engine` to the listener (line 1969); an incremental splitter that
distinguishes "no more text yet" from "end of reply" (the boundary rule at `fish_tts.py` line 172 is
anchored with `(\s+|$)`, so end-of-buffer looks like a sentence end); markdown sanitised per sentence
on the buffer rather than on the finished reply; a fallback to the complete-reply path; and explicit
cancellation for barge-in and the stop command. Note also that **piper does not pipeline**:
`src/claire/output/tts.py` accumulates every chunk and concatenates at line 951 before playback.

**The clip player is the shipped answer to the perceived problem.** The pre-recorded acknowledgement
clip covers the wait **before the first token**, which is where the perceived silence is; streaming
would compress the **answer itself**. The clip is the cheaper half, and it has shipped.

### 7.4 Make the semantic cache visible and controllable

**What and why.** OmniRouter's semantic cache answers a repeated request from its own store instead
of calling a model. That is good for latency and a menace for measurement. The gateway's forensic
report (`/home/mana/Documents/Jarvis/omniroute-latency-forensics.md`, its section 6b) measured
**302 of 418**
logged requests served from the cache in its sample (about 72%), and
the same report records a route that appeared about **80× faster** than it was because the second of
two byte-identical requests came back in 13 to 18 ms; that false conclusion was drawn once before
being caught. The gateway's own call log (`~/.omniroute/storage.sqlite`, `call_logs.cache_source`)
records **1,414 semantic of 29,649 daemon-session chat calls, about 4.8%**, over 2026-10-06 to
2026-10-13, which does not agree with the forensic sample, so the exact current share is
**unverified**. That disagreement is itself the case for tagging hits in Claire's own trace instead of
inferring them: the gateway's `semantic_cache` table records **468,697 hits** since 2026-09-26, far
more than `call_logs` accounts for, so some hit paths are not logged as calls at all.

**Where.** The bypass is gateway-side and already supported: the request header
`x-omniroute-no-cache: true`, or `Cache-Control: no-cache`, makes a request uncacheable for read and
for write (gateway source
`/home/mana/.local/lib/node_modules/omniroute/src/lib/semanticCache.ts` lines 484 and 502). On
Claire's side that header belongs in
`OpenAICompatibleBackend._headers` at `src/claire/llm/openai_compatible.py` line 137. The visible half
belongs in the §7.1 turn trace and in the debug log.

**Approach.** (1) Add a config key (for example `llm_bypass_semantic_cache`, default off) and a
per-turn parameter that adds `x-omniroute-no-cache: true`; turns that need a fresh answer (weather,
time, anything live) set it. (2) Record `cache_hit` in the trace. The reliable source is the gateway,
so first inspect one real response's headers and `usage` block for a hit marker (a response-side
marker was **not found** in the gateway bundle on this machine), then either read it or join the turn
to the gateway's `call_logs.cache_source` by time and model; if no join key exists, the smallest
upstream change is a response header, which is worth asking for. (3) Log it: one `debug_log` line per
turn saying whether the cache was bypassed, so nobody reading the journal mistakes a hit for a fast
model. (4) Any latency measurement must use **fresh nonces** in the prompt, as the measurement
programme does (`/home/mana/Documents/Jarvis/optimizations/latencyreport.md` line 50); without them
the cache answers and the number is meaningless.

**Verify.** Send the same prompt twice with a nonce, once with the bypass off: assert the second is
served from the cache (fast, and `cache_source = 'semantic'` in the gateway log). Repeat with the
bypass on: assert the second reaches upstream. Then assert the trace marks `cache_hit` and the bypass
flag on the same turn.

**Risk.** A blanket bypass makes every turn slower and spends money, so it is per turn, not global.
A cache hit can also replay an id-less row: the measurement programme records a refusal-shaped answer
from a stale hit (`/home/mana/Documents/Jarvis/optimizations/latencyreport.md`), which is why a
cached answer to "what is the weather" is a bug, not a speedup.

### 7.5 Fix the PortAudio crash and make audio failures non-fatal

**What and why.** A genuine crash, found by running the test suite: `src/claire/output/tune_player.py`
line 238, inside `sd.OutputStream`, **segfaults** in native PortAudio when the audio device is
contended. The whole process dies with exit 139; no test failure is reported, only a crash dump. It
happened when the suite ran while the live daemon held the audio device.

**Where.** The crash site is `src/claire/output/tune_player.py` line 238
(`stream = sd.OutputStream(` under `with portaudio_lock:` at line 237). The lock's contract is
`src/claire/utils/audio_lock.py`: every stream lifecycle call runs inside `portaudio_lock`, taken
**innermost** and around the direct `sounddevice` call only, never held across a playback wait and
never nested with another lock; the one accepted exception is the Windows mic-permission probe, which
may hang indefinitely. `src/claire/output/clip_player.py` is the newer module that follows the rule
(open under the lock at line 119, release, then re-acquire for start and close) and is the model to
copy.

**Approach.** A native segfault cannot be caught in Python, so the fix is to stop the contended open
from happening in-process and to make every other audio failure non-fatal. (1) Treat "device busy" as
a first-class outcome: before opening, check that the device is free, serialise against the other
in-process audio users through the same lock discipline, and retry with a short backoff; if the device
is still busy, skip the tune entirely instead of opening. (2) Make failures degrade to silence end to
end: a failed tune, clip or TTS stream logs once, marks the output as skipped, and lets the turn
continue. (3) In tests, stub audio rather than share the device with a live assistant: a fixture that
replaces `sounddevice` with a silent fake, beside the existing offscreen Qt setup in
`tests/conftest.py`, so a run never opens the owner's real device.

**Verify.** Reproduce first: run the full suite while another stream holds the device, or run the tune
player against a held device, and confirm the crash. After the fix, hold the device deliberately, run
the tune player, and confirm it reports "skipped, device busy" and exits 0. Add a unit test whose fake
`sd.OutputStream` raises, assert playback is skipped and the turn continues, then break the fix on
purpose and watch the test fail. Finally, run the full suite while the live daemon holds the audio
device and confirm it completes.

**Risk.** The lock is process-wide and shared with the microphone and TTS. Widening what it protects,
or holding it across a playback wait, would freeze every audio user in the process, which is worse
than the crash it prevents. Keep it innermost, exactly as `src/claire/utils/audio_lock.py` states.

---

## 8. What is still open

**Latency, and what not to try again.** This paragraph corrects the earlier revision of this page,
which put the warm turn at ~9.3 s and said that streaming the answer "made the first sentence *later*
in practice" and was retired with that measurement. The end-to-end total is **unverified** here: the
measurement programme reports the turn up to the answer starting at **3.98 to 8.56 s** (**measured**),
with the router at **1.27 to 1.54 s** cold, the planner at **1.04 to 2.55 s** cold and **0.015 s**
warm, and the gateway's own TTFB at **1.27 to 1.44 s** per call. The "later first sentence" claim is
not what the measurement shows: the recorded streaming timelines have speech starting earlier by 0.028
to 2.087 s, and the design was set aside because that win was small against the size of the change.
The full account is in `docs/ANSWER_STREAMING_DESIGN.md`, and §7.3 states why it is not being built.
These were measured and **failed**; do not spend more time on them: other LLM providers (the gateway
is the fastest route available here), "reduce reasoning" instructions, router concurrency, shrinking
the prompts, and skipping the planner on tool turns.

**Open items, with the owner's decision or action attached:**

1. `🎙️ Listening!` is printed ~17 s before she can actually transcribe (parakeet restore). Reword or
   delay the banner.
2. The panel's chat box is a separate tool-less path: it cannot reach the brain
   (`src/claire/panel/server.py` line 4459, calling `backend.direct` at line 1422). The control socket
   is the obvious way to fix that; it is not wired. **Still open.**
3. Web search needs a **Brave API key**; until then it answers from Wikipedia and says so.
4. The microphone chain is quiet end to end (source at 47 %, USB capture at 64 %): the *hardware*
   gain is the fix, not a threshold.
5. The gateway API key reached the journal once and should be rotated. The phone bridge is bound to
   `100.104.1.69` on `tailscale0`, so it is not on the LAN and its transport is already inside
   Tailscale's WireGuard tunnel; the application layer is still plain HTTP, so the bearer token is
   unencrypted **inside** that tunnel. An ACL is the right control, and a rotated token if the old one
   ever reached the journal.
6. Meal logging needs one product decision (how much detail to ask for) before its ~26 s flow is
   worth shortening.
7. Outreach (proactive messages) is built but switched off: it needs a phone number or a webhook, and
   a decision about when Claire may interrupt.
8. `tests/test_panel_chat.py::test_the_startup_line_does_not_claim_loopback_when_it_is_not` passed
   **10 of 10** in isolation on 2026-10-14 (the owner's run) and was **not observed failing** in the
   latest full run
   (6148 passed). **Open but not currently reproducing**: the diagnosis is full-run cross-test
   interference, deliberately not papered over, so do not mark it resolved and do not treat a clean
   run as proof.

### Small, well-scoped fixes

Each is independent, each has a location, and each was checked against the tree while writing this
page unless marked otherwise.

- **`STATUS.md` line 138, and the copy at `/home/mana/Documents/Jarvis/optimizations/STATUS.md`.**
  The table calls Fish first-audio latency "not measurable" because the account had no API credit.
  It has been measured: **0.717 s** median of three (0.717, 0.682, 1.067) with
  `python scripts/measure_voice_latency.py --only first_audio_latency --live-fish --json --quiet`,
  recorded in `/home/mana/Documents/Jarvis/optimizations/latency_voice_path.md` lines 200 to 201, and
  it is network-bound rather than a local fix. The cause was an account-credit limit at the time, not
  a permanent impossibility. Line 139's barge-in row rests on the same stale cause.
- **`STATUS.md` lines 233 to 236.** They say Fish "is not yet the default engine" and that
  `tts_engine` "still defaults to `piper`". The default is now **`fish`** (`src/claire/config.py`
  line 731 and line 1108) and the live config sets `fish_model` to `s2.1-pro-free`, a free model. The
  acceptance check is reported to take the Fish branch with no `tts_engine` key in `config.json`
  (**unverified here**).
- **`STATUS.md` line 541.** The acceptance tally "11 READY, 6 BLOCKED, 3 UNMEASURED" is stale; the
  owner's run on 2026-10-14 reports **16 READY, 1 BLOCKED, 3 UNMEASURED** of 20, blocked on `backups`
  (**unverified here**). Keep the
  distinction that `tts_synthesis` is UNMEASURED **by policy**: `scripts/acceptance_check.py`
  `probe_tts_synthesis` says in its own docstring "Spends money, so this check does not do it"
  (line 697) and returns `unmeasured` citing the constant `FISH_LAST_LIVE_RESULT` at line 187, which
  is baked in from an earlier attempt rather than re-checked live. A measurement exists in the record;
  the readiness check declines to repeat it.
- **`docs/subsystems/memory.md` and `src/claire/memory/router.spec.md`.** Both still describe markdown
  and vault behaviour. `docs/subsystems/memory.md` carries 30 occurrences of "vault", 9 of "obsidian"
  and 3 of "CLAIRE.md"; `router.spec.md` carries 4 of "vault" and 4 of "CLAIRE.md". Bring them to the
  Postgres state (`docs/PGVECTOR.md`, `docs/PGVECTOR_DESIGN.md` and `src/claire/memory/pg.py` are the
  current account).
- **`src/claire/phone/bridge.py`: the non-loopback warning is not Tailscale-aware.** The docstring at
  lines 56 to 62, the message at lines 752 to 765 and `resolve_bind_host` at line 919 (with
  `allow_public_bind` at line 922 and the warning at line 936) treat every non-loopback bind as
  exposed public HTTP. On this machine the bind is `100.104.1.69`, the `tailscale0` interface, so the
  transport is already inside WireGuard and the bridge is not on the LAN or the internet. Make the
  warning distinguish a Tailscale address (the `100.64.0.0/10` range, or being bound on `tailscale0`)
  from a genuinely public bind: for the Tailscale case, say that the traffic is inside the tunnel and
  the token is still plain HTTP **within** it. Keep the loud warning for anything else. A warning that
  is usually wrong is ignored when it is right.
- **`docs/MEMORY_EDITOR_UX.md`: no defect, one optional improvement.** The document is honest about
  its own status: line 5 says it is "Implemented in part. §1.0 lists the surface the panel ships; the
  routes, parameters and fields elsewhere in §1 to §3 and §6 are the proposal this document was
  written as", lines 33 to 34 say there is no `?edit=`, `?new=` or `?reindexed=`, and line 55 marks
  `/memory/reindex` as "proposed, not built". If a reader could still begin at §2 and take the
  proposal for shipped API, a one-line pointer at the head of §2 restating the status row would fix
  it. That is optional polish, not a correction.
- **Move `two_tier_design.md` into the repository.** It sits at
  `/home/mana/Documents/Jarvis/two_tier_design.md`, outside the repository and therefore unversioned.
  It is the design record for the filler and escalation work and belongs in `docs/`, referenced from
  the document map in §9. **The owner makes this move**; do not copy it in as a side effect of another
  change.

---

## 9. Document map

| read this | for |
|---|---|
| `README.md` | what the project is, install and first run |
| this file | the general picture, operating commands, what to build next, open items |
| `docs/HANDOVER.md` | the long handover: the hardening programme, the gate's history, the measured wins |
| `STATUS.md` | the build log wave by wave, including its own corrections and retractions |
| `CLAIRE_FIX_REPORT_2026-10-12.md` (outside the repo) | measured latency budget, the tool fixes, and that round's verification |
| `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/MEMORY.md`, `docs/AUDIO_ROUTING.md`, `docs/PHONE_CLIENT.md`, `docs/MCP_SERVERS.md` | one subsystem each |
| `docs/PANEL.md` | the web panel: scope, the page, the memory editor, the face, the colour law, the API, the security contract; also records the desktop app's deprecation |
| `docs/PANEL_FACE.md` | the face, its five states, and the state file the daemon writes |
| `docs/PANEL_CONVERSATION.md` | what the panel's transcript and chat box can and cannot show |
| `docs/ANDROID.md` | the Android companion client |
| `docs/UI_UX_GUIDE.md` | how to add or restyle a surface under the colour law |
| `docs/MEMORY_EDITOR_UX.md` | the memory editor's shipped surface (§1.0) against its proposal, plus the token law (§4) and measured contrast (§4.1) |
| `docs/PGVECTOR.md` | the Postgres + pgvector memory store, operationally |
| `docs/PGVECTOR_DESIGN.md` | the design of that store |
| `docs/subsystems/memory.md` | the memory subsystem's contract (vault framing is stale, see §8) |
| `docs/ANSWER_STREAMING_DESIGN.md` | the retired answer-streaming design and the measurement behind it |
| `AGENTS.md` | the house rules for anyone (human or agent) changing this repository |
| `src/**/*.spec.md` | the contract for the file next to it: the most reliable description of behaviour |

---

## 10. If you are an agent taking over

The owner runs this machine and plays games on it; the rules below exist because breaking them cost
him real time.

- **Do not open windows or type on `DISPLAY=:0` while he is using it.** GUI verification goes through
  Xvfb or Qt's offscreen platform. Never start a second daemon, panel or web server: the units own
  those ports and the microphone, and a duplicate fights them for it.
- **Use the isolated-instance recipe** (§3) for anything that talks to audio, the database, or the
  control socket, so his live data and his microphone are never in the blast radius. If a database is
  needed for a test, use **`claire_test`**, never `claire`.
- **Verify with evidence, not assertion:** reproduce first, then test, then break the fix on purpose.
  Quote the command and its output in the commit. If you cannot verify something, say that plainly
  instead of implying success.
- **One full gate at a time, and re-run it after your last edit**, not before it.
- **Commit in small, explained steps and push to `personal`.** Never push to `origin` (`isair/jarvis`,
  upstream). Do not commit anything you have not run.
- **Do not run the audio latency harness unless you mean to play sound aloud.** It speaks through the
  owner's speakers, and he has asked for the noise to stop before.
- **Do not restart the live units without being asked.** A restart drops the microphone for about 20 s
  and loses a turn in flight; if a restart is genuinely needed, restart between turns and say so.
- **If you delegate, check what came back.** Subagent reports are the least reliable source here. One
  round's subagents once spent 75 minutes reading `daemon.py` and a 169 KB `app.py` and wrote nothing.

*Revision: written 2026-10-14 at `2c4c9d4` on `main`, replacing no existing document;
`docs/HANDOVER.md` remains the historical record. Update this page in the same commit as any change
that makes it wrong, and keep the labels (measured / verified live / unverified) honest.*
