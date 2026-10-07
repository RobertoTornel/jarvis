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
| Web panel | `python -m claire.panel.server`, standalone stdlib server | `127.0.0.1:8765` ([`server.py:149`](../src/claire/panel/server.py)) |
| Phone bridge | started *by the daemon*, not by `start.sh` | `127.0.0.1:8788` by default ([`bridge.py:193`](../src/claire/phone/bridge.py)), bound here to `100.104.1.69:8788` |

### `./start.sh` is the single entry point

[`start.sh`](../start.sh) resolves every path from its own location and reads every default out of
the project's own code, so nothing in it is machine-specific. Its real interface, read from the
file's argument parser (`start.sh:877`-`:895`) and its own `usage()` (`start.sh:831`):

| Invocation | Effect |
|---|---|
| `./start.sh` or `./start.sh start` | Create/use `.venv`, install `requirements.txt` idempotently, run the self-check, then bring up the daemon and the panel. Idempotent. |
| `./start.sh stop` | Stop the panel and the daemon. |
| `./start.sh restart` | Stop, then start. |
| `./start.sh status` | What is running right now. |
| `./start.sh check` (or `--check`) | Preflight and health report only; writes nothing. |
| `--dry-run`, `-n` | Print every action, execute none. |
| `--no-panel` / `--no-daemon` | Manage only the other half. |
| `--systemd` / `--direct` | Force the daemon through the systemd user unit, or as a detached process. |
| `--strict` | Refuse to start unless the self-check is all green. |
| `--force-install` | `pip install` even while a Claire daemon is live (otherwise the live venv is left alone). |
| `--install-service` | Render [`deploy/claire.service`](../deploy/claire.service) for this checkout and install it as a systemd **user** unit. |
| `-h`, `--help` | Usage. |

Environment overrides (all optional, documented at `start.sh:30`-`:41`): `CLAIRE_VENV`,
`CLAIRE_PYTHON`, `CLAIRE_STATE_DIR`, `CLAIRE_LOG_DIR`, `CLAIRE_SERVICE_NAME`, `CLAIRE_DAEMON_MODE`
(`auto|systemd|direct`), `CLAIRE_PANEL_HOST`, `CLAIRE_PANEL_PORT`, `CLAIRE_PREFLIGHT_TIMEOUT`,
`CLAIRE_START_WAIT`, `CLAIRE_FORCE_INSTALL`. Exit codes: `0` ok, `2` usage, `3` preflight failed,
`4` a component did not come up.

Two things about startup that are easy to get wrong:

* **The daemon starts the phone bridge itself** (`_start_phone_bridge`, opt out with
  `CLAIRE_PHONE_BRIDGE=0`). Never start the bridge a second time: two recogniser instances on one
  GPU fault.
* **`deploy/claire.service` is a template**, not an installable unit. It contains
  `@CLAIRE_REPO_ROOT@` where the checkout path belongs. Do not copy it verbatim into
  `~/.config/systemd/user`: the placeholder is not a path and `ExecStart` fails with "No such file
  or directory". Install it with `./start.sh --install-service`. It is a *user* unit on purpose:
  Claire needs the login session's microphone, speakers and display, which a system unit started by
  PID 1 does not have.

### Configuration

* Live config: `~/.config/claire/config.json`, mode `0600`. The installed file currently holds 16
  keys. Anything not present falls back to the defaults in [`src/claire/config.py`](../src/claire/config.py).
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

---

## 2. The test gate, and its most important trap

```
cd /home/mana/Documents/jarvis_cloud
.venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider
```

Current result on a clean tree at `1231655`: **3822 passed, 1 skipped, 1485 deselected, 0 failed**
(about 157 s). The total collected is 5308.

### The trap: an unmarked test file is deselected, not failed

`pytest -m unit` selects only tests carrying the `unit` marker. A test file with no marker is
**deselected**: it still counts as "green", and its failures are invisible in the gate. This is not
hypothetical.

Of the 1485 deselected tests:

