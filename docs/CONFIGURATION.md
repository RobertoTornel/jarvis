# Configuration guide

[← Back to Claire](../README.md)

Detailed settings, integrations and troubleshooting. Start with the desktop Settings window; JSON examples are for custom setups. Cloud services and what leaves the machine are covered in [CLOUD_POLICY.md](CLOUD_POLICY.md).

## Configuration

Most users won't need to change anything. Open **⚙️ Settings** from the tray menu to configure Claire through a graphical interface: no JSON editing required. Settings are saved to `~/.config/claire/config.json`.

Claire also keeps state in your own data directory, outside both the settings file and the repository:

| What | Default | Move it with |
|---|---|---|
| Memory vault — `CLAIRE.md` and `notes/` | `~/.local/share/claire/vault` | `CLAIRE_VAULT_DIR`, or the `vault_dir` key in `config.json` |
| Conversation database — diary, topics, recall | `~/.local/share/claire/claire.db` | the `db_path` key |

The memory vault is a plain, Obsidian-compatible markdown folder: point Obsidian at it and read Claire's memory as ordinary notes. [OBSIDIAN.md](OBSIDIAN.md) covers the layout and the conventions; [MEMORY.md](MEMORY.md) covers what the router and the notes mean.

<details>
<summary><strong>LLM Provider (Ollama or OpenAI-compatible)</strong></summary>

Claire's default language route is **OmniRouter**: an OpenAI-compatible endpoint at `http://127.0.0.1:20129/v1`, selected with provider `omniroute`, authenticated with the `OMNIROUTE_API_KEY` environment variable. Conversation text and images go to that endpoint; the details are in [CLOUD_POLICY.md](CLOUD_POLICY.md). The offline alternative is [Ollama](https://ollama.com), which runs entirely on this machine. If you run any other OpenAI-compatible server you can point Claire at that instead.

Pick the provider after speech recognition in the Setup Wizard, or under **⚙️ Settings → 🔌 LLM Provider**. No JSON editing required. On the OpenAI-compatible page the wizard does the legwork for you: it auto-detects running local servers, offers a one-click preset for your app, and when you press **Connect** it loads the server's model list and checks the chosen model for chat, tool calling, and embeddings, so you know it works before you finish setup.

Both provider paths include a model-memory budget. Ollama uses its known model estimates; the OpenAI-compatible page lets you edit estimates because servers usually expose model names but not their memory needs. A server on another machine has a separate budget from the speech recogniser on this machine. Estimates are guidance, not a guarantee: quantisation, context length and concurrent workloads affect actual use.

Tested OpenAI-compatible servers:

| App | Default base URL | Notes |
|-----|------------------|-------|
| OmniRouter (default) | `http://127.0.0.1:20129/v1` | Claire's configured route; provider `omniroute`, key from `OMNIROUTE_API_KEY`. |
| LM Studio | `http://localhost:1234/v1` | Chat, tool calling, and embeddings. |
| Ollama (OpenAI API) | `http://localhost:11434/v1` | The native Ollama path is the offline default; the OpenAI shape works too. |
| Jan | `http://localhost:1337/v1` | Chat and tool calling. |
| llama.cpp (`llama-server`) | `http://localhost:8080/v1` | Tool calling depends on the model. |
| LocalAI | `http://localhost:8080/v1` | Feature support depends on the backend model. |
| vLLM | `http://localhost:8000/v1` | Tool calling depends on the model. |

For reference, the underlying config keys are:

```json
{
  "llm_provider": "openai_compatible",
  "llm_base_url": "http://127.0.0.1:20129/v1",
  "llm_api_key": "",
  "llm_chat_model": "your-served-model-name"
}
```

