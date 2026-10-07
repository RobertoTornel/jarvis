# Claire

<p align="center">
  <img src="docs/img/face.png" alt="Claire's animated amber wireframe face, the desktop presence of the Linux voice assistant" width="460">
</p>

A standalone, always-on voice assistant for Linux, built for one machine and one
person. She listens continuously, wakes on her name, answers out loud, remembers
what happened, and can look at a screen, a camera, a web page or a calendar.

Claire is a **personal fork of [`isair/jarvis`](https://github.com/isair/jarvis)**
that deliberately allows cloud services (see [Attribution](#attribution-and-licence)).
Two facts define this fork and are worth stating before anything else:

- **Every LLM call Claire makes is routed through a local OpenAI-compatible
  gateway** — OmniRouter at `http://127.0.0.1:20129/v1`. Chat, tool routing,
  planning, digests, the intent judge and vision all dispatch through the single
  `claire.llm` backend. The key comes from the `OMNIROUTE_API_KEY` environment
  variable and never from a repository file.
- **Speech recognition, memory, the diary, the knowledge graph and tool
  execution stay on the machine.** Cloud is used where it is the better tool:
  the language model, vision, and speech synthesis.

---

## Contents

- [What Claire is](#what-claire-is)
- [Screenshots](#screenshots)
- [Voice interaction](#voice-interaction)
- [The reply pipeline and tools](#the-reply-pipeline-and-tools)
- [Memory](#memory)
- [Senses](#senses)
- [Scheduling, weather and location](#scheduling-weather-and-location)
- [The phone bridge](#the-phone-bridge)
- [The web panel](#the-web-panel)
- [The desktop app](#the-desktop-app)
- [The Android companion](#the-android-companion)
- [What leaves the machine](#what-leaves-the-machine)
- [Configuration](#configuration)
- [Installing and running](#installing-and-running)
- [Health and diagnostics](#health-and-diagnostics)
- [Development](#development)
- [Attribution and licence](#attribution-and-licence)

---

## What Claire is

Claire is a Python package (`src/claire/`) whose entry point is the **daemon**
(`src/claire/daemon.py`). The daemon loads settings, opens the memory database,
brings up speech synthesis, starts the voice listener on its own thread, wires
the phone bridge to that listener, and then runs a one-second main loop that
periodically flushes the diary. Speech output failing does not stop the daemon:
an unusable TTS engine is reported and replaced with an explicitly unavailable
one, and listening and text chat continue.

A fresh install on the OmniRouter route prints lines in this shape (emoji and
indentation are the house convention for CLI output):

```text
✓ Daemon started
🧠 Using chat model: cmd/deepseek/deepseek-v4.1-flash
🎤 Authoritative recogniser: parakeet (the one listening model; no fallback engine)
📡 Discovering MCP tools from 2 server(s)...
  ✅ <server>: <n> tools available
```

Everything the daemon composes is a module with its own `*.spec.md` sitting next
to it — the specs are the authoritative description of each subsystem:

| Area | Package | Spec |
| :--- | :--- | :--- |
| Voice loop, wake word, modes | `src/claire/listening/` | `listening.spec.md` |
| Speech recognition engines | `src/claire/listening/stt/` | `stt.spec.md` |
| Reply generation, planner | `src/claire/reply/` | `reply.spec.md`, `planner.spec.md` |
| Model gateway | `src/claire/llm/` | `llm.spec.md` |
| Speech synthesis | `src/claire/output/` | `tts.spec.md` |
| Memory, diary, graph | `src/claire/memory/` | `db.spec.md`, `graph.spec.md`, `router.spec.md` |
| Senses (screen, camera, browser) | `src/claire/senses/` | `screen.spec.md`, `camera.spec.md`, `browser.spec.md` |
| Tools | `src/claire/tools/` | see `builtin/*.spec.md` |
| Phone bridge | `src/claire/phone/` | `bridge.spec.md` |
| Web panel | `src/claire/panel/` | `panel.spec.md` |
| Calendar | `src/claire/schedule/` | `schedule.spec.md` |
| Secrets, backup, self-check | `src/claire/platform/` | `keys.spec.md`, `backup.spec.md`, `service.spec.md` |
| Desktop tray app (separate package) | `src/desktop_app/` | `desktop_app.spec.md` and siblings |

---

## Screenshots

The desktop tray app, from first-run setup through to the logs and settings
panes she is configured with.

<p align="center">
  <img src="docs/img/setup-provider.png" alt="Current setup wizard with separate cards for Ollama and an OpenAI-compatible server, the card used for OmniRouter" width="900">
</p>

<p align="center">
  <img src="docs/img/settings-window.png" alt="Claire Settings with a category sidebar and speech recognition controls" width="900">
</p>

<p align="center">
  <img src="docs/img/logs.png" alt="Claire Logs showing a speech model download at 48 percent, transferred bytes, speed, remaining time and a yellow optional-location warning" width="900">
</p>

<p align="center">
  <img src="docs/img/chat-window.png" alt="Claire companion chat in its rounded graphite phone-style window, showing illustrative messages and an amber composer" width="480">
</p>

The Android companion talks to the same [phone bridge](#the-phone-bridge) the
desktop app uses.

---

## Voice interaction

### Wake word

Claire is woken by her name appearing in the live transcript, not by an acoustic
wake-word model. Detection is text-based and matches whole words:

- `wake_word` (default `"claire"`).
- `wake_aliases` — the mishearings a recogniser actually produces:
  `clair`, `clare`, `claira`, `claya`, `klair`, `klare`, `claires`.
- `wake_fuzzy_ratio` (default `0.82`) — the similarity floor for the approximate
  match, deliberately tight so ordinary words do not wake her.

Waking sets the face to a listening state and starts the thinking tune
immediately, so there is audible and visual feedback before the answer exists.

### Listening modes

The listener is **transcript-first**: speech is transcribed continuously and an
LLM **intent judge** decides whether the last utterance was directed at Claire.
Three modes are defined in `listening.spec.md`:

1. **Wake word mode** (default) — waiting for the wake word.
2. **Hot window mode** — after Claire finishes speaking, a wake-word-free
   follow-up is allowed for `hot_window_seconds` (default `3.0`) once the echo
   has settled. At most **one** consecutive follow-up per explicit wake word is
   permitted; a second one is refused with `Follow-up window closed — say
   "Claire" to continue`.
3. **During TTS** — only a stop command is expected, with a shorter utterance
   timeout.

The intent judge receives the **transcript buffer** (default
`transcript_buffer_duration_sec`: 120 seconds) and synthesises a complete query
from it, so vague references ("what do you think about that?") are resolved
against what was actually being discussed. A multi-layer echo detector prevents
Claire's own speech from waking or re-triggering her.

### Speech recognition

There are **two recognisers with different jobs** (`stt.spec.md`, decision D4):

| | Streaming | Authoritative |
| :--- | :--- | :--- |
| Engine | sherpa-onnx zipformer transducer | Parakeet v2 |
| Runs | always on, frame by frame | once per turn, on the clipped utterance |
| Output | **provisional** | **authoritative** |
| Used for | wake-word matching, pre-wake context | intent, tools, reply, diary, graph |
| May write memory? | never | yes |

The two texts are kept apart by `TurnTranscript`, which refuses provisional
input to anything that feeds memory or a tool call. A Parakeet failure is
reported as a named, visible degradation — Whisper is never used as a fallback.
`faster-whisper` remains available as an **explicitly opt-in** engine
(`stt_engine: "faster_whisper"`), and swapping in a finetuned Parakeet is a
change to `stt_model_path` alone.

Relevant configuration: `stt_engine`, `stt_model_path`, `stt_model_kind`,
`stt_device`, `streaming_stt_engine`, `streaming_stt_model_dir`, and the
`whisper_*` keys used on the opt-in path. Voice-activity settings
(`vad_enabled`, `endpoint_silence_ms`, `max_utterance_ms`,
`tts_max_utterance_ms`) and capture settings (`voice_device`, `sample_rate`,
`voice_min_energy`) live here too.

### Speech synthesis

Three engines, selected by `tts_engine`:

- **`fish` (default)** — Fish Audio, with a **cloned voice**
  (`fish_voice_id`). The engine chops the reply into sentences so synthesis of
  the next sentence overlaps playback of the current one, streams audio through
  a bounded queue, and supports **barge-in**: `flush()` drops queued audio and
  aborts playback immediately.
- **`piper`** — fully local ONNX voice (default `en_GB-alan-medium`, downloaded
  from the public Piper voice repository with bounded retries and atomic
  publication).
- **`chatterbox`** — local model weights with optional voice cloning.

An unknown engine name fails loudly rather than silently becoming Piper. If TTS
cannot load, the daemon reports the reason, disables speech output for that
engine instance and keeps listening and text chat running.

Fish keys: `fish_voice_id`, `fish_key_file`, `fish_latency` (`"low"` or
`"normal"`), `fish_model`. Piper keys: `tts_piper_model_path`,
`tts_piper_speaker`, `tts_piper_length_scale`, `tts_piper_sentence_silence`.

### Interruption and presence

While a query is being processed, a short **thinking tune** plays; it stops when
the reply starts. The desktop app shows an animated face whose state follows the
loop (asleep, idle, listening, thinking, speaking), and `stop` stops her
speaking and cancels the turn.

---

## The reply pipeline and tools

### The pipeline

`src/claire/reply/engine.py` orchestrates one reply. The shape of the flow is:

1. **Redact** the input before any model sees it.
2. **Recent dialogue context** — the last five minutes of dialogue memory
   (`DialogueMemory`, 300-second window), including tool-call and tool-result
   messages carried over from prior replies so a follow-up can reuse a result
   instead of re-fetching it.
3. **Tool routing** — `select_tools` narrows the catalogue to what the turn
   needs. `tool_selection_strategy` is one of `"all"`, `"keyword"`,
   `"embedding"`, or `"llm"` (default `"llm"`). `stop` is always available.
4. **Planning** — a single cheap pass at the front of the flow emits a short
   ordered task list. It gates memory enrichment, confirms the tool allow-list,
   and for small models drives **direct execution**: each planned step is
   resolved to a concrete tool call without round-tripping the chat model.
5. **Memory enrichment** — keyword/time-bounded diary and knowledge-graph
   recall. A deterministic, no-LLM **recall gate** skips this when the hot
   window already grounds the follow-up.
6. **Agentic messages loop** — up to `agentic_max_turns` (default `8`) turns.
   The model can call tools; results are digested for small models rather than
   pasted whole. Fresh contextual information (date, time, location) is injected
   at the tail of the system message and memoised so the prompt head stays
   byte-identical across calls, which lets the server's prefix cache do its job.
7. **Output and memory update** — the answer is printed and (optionally) spoken,
   and dialogue memory is updated.

The persona is a British butler with dry wit and light, good-natured sarcasm,
with concrete rules: answer first, quip second, at most one quip, no quips on
serious topics, sarcasm never aimed at the user. The wake word is also the
assistant's name in the prompt, so renaming the wake word renames her.

### Model routing

`src/claire/llm/` owns **every** LLM HTTP call in the codebase. Call sites use
the factory (`get_llm_backend(cfg)` / `get_embedding_backend(cfg)`) and never
speak HTTP directly, so the provider is a configuration choice:

- `llm_provider: "openai_compatible"` (default) with `llm_base_url` pointing at
  OmniRouter. `llm_api_key` stays empty; the key is read from
  `OMNIROUTE_API_KEY` at runtime.
- `llm_provider: "ollama"` for a fully local Ollama server, defaulting to
  `http://127.0.0.1:11434`.

Two tiers are resolved from settings: the **chat model** (`llm_chat_model`) and
a **fast model** (`fast_model`) used for real-time classification-shaped work —
the intent judge, tool routing, planner, digests and the weather place
extractor. On an OpenAI-compatible provider an unset `fast_model` resolves to the
active chat model rather than to an Ollama pull name. Separate timeouts exist
for chat (`llm_chat_timeout_sec`), tool calls (`llm_tools_timeout_sec`), cheap
digests (`llm_digest_timeout_sec`), embeddings (`llm_embedding_timeout_sec`) and
profile selection (`llm_profile_select_timeout_sec`).

`config.py` also ships a list of supported Ollama chat models
(`gemma4:e2b`, `gemma4:e4b`, `qwen3.8:27b`, `qwen3.5:0.8b`) for the offline path.

### Tools

Built-in tools are registered in `src/claire/tools/registry.py` and documented by
specs beside their code:

| Tool | What it does |
| :--- | :--- |
| `webSearch` | DuckDuckGo search: instant answer, top links, then a parallel cascade fetch of the top three results with a relevance filter |
| `fetchWebPage` | Fetch a URL and extract readable text (2 MiB body cap, script/style stripped, optional labelled links) |
| `localFiles` | `list`, `read`, `write`, `append`, `delete` under the home directory only |
| `readScreen` | Read the words off the screen (OCR) |
| `lookThroughCamera` | Take one picture and ask a vision model what is in it |
| `browseWeb` | Real browser: open a page, read it, find a phrase, click, type, screenshot, back, close |
| `schedule` | `add`, `list`, `move`, `cancel`, `due` against Claire's own calendar |
| `getWeather` | Current weather, with place extraction through the fast tier when no coordinates are known |
| `logMeal`, `fetchMeals`, `deleteMeal` | Nutrition log: estimate macros for a described meal, list or delete entries |
| `getTime` | Current date and time |
| `screenshot` | Native screenshot capture |
| `toolSearchTool` | Escape hatch that widens the tool allow-list mid-conversation by re-running the router |
| `refreshMCPTools` | Re-discover tools from configured MCP servers |
| `stop` | Stop speaking and cancel the current turn |

There is no media or music playback control: the only audio Claire plays herself
is speech synthesis and the thinking tune.

### MCP servers

External **MCP** servers can be configured under the `mcps` key and are launched
as stdio subprocesses. Each server keeps **one persistent session** for the
daemon's lifetime (so stateful servers such as a browser-automation server keep
their browser alive), with optional `idle_timeout_sec` and `timeout_sec` per
entry. Tool names are namespaced by server. The daemon discovers and caches them
at startup and prints a per-server count. Worked configuration examples (Home
Assistant, Google Workspace, GitHub and others) are in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md#mcp-integrations).

---

## Memory

Claire has four distinct memory stores, and they answer different questions.

### Dialogue memory

The short-term window: recent user/assistant turns plus tool messages from prior
replies, kept for 300 seconds by default and handed to the reply engine as prior
messages. It is the reason a follow-up can refer to "that" and be understood.

### The diary

`claire.memory.db` — SQLite. Raw conversation chunks are condensed into daily
`conversation_summaries` rows by the **diary summariser**, which runs as
delay-tolerant background work (checked every 60 s, and skipped rather than
serialised behind a user-visible reply). The summariser prompt carries
cumulative hygiene rules because a corrupted summary poisons every downstream
consumer; there is no post-process scrub, so the prompt is the single write-time
defence. A user-triggered LLM rewrite sweep can clean older entries.

Retrieval is **hybrid**: FTS5 keyword search and vector candidates are fused
with weighted reciprocal rank fusion (`0.6` vector, `0.4` keyword). Raw BM25
values and vector distances are never compared across sources.

### The knowledge graph

`claire.memory.graph` records *what was learned* (the diary records *what
happened*). It is a self-organising node graph that seeds three non-deletable
top-level branches — **User**, **Directives** and **World** — and auto-splits
nodes as they grow. A **warm profile** keeps the most relevant branches cheaply
reachable and is invalidated when the graph mutates.

### The router and notes

`claire.memory.router` owns the always-loaded memory file `<vault>/CLAIRE.md`:
one line per fact plus pointers into `notes/`. It is a router, not a database —
anything larger than a line belongs in a note. The file is re-read on every
request, honouring a byte budget (default **6000** bytes); the web panel shows
its size against that budget and repeats the over-budget warning verbatim, and
the acceptance scoreboard measures it. The owner's guide is
[docs/MEMORY.md](docs/MEMORY.md).

---

## Senses

### Screen

`claire.senses.screen` captures the screen to a PNG and reads text off it with
**pytesseract** (OCR, no LLM, no network). It is Linux-only and deliberately
verbose about absence: a missing OCR engine raises `OcrEngineMissingError` with
the install command, rather than returning an empty string that would be
indistinguishable from "the screen has no words on it". The tool wrapper
(`readScreen`) converts that verdict into one clean spoken sentence plus the
fix.

### Camera and vision

`claire.senses.camera` can report whether a camera exists, take exactly one
frame, and ask a vision model what is in it. Vision rides the project's existing
`claire.llm` route (OmniRouter), so there is no second HTTP client, base URL or
model selection. It is honest about absence: a laptop with no `/dev/video*` is a
capability verdict, not an error. `seen` is true only with a non-empty
description; every other outcome is a named *not seen* result, because a
confident description of an image that was never captured is the worst failure
the module could produce.

The phone bridge supplies the frames this laptop cannot (`POST /see`), and the
web panel and `lookThroughCamera` tool both call the same seam.

### Browser

`claire.senses.browser` drives a **real Chromium via Playwright** — real
rendering, real clicks, real form filling, real screenshots — exposed as the
`browseWeb` tool. The browser is process-wide state: exactly one Chromium, one
profile directory and one operation ordering, created lazily and once, because
Chromium takes an exclusive `SingletonLock` and a second browser on that profile
can exit having done nothing at all (which is indistinguishable from success if
nobody checks).

---

## Scheduling, weather and location

### Calendar

`claire.schedule` is a **local-first calendar** with no timezone library and no
LLM in the logic. Dates are `YYYY-MM-DD`; times are whole minutes from local
midnight; the spoken form is produced by `format_spoken` ("Thursday 24 September
at 16:00 to 17:00").

The `schedule` tool exposes five actions — `add`, `list`, `move`, `cancel` and
`due` — and resolves every date and time expression itself ("tomorrow at 4pm",
"next week") rather than trusting the model with arithmetic. Guarantees that
matter in practice:

- **Overlap is half-open**: an event ending 15:00 and one starting 15:00 do not
  collide.
- A move excludes the event being moved from its own conflict check.
- A clash returns ranked alternatives, and a free request returns the requested
  slot as `proposals[0]` — write if equal, refuse and offer alternatives if not.

Events may recur (`daily` or `weekly`, with an `until` date) and each occurrence
can be cancelled individually. Reminders attached to events are read on request
through the `due` action; there is no background announcer that speaks them
unprompted. The schedule is a versioned JSON file written atomically with
rotating backups.

### Weather

The `getWeather` tool answers with current conditions. When no coordinates are
known it extracts a place from the (redacted) utterance using the fast-tier
model, bounded to a short generation, and geocodes it. If extraction fails it
asks for a city rather than inventing coordinates.

### Location

`claire.utils.location` resolves the user's location from an IP address using a
**local MaxMind GeoLite2 database**. Keys: `location_enabled`,
`location_auto_detect`, `location_ip_address`,
`location_cgnat_resolve_public_ip` and `location_cache_minutes` (default 60).
Auto-detection may query the local router over UPnP; the only external query is
a single opt-in OpenDNS DNS lookup for CGNAT addresses. Without the database the
daemon prints a note and location features stay off.

---

## The phone bridge

`src/claire/phone/bridge.py` is the HTTP surface the phone talks to. Its job is
the three things a laptop cannot supply: the owner's voice when they are away
from the desk, a camera, and a position. It uses the standard library only — no
framework, no TLS, no database.

| Endpoint | Method | Body | Answer |
| :--- | :--- | :--- | :--- |
| `/text` | POST | `{"text": "..."}` | `202` accepted |
| `/say` | POST | `{"text":"..."}` or `{"command":"photo"\|"location"}` | `200` queued |
| `/see` | POST | raw `image/jpeg`/`image/png`/`image/webp` (question via `?question=` or `X-Claire-Question`), or JSON `{"image_base64":...,"question":...}` | `200` with a description, or a named not-seen failure |
| `/loc` | POST | `{"latitude":..,"longitude":..,"accuracy":..}` | `200` recorded |
| `/transcribe` | POST | raw `audio/wav` | `200` with the recognised text |
| `/reply` | GET | — | `200` queued items, **drained once** |
| `/health` | GET | — | reachability, capability verdict, **masked** token handle |

`/location` is an alias of `/loc` and `/where` is an alias of `/reply`. Every
endpoint is authenticated with a bearer token, **including `/health`** — there is
no unauthenticated surface at all. Failures are named with a fix:
`unauthorized`, `body_too_large` (8 MiB), `unsupported_content_type`,
`invalid_payload`, `invalid_audio`, `transcription_unavailable`,
`transcription_failed`, `sink_unconfigured`, `vision_not_configured` and so on.

- **Default bind**: `127.0.0.1:8788` (`DEFAULT_HOST` / `DEFAULT_PORT`).
- **Loopback only.** Binding to a non-loopback address requires an explicit
  opt-in and prints a loud warning that everything the phone sends — utterances,
  photos, location, and the token itself — would travel as unencrypted plain
  HTTP. A tunnel is the supported way to reach it from outside the machine.
- **Started by the daemon** after the listener, so a phone utterance reaches the
  same conversation as speech, with the *same* recogniser instance (two NeMo
  instances on one GPU fault). Opt out with `CLAIRE_PHONE_BRIDGE=0`. Environment:
  `CLAIRE_PHONE_HOST`, `CLAIRE_PHONE_PORT`, `CLAIRE_PHONE_ALLOW_PUBLIC`,
  `CLAIRE_PHONE_PHOTO_DIR`.
- **Photos** are written `0600` into a `0700` directory
  (`$CLAIRE_PHONE_PHOTO_DIR`, else `$XDG_CACHE_HOME/claire/phone`, else
  `~/.cache/claire/phone`); the path is always reported even when description
  fails.

The token lives in the key store under `PHONE_BRIDGE_TOKEN`. Manage it with:

```bash
python -m claire.phone serve                 # bind and serve (loopback)
python -m claire.phone keys set PHONE_BRIDGE_TOKEN    # mint or rotate
python -m claire.phone keys list             # masked: name, last-4, fingerprint
python -m claire.phone keys show PHONE_BRIDGE_TOKEN --reveal   # the one time the phone needs it
```

Every command except `--reveal` reports the token masked; `--reveal` exists only
because the raw value has to be typed into the phone once.

---

## The web panel

`python -m claire.panel.server [--host H] [--port N]` serves a small,
**loopback-only**, server-rendered page at `http://127.0.0.1:8765/` — the
standard library only, no JavaScript, no build step. It reads the things that
are otherwise buried in config, and shows:

| Section | Shows |
| :--- | :--- |
| Conversation | the turns sent through this page, newest first, labelled you/Claire/panel, plus the persisted daily summaries read-only |
| Chat with Claire | a text box that sends one message through the same LLM stack the daemon uses, and shows the reply |
| Provider and model | selected provider, resolved model, endpoint, plus the selector form |
| Providers and keys | one row per known credential, **masked** (last-4 + fingerprint) or `absent`, with its source |
| Memory router | `CLAIRE.md` path, size against its budget, and the verbatim over-budget warning |
| Today's calendar | today's events, formatted by the calendar's own `format_spoken` |
| Self-check | the summary line, a link to the full report, and every non-READY check with its fix |
| Last actions | the last eight action-journal records |

The panel owns none of that state — it reads `claire.config`,
`claire.platform.keys`, `claire.memory.router`, `claire.schedule`,
`claire.platform.service` and the database, and never edits them.

---

## The desktop app

`src/desktop_app/` is a **separate PyQt6 package**: `claire` has no knowledge of
it, so the assistant runs headless without it. It provides a system-tray
application with:

- an animated **face widget** whose state follows the voice loop;
- a **chat window** for text — the same shared conversation as voice, but text
  replies are silent;
- an **auto-generated settings window** built from config metadata (only
  non-default values are written, unknown keys are preserved);
- a **first-run setup wizard** that doesn't configure everything, only what
  needs a user decision (provider, models, recogniser, location);
- a **memory viewer** (Flask) for diary entries, knowledge-graph nodes and meals;
- an end-of-session **diary dialog** and an **update checker**.

Run it with `bash scripts/run_desktop_app.sh` (add `--voice-debug` for verbose
voice logging). `claire.spec` builds the app with PyInstaller.

---

## The Android companion

`android/` is a Gradle project for a companion app
(`com.claire.companion`, `versionName 0.4.0`). It is not just a remote control:
it is an always-on foreground service declaring three types
(`microphone|location|camera`) that keeps the microphone open, segments the
owner's sentences itself, sends each one to `POST /transcribe`, keeps the
position moving when asked, and takes a photo when Claire asks through the
bridge. It talks to `/transcribe`, `/see`, `/loc`, `/text`, `/health` and
`/reply`. Build the debug APK with `./gradlew assembleDebug`.

---

## What leaves the machine

| Capability | Service | Where | Auth |
| :--- | :--- | :--- | :--- |
| Language model — chat, routing, planning, digests, intent judge | OmniRouter | `http://127.0.0.1:20129/v1` | `OMNIROUTE_API_KEY` env var |
| Vision — screenshots, camera frames, phone photos | OmniRouter (multimodal) | the same endpoint | `OMNIROUTE_API_KEY` env var |
| Speech synthesis | Fish Audio (cloned voice) | fish-audio SDK | key file (`fish_key_file`) |
| Web search, page fetch, weather, MCP servers | whichever service those tools use | requested only when used | per tool |

Everything else — speech recognition, the diary, the knowledge graph, hybrid
retrieval, the calendar, tool execution, the phone bridge and the web panel —
stays on the machine. Conversation text is redacted before it reaches a model,
and the always-loaded memory router is re-read per request.
[docs/CLOUD_POLICY.md](docs/CLOUD_POLICY.md) is the authoritative statement of
what may leave the machine.

---

## Configuration

### Where things live

| What | Path | Override |
| :--- | :--- | :--- |
| Config | `~/.config/claire/config.json` | `CLAIRE_CONFIG_PATH`, `$XDG_CONFIG_HOME` |
| Runtime secrets (env file, `0600`) | `~/.config/claire/claire.env` | `CLAIRE_ENV_FILE` |
| Key store (`0600` JSON) | `~/.config/claire/keys.json` | `CLAIRE_KEY_STORE` |
| Memory database | `~/.local/share/claire/claire.db` | `db_path` |
| Snapshots | `~/.local/share/claire/snapshots` | `CLAIRE_BACKUP_ROOT` |

The real configuration surface is `get_default_config()` in
`src/claire/config.py`; the desktop settings window is generated from that
metadata, and [docs/CONFIGURATION.md](docs/CONFIGURATION.md) documents the same
keys by hand. The knobs most worth knowing:

- **Model route**: `llm_provider`, `llm_base_url`, `llm_api_key`,
  `llm_chat_model`, `fast_model`, `embedding_provider`, `embedding_model`, and
  the `*_timeout_sec` family. The `ollama_*` keys remain readable for older
  config files and are promoted into the provider-aware keys on load.
- **Voice**: `wake_word`, `wake_aliases`, `wake_fuzzy_ratio`, `stt_engine`,
  `stt_model_path`, `streaming_stt_engine`, `tts_engine` and the per-engine voice
  keys, `vad_enabled`, `endpoint_silence_ms`, `max_utterance_ms`,
  `hot_window_enabled`, `hot_window_seconds`, `echo_tolerance`.
- **Reply behaviour**: `tool_selection_strategy`, `agentic_max_turns`,
  `tool_carryover_max_turns`, `low_power_mode`,
  `transcript_buffer_duration_sec`.
- **Location**: `location_enabled`, `location_auto_detect`,
  `location_ip_address`, `location_cache_minutes`.
- **MCP**: `mcps` (empty by default).

### Secrets

Secrets never live in the repository or in `config.json`, never get printed, and
are never world-readable:

- **Language model and vision** — `OMNIROUTE_API_KEY` comes from the
  environment. In practice it is placed in the `0600` runtime env file
  (`~/.config/claire/claire.env`), which the systemd unit loads with
  `EnvironmentFile=`; a real environment variable always outranks the file. The
  `llm_api_key` config field stays empty.
- **Key store** — `claire.platform.keys` writes `keys.json` from a
  `tempfile.mkstemp` handle (created `0600` by the kernel) and `os.replace`s it
  over the target, so there is no window in which a group- or world-readable file
  holds a key; the final mode is re-checked and a write fails loudly if any
  group/other bit survived. `keyring` (OS keychain) is a runtime dependency for
  the migration away from plaintext values.
- **Fish Audio** — `fish_key_file` points at a `0600` key file outside the repo.
- **Reporting** — the key store, the web panel and the phone bridge expose at
  most the last four characters plus a fingerprint. The only way this project
  ever shows a raw secret is `python -m claire.phone keys show NAME --reveal`,
  which exists because the phone must be given the token once.
- `.gitignore` refuses `*.env`, `*keys*.txt` and `*token*` files.

---

## Installing and running

### Requirements

- 64-bit Linux (this fork is Linux-only; the Windows and macOS branches are
  gone).
- **Python 3.11 or newer** (`requires-python = ">=3.11"`; the classifiers name
  3.11 and 3.12).
- A microphone and speakers for voice; a reachable OmniRouter for the language
  model; a Fish Audio voice for the default cloud speech.
- GPU is optional. NeMo/Parakeet tooling is deliberately not installed by
  default (it pulls a large CUDA stack); the ONNX Parakeet path uses
  `onnxruntime`.

### From source

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# check the entry point
PYTHONPATH=src python -m claire --version
PYTHONPATH=src python -m claire --help

# start her
./scripts/run_linux.sh
```

`scripts/run_linux.sh` creates `.venv` when missing, installs
`requirements.txt`, exports `PYTHONPATH=src` and runs `python -m claire`, which
hands off to the daemon. The console script `claire` (from `pyproject.toml`) and
`python -m claire` both land in `claire.cli`, which parses `--version` and
`--smoke-test` and calls the daemon.

`pip install -r requirements.txt` installs the dependencies but not Claire
herself, so `src/` must be on `PYTHONPATH` — the run script exports it.
`pip install -e .` needs `src/claire/_version.py`, which is generated at build
time and is **not tracked**, so a fresh checkout may not have it; running from
`src/` with `PYTHONPATH` is the supported path.

Available flags:

```text
claire --version        # print the Claire version and exit
claire --smoke-test     # start the daemon, verify core subsystems, then exit
```

### As a systemd user service

`deploy/claire.service` is a **user** unit, not a system unit, and that is
deliberate: Claire needs the microphone, the speakers and (for the screen
senses) the graphical session, all of which belong to a human login session. A
system unit starts before any session exists and has no audio server or display,
so the failure surfaces as "no microphone" and looks like a Claire bug. Install:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/claire.service ~/.config/systemd/user/claire.service
systemctl --user daemon-reload
systemctl --user enable --now claire
journalctl --user -u claire -f
```

Consequence, stated honestly: Claire runs while that user is logged in.
`loginctl enable-linger` keeps the process alive without a login but the audio
and display parts are unavailable by definition in that case.

### In Docker

`deploy/Dockerfile` builds the **text/tool half only**. A container has no audio
server and no display, so voice input, the wake word, TTS playback, screenshots
and on-screen automation do not work there; chat, memory, scheduling, web fetch,
headless browsing and OCR over an image you hand it do. No key is baked into the
image — secrets arrive at run time via `--env-file` or a secrets mount. Run the
daemon on the host under the systemd unit for voice.

---

## Health and diagnostics

- **Self-check** — `python -m claire.platform.service --self-check` (add
  `--json` for machine-readable output). Every probe runs to completion: a probe
  that crashes is reported as a *problem* with the exception named, never as a
  traceback from the diagnostic tool itself. `READY` and `MISSING` are kept
  distinct, and every non-READY check carries a fix.
- **Acceptance scoreboard** — `python scripts/acceptance_check.py` measures the
  install capability by capability (config, vault, key store, audio device, STT
  model, LLM endpoint, memory router, calendar, timers, screen capture, OCR,
  camera, browser, outreach ladder, backups, self-check) and records honestly
  what is unmeasurable on a given machine.
- **Logs** — `debug_log` (`src/claire/debug.py`) writes the important points in
  each flow with a topic tag, kept deliberately readable; `CLAIRE_VOICE_DEBUG=1`
  turns on verbose voice logging.
- **Backups** — `claire.platform.backup` takes rotating, verified snapshots:
  `snapshot()`, `verify()` (re-hashes against the manifest rather than trusting
  it) and `restore()`.

---

## Development

### Tests

```bash
# unit and integration tests (performance tests are excluded by default)
pytest

# a single file
pytest tests/test_phone_bridge.py -v

# just the fast unit tier
pytest -m unit
```

`pytest.ini` sets `testpaths = tests` and `addopts = -m "not performance"`. The
markers are `unit`, `integration`, `e2e`, `eval` and `performance`. The suite is
the best record of pinned behaviour — it covers the listener, the reply engine
and planner, the phone bridge, every sense, the calendar rules, the memory
stores, the panel, the daemon's TTS wiring and more. `tests/performance/` holds
the timing harness against a live model, and is excluded unless asked for.

### Evals

`evals/` holds quality evaluations (intent judge, hybrid retrieval, memory
digest, tool digest and others) that need a live model. They are excluded from
the default run; `scripts/run_evals.sh` drives them and can generate `EVALS.md`.
`scripts/measure_voice_latency.py` measures voice latency, and
`scripts/check_stt_model.py` verifies the configured recogniser model.

### Project layout

```text
src/claire/           the assistant package (daemon, config, all subsystems)
src/desktop_app/      the PyQt6 tray app — separate, depends on claire, not vice versa
android/              the companion phone app (Gradle)
deploy/               systemd user unit and the Dockerfile
docs/                 configuration, cloud policy, memory and acceptance guides
evals/                live-model quality evaluations
examples/             example configuration
scripts/              run, acceptance, latency, eval and screenshot helpers
tests/                the pytest suite
*.spec.md             per-subsystem specifications, next to the code they describe
```

### House conventions

- **Specs are authoritative.** Each subsystem's behaviour is described by a
  `*.spec.md` file next to its code; a behaviour change that contradicts a spec
  must update the spec.
- **User-facing CLI output uses emoji and indentation** to make it easy to
  sift through.
- **Never claim an action that was not performed.** If a transport cannot send,
  or a call cannot be placed, the honest failure is reported instead of success.

### Present as libraries, not yet wired into the assistant

Two modules are implemented and tested but have no daemon integration, so
Claire does not currently use them unprompted:

- `src/claire/outreach/` — the urgency ladder (low/medium/high/critical) plus
  **Signal** and **Discord** transports, with three never-collapsed outcomes
  (sent / rehearsed / failed). Nothing outside the package imports it.
- `src/claire/schedule/timers.py` — an in-memory `TimerManager` (start, cancel,
  list, fire due, remaining) with spoken formatting. It is exercised by the
  acceptance scoreboard but is not exposed as a tool.

---

## Attribution and licence

Claire is a fork of **jarvis** by Baris Sencan
([`isair/jarvis`](https://github.com/isair/jarvis)), forked at commit
`94c54491` on the `claire-rewrite` branch. The working tree, test suite, specs,
memory system, tool architecture, MCP runtime and desktop app were inherited and
then renamed and extended. Full details are in
[ATTRIBUTION.md](ATTRIBUTION.md).

Claire is distributed under the **same licence as upstream**, kept verbatim in
[LICENSE](LICENSE): non-commercial use only, derivative works inherit the terms.
This is a private personal/research build; it is not sold, hosted or offered as
a service.