* **1117 are the entire contents of 56 files** whose text contains no `pytestmark` and no
  `pytest.mark.unit` anywhere. Collecting just those files under `-m unit` collects nothing at all.
  The list is pinned in `PRE_EXISTING_UNMARKED` in
  [`tests/test_test_files_are_marked.py`](../tests/test_test_files_are_marked.py), and that list is
  the baseline inherited from upstream. Do not add to it: mark the new file.
* **Only 28 tests in the whole suite carry an explicit `integration`, `e2e` or `performance`
  marker**, and some of those sit inside the 56 files above, so the two groups are not disjoint. The
  point is that almost none of the 1485 is a deliberate "deferred suite" decision: the remainder are
  tests that simply carry no marker, and they are not running anywhere by default.
* The guard in `tests/test_test_files_are_marked.py` is looser than it looks: `_unmarked_files()`
  skips a file as soon as the *strings* `pytest.mark.unit` or `pytestmark` appear anywhere in it
  (`test_test_files_are_marked.py:36`). A file with one marked test and a hundred unmarked ones
  passes the guard while those hundred are deselected. For example,
  [`tests/test_voice_listener.py`](../tests/test_voice_listener.py) has exactly one
  `@pytest.mark.unit` (line 2063) and **101 of its 104 tests are deselected**; the listener suite for
  the core voice path is largely outside the gate.

**Verify with `-m ""` before believing a green gate:**

```
.venv/bin/python -m pytest tests/ -m "" -q -p no:cacheprovider
```

This has already masked real breakage. Two tests in
[`tests/test_engine_planner_integration.py`](../tests/test_engine_planner_integration.py) were
failing because they drove their scenario through a tool that had been deleted; the gate
deselected the file, so the failures never surfaced until the file was run with `-m ""`. They were
repaired by `1acc426` and `4722826`.

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

---

## 4. The measured wins, and the kind of evidence for each

| Win | Measurement | Evidence |
|---|---|---|
| One weather question, end to end | **129 s -> 8.7 s** | A driven harness. No committed artifact; **the measurement predates the tool's removal**, by `82605e5`, so it is evidence for the reply path of that era, not something reproducible today. |
| Plan-driven direct-exec | median **6.06 s -> 4.36 s**, chat turns **2 -> 1** | Interleaved A/B against a pristine worktree, **n=6**, recorded in the `e6e682b` commit body. Measured on the since-removed weather tool, so it is evidence for the *mechanism* (a plan step is executed without paying a chat turn), not for that query. The same run shows the time query correctly deferring (`5.57 -> 5.91 s`, turns 2 -> 2, contention noise). |
| Speculative memory-extractor prefetch | chain median **5.97 s -> 2.90 s** | Adjacent arms of a deterministic target case, **n=3**; fire rate about **25 % of real memory turns**, so the expected saving is about **0.75 s per memory turn**. The mechanism lives in [`src/claire/reply/memory_prefetch.py`](../src/claire/reply/memory_prefetch.py) (`MemoryExtractorPrefetch`), wired into `run_reply_engine`, with the epoch guard at `DialogueMemory.hot_cache_epoch` ([`conversation.py:1278`](../src/claire/memory/conversation.py)). |
| Location resolution | **30.1 s -> 0.001 s** | `f615e66`: an untimed UPnP probe blocked about **33 s** on this host; a DNS query resolves the same public IP in about 0.01 s; `get_location_info(auto_detect=False)` returns in 0.00 s. |
| A stalled memory extractor | **30.04 s -> 15.02 s** | `9e5714f` and [`docs/llm_contexts.md`](llm_contexts.md): a black-holed endpoint took exactly two ceilings and returned `{}` either way, so a timeout is no longer retried. The extractor's own ceiling is now `llm_extractor_timeout_sec` (default `8.0`). |
| Reply-path LLM ceilings | **180/300/60 s -> 45/15/5 s** | `9e5714f` config diff: `llm_chat_timeout_sec`, `llm_tools_timeout_sec`, `llm_embedding_timeout_sec`. A healthy turn spends 2-4 s per chat round-trip and 2-3 s on the router, so the ceilings bound the worst case while leaving several times the observed median. |
| The audio burst | normal speech peaks at **5587/32768 (-15.4 dBFS)**; an MP3 body played as if it were 16-bit PCM peaked at **32768/32768 (0.0 dBFS)**, RMS about **27 dB up** | `32472ba` commit body, with the values also in [`src/claire/output/fish_tts.py`](../src/claire/output/fish_tts.py) (`:122`, `:141`, `:309`, `:417`) and [`fish_client.py:176`](../src/claire/output/fish_client.py). Live after the fix: a real client and device played 38 blocks, max peak 4555 (-17.1 dBFS), with `pactl` showing the stream at 100 %/0.00 dB, so nothing raises the stream volume. The `-3` dBFS backstop never engages on her speech. |

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
* [`STATUS.md`](../STATUS.md) is stale: it still says "Last updated: Wave 0". [`HANDOFF.md`](../HANDOFF.md)
  is an earlier overnight handoff, partly superseded by this document. Neither has been removed.