- `llm_base_url`: the endpoint's OpenAI API base URL. The default route is OmniRouter at `http://127.0.0.1:20129/v1`.
- `llm_api_key`: leave empty. The key is read at runtime from the `OMNIROUTE_API_KEY` environment variable (or the `fish_key_file` for Fish Audio) — never put a key value in `config.json`.
- `llm_chat_model`: whatever model name the endpoint exposes.
- `fast_model` (optional): the small, quick model used for real-time work (voice intent, tool routing, quick classifications). Leave empty for automatic: `gemma4:e2b` on Ollama, your chat model on an OpenAI-compatible endpoint. Set it to pin a dedicated small model.

**Conversation history bound** (what a turn sends, not what is stored). The wire
payload is capped so a long session cannot grow the prompt without bound
(measured: 133,893 input tokens at 324 messages before the cap). Nothing is
evicted from storage; only what is *sent* is bounded, oldest-first in whole
user-led exchanges so tool results are never orphaned from their parents:

```json
{
  "dialogue_memory_timeout": 300.0,
  "dialogue_memory_max_tokens": 8000,
  "dialogue_memory_max_messages": 40
}
```

**Embeddings** (used for memory search) can run on a different backend from chat. `embedding_provider` accepts `""` (inherit the chat provider), `"ollama"`, `"openai_compatible"` or `"gemini"`.

Route embeddings to Ollama to keep full semantic memory when your chat server has no embeddings endpoint (the wizard offers this automatically when it detects a server that cannot embed):

```json
{
  "embedding_provider": "ollama",
  "embedding_model": "nomic-embed-text"
}
```

Route them to Google's Gemini embedding models with `"gemini"`:

```json
{
  "embedding_provider": "gemini",
  "embedding_model": "gemini-embedding-2"
}
```

| Key | Default | Meaning |
| :--- | :--- | :--- |
| `embedding_provider` | `""` | `""` = same as `llm_provider`; or `"ollama"`, `"openai_compatible"`, `"gemini"` (alias `"google"`) |
| `embedding_model` | provider-aware | `ollama_embed_model` on the Ollama/OpenAI-compatible paths; `gemini-embedding-2` on the Gemini path. Set it explicitly to pick `gemini-embedding-001` instead |
| `embedding_base_url` | provider default | Gemini defaults to `https://generativelanguage.googleapis.com/v1beta` |
| `embedding_api_key` | `""` | On the Gemini path an empty value is read from `GEMINI_API_KEY`, then `GOOGLE_API_KEY`. The chat key (`llm_api_key` / `OMNIROUTE_API_KEY`) is never sent to Google |

Notes on embeddings:

