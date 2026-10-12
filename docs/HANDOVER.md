# Claire: handover

Read this before you change anything. It states what the system is, how to run it, where the
current test gate stands and where it lies to you, what the recent hardening programme actually
fixed, what it measured, where the documentation lives, and what is still open. Where the code
and this document disagree, the code is right; where a `*.spec.md` and a prose page disagree, the
spec and the code win.

The repository is `/home/mana/Documents/jarvis_cloud`, branch `claire-rewrite`. It is a personal
fork of [`isair/jarvis`](https://github.com/isair/jarvis) (see [`ATTRIBUTION.md`](../ATTRIBUTION.md)),
rewritten to allow cloud services on purpose rather than decline them on principle. The
load-bearing rules are in [`AGENTS.md`](../AGENTS.md): all LLM traffic goes through one local
gateway, secrets never enter a repository file, and no action is ever claimed that was not
performed.

The hardening work through `1305415` is committed and pushed on both `claire-rewrite` and `main` of the
`personal` remote ([`RobertoTornel/jarvis`](https://github.com/RobertoTornel/jarvis.git)); that work is
in the merge `0db56d9`, whose tree is byte-for-byte identical to `1305415`, and `main` has since
advanced to `b60788a`.

The wave after that runs through `6342c10` on `claire-rewrite`: the three components now start
together at login, the self-check stops calling a working gateway broken, the NeMo recogniser is
serialised, and the speech gate has an absolute level floor. **Those 21 commits are local only** -
`personal/claire-rewrite` and `personal/main` are each 21 commits behind, so nothing after `1305415`
has been pushed. Pushing a personal fork is the owner's decision, not an agent's; the command is
`git push personal claire-rewrite`. The commits carry the clone's configured identity, as `1305415`
does.

---

## 1. What this is, and how to run it

Claire is a standalone, always-on Linux voice assistant for one machine and one person. A spoken
sentence travels wake word -> transcription -> reply engine -> speech synthesis. She also exposes
a local web panel and a phone bridge, keeps a diary and a knowledge graph, and can call built-in
tools plus any MCP server configured for her. The full walk-through is
[`ARCHITECTURE.md`](ARCHITECTURE.md).

Three surfaces:

| Surface | Process | Default address |
|---|---|---|
| Daemon | `python -m claire` ([`src/claire/daemon.py`](../src/claire/daemon.py)), systemd user unit `claire` | none |
| Web panel | `python -m claire.panel.server`, standalone stdlib server | `127.0.0.1:8765` ([`server.py:152`](../src/claire/panel/server.py#L152)) |
| Phone bridge | started *by the daemon*, not by `start.sh` | `127.0.0.1:8788` by default ([`bridge.py:203`](../src/claire/phone/bridge.py)), bound here to `100.104.1.69:8788` |

### `./start.sh` is the single entry point

[`start.sh`](../start.sh) resolves every path from its own location and reads every default out of
the project's own code, so nothing in it is machine-specific. Its real interface, read from the
file's argument parser (`start.sh:1223`-`:1240`) and its own `usage()` (`start.sh:1173`):

| Invocation | Effect |
|---|---|
| `./start.sh` or `./start.sh start` | Create/use `.venv`, install `requirements.txt` idempotently, run the self-check, then bring up the daemon and the panel. Idempotent. |
| `./start.sh stop` | Stop the panel and the daemon. |
| `./start.sh restart` | Stop, then start. |
| `./start.sh status` | What is running right now. |
| `./start.sh check` (or `--check`) | Preflight and health report only; writes nothing. |
| `--dry-run`, `-n` | Print every action, execute none. |
| `--no-panel` / `--no-daemon` / `--no-desktop` | Leave that one component out of this start. |
| `--systemd` / `--direct` | Force the daemon through the systemd user unit, or as a detached process. |
| `--strict` | Refuse to start unless the self-check is all green. |
| `--force-install` | `pip install` even while a Claire daemon is live (otherwise the live venv is left alone). |
| `--install-service` | Render the daemon, panel and tray units plus the tray XDG autostart entry for this checkout, install them, reload systemd and enable them. |
| `-h`, `--help` | Usage. |

Environment overrides (all optional, documented at `start.sh:35`-`:52`): `CLAIRE_VENV`,
`CLAIRE_PYTHON`, `CLAIRE_STATE_DIR`, `CLAIRE_LOG_DIR`, `CLAIRE_SERVICE_NAME`, `CLAIRE_DAEMON_MODE`
(`auto|systemd|direct`), `CLAIRE_PANEL_HOST`, `CLAIRE_PANEL_PORT`, `CLAIRE_PREFLIGHT_TIMEOUT`,
`CLAIRE_START_WAIT`, `CLAIRE_FORCE_INSTALL`. Exit codes: `0` ok, `2` usage, `3` preflight failed,
`4` a component did not come up.

Four things about startup that are easy to get wrong:

* **The daemon starts the phone bridge itself** (`_start_phone_bridge`, opt out with
  `CLAIRE_PHONE_BRIDGE=0`). Never start the bridge a second time: two recogniser instances on one
  GPU fault.
* **`deploy/claire.service`, `deploy/claire-panel.service` and `deploy/claire-desktop.service` are
  templates**, not installable units, and `deploy/claire-desktop.desktop` is the tray's XDG
  autostart entry. They contain `@CLAIRE_REPO_ROOT@` where the checkout path belongs. Do not copy
  one verbatim into `~/.config/systemd/user`: the placeholder is not a path and `ExecStart` fails
  with "No such file or directory". Install all four with `./start.sh --install-service`. The daemon
  and the panel are *user* units on purpose: Claire needs the login session's microphone, speakers
  and display, which a system unit started by PID 1 does not have. The tray unit is deliberately not
  enabled at `default.target`; it is triggered by the desktop session instead.
* **Local unit policy belongs in a drop-in, never in the unit.** `deploy/claire.service` is a template
  that `--install-service` re-renders, so hand-editing `~/.config/systemd/user/claire.service` to add
  `After=omniroute.service` is silently undone by the next install - which is how this machine lost
  its provider ordering once. The extension belongs in
  `~/.config/systemd/user/claire.service.d/*.conf` plus `systemctl --user daemon-reload`, and
  `systemctl --user show claire -p DropInPaths` proves it merged. This machine carries
  `omniroute-ordering.conf`, and `tests/test_start_script.py` pins that the installer never deletes a
  drop-in.
* **The tray app must not start a second daemon.** `--install-service` enables the daemon unit and the
  desktop session starts the tray; an app that cannot see the running daemon used to spawn its own,
  which means two recognisers on one GPU, two microphones and a phone bridge that fails to bind. It
  now discovers a supervised daemon (`_systemd_claire_daemon_pid`, `_running_claire_daemon_pid`) and
  attaches to it; `tests/test_desktop_app.py` pins that path. One daemon, always.

### Configuration

* Live config: `~/.config/claire/config.json`, mode `0600`. The installed file currently holds 20
  keys (it held 16 when this document was written; later waves added and retired fields). Anything
  not present falls back to the defaults in [`src/claire/config.py`](../src/claire/config.py).
* [`examples/config.json`](../examples/config.json) is **generated**, never hand-edited. It is
  produced from `get_default_config()` (via `export_example_config()`) by
  [`scripts/generate_config_examples.py`](../scripts/generate_config_examples.py) and pinned by
  [`tests/test_examples_config_current.py`](../tests/test_examples_config_current.py), which fails
  if a config surface change ships without regenerating it. Regenerate with that script.
* Secrets live in the OS keychain (`keyring`) or in `0600` files outside the repository. The one
  documented exception is the phone bridge token, revealable once on the machine with
  `python -m claire.phone keys show PHONE_BRIDGE_TOKEN --reveal`.
* Config keys that change deployment behaviour and the resolution order of the vault are tabulated
  in [`docs/subsystems/configuration.md`](subsystems/configuration.md) and
  [`docs/subsystems/deployment.md`](subsystems/deployment.md); the vault's Obsidian shape is in
  [`docs/OBSIDIAN.md`](OBSIDIAN.md).
* **The pinging "thinking" tune can be silenced for testing**: `python -m claire --no-tune`, or
  `CLAIRE_TUNE_ENABLED=0` in `~/.config/claire/claire.env` for the installed unit. `--no-tune` is the
  only CLI flag that changes a *setting*; `--smoke-test` only changes how startup is verified. The
  environment flag accepts `1/true/yes/on` and `0/false/no/off` in any case, and anything else -
  including an empty value - leaves the config file's value alone rather than guessing. Speech is not
  affected: the tune is the thinking sound from `output/tune_player.py`.

---

## 2. The test gate, and why it is now honest

```
cd /home/mana/Documents/jarvis_cloud
.venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider
```

Current result: **5507 passed, 3 skipped, 25 deselected, 0 failed** (about 3.7 min). The total
collected is 5535. A full run with no marker filter is **5530 passed, 3 skipped, 2 deselected** - the
two are the deliberate `performance` tests - so the gate and the whole suite agree.

### The trap that used to hide tests: an unmarked test is deselected, not failed

`pytest -m unit` selects only tests carrying the `unit` marker; a test carrying no marker is
**deselected**. Deselection is silent — the gate prints a green summary and the test never runs.
That trap had hidden 1,460 tests, and it is closed: those tests now run in the
gate, so the only deselections left — **25** — are deliberate (`integration`, `e2e`, `performance`).

The guard is [`tests/test_test_files_are_marked.py`](../tests/test_test_files_are_marked.py). It
no longer scans a file's *text* for a marker string. That whole-file heuristic passed a file with
one marked test and a hundred unmarked ones, and it was hiding **356** unmarked tests inside
otherwise-marked files — [`tests/test_voice_listener.py`](../tests/test_voice_listener.py) was the
worst: one marker, **101 of its 104 tests deselected**, for the core voice path. The guard now runs
`pytest tests/ --collect-only -m ""` in a subprocess with a small recording plugin and asks pytest
itself for each collected test's own marker set, failing on any test that is neither `unit` nor
deliberately `integration` / `e2e` / `performance` / `eval`. There is no file-level baseline and no
cache to go stale. `PRE_EXISTING_UNMARKED` is **empty**, and
`test_deselection_allowlist_stays_empty` fails if anyone re-adds to it. Mark a new test `unit`, or
its honest non-unit marker; do not allowlist it.

The newly visible tests had rotted while nothing ran them: two tests in
[`tests/test_engine_planner_integration.py`](../tests/test_engine_planner_integration.py) failed
because they drove their scenario through a tool that had been deleted, and were repaired by
`1acc426` and `4722826`.

The cost is real. The gate now takes about **3.5 min** and peaks near **6.8 GB RSS**, against roughly
138 s and 1.75 GB before the change; run it alone on a memory-tight machine.

Three tests carry both `unit` and `integration` — one in
[`tests/test_piper_download_recovery.py`](../tests/test_piper_download_recovery.py) and two in
[`tests/test_senses_browser.py`](../tests/test_senses_browser.py) — so they run in the gate despite
their honest `integration` marker. That was already true before the marking pass.

To see every test regardless of markers:

```
.venv/bin/python -m pytest tests/ -m "" -q -p no:cacheprovider
```

### The gate also never sees `evals/`

[`pytest.ini`](../pytest.ini) sets `testpaths = tests`, so `evals/` is not collected by the gate at
all. The eval suite has its own entry point ([`EVALS.md`](../EVALS.md),
[`scripts/run_evals.sh`](../scripts/run_evals.sh)). `pytest.ini` also defaults to
`addopts = -m "not performance"`, but an explicit `-m` on the command line overrides that.

---

## 3. What the programme delivered

The commit set below is the hardening work, oldest first. It was cross-checked against `git log`;
every hash resolves. A short groundwork table comes first because the hardening programme assumes
it.

### Groundwork

| Commit | What it fixed |
|---|---|
| `2f1bb6a` | Forked jarvis into Claire: renamed, dropped dictation and non-Linux paths. |
| `6d18bbb`, `30d2dff` | The two-recogniser voice pipeline, Fish TTS and the wake word, wired into the runtime. |
| `7235752` | The phone bridge, the web panel and the acceptance scoreboard. |
| `67e6429` | Made `python -m claire` actually start the daemon. |
| `d4d16b7` | The web panel had never worked over a socket; it does now. |
| `136a37e` | Made Claire audible and intelligible: ALSA routing, output device, WAV. |
| `5f99ec0`, `1ad1f85` | Overnight hardening: self-echo gate, tail guard, context budget, Parakeet-only STT, phone wake gate and queue, panel hardening, `stt_device` enforcement, bounded daemon locks, evaluator removal, `sk-proj` redaction. |
| `07f7d45` | Granted `%h/.cache/claire` in the shipped unit so `POST /see` can store photos. |
| `60bf9db` | Added the Android client (always listening, camera, GPS). |

### The hardening programme

| Commit | What it fixed |
|---|---|
| `87e8e85` | **Voice turns never reached the reply engine.** `_dispatch_query` captured the capture generation and then called `_clear_audio_buffers()`, which bumped that same generation, so its own staleness guard was always true and every voice turn returned early. Typing worked, which hid it. The guard now captures the generation after the clear, and `tests/test_voice_dispatch.py` covers the path for the first time. |
| `377913b` | **Ambient noise no longer becomes an utterance.** An utterance was only checked for total buffered duration, so a single noise click cleared the 0.15 s minimum and silence decoded into "Yeah." `min_speech_ms` (250) now counts only frames VAD called speech, and `noise_gate_margin` (2.0) requires speech to stand above the room's own 30 s noise floor. Language-agnostic; no word list. |
| `f615e66` | **Stopped paying a blocking ~33 s UPnP probe on every reply.** The probe result is remembered for the process lifetime (success or failure), and a new `location_city` setting answers weather questions without resolving an IP. `get_location_info(auto_detect=False)` returns in 0.00 s. It also marked the location tests `unit`, which is why nothing had caught the stall. |
| `32472ba` | **Never play a body that is not the PCM the output stream expects.** `_PcmStreamFormat` now classifies leading bytes and raises `FishUnsupportedAudioFormatError` for known non-PCM containers (MP3/ID3, Ogg, FLAC, ADIF, gzip, HTML, XML, JSON) and for an unresolved RIFF header; declared layouts are converted to 16-bit mono; every block is whole frames; the player applies a `-3` dBFS peak backstop. |
| `5b14bc7` | A runnable stdio MCP server template ([`templates/mcp_server/`](../templates/mcp_server)) plus build/connect docs, with a unit test that round-trips a tool call through Claire's real MCP client. |
| `9e5714f` | **Bounded reply-path LLM ceilings** `180/300/60 s` -> `45/15/5 s` (`llm_chat_timeout_sec`, `llm_tools_timeout_sec`, `llm_embedding_timeout_sec`) and stopped the memory extractor retrying a stall (two attempts at the ceiling became one). Added `tests/test_reply_path_timeouts.py`. |
| `65ff893` | One-command `start.sh`; `deploy/claire.service` templated with `@CLAIRE_REPO_ROOT@` instead of one machine's checkout path; portable env paths in the Discord/Signal/acceptance code. |
| `fdde5f8` | The panel `/memory` browser plus an Obsidian-compatible vault (`notes/` layout, frontmatter, `[[wiki links]]`, `.obsidian` marker), with the location configurable through `vault_dir`. |
| `166d88b`, `0f9fde6` | The hub-and-spoke architecture documentation set, and the panel spec followed to the memories browser it now documents. |
| `b484518` | **Only downgrade native tool calling for a 400 whose body actually blames the tool field**, and **stop `warm_up` lying**: its reachability gate capped `GET /models` at 5 s against a catalogue measured at 6.9-8.0 s, so the gate always timed out and the inference that warms the model never ran, reported as a model failure. |
| `2777af4` | Made the Fish key path portable (`~/.config/claire/fish-keys.txt`, expanded at use time) instead of an absolute path inside one machine's home. A configured `fish_key_file` still wins. |
| `e6e682b` | **Direct-exec a concrete plan step on LARGE models** through the deterministic fast path only, so a plan that already held the tool call is not thrown away for a whole extra chat turn. Includes the parser fix for a step using an undeclared argument key. |
| `d270963` | Keep the panel's memories browser inside the paths the state was built with: a state with an injected `router_path` but no `vault_dir` read its router card and its memories browser from two different vaults. The vault is now derived from `router_path`. |
| `82605e5` and follow-ups `1e812ed`, `6cdc656`, `02a6c0f`, `6bc9ac9`, `868a974`, `7172d1b` | **Removed the `getWeather` builtin tool** (owner decision; weather as a topic is now routed to web search) and followed it through the prompts, the specs, the eval suite and the planner test suite. `1e812ed` brings the planner suite into the unit gate. |
| `d3f242f`, `c5a251b` | **Speculative memory-extractor prefetch** started alongside the router and published into the same hot-window entry the serial path reads, with `DialogueMemory.hot_cache_epoch` preventing an abandoned prefetch from repopulating a later conversation's cache. |
| `a1f6383` | **The extractor gets its own short ceiling**, `llm_extractor_timeout_sec` (default `8.0`), rather than sharing the ceiling sized for tool execution. |
| `61a4842`, `bf1af83`, `2b01364`, `b57faa8` | **The planner/router overlap**: implemented, spec'd, reduced to a settle-only race, then reverted. The revert is the operative commit; see section 6. All four are deliberately left in history unscrubbed so the negative result stays auditable. |
| `1acc426`, `4722826` | Planner-integration test repairs: one asserted a contract the extractor prefetch had invalidated, two drove their scenario through the deleted `getWeather` and now use `getTime`. |
| `cd45a0b`, `1fee0d1`, `d055b49`, `a61965e`, `8fa6143`, `330ebc5`, `b870d1b`, `1231655` | **The Phase 5 documentation pass**: the weather removal and the extractor ceiling followed through the spokes, the 400-blame rule and the single-phase warm-up written down, and the hub and spoke symbol anchors re-based onto current source. |
| `62c808c` | **The built distribution excludes the development trees.** `pyproject.toml` names the runtime roots and excludes `tests/`, `evals/` and the dev harnesses under `packages.find`; a new `MANIFEST.in` prunes the sdist, which had been leaking 201 `tests/test*.py` files from distutils' own default. sdist 369 -> 168 members, wheel unchanged at 139. Pinned offline by `tests/test_prod_packaging_excludes_dev_trees.py`. |
| `c5fefe5` | **Retired seven dead config keys** (`location_city`, `stop_command_fuzzy_ratio`, `whisper_min_word_length`, `evaluator_enabled`, `evaluator_nudge_max`, `allowlist_bundles`, `active_profiles`), fixed a stale comment and a bogus `__all__` export in `notes.py`, corrected `recall_gate.py`'s module docstring and `summariser.spec.md`'s signature, and regenerated `examples/config.json`. |
| `5bc78cb` | **Made 1,460 previously invisible tests visible to the unit gate** by classifying each as `unit` or its honest `integration` / `e2e` / `performance` marker, and repaired the tests that then failed. |
| `83154d2` | **The marking guard detects partial marking per test** by asking pytest's own collection (`--collect-only -m ""` with a recording plugin), replacing a whole-file substring match that hid 356 tests inside otherwise-marked files (101 of 104 in `test_voice_listener.py`). `PRE_EXISTING_UNMARKED` is now empty. |
| `4b62a82` | **The Settings window can represent the shipped `fish` TTS default.** The `tts_engine` dropdown had offered only Piper and Chatterbox while `config.py` defaults the engine to `fish`; Fish is now the first choice. Removed the strict xfail that had recorded the bug and left the per-field default-in-choices test asserting it. |
| `d49f9d5` | Dropped the retired keys from `docs/CONFIGURATION.md` (the `whisper_min_word_length` "vestigial key" paragraph) and from `src/desktop_app/settings_window.spec.md` (`active_profiles`, `allowlist_bundles`, `stop_command_fuzzy_ratio`, `whisper_min_word_length`). |
| `8ee91a2`, `c26af76`, `8287345` | **Removed the whole location-discovery chain** — `get_location_info()`, the IP/UPnP/OpenDNS probes, the GeoLite2 download and its disk caches — and re-exposed `location_city` as the only location source, so the reply path never resolves an IP or touches the network. See §6.5. |
| `88ce6d4`, `7273cb3` | **Added a Gemini embeddings provider** (`embedding_provider: "gemini"`, output pinned to the 768-dim index, key in `x-goog-api-key`) and made embedding asymmetric (`RETRIEVAL_DOCUMENT` when indexing, `RETRIEVAL_QUERY` when searching, tool selection included), with `embed_batch` on the backend interface. The live config leaves it unset and does not need it: the local embedding model is pulled and the probe passes; see §6.6. |
| `8ee8c78` | **A refused TTS start disables speech instead of killing the daemon.** The Fish engine reads its key file lazily inside `start()`, after `_initialize_tts_engine` returned, so a missing, empty or world-readable key file, or an absent voice id, raised out of `daemon.main`. The refusal is now caught and logged, the inert stand-in is swapped in, and Claire keeps running with speech disabled. |
| `07113b0`, `1305415` | **The Android app was run on a real phone for the first time**, which caught and fixed a dead-shutter defect and refuted two others; the underlying intermittent CameraX bind failure is still open. See §6.8. |
| this revision | Refreshed this handover: the unit-gate numbers (5364 passed / 5391 collected), the on-device Android results and the open CameraX bind failure (§6.8), the Gemini embeddings route (§6.6), the corrected §6.3 list, and the delivery table. |

---

### The 2026-10-11/12 wave: startup, an honest self-check, and the speech gate

Twenty-one commits, `ceccc4a` through `6342c10`, all on `claire-rewrite` and none of them pushed.
They came from one long session against the live machine, so much of the evidence is what the running
daemon did rather than what a harness imagined.

| Commit | What it changed |
|---|---|
| `ceccc4a` | **A built-in `linux-desktop` MCP server**, so she can act on this machine - windows, input, clipboard - without an external server process. |
| `354e5d5` | **She answers back immediately.** An accepted turn gets a short spoken acknowledgement instead of silence until the whole reply is ready. |
| `8de6c80` | **All three components start at login.** `--install-service` renders and enables the panel unit and the tray's XDG autostart entry beside the daemon unit. This commit also wrote the startup section of this document. |
| `97cc346`, `72dffac` | **The tray app attaches to a supervised daemon** and stops claiming it is missing; its log window follows `journalctl --user -u claire -f` when the app did not spawn the process. |
| `ebed955`, `4baa11f`, and the anchor fix in this revision | **The documentation anchors were re-measured**, not offset: about 1,519 anchor instances walked, 260 numbers re-pointed across 11 files, and prose that was *wrong* rather than moved corrected - the loader reads **seven** environment variables (a `GEMINI_API_KEY`/`GOOGLE_API_KEY` pair had been missed), shutdown order (diary update, then TTS stop, then `db.close`), the panel's `route-not-found` code and an `Allow` header it never sends, "both write routes" where four handlers carry the guard. Commit `8908587` re-anchored a further 155 numbers after the gate work moved `config.py`, `listener.py`, `tts.py` and `debug.py`, with two tools committed so the next move is mechanical; see section 7. |
| `ed988df` | **An embedding probe says why it failed**, in the runtime's own words (`404 model "nomic-embed-text" not found, try pulling it first`, `402 Insufficient credits`, `400 Unknown embedding provider`), redacted, with the consequence. `embed_with_reason()` on every backend; `embed()` delegates. This is what made section 6.6 fixable. |
| `0242def` | **The NeMo recogniser is serialised.** Its decode is not re-entrant and the voice loop and the phone walkie-talkie share one loaded model by design, so a phone upload could corrupt a live transcription. Reproduced at **3 of 18** concurrent decodes; 18 of 18 pass with `_decode_lock`. |
| `d91494e` | **A 5xx model list is settled by a real turn.** The gateway answers `GET /models` with `503 catalog_build_timeout` while it builds a 9,508-entry catalogue (4.5 MB, 8.1 s) and `POST /chat/completions` with 200 in about a second; the self-check called that "no ready LLM" and now runs one `max_tokens: 1` turn before believing it. |
| `5017776`, `76c53a8` | **Local unit policy survives a re-install** (the drop-in rule in section 1), and **`--no-tune` / `CLAIRE_TUNE_ENABLED`** silence the pinging sound for testing. |
| `f700cd0`, `e8e23aa` | The panel's Origin guard docstring and spec now describe the rule the code implements - a missing or opaque (`Origin: null`) origin is accepted **only** with `Sec-Fetch-Site: same-origin`; and `stt_engine` accepts every spelling the factory knows (`whisper`, `faster-whisper`, `faster_whisper`) instead of silently running Parakeet for a config that named Whisper. |
| `d7d16ee`, `b4bf126` | Two `tts.spec.md` anchors and the `create_tts_engine` docstring: `piper` is the *signature* default, not what a fresh install runs (`fish` is). |
| `9f468ec`, `19b0bb0`, `31c1dd1`, `6379181`, `6342c10` | **The speech gate got an absolute level floor**, and a VAD that cannot start is no longer silent. The mechanism, the numbers and what it does not solve are in section 6.13; the config key is `noise_gate_min_rms`, default `0.002`, and `0.0` disables it. |

The commits were verified the way this document asks: `git log` for every hash, the unit gate for
every code change, and a live daemon for the voice claims.

---

## 4. The measured wins, and the kind of evidence for each

| Win | Measurement | Evidence |
|---|---|---|
| One weather question, end to end | **129 s -> 8.7 s** | A driven harness. No committed artifact; **the measurement predates the tool's removal**, by `82605e5`, so it is evidence for the reply path of that era, not something reproducible today. |
| Plan-driven direct-exec | median **6.06 s -> 4.36 s**, chat turns **2 -> 1** | Interleaved A/B against a pristine worktree, **n=6**, recorded in the `e6e682b` commit body. Measured on the since-removed weather tool, so it is evidence for the *mechanism* (a plan step is executed without paying a chat turn), not for that query. The same run shows the time query correctly deferring (`5.57 -> 5.91 s`, turns 2 -> 2, contention noise). |
| Speculative memory-extractor prefetch | chain median **5.97 s -> 2.90 s** | Adjacent arms of a deterministic target case, **n=3**; fire rate about **25 % of real memory turns**, so the expected saving is about **0.75 s per memory turn**. The mechanism lives in [`src/claire/reply/memory_prefetch.py`](../src/claire/reply/memory_prefetch.py) (`MemoryExtractorPrefetch`), wired into `run_reply_engine`, with the epoch guard at `DialogueMemory.hot_cache_epoch` ([`conversation.py:1306`](../src/claire/memory/conversation.py#L1306)). |
| Location resolution | **30.1 s -> 0.001 s** | `f615e66`: an untimed UPnP probe blocked about **33 s** on this host; a DNS query resolves the same public IP in about 0.01 s; `get_location_info(auto_detect=False)` returns in 0.00 s. |
| A stalled memory extractor | **30.04 s -> 15.02 s** | `9e5714f` and [`docs/llm_contexts.md`](llm_contexts.md): a black-holed endpoint took exactly two ceilings and returned `{}` either way, so a timeout is no longer retried. The extractor's own ceiling is now `llm_extractor_timeout_sec` (default `8.0`). |
| Reply-path LLM ceilings | **180/300/60 s -> 45/15/5 s** | `9e5714f` config diff: `llm_chat_timeout_sec`, `llm_tools_timeout_sec`, `llm_embedding_timeout_sec`. A healthy turn spends 2-4 s per chat round-trip and 2-3 s on the router, so the ceilings bound the worst case while leaving several times the observed median. |
| The audio burst | normal speech peaks at **5587/32768 (-15.4 dBFS)**; an MP3 body played as if it were 16-bit PCM peaked at **32768/32768 (0.0 dBFS)**, RMS about **27 dB up** | `32472ba` commit body, with the values also in [`src/claire/output/fish_tts.py`](../src/claire/output/fish_tts.py) (`:122`, `:141`, `:309`, `:417`) and [`fish_client.py:176`](../src/claire/output/fish_client.py). Live after the fix: a real client and device played 38 blocks, max peak 4555 (-17.1 dBFS), with `pactl` showing the stream at 100 %/0.00 dB, so nothing raises the stream volume. The `-3` dBFS backstop never engages on her speech. |

### Measured against the live machine

The wave in section 3 was measured on the running daemon, which is the only way to see any of this:

| Win | Measurement | Evidence |
|---|---|---|
| A quiet room stops inventing speech | **22 hallucinated utterances in 7 min -> 1 transcript in 3 min** | The 22 measured 0.00021-0.00151 RMS (`"Yeah."` x8, `"Okay."` x4, `"Uh."`, `"Oh."`, `"Mm-hmm."`, `"What the fuck?"`, `"It's always nice, okay."`) against a room floor of 0.00008. Real speech that same session measured 0.00226-0.00926, and after the fix 0.00325 for "Claire, can you hear me right now?" - accepted, 2,460 ms of VAD speech. The new floor sits at 0.002, above every hallucination and below the softest real utterance. |
| A working gateway is not called broken | `GET /models` **503 after 8.1 s** against `POST /chat/completions` **200 in about 1 s** | The 9,508-entry catalogue build, reproduced twice, then settled by one `max_tokens: 1` turn (`d91494e`). The old check called the pair "no ready LLM". |
| The recogniser survives concurrent decodes | **3 of 18 corrupted -> 0 of 18** | A phone upload against a live transcription (`0242def`). |
| Daemon memory is stable, not leaking | **10.09 GB over 60 s**, host 23 of 31 GB used, no kernel OOM evidence | One process, one measurement. The 01:34 restart was a coordinated `start.sh restart` with `NRestarts=0`, not a crash. |
| The gate | **5507 passed, 3 skipped, 25 deselected, 0 failed in 225 s** | Section 2. |

Two numerical estimates on the planner/router question were also corrected by measurement; see
section 6.

---

## 5. Architecture and documentation map

[`docs/ARCHITECTURE.md`](ARCHITECTURE.md) is the **hub**. It orients you, shows the spoken-sentence
path end to end, and links one focused page per subsystem. The nine spokes in
[`docs/subsystems/`](subsystems) are:

| Spoke | Owns |
|---|---|
| [`listening.md`](subsystems/listening.md) | Device capture, VAD, utterance assembly, the two recognisers, wake word, echo detection, the intent judge |
| [`intent-and-reply.md`](subsystems/intent-and-reply.md) | Redaction, context budget, tool routing, planning, prompts, the agentic loop, digests |
| [`memory.md`](subsystems/memory.md) | The diary store, the knowledge graph, the router file, the notes, recall, the extractor prefetch |
| [`speech-output.md`](subsystems/speech-output.md) | Fish streaming TTS, Piper and Chatterbox fallbacks, the PCM contract and the player |
| [`phone-bridge.md`](subsystems/phone-bridge.md) | The HTTP bridge, routes, auth, bind policy |
| [`panel.md`](subsystems/panel.md) | The local web panel, its origin guard and its memory browser |
| [`configuration.md`](subsystems/configuration.md) | Config keys, defaults, migration and the example file |
| [`deployment.md`](subsystems/deployment.md) | `start.sh`, the systemd unit, Docker, logging, shutdown |
| [`mcp.md`](subsystems/mcp.md) | MCP discovery, the client and the server template |

Other documentation worth knowing:

* [`docs/llm_contexts.md`](llm_contexts.md) documents every prompt and its context budget: which
  model tier, the `num_ctx`, the timeout, the token cap, and the call site for each LLM context.
  Read it before changing a prompt or a timeout.
* [`README.md`](../README.md) is the user-facing page. [`docs/CONFIGURATION.md`](CONFIGURATION.md),
  [`docs/MEMORY.md`](MEMORY.md), [`docs/OBSIDIAN.md`](OBSIDIAN.md),
  [`docs/MCP_SERVERS.md`](MCP_SERVERS.md), [`docs/AUDIO_ROUTING.md`](AUDIO_ROUTING.md),
  [`docs/ACCEPTANCE.md`](ACCEPTANCE.md), [`docs/CLOUD_POLICY.md`](CLOUD_POLICY.md) and
  [`docs/PANEL_CONVERSATION.md`](PANEL_CONVERSATION.md) are the task-shaped companions.
* [`EVALS.md`](../EVALS.md) and [`evals/`](../evals) hold the evaluation suite, which runs outside
  the unit gate.
* [`STATUS.md`](../STATUS.md) is the wave-by-wave build log — the later sections carry the current
  state, the older wave entries are historical. [`HANDOFF.md`](../HANDOFF.md)
  is an earlier overnight handoff, partly superseded by this document. Neither has been removed.

**The rule that outranks the prose:** a `*.spec.md` file sitting next to code is the authoritative
contract for that code. When a spec and a prose page disagree, the spec and the code win, and the
prose page is the thing to fix.

---

## 6. What is still open

This is the section to read twice. Items are ordered roughly by how much they cost to get wrong.
A few subsections record work that has since closed — the doc/code contradiction list (6.3), the
unit gate (6.2), the retired `location_city` key (6.4), the location-discovery landmine (6.5), the
embeddings route (6.6) and the two owner follow-ups (6.11) — because the closing facts still matter
to whoever reads this next.

### 6.1 The planner/router overlap is disproven, not pending a retry

The idea was to run the task planner concurrently with the tool router, so the router's round-trip
would stop being dead time in front of the planner. It was implemented (`61a4842`), spec'd
(`bf1af83`), reduced to a settle-only race (`2b01364`), measured, and reverted (`b57faa8`) along
with `planner_prefetch.py` and its tests.

The counterexample is concrete: over 32 paired turns (real router and planner, both trees,
alternating, 4 query classes x 8 repeats), the live arm had a usable speculative plan on only
**8/32** turns. On the other 24 the router won the race, so the prefetch contributed nothing but a
second in-flight LLM request. On those turns the serial fallback planner then ran against a backend
that was also serving the abandoned speculative call, and **5/32 turns lost a plan the serial-only
arm produced**, each one simultaneously a lost plan and a slower turn (for example `sched-add i=0`
base 4.14 s plan=2 -> live 4.60 s plan=0). Across all 32 turns, 7 were slower by more than 0.25 s
for 18 faster (median -0.36 s, mean -0.93 s).

The root cause is structural, not a tuning problem. The premise was that the router's round-trip is
dead time in front of the planner, but on this capacity-limited shared gateway the two LLM calls
contend for the same capacity. The overlap therefore does not move work off the critical path; it
converts a bounded, predictable serial cost into a load-dependent risk of losing the plan, and it
costs the fallback planner the ceiling it used to meet comfortably (a contended `plan_query` call
hits `planner_timeout_sec` and returns the `[]` that means "no plan"). No bound repairs that: a
design that waits does so and loses, and a design that does not wait still contends, and the loss
lands in the fallback, which is supposed to be the safe path.

Two earlier numerical estimates on this question were also wrong and were corrected by
measurement: a token-count rejection that claimed the overlap would be "2-3x slower", and the
rebuttal's "+0.26 s". Thirty paired calls gave median **+0.00 s** and mean **+0.09 s** for the full
catalogue.

**Do not re-attempt the overlap.** The productive direction is the backend's capacity, not the
ordering of the two calls. The four commits are left in history on purpose.

### 6.2 The unit gate is no longer a hole (resolved)

See section 2. The deselection trap is closed: 1,460 formerly invisible tests run in the gate, the
25 remaining deselections are all deliberate, `PRE_EXISTING_UNMARKED` is empty, and the guard now
detects partial marking per test through pytest's own collection rather than a file-level substring
match. What remains is cost, not a hidden hole: about 3.5 min and ~6.8 GB peak RSS per run, so it is
not something to run alongside other heavy work. Fixing the gate exposed rot as well as missing
coverage — some of the newly visible tests were stale — so when a test is added, give it an honest
marker rather than shrinking what the gate runs.

### 6.3 Doc/code contradictions (the listed ones are corrected)

The contradictions this section used to record as deliberately left have since been corrected in the
source. Each was re-checked against the code for this revision, and none remains:

* [`src/claire/output/tts.spec.md`](../src/claire/output/tts.spec.md) now opens by naming `piper`,
  `chatterbox` and `fish` as the three engines, records that `fish` is the shipped default, and says
  plainly that the cloud path is what a fresh install takes. The old "runs locally / neither backend
  sends user text or audio to a cloud synthesis service" claim is gone.
* [`src/claire/phone/bridge.spec.md`](../src/claire/phone/bridge.spec.md) now reads
  `DEFAULT_PORT == 8788`, matching [`bridge.py:203`](../src/claire/phone/bridge.py); the remaining
  `8765` mentions are the panel-port explanation, not a stale default.
* [`src/claire/llm/llm.spec.md`](../src/claire/llm/llm.spec.md)'s `warm_up` row now describes the
  single-token `OpenAICompatibleBackend` probe with no `GET /models` gate, and its factory table
  gives `llm_provider` the code default `"openai_compatible"`. The only `"ollama"` default left is
  the v1 -> v2 migration step, which is what `_migrate_config` does.

Two other candidates reported as drift turned out to be **already correct**:

* [`src/claire/memory/graph.spec.md`](../src/claire/memory/graph.spec.md) line 133 heads the section
  "Auto-Merge (Future — requires LLM integration)". It does: `MERGE_THRESHOLD` (`graph.py:90`) is
  read by no code, so the spec accurately describes unimplemented future work.
* [`docs/MEMORY.md`](MEMORY.md) already documents the `vault_dir` key (lines 47 and 145), so it
  needed no correction.

The owner-approved cleanup (`c5fefe5`) did correct `src/` prose that really was stale:
[`src/claire/reply/engine.py`](../src/claire/reply/engine.py) no longer names the removed
`getWeather`; [`src/claire/memory/recall_gate.py`](../src/claire/memory/recall_gate.py)'s module
docstring now says "asymmetric keyword coverage" rather than the symmetric Jaccard it never
computed; and
[`src/claire/memory/summariser.spec.md`](../src/claire/memory/summariser.spec.md)'s signature
description matches the code. Two specs cite the removed tool on purpose, as historical measurement
context, and stay as they are:
[`src/claire/reply/reply.spec.md`](../src/claire/reply/reply.spec.md) line 122 and
[`src/claire/reply/planner.spec.md`](../src/claire/reply/planner.spec.md) lines 231-233.

### 6.4 `location_city` was a dead config key (re-exposed, live again)

`location_city` was declared, defaulted and merged but read by no code — its only consumer was the
`getWeather` tool that `82605e5` removed — so it was **retired** along with six other dead keys
(`c5fefe5`). The live `~/.config/claire/config.json` kept the old `"Lisbon"` value as an unknown
key: config load preserved it, but nothing read it.

**Resolved:** with IP detection and the GeoLite2 database gone, a configured city is the only
location source left, so the key is **exposed again** as `Settings.location_city` (default `""`)
and is read by `claire.utils.location`. The owner's existing `"Lisbon"` takes effect with no
migration, because unknown keys are merged over the defaults — the value was already on disk.

### 6.5 `get_location_info()` is a live landmine (resolved)

[`get_location_info()`](../src/claire/utils/location.py) had `auto_detect: bool = True`, and with
auto-detect it took about **12 s** on this host because there was no GeoLite2 database installed
(the seeder was `scripts/setup_geolocation.py`, deleted with the feature). It was off the hot path
only because the live config set `location_auto_detect: false` — a default-safe call was all that
stood between Claire and a 12 s stall per reply.

**Resolved:** the whole mechanism was removed rather than left dormant.
`get_location_info()`, the IP/UPnP/OpenDNS discovery chain, the GeoLite2
download and the disk caches are gone; `location.py` now reports only an
explicitly configured city and never touches the network, so the config line
is no longer load-bearing. The Location page was deleted from the setup
wizard, and the reply path and time tool fall back to the OS timezone. See
[`src/claire/utils/location.spec.md`](../src/claire/utils/location.spec.md).
The city is now whatever `location_city` says — the live config sets
`"Lisbon"`, so the assistant names a real city — and an empty value falls back
to `"Location: Unknown"`.

### 6.6 Embeddings are reachable at runtime (resolved)

This used to be recorded as unreachable. The provider follows the LLM provider (`openai_compatible`,
the local gateway), whose `/v1/embeddings` answered `model "nomic-embed-text" not found, try pulling
it first`, because Ollama held only a chat model. Two things changed.

The probe now reports the runtime's own reason (`ed988df`), which is what turned a vague
"embeddings unavailable" into an actionable message; and the model was pulled - `ollama pull
nomic-embed-text` (274 MB, 768 dimensions, matching the `sqlite-vss` table and the FAISS index). The
live banner reads `Embedding probe passed: 'nomic-embed-text'` and memory search has a real vector
path. Do not re-report this as broken.

The alternative route still exists: a native Gemini embeddings provider, selected with
`embedding_provider: "gemini"`, keyed by `embedding_api_key` or `GEMINI_API_KEY` / `GOOGLE_API_KEY`,
output pinned to the 768-dim index and embedded asymmetrically (`RETRIEVAL_DOCUMENT` when indexing,
`RETRIEVAL_QUERY` when searching, tool selection included), documented in [`README.md`](../README.md)
and [`docs/CONFIGURATION.md`](CONFIGURATION.md), specified in
[`src/claire/llm/gemini.spec.md`](../src/claire/llm/gemini.spec.md). It is unnecessary while the local
model is present. If the probe fails again, read its message before changing providers: three
different failures were seen on the way here - `404` not pulled, `400` unknown embedding provider,
`402` insufficient credits - and only the first is fixed by pulling a model.

### 6.7 Graceful shutdown is still not graceful

SIGTERM has historically hung past 90 s until systemd SIGKILLed the process mid-write. It is
intermittent, so it is not reproducible on demand. `_install_signal_handlers()` in
[`daemon.py`](../src/claire/daemon.py) maps
`SIGINT`, `SIGTERM` and `SIGBREAK` to the same handler, and
[`docs/subsystems/deployment.md`](subsystems/deployment.md) lines 140-141 document `start.sh stop`
using SIGTERM with a 20 s wait then SIGKILL. The risk that matters is the mid-write kill, not the
delay. Treat as unverified and worth a dedicated investigation.

### 6.8 The Android app: on-device results, and three things still open

The app has now been run on a physical phone — the first time — and the instrumented suite was driven
against it. Device: a Redmi 15 5G (`25057RN09E`, `spring_eea`, `device:spring`), Android 15 / SDK 35,
arm64-v8a, 1080x2340 @ 450 dpi, reached over wireless ADB at `100.68.123.69:36077`. The app under
test is `com.claire.companion`, versionCode 4 / "0.4.0", a **debug** build. The client source is
[`android/`](../android).

**Instrumented result: 11 executed — 10 passed, 1 skipped, 0 failed**
(`CameraAndVoiceTest` 7, `CompanionUiTest` 4).

* **Defects 4 and 5 are refuted on-device** (both pass): the shutter clears the gesture area and is
  large enough to hit, and the rendered location line never recites coordinates —
  `ClaireState.setLastFix` discards lat/lon and stores only `"±%.0f m"`.
* **A real defect was found and fixed** (`1305415`). In
  [`CameraActivity.java`](../android/app/src/main/java/com/claire/companion/CameraActivity.java),
  `startCamera()`'s catch block called `shutter.setEnabled(false)`. That made `capture()`'s
  deliberate "the camera had not finished binding — tap again" retry unreachable, because `showLive()`
  is the only other enable path and a bind failure never reaches it; the owner was stranded on a dead
  shutter button, which is exactly the reported "photos error out" / "I can't tap it" symptom. The fix
  keeps the shutter enabled so the retry works. The pre-existing test
  `theShutterEitherTakesAPhotoOrNamesTheRealError` caught it, and no assertion was weakened.

**Still open — the shutter fix must not be read as having solved the camera problem:**

1. **The intermittent CameraX bind failure itself is not fixed.** Logs show `com.claire.companion`
   connecting to camera device 0 and dying within about 4 s with
   `DIED client(s) with PID ... reason: Binder died unexpectedly`. The next step is capturing logcat
   tags `CameraX` / `Camera2CameraImpl` at the moment of failure.
2. **Defect 3 is not verified — it skips, which is not a pass.** It is gated by
   `assumeTrue(!prefs.fishToken().isEmpty())`, and the test device has no Fish key
   (`fish_token non_empty=False len=0`). It needs a saved Fish Audio key plus internet to
   `api.fish.audio`.
3. **Defect 6 has no definition anywhere in the repository.** A repo-wide grep finds only defects 3, 4
   and 5 labelled, and only inside `CameraActivity` / `CameraAndVoiceTest`-adjacent files. This is a
   documentation gap to fill, not a defect to test — do not invent one.

**A MIUI/ROM obstacle anyone repeating this will hit.** `connectedDebugAndroidTest` hangs rather than
fails when the phone is in use: Xiaomi denies an activity launch from the instrumentation process
while another app is foreground (`ActivityStarterImpl: MIUILOG- Permission Denied Activity`), and UTP
passes `-e testTimeoutSeconds 31536000`, so a blocked run hangs for a year. The proven workaround is to
run `adb shell am start -W -n com.claire.companion/.MainActivity` immediately before a launch-based
test and leave the phone idle and unlocked. `adb install -g` and `pm grant` are both **blocked on this
ROM** (`INSTALL_GRANT_RUNTIME_PERMISSIONS` / `GRANT_RUNTIME_PERMISSIONS` denied to shell), but the
owner's five runtime permissions were already granted and survived reinstalls.

### 6.9 The memory vault has no notes

`~/.local/share/claire/vault/` currently holds `CLAIRE.md`, a `CLAIRE.md.save` backup, an empty
`notes/` and an empty `.obsidian/`. The router's `CLAIRE.md` points at `notes/dentist.md` and
`notes/car-service.md`, which were never written, so both pointers are dead and
[`docs/OBSIDIAN.md`](OBSIDIAN.md) illustrates the same two files in its layout example.

To use the vault from Obsidian: point it at `~/.local/share/claire/vault`, or set `vault_dir` (the
config key, or `$CLAIRE_VAULT_DIR`) to somewhere you prefer, then "Open folder as vault". The vault
sits outside the repository on purpose; resolution order is in `docs/OBSIDIAN.md` and
[`config.py`](../src/claire/config.py) `_default_vault_dir()`.

### 6.10 `fast_model` is deliberately unset

The owner's model choice (`cmd/deepseek/deepseek-v4.1-flash` through the local OmniRoute gateway)
is not to be changed, and `fast_model` is intentionally left empty so the tier resolution falls
back as designed. Do not "helpfully" fill it in.

### 6.11 The two owner-approved follow-ups are done

* **The production deployment no longer ships development trees.** `pyproject.toml` names the
  runtime roots positively and the eight development trees negatively under
  `[tool.setuptools.packages.find]`, and a new `MANIFEST.in` prunes the same trees from the sdist.
  The wheel was already clean (139 members, no development files). The sdist was the real leak:
  distutils adds `tests/test*.py` to every source distribution from `_add_defaults_optional()`,
  which `packages.find` cannot override, so it carried **201** files under `tests/`; with
  `MANIFEST.in` it drops from 369 members to **168** with none. The suites stay in the repository and
  runnable from a checkout; nothing was deleted. `deploy/claire.service` needed no edit because it
  runs the checkout, not an installed copy. `62c808c`; pinned offline by
  [`tests/test_prod_packaging_excludes_dev_trees.py`](../tests/test_prod_packaging_excludes_dev_trees.py).
* **The "things we don't care about" list was produced, approved and executed.** Seven dead config
  keys were retired — `location_city`, `stop_command_fuzzy_ratio`, `whisper_min_word_length`,
  `evaluator_enabled`, `evaluator_nudge_max`, `allowlist_bundles`, `active_profiles` — together with
  a stale comment, a bogus `__all__` export in `notes.py`, and stale `src/` spec/doc text, in
  `c5fefe5`. It is described in the delivery table (section 3); this is no longer open work.

One leftover from that sweep is still on disk by design: the compiled artifacts
`src/claire/tools/builtin/__pycache__/weather.cpython-312.pyc` and the two
`tests/tools/builtin/__pycache__/test_weather*.pyc` files survive the `getWeather` removal while
their sources and specs are gone. The hard rule stands — describe, do not delete.

### 6.12 Smaller, verified loose ends

* `README.md` and the prose docs have been cleared of references to the removed tool; do not
  re-report those.
* Five earlier handoff/status documents remain on disk and are stale or partly superseded:
  [`STATUS.md`](../STATUS.md) (wave-by-wave log), [`HANDOFF.md`](../HANDOFF.md) (overnight, dated), and
  three outside the repository in `/home/mana/Documents/Jarvis/` (`handoff_tmrw.md`,
  `OWNER_ACTIONS_2026-10-07.md`, `omniroute-latency-forensics.md`). Of these,
  `omniroute-latency-forensics.md` is the most detailed latency evidence available for the shared
  gateway and is worth reading before touching anything about capacity.

### 6.13 A loud room is not a threshold problem (open, the owner's call)

The new absolute floor removed the quiet-room storm (section 4). What remains is *genuine* background
audio: with music and conversation in the room, the transcripts that still reach `Heard:` measured
**0.0048-0.027** RMS - song lyrics, other people, Spanish chatter - and the wake word then ignores
them (`input ignored (no wake word)`, 12 times in one window), so nothing is acted on. The cost is
CPU, log volume and a noisier context buffer, not wrong behaviour. In the same window the gates
discarded 14 utterances as `speech below the audible floor` and 30 as `speech too short`.

No threshold fixes this, and the measurements say why. On this machine the owner's voice measured
**0.0032-0.0093** RMS while the background measured **0.0048-0.027**: the ranges overlap and the
background is often *louder* than the voice. A level gate cannot prefer a quiet voice to loud music;
only the wake word separates them, and it already does.

The real levers, in order of value:

1. **Microphone gain.** The input chain is quiet end to end: PulseAudio source volume **47 %
   (-19.6 dB)** into USB capture at **164/256 = 64 % (-34.2 dB)**, on a C-Media device the kernel
   falls back to mono for. Claire opens `default` at 16 kHz mono. Raising the *hardware* capture gain
   improves the signal-to-dither ratio; raising the software volume only scales digitally. A healthy
   level would make every threshold in this pipeline behave as designed, including `voice_min_energy`,
   whose default of `0.02` expects speech roughly ten times louder than this microphone delivers.
2. **`vad_aggressiveness: 3`** would trim instrumental music, but not singing or speech (`webrtcvad`
   reads both as speech) and it risks clipping softer words. Deliberately left at `2`.
3. **Suppress non-wake transcription or logging** if the log volume is the annoyance: keep the decode
   and the wake path, retain transcripts only inside a hot window. Not implemented, and not asked for.

None of the three is a defect; the microphone is the cheap one, and it is the owner's hardware to
change.

### 6.14 Two load-sensitive flakes (open, low cost)

Neither is a product defect and neither comes from this wave; both are timing assumptions in mocked
harnesses that surface when the machine is busy.

* `tests/test_voice_listener.py::TestSampleRateFallback::test_fallback_to_native_rate_on_invalid_sample_rate`
  (4 parametrisations) asserts the recogniser receives exactly 6,400 samples and occasionally gets
  16,000 - an unfinalised 1.0 s buffer instead of the 0.4 s utterance. It was instrumented: the
  deliverable does reach the decode, so the speech gate is not implicated. Deferring the fake stop
  flag by one call reduced it from about 7/10 to 2/12 runs but did not eliminate it, so that partial
  fix was reverted rather than left in as a guess. The same test passes **15/15** on a calm machine.
* The Piper 429-retry test passes 6/6 alone and 3/3 as a file, and fails only inside a full run under
  load.

A real fix for the first is to drive that mocked capture synchronously and assert on what the
segmenter produced, rather than on an exact buffer length handed to a mocked model.

### 6.15 Refactor-grade leftovers the audit noticed (open, harmless)

Reported by the documentation audit that re-anchored the docs, verified to exist, and deliberately
left: none has a user-visible symptom and each carries regression risk.

* `listener.py:871` carries an early echo check that the audit read as a duplicate of the later one at
  `:1147`.
* `mcp_client.py` has a duplicated transport guard.
* `tools/registry.py:334` has a dead `if MCPClient is None` branch.
* `memory/db.py:19` mixes a legacy `meals` table into the memory schema.
* `voice_min_energy` disagrees with itself: the shipped default is `0.02`
  ([`config.py:661`](../src/claire/config.py#L661)) while the listener falls back to `0.0045`
  (`listener.py:1706`, `:1718`), and [`docs/subsystems/listening.md`](subsystems/listening.md) already
  flags the discrepancy. With a microphone this quiet neither value describes this machine, which is
  why the gate's absolute floor is the knob that actually works here (section 6.13).

---

## 7. Operational gotchas that cost real time

* **`pactl` and PipeWire commands need `XDG_RUNTIME_DIR=/run/user/1000`.** Without it they fail with
  `Connection refused`; with it `pactl info` reports the server at `/run/user/1000/pulse/native`.
  Wrap audio commands rather than assuming the environment.
* **The phone bridge's `/text` route carries utterances from the phone to Claire.** Claire's replies
  go back to the phone through the drain-once queue served by `GET /reply` (`ReplyQueue.put_text()`
  shape, pushed with `POST /say`); they are queued for the phone, not spoken aloud through the
  laptop. Do not expect a phone turn to be audible in the room.
* **A stale `.git/index.lock` can survive a crash.** Only remove it after `pgrep -af git` shows
  nothing running. Removing it under a live git process corrupts the index.
* **Several agents worked in this one tree simultaneously, and uncommitted work was destroyed more
  than once.** The durable lesson: commit each verified piece immediately with explicit pathspecs
  (`git -c user.name="Claire Build" -c user.email="claire@localhost" commit -m "..." -- docs/HANDOVER.md`),
  never `git add -A`, and use a separate `git worktree` for a pristine A/B baseline rather than
  stashing, restoring or cleaning the shared tree. Never run `git restore`, `git checkout --`,
  `git reset --hard`, `git stash` or `git clean` in this tree.
* **The machine is memory-tight.** Measured: about **21.8 GiB of 31.3 GiB** in use with the **2.0 GiB
  swap essentially fully consumed**, and the running `claire` unit reporting about 10.3 GiB
  `MemoryCurrent`. Claire was OOM-killed at least once during this programme. Before starting
  anything heavy, check `free -m` and expect contention; assert nothing about latency on a loaded
  machine without taking an interleaved A/B baseline.
* **A green gate is not the whole story.** The gate runs `unit` only; the deliberately deselected
  `integration`, `e2e` and `performance` tests still run outside it, so use `-m ""` (section 2) when a
  change touches those paths. The unmarked-test trap that used to hide tests *inside* the gate is
  closed and guarded, so the gate's own count is trustworthy.
* **A level read off a test double is full scale.** `float(MagicMock())` returns **`1.0`** - not an
  error and not the default - so `float(getattr(cfg, "some_threshold", default))` turns a mock into
  maximum sensitivity and the thing under test goes deaf. This broke the speech gate during the wave
  in section 3, and the suite reported it only as a sample-rate mismatch (`assert 16000 == 6400`).
  Validate a level before using it (the `_audible_floor()` validator in `listener.py` rejects
  non-finite, mock-like and out-of-range values) and make test doubles carry the key.
* **`load_dotenv(path, override=False)` can set a variable but never unset one.** Deleting
  `CLAIRE_VOICE_DEBUG=1` from `~/.config/claire/claire.env` leaves it on in a running daemon, which
  then looks like the flag being ignored. Turning a debug flag off needs a restart; turning one on is
  live within a couple of seconds. Make sure you are reading the *new* process's log rather than the
  old pid's buffered output.
* **`src/claire/config.py` must not import the debug helpers.** `debug` imports `load_settings` from
  `config`, so `from .debug import debug_log` in `config.py` is a circular import. The public
  `debug_enabled()` in [`src/claire/debug.py`](../src/claire/debug.py) is for callers such as the
  listener; config-side code stays out of that module.
* **A doc anchor is correct only relative to a commit.** `docs/` and `*.spec.md` cite `file.py:123`,
  and any code move above that line silently invalidates every citation below it - twice in this
  programme, about 260 numbers each time, and 155 more after the gate work (`8908587`). Two scripts
  exist for it: [`scripts/check_doc_anchors.py`](../scripts/check_doc_anchors.py) is the audit, whose
  only *hard* failure is a line past the end of its file (everything else is advisory and needs a
  human), and [`scripts/reanchor_doc_anchors.py`](../scripts/reanchor_doc_anchors.py) re-points
  anchors exactly from `git diff <commit>..HEAD`, refuses to guess anything it cannot map, and refuses
  to touch a document with uncommitted changes so it cannot be run twice over the same shift. Run the
  checker after moving code. Anchors into the generated
  [`examples/config.json`](../examples/config.json) cannot be mapped exactly at all - both scripts
  report them instead of rewriting, and the durable fix is to cite the key, not the line.
* **`.gitignore` line 29 ignores `*token*` anywhere in a filename**, so a new test called
  `test_token_budget.py` would be silently untracked; that is why the recogniser-spelling test is
  `tests/test_stt_engine_spellings.py`. Check with `git check-ignore -v <path>` when a new file
  mysteriously does not appear in `git status`.
* **One daemon, and prove it before debugging audio.** `pgrep -af "python -m claire"` and
  `systemctl --user show claire -p MainPID` must name the same process. The daemon starts the phone
  bridge itself, so a second daemon means two recognisers, two microphones and a bridge that cannot
  bind (section 1).
* **The rules in this section were broken once during the wave in section 3, and it must not happen
  again.** An A/B on the speech gate used `git checkout -- tests/test_voice_listener.py` (twice) and
  `git stash push -u` / `git stash pop` in this shared tree, all of which the bullet above forbids.
  The tree was verified intact afterwards - `git status` clean, only the expected files changed, the
  suite green - but that is luck, not method; the correct instrument is a separate `git worktree`. It
  is written down because a handover that hides its own violations is worse than no handover.