**The rule that outranks the prose:** a `*.spec.md` file sitting next to code is the authoritative
contract for that code. When a spec and a prose page disagree, the spec and the code win, and the
prose page is the thing to fix.

---

## 6. What is still open

This is the section to read twice. Items are ordered roughly by how much they cost to get wrong.

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

### 6.2 The unit gate hides 1485 tests, and some of them are stale

See section 2 for the mechanics. Two consequences for whoever continues:

* Fixing the gate is a real task. The two options are to mark the 56 pinned files (and the unmarked
  tests inside partially marked files) and repair whatever then fails, or to make the gate explicit
  about what it excludes. The gate as it stands is not proof the suite passes.
* Some of the hidden tests are genuinely stale. `4722826` and `1acc426` repaired two files that
  were failing for reasons unrelated to what they asserted; expect more, since nothing has been
  running them.

### 6.3 Doc/code contradictions reported but not fixed

These live in files the documentation pass could not touch (they sit under `src/`). Each was
verified against the current source; these are the current line numbers.

| Location | The disagreement |
|---|---|
| [`src/claire/output/tts.spec.md`](../src/claire/output/tts.spec.md) lines 3-5 | Claims synthesis "runs locally" and that "neither backend sends user text or audio to a cloud synthesis service". Fish Audio is the default engine and is cloud. The real policy is stated in [`ARCHITECTURE.md`](ARCHITECTURE.md) and `AGENTS.md`: synthesis is the cloud exception, Piper and Chatterbox are the offline fallbacks. |
| [`src/claire/phone/bridge.spec.md`](../src/claire/phone/bridge.spec.md) line 186 | Says `DEFAULT_PORT == 8765`. Its own section 2.1 (line 51) and the code say `8788` ([`bridge.py:203`](../src/claire/phone/bridge.py)). The SSH example at line 200 repeats 8765. |
| [`src/claire/llm/llm.spec.md`](../src/claire/llm/llm.spec.md) line 43 and line 123 | Line 43's `warm_up` row and line 123's "two-phase probe" both describe the removed two-phase warm-up. `b484518` replaced it with a single-token chat completion, because the `GET /models` gate always timed out. Line 123 is the explicit "two-phase" statement; line 43 is the API-table description that carries the same stale shape. Neither names the 180/300/60 ceilings that `9e5714f` replaced either. |
| [`src/claire/llm/llm.spec.md`](../src/claire/llm/llm.spec.md) line 67 | Gives `llm_provider` a default of `"ollama"`. The code default is `"openai_compatible"` ([`config.py:570`](../src/claire/config.py), also `:894`). |
| [`src/claire/reply/engine.py`](../src/claire/reply/engine.py) line 1974 | Still names the removed `getWeather` in a comment. |

### 6.4 `location_city` is a dead config key