- **Gemini uses the native API**, not the OpenAI-compatible shim, so the two sides of a retrieval can carry different task types (`RETRIEVAL_DOCUMENT` when a memory is indexed, `RETRIEVAL_QUERY` when it is searched). Indexing many memories at once uses `batchEmbedContents` instead of one request per memory.
- **The model is a config value, and the two Gemini models are not interchangeable.** `gemini-embedding-2` is the default (newest; the owner's `gemini-embedding-002` does not exist). `gemini-embedding-001` is the text-only alternative. Google documents their embedding spaces as incompatible, so switching between them requires re-embedding existing memories.
- **The output length is pinned to the index, not configured.** Claire's vector index is 768 floats wide — `vss0(vec(768))` for sqlite-vss and a 768-dimension FAISS index otherwise — and the Gemini client requests exactly that `outputDimensionality`. A vector of any other length is discarded instead of written.
- Leave `embedding_provider` empty to use the same provider as chat. With no working embeddings — no key, no reachable service, a renamed model — memory search logs the failure and degrades to keyword search.

</details>

<details>
<summary><strong>Power and Startup</strong></summary>

Claire favours fast first responses by default: it warms the speech recogniser,
the chat model, and the intent judge before announcing that it is listening. On
laptops where heat and battery matter more than instant first-token latency,
enable **⚙️ Settings → ✨ Features → Low Power Mode**.

```json
{
  "low_power_mode": true
}
```

Low Power Mode skips LLM startup warmup and shortens Ollama model residency for
the intent judge from 30 minutes to 1 minute. The speech recogniser still warms so
voice input is ready. The first LLM-backed request after startup or idle may be
slower.

</details>

<details>
<summary><strong>Speech-to-Text Engines (Parakeet and the streaming zipformer)</strong></summary>

Claire runs **two recognisers with different jobs**. Which engines run is a
configuration choice, never a code change; the model itself is a path.

```json
{
  "stt_engine": "parakeet",
  "stt_model_path": "/path/to/parakeet.nemo",
  "stt_model_kind": "auto",
  "stt_device": "cpu",
  "streaming_stt_engine": "sherpa",
  "streaming_stt_model_dir": "~/.local/share/claire/models/sherpa-onnx-streaming-zipformer-en-20M",
  "streaming_stt_num_threads": 2
}
```

#### Parakeet - the authoritative recogniser

The text a turn acts on (intent, tool calls, the reply, memory) comes from the
authoritative engine, `stt_engine`, which is `"parakeet"` by default. **Swapping it
is a change of `stt_model_path` alone** - including dropping in your own finetuned
Parakeet. No code changes.

- `stt_model_path`: a `*.nemo` checkpoint, or a directory holding an ONNX export.
  `examples/config.json` ships it as `""`, which means **"not configured"**: an
  empty value raises a named error naming the path and the fix rather than
  producing an empty transcript that would look like "the user said nothing".
- `stt_model_kind`: `"auto"` (default; decides from the path), `"nemo"` or `"onnx"`.
- `stt_device`: `"cpu"` (default) or `"cuda"`. Enforced at load: `"cpu"`
  restores through `map_location` (NeMo) / `CPUExecutionProvider` (ONNX);
  `"cuda"` requests GPU providers and raises a named error when CUDA is not
  available. The value is also recorded in the engine's identity.
- Set `stt_engine` to `"faster_whisper"` (or `"whisper"`) only if you
  deliberately want Whisper as the recogniser instead; the `whisper_*` settings
  in the next section then apply. **Whisper is opt-in only and never a
  fallback:** with `"parakeet"` configured, `faster_whisper`/`mlx_whisper` are
  not imported, no Whisper model is downloaded or loaded, and no memory is held
  for one. Any other `stt_engine` value raises a named error instead of quietly
  selecting an engine. If Parakeet cannot answer, the turn is reported as a
  named, visible degradation (`⚠️ Voice transcript degraded: …`) — it is not
  silently re-decoded by a second recogniser.

#### Streaming zipformer - the always-on recogniser (optional)

With `streaming_stt_engine` set to `"sherpa"` (the default), a sherpa-onnx
zipformer transducer runs continuously on microphone audio. Its output is
**provisional by definition**: it is used for wake-word matching and pre-wake
context only, and must never become the text a turn acts on. Wake matching is the
one place a cheap, always-on partial transcript is worth having.

- `streaming_stt_model_dir`: the zipformer export directory. The streaming engine
  is optional, not a hard requirement: set `"streaming_stt_engine": ""` to disable
  it entirely, and wake detection then runs on whatever the authoritative path
  produces.
- `streaming_stt_num_threads`: CPU threads for the streaming decoder (default `2`).
- A missing model directory, a missing component file, or an unimportable
  `sherpa_onnx` package raises a named error (`SttModelMissingError` /
  `SttBackendUnavailableError`) that names the exact path and the fix. An unknown
  engine name raises as well. None of these degrade into an empty transcript: an
  empty string from the streaming engine means "this segment contained no speech",
  never "the model was missing".

</details>

<details>
<summary><strong>Speech Recognition (Whisper — opt-in only)</strong></summary>

These settings apply when `stt_engine` is `"faster_whisper"` (or `"whisper"`):
Whisper is then the explicitly configured recogniser. With the default
`"parakeet"` every key in this section is dead configuration — it is read for
nothing, and no Whisper backend is imported, downloaded or loaded.

The keys that go dead on the Parakeet path are `whisper_model`,
`whisper_backend`, `whisper_device`, `whisper_compute_type`, plus the
low-confidence policy keys `whisper_min_confidence`,
`whisper_no_speech_threshold`, `whisper_min_audio_duration` and `whisper_vad`.
They are kept in the schema so the opt-in path stays a configuration change,
not a code change.

#### Language Modes
- **Multilingual** (default, 99 languages): `"whisper_model": "medium"`
- **English Only** (slightly better English accuracy): `"whisper_model": "medium.en"`

#### Model Sizes
| Model | English | Multilingual | Download | VRAM | Speed |
|-------|---------|--------------|----------|------|-------|
| Tiny | `tiny.en` | `tiny` | ~75 MB | ~1 GB | ~10x |
| Base | `base.en` | `base` | ~140 MB | ~1 GB | ~7x |
| Small | `small.en` | `small` | ~465 MB | ~2 GB | ~4x |
| **Medium** | `medium.en` | `medium` | ~1.5 GB | ~5 GB | ~2x |
| Large V3 Turbo | - | `large-v3-turbo` | ~1.5 GB | ~6 GB | ~8x |

Speed is relative to the original large model. [Source](https://github.com/openai/whisper)

`large-v3-turbo` is available when faster-whisper 1.1.0 or newer is installed.
If the selected faster-whisper cannot load turbo, the wizard hides that option
and an existing turbo setting loads `medium` instead. Choose a different model in
Settings → Whisper.

With the faster-whisper backend, `whisper_model` also accepts a Hugging Face
model ID such as `deepdml/faster-whisper-large-v3-turbo-ct2`, or a local
converted model directory.

#### GPU Acceleration (NVIDIA on Linux)
If you have an NVIDIA GPU, Claire can use CUDA for much faster speech recognition. Install the CUDA runtime libraries into the virtual environment:
```bash
.venv/bin/pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```
CUDA is detected automatically: no configuration needed.

#### Hallucination Filters
Whisper sometimes produces confident but false transcriptions during silence or background noise (e.g. news-show intros, music). Two thresholds filter these out before they reach the intent judge:

- `"whisper_min_confidence": 0.3`: drops segments whose `avg_logprob`-derived confidence falls below this value. Raise if you see low-confidence noise leaking through; lower if real speech is being dropped.
- `"whisper_no_speech_threshold": 0.5`: drops any segment whose `no_speech_prob` is at or above this value, regardless of `avg_logprob`. Catches the case where Whisper is confident about a hallucinated phrase but its own no-speech signal says the audio was silent. Applies to the faster-whisper backend.

Both thresholds are exposed in the Settings window under *Whisper*.

</details>

<details>
<summary><strong>Wake Word</strong></summary>

```json
{
  "wake_word": "claire",
  "wake_aliases": ["clair", "clare", "claira", "claya", "klair", "klare", "claires"],
  "wake_fuzzy_ratio": 0.82
}
```

- `wake_word`: the primary name, matched exactly at word boundaries (`"claire"`).
- `wake_aliases`: the mishearings a recogniser actually produces, matched exactly.
  `claya` is in this list because its similarity to `claire` is only 0.5455 — no
  safe fuzzy threshold admits it (a threshold low enough to admit `claya` also
  admits the ordinary word `clay` at 0.6000), so it is carried by the curated list,
  which is what an alias list is for.
- `wake_fuzzy_ratio`: the similarity floor for the approximate match that runs
  *after* the exact word-boundary match, on the near-misses recognisers emit.

#### Why the fuzzy ratio is 0.82

At the previous default of `0.78` the fuzzy fallback was too loose and woke her on
ordinary English — `"care"` scores 0.8000 against `claire` (and `"lair"` 0.8000),
so *"I do not care"* woke her. `0.82` sits above that worst ordinary word (a 0.02
margin) and at or below the weakest genuine mishearing that must still match,
`claira` at 0.8333:

| Token | Ratio vs `claire` | Outcome |
|-------|-------------------|---------|
| `claires` | 0.9231 | genuine mishearing, fuzzy-accepted |
| `clair` | 0.9091 | genuine mishearing, fuzzy-accepted |
| `clare` | 0.9091 | genuine mishearing, fuzzy-accepted |
| `klair`, `klare` | 0.9091 | accepted after folding `k` to `c` (same /k/ before /l/) |
| `claira` | 0.8333 | genuine mishearing, fuzzy-accepted |
| **`claire`** | **1.0000** | exact match (not fuzzy) |
| `care`, `lair` | 0.8000 | **false positive at 0.78**, rejected at 0.82 |
| `claya` | 0.5455 | alias list only, below any safe threshold |

It cannot be pushed higher without breaking real speech, because `claira` has to
keep matching when only another alias is configured (`claira` and the obscure word
`claret` both score 0.8333, so no threshold that keeps one can exclude the other).

Two independent defences therefore apply, so loosening the ratio alone cannot
reintroduce the false wake: the token must clear `0.82` **and** start with the
primary name's onset consonant cluster (`k` folded to `c`), which is what rejects
`care`, `lair` and `declare` even if `0.78` is configured explicitly.

</details>

<details>
<summary><strong>Voice Interface (Advanced)</strong></summary>

**LLM Intent Judge** - Claire uses a small LLM for intelligent voice intent classification (echo detection, query extraction, stop commands). On the default Ollama setup this is `gemma4:e2b`, installed automatically alongside your chosen chat model during setup. On an OpenAI-compatible provider the judge uses your served chat model instead, so there is nothing extra to install. The intent judge cannot be disabled but gracefully falls back to simpler text matching if the LLM server is unavailable.

**Tool Router** - When `"tool_selection_strategy": "llm"` (the default), Claire asks the fast model to pick which tools are relevant for each query, shrinking the tool catalogue the chat model sees. It's already warm and small enough not to stall the turn. Other strategies: `"keyword"` (fast, no LLM), `"embedding"` (nomic-embed-text), `"all"` (no filtering).

**Task-list Planner** - Before the agentic loop, Claire runs a short planning pass that decomposes multi-step queries into an ordered list of sub-tasks. A planned step that already names a tool and every argument it needs is executed directly, without a chat turn to re-derive it: for small models (`gemma4:e2b` class) every planned step takes that path, and large models take it whenever the step is fully concrete, through the deterministic parser only (a step that would need a resolver call falls back to the model's own native tool calling). This significantly improves multi-step reliability. Config options:

```json
{
  "planner_enabled": true,          // set to false to disable the planner entirely
  "planner_timeout_sec": 3.0        // per-call timeout for plan and step-resolver LLM calls
}
```

</details>

<details>
<summary><strong>Small-Model Digest Passes (Advanced)</strong></summary>

Small chat models (~2B, e.g. `gemma4:e2b`) degrade sharply as their prompt grows. Claire runs two cheap distil passes to keep the prompt tight:

- **Memory digest**: boils diary + graph recall into a short relevance-filtered note before injecting it as background context.
- **Tool-result digest**: boils a raw tool payload (especially webSearch UNTRUSTED WEB EXTRACT blocks) into a short attributed fact note before it reaches the main reply model.

Both digest passes auto-enable for small models (≤7B) and stay off for large models. For small models, tool-result digest also prevents large fetch_web_page payloads from blowing the context window. Override in `~/.config/claire/config.json`:

```json
{
  "memory_digest_enabled": null,          // null = auto-on for SMALL, false to force off, true to force on
  "tool_result_digest_enabled": null,     // null = auto-on for SMALL, false to force off, true to force on
  "llm_digest_timeout_sec": 8.0,          // tight ceiling shared by both passes
  "llm_extractor_timeout_sec": 8.0        // tight ceiling for the memory keyword extractor
}
```

The memory keyword extractor (which turns a query into diary/graph search parameters) is a small classification-shaped call, so it gets its own ceiling rather than the tool-execution one; it is started speculatively alongside the tool router and its answer is used only when the turn needs memory.

Field logs show `🧩 Memory digest: …` and `🧩 Tool digest: …` lines when a pass ran, so you can see when the substrate was replaced.

</details>

## Text-to-Speech

<details>
<summary><strong>Text-to-Speech</strong></summary>

**Fish Audio (default cloud voice)** - Claire's reply text is sent to Fish Audio to synthesize the configured cloned voice. Only the spoken reply text leaves the machine; see [CLOUD_POLICY.md](CLOUD_POLICY.md).

```json
{
  "tts_engine": "fish",
  "fish_voice_id": "your-cloned-voice-id",
  "fish_key_file": "~/.config/claire/fish-keys.txt",
  "fish_latency": "low",
  "fish_model": "s1"
}
```

- `tts_engine`: which synthesis engine to use - `"fish"` (cloud), `"piper"` (offline default fallback), or `"chatterbox"` (offline, optional).
- `tts_voice`: the generic voice selector; leave `null` to use the engine's default.
- `tts_rate`: speaking rate in words per minute, `200` = normal.
- `fish_voice_id`: the cloned voice to speak with (the Fish Audio voice id; `null` falls back to the engine voice).
- `fish_key_file`: path to a `0600` file **outside the repository** holding the Fish Audio key; the default is `~/.config/claire/fish-keys.txt`, expanded at use time. The key value is never written to `config.json` and is never printed or logged. A missing file, an empty one, or permissions looser than `0600` raises a named error (`FishKeyFileMissingError`, `FishKeyFileEmptyError`, `FishKeyFilePermissionsError`) that carries the exact path, and an unknown `tts_engine` raises rather than substituting a different engine — there is no silent fallback to another voice.
- `fish_latency`: `"low"` is the default and the only setting suitable for conversation; `"normal"` makes Fish Audio wait roughly 3.4 s *before the first audio* and that wait grows with the length of the reply, which to a listener is indistinguishable from being ignored. Do not make `"normal"` the conversation default — it is opt-in only, for offline or pre-rendered audio.
- `fish_model`: the Fish Audio model that serves the voice (`"s1"` by default).

**Piper TTS (offline fallback)** - Neural TTS that auto-downloads on first use (~60MB):
- Works out of the box - no setup required
- High-quality British English male voice (en_GB-alan-medium)
- Fast local synthesis with exact duration tracking

To use different Piper voices, download from [HuggingFace](https://huggingface.co/rhasspy/piper-voices) and set:
```json
{
  "tts_engine": "piper",
  "tts_piper_model_path": "~/.local/share/claire/models/piper/en_GB-alan-medium.onnx"
}
```

**Chatterbox (offline fallback)** - AI voice with emotion control (requires running from source):
```json
{ "tts_engine": "chatterbox" }
```

Voice cloning with Chatterbox - add a 3-10 second .wav sample:
```json
{
  "tts_engine": "chatterbox",
  "tts_chatterbox_audio_prompt": "/path/to/voice.wav"
}
```

</details>

<details>
<summary><strong>Location</strong></summary>

Claire does **not** detect her own location. IP geolocation — UPnP router
queries, the socket heuristic, the OpenDNS lookup and the local MaxMind
GeoLite2 database — has been removed. On a host without the GeoLite2 database
it still ran every network probe first, blocking the reply path for around
**12 seconds** per turn to produce a value that was usually unknown anyway.

The only remaining source is an explicitly configured city, read from
`location_city` (default `""`, meaning no city is set):

```json
{
  "location_enabled": true,
  "location_city": "Lisbon"
}
```

With no city set, location-aware answers (for example "what's the weather?")
ask for a place instead. The local time is still correct — it comes from the
operating system's timezone, since a city name is not an IANA zone and there
is no database left to derive one.

`location_enabled` is still honoured as a master toggle (setting it to
`false` reports `"Location: Disabled"`). The other `location_*` keys
(`location_auto_detect`, `location_ip_address`,
`location_cgnat_resolve_public_ip`, `location_cache_minutes`) no longer have
any effect.

</details>

<details>
<summary><strong>MCP Tool Integration</strong></summary>

Connect Claire to external tools via [MCP servers](https://github.com/topics/mcp-server):

```json
{
  "mcps": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "your-token" }
    }
  }
}
```

**Popular integrations:**
- **Home Assistant** - Voice control for smart home
- **Google Workspace** - Gmail, Calendar, Drive, Docs
- **GitHub** - Issues, PRs, workflows
- **Notion** - Knowledge management
- **Slack/Discord** - Team communication
- **Databases** - MySQL, PostgreSQL, MongoDB
- **Composio** - 500+ apps in one integration
- **Linux Desktop** - Mouse, keyboard, windows and app launching on this machine (built in, no key needed)

See [full MCP setup guide](#mcp-integrations) below.

</details>

## MCP Integrations

> **Session persistence:** each MCP server is launched once and its stdio session is kept open across tool calls. Stateful servers (e.g. browser automation, where the server owns a long-running Chrome process) work correctly. If you have a server you'd rather not keep resident, set `"idle_timeout_sec": 300` on its config entry and Claire will free it after that long without activity. If a server's tools legitimately run long (e.g. delegating a task to an external CLI agent), set `"timeout_sec": 600` to raise its 120-second default call timeout.

How the connection works, and how to build your own server from the repository template, is in [MCP_SERVERS.md](MCP_SERVERS.md).

<details>
<summary><strong>Linux Desktop Control</strong> - Mouse, keyboard, windows and apps (built in)</summary>

Ships inside the `claire` package: nothing to install, no API key. Point it at
the interpreter of your virtual environment, and keep the `DISPLAY` entry —
the MCP SDK otherwise spawns the server without one and it cannot reach X.

```json
{
  "mcps": {
    "linux-desktop": {
      "command": "/home/you/jarvis_cloud/.venv/bin/python",
      "args": ["-m", "claire.mcp_servers.linux_desktop"],
      "env": { "DISPLAY": ":0" }
    }
  }
}
```

Requires an X11/Xwayland session, and `wmctrl` for the window tools
(`sudo apt install wmctrl`). In Settings you can simply use
**MCP Servers → Add from catalogue → 🖱️ Linux Desktop Control**.

"Claire, click the play button" / "type this into the form" / "switch to Firefox" / "open the terminal"

</details>

<details>
<summary><strong>Home Assistant</strong> - Smart home voice control</summary>

1. Add MCP Server integration in Home Assistant (Settings → Devices & services)
2. Expose entities you want to control (Settings → Voice assistants → Exposed entities)
3. Create Long-lived Access Token (Profile → Security → Create token)
4. Install proxy: `uv tool install git+https://github.com/sparfenyuk/mcp-proxy`
5. Add to config:
```json
{
  "mcps": {
    "home_assistant": {
      "command": "mcp-proxy",
      "args": ["http://localhost:8123/mcp_server/sse"],
      "env": { "API_ACCESS_TOKEN": "YOUR_TOKEN" }
    }
  }
}
```

"Claire, turn on the living room lights" / "set bedroom to 72°" / "run good night scene"

</details>

<details>
<summary><strong>Google Workspace</strong> - Gmail, Calendar, Drive, Docs, Sheets</summary>

```json
{
  "mcps": {
    "google_workspace": {
      "command": "npx",
      "args": ["-y", "google-workspace-mcp"],
      "env": {
        "GOOGLE_CLIENT_ID": "your-client-id",
        "GOOGLE_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```
Setup: [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)

</details>

<details>
<summary><strong>GitHub</strong> - Repos, issues, PRs, workflows</summary>

```json
{
  "mcps": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "your-token" }
    }
  }
}
```

</details>

<details>
<summary><strong>Notion, Slack, Discord, Databases</strong></summary>

**Notion:**
```json
{ "mcps": { "notion": { "command": "npx", "args": ["-y", "@makenotion/mcp-server-notion"], "env": { "NOTION_API_KEY": "your-token" } } } }
```

**Slack:**
```json
{ "mcps": { "slack": { "command": "npx", "args": ["-y", "slack-mcp-server"], "env": { "SLACK_BOT_TOKEN": "xoxb-...", "SLACK_USER_TOKEN": "xoxp-..." } } } }
```

**Discord:**
```json
{ "mcps": { "discord": { "command": "npx", "args": ["-y", "discord-mcp-server"], "env": { "DISCORD_BOT_TOKEN": "your-token" } } } }
```

**Databases:** [bytebase/dbhub](https://github.com/bytebase/dbhub) (SQL), [mongodb-mcp-server](https://github.com/mongodb-js/mongodb-mcp-server) (MongoDB)

</details>

<details>
<summary><strong>Composio</strong> - 500+ apps in one integration</summary>

```json
{
  "mcps": {
    "composio": {
      "command": "npx",
      "args": ["-y", "@composiohq/rube"],
      "env": { "COMPOSIO_API_KEY": "your-key" }
    }
  }
}
```
Get API key at [composio.dev](https://composio.dev)

</details>

## Troubleshooting

**Warmup passes but intent detection times out?** Warmup checks model loading with a small request, not a full intent decision. Intent detection has its own `intent_judge_timeout_sec` (6 seconds by default), separate from chat. A timeout does not necessarily mean your server is offline; the log shows the configured limit.

**Model downloads look paused?** Open **Logs** from the tray. The download card shows real transferred bytes, percentage, speed and estimated time remaining when available, without flooding the activity timeline. If updates pause, it shows how long it has been waiting. A speech recogniser's first download can be large; loading and warming up are shown separately after its files are ready.

<details>
<summary><strong>Common issues</strong></summary>

**First startup takes a bit** - Initial model downloads can take several minutes, followed by loading and warmup. Watch the Logs window for transfer progress and readiness. Enable **Low Power Mode** in Settings to skip LLM startup warmup.

**Claire doesn't hear me** - Check microphone permissions and the selected input in Settings. Say "Claire" anywhere in your sentence.

**Linux says Listening but nothing is transcribed** - Open Logs and check for capture warnings. Missing callbacks mean the recording stream is not delivering blocks; silent samples mean blocks arrive but contain no usable signal. Check input mute and the recording source in PipeWire/PulseAudio (`pavucontrol` or `wpctl status`), and choose a microphone rather than an output monitor. Try the system default or the `pipewire` input where available. Enable `voice_debug` in Settings for periodic callback counts, signal peak, speech-frame counts and capture rate. These diagnostics do not save microphone audio. Native 44.1/48 kHz inputs are resampled for speech detection and transcription.

**Not sure what is running** - Open the tray menu and click **🩺 Runtime Status**. It shows whether Claire is listening, whether Low Power Mode is active, whether Ollama is needed/running, which models are configured, and how many MCP servers are enabled.

**Responses are slow** - Try a smaller model and check available memory. See [hardware and model choices](../README.md#quick-install); requirements depend on model size, quantisation and context length.

**Too warm while Claire is active** - Enable **⚙️ Settings → ✨ Features → Low Power Mode**. This keeps voice recognition ready while avoiding background LLM warmup and shortening Ollama's idle residency window.

**Still warm after quitting** - If Claire starts Ollama for you, quitting Claire also stops that owned Ollama runtime. If Ollama was already running before Claire opened, Claire leaves it running so it does not interrupt your other local AI tools.

**Linux: No tray icon** - `sudo apt install libayatana-appindicator3-1`

**Claire keeps deflecting on questions it answered before** - small models can record their own past failures into the diary, which then primes future sessions to repeat them. New writes are scrubbed automatically; to clean historical entries, open the Memory Viewer, switch to the Diary tab, and click **Clean up deflection narration** in the sidebar Maintenance section. Only sentences that narrate the assistant's failures are removed; the rest of each entry stays.

</details>