[`config.py:321`](../src/claire/config.py) declares `location_city`, `:787` defaults it to `""`,
and `:1109`/`:1281` accept and merge it. Its only consumer was the `getWeather` tool that `82605e5`
removed, and a grep finds no other reader. The live `~/.config/claire/config.json` still sets it to
`"Lisbon"`, which makes the deadness concrete: it is accepted, defaulted and merged, and nothing
reads it. Either give it a consumer or retire it (retiring needs a config migration, and note the
hard rule against deleting files: this is a key, not a file).

### 6.5 `get_location_info()` is a live landmine

[`get_location_info()`](../src/claire/utils/location.py) has `auto_detect: bool = True` (`:417`),
and with auto-detect it takes about **12 s** on this host because there is no GeoLite2 database
installed (the seeder is [`scripts/setup_geolocation.py`](../scripts/setup_geolocation.py)). It is
off the hot path only because the live config sets `location_auto_detect: false`. A default-safe
call is all that stands between Claire and a 12 s stall per reply; do not remove that config line
without replacing the mechanism.

### 6.6 Embeddings do not work; memory search is keyword-only

The embedding provider is unset, so it follows the LLM provider (`openai_compatible`, the local
OmniRoute gateway), whose `/v1/embeddings` answers `model "nomic-embed-text" not found, try pulling
it first`. Ollama *is* installed and listening on `127.0.0.1:11434`, but it holds only a chat model
(`gemma-4-26b-heretic:latest`) and no embedding model. Memory search therefore falls back to
keyword matching. [`docs/CONFIGURATION.md`](CONFIGURATION.md) line 71 documents the intended
fallback; the fix is either to configure an embedding route or to accept keyword search as the
state.

### 6.7 Graceful shutdown is still not graceful

SIGTERM has historically hung past 90 s until systemd SIGKILLed the process mid-write. It is
intermittent, so it is not reproducible on demand. [`daemon.py:788`](../src/claire/daemon.py) maps
`SIGINT`, `SIGTERM` and `SIGBREAK` to the same handler, and
[`docs/subsystems/deployment.md`](subsystems/deployment.md) line 127 documents `start.sh stop`
using SIGTERM with a 20 s wait then SIGKILL. The risk that matters is the mid-write kill, not the
delay. Treat as unverified and worth a dedicated investigation.

### 6.8 Mobile/phone defects 3-6 are unverified

No Android device or emulator was ever attached during the programme (`adb devices` is empty), so
the instrumented tests for defects 3-6 have never run. The client source is
[`android/`](../android). Everything about those defects is unverified by construction.

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

### 6.11 Two owner-approved follow-ups are still outstanding

* **The production deployment must not ship tests.** `tests/`, `evals/` and the development
  harnesses should be excluded from what `start.sh` and the systemd unit install onto the running
  machine, while the suites stay in the repository. This is an owner decision, not yet implemented.
* **A candidate list of "things we don't care about"** was requested: dead code, orphaned config,
  leftover non-Linux paths, stale modules, for the owner to approve before anything is removed.
  Nothing has been removed. A first concrete entry already exists in this tree: the compiled
  leftovers `src/claire/tools/builtin/__pycache__/weather.cpython-312.pyc` and the two
  `tests/tools/builtin/__pycache__/test_weather*.pyc` files survive the `getWeather` removal, while
  the source and specs are gone. The hard rule stands: describe, do not delete.

### 6.12 Smaller, verified loose ends

* `README.md` and the prose docs have been cleared of references to the removed tool; do not
  re-report those.
* Four earlier handoff/status documents remain on disk and are stale or partly superseded:
  [`STATUS.md`](../STATUS.md) (Wave 0), [`HANDOFF.md`](../HANDOFF.md) (overnight, dated), and two
  outside the repository in `/home/mana/Documents/Jarvis/` (`handoff_tmrw.md`,
  `omniroute-latency-forensics.md`). The last is the most detailed latency evidence available for
  the shared gateway and is worth reading before touching anything about capacity.

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
* **A green gate is not evidence.** Run the gate, then run the hidden files with `-m ""` before
  reporting success (section 2).
