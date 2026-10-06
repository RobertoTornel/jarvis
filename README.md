<div align="center">

# Claire

### A voice in the room, on your own machine.

A standalone, always-on voice assistant for Linux, built for one machine: `mana-pc`.<br>
A personal, non-commercial research fork of [isair/jarvis](https://github.com/isair/jarvis).

**Voice-first · Linux-only · Personal memory · Non-commercial fork**

[**Quick install →**](#quick-install) &nbsp; · &nbsp; [Cloud services](#cloud-services) &nbsp; · &nbsp; [Inside Claire](#inside-claire) &nbsp; · &nbsp; [Attribution](#attribution)

</div>

---

## An assistant in the conversation

Claire is built to be part of the discussion, not another screen to type into. Talk through an idea, discuss plans with a friend, then ask “Claire, what do you think?” While listening, it keeps a short, temporary rolling transcript of nearby speech so it can join an ongoing conversation using what was just discussed, without you having to repeat the background.

Address Claire by name anywhere in a sentence and follow up naturally. Wake aliases match whole words so unrelated words do not trigger their approximate variants. Mentions while speaking to someone else are not invitations to reply. The animated face gives the assistant a presence on the desktop; chat is there when you would rather type.

Conversation memory stays on this machine. Separate local databases keep their semantic search indices separate, and sensitive information is redacted before it reaches model context or the saved diary. Speech recognition runs locally too. The language model, the synthesised voice and vision are reached over the network — see [Cloud services](#cloud-services) for exactly what leaves the machine. Web search, weather and connected tools use the network when you ask for those capabilities.

<p align="center">
  <img src="docs/img/face.png" alt="Claire's animated amber wireframe face, the desktop presence of the Linux voice assistant" width="460">
</p>

<p align="center"><sub>A voice, a face, and a place in the conversation.</sub></p>

## Quick install

Claire is a Linux application, and this fork is run from source. No packaged releases are published for it, so there is nothing to download.

**What you need:** 64-bit Linux, Python 3.11 or newer, a working microphone, a reachable OmniRouter instance for the language model, and a Fish Audio voice for speech synthesis (see [Cloud services](#cloud-services)).

```bash
# from the repository root — this checkout lives at ~/Documents/jarvis_cloud on mana-pc
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# check the command-line entry point is wired up
PYTHONPATH=src .venv/bin/python -m claire --help

# start the assistant
./scripts/run_linux.sh
```

`scripts/run_linux.sh` repeats the setup for you: it creates `.venv` when it is missing, installs `requirements.txt`, exports `PYTHONPATH=src` and runs `python -m claire`, which hands off to the daemon. On a fresh checkout, running it on its own is enough.

`pip install -r requirements.txt` installs the dependencies but not Claire itself, so `python -m claire` needs `src/` on `PYTHONPATH` — `run_linux.sh` exports that, and the `--help` line above shows it explicitly.

**Try it.** Once Claire reports that it is listening, try:

> “Claire, help me think through my day.”

Open **Chat** from the tray menu when you would rather type. Text replies are silent.

Where things live:

| | |
| :--- | :--- |
| Configuration | `~/.config/claire/config.json` (override the location with `CLAIRE_CONFIG_PATH`) |
| Data | `~/.local/share/claire/` — diary, knowledge graph and memory database (`claire.db`) |
| Voice models | `~/.local/share/claire/models/` |

<details>
<summary><strong>Hardware and model choices</strong></summary>

The chat model this fork uses is served by OmniRouter, so its name comes from that router's catalogue. If you point Claire at a local server instead, memory needs depend on model size, quantisation, context length and speech recognition. Both the Ollama and the OpenAI-compatible setup show a memory budget; you can edit estimates for models served through an OpenAI-compatible endpoint. Smaller models trade capability for lower resource use.

| Starting point | Example local chat model |
| :--- | :--- |
| Smaller hardware | `qwen3.5:0.8b` |
| Default | `gemma4:e2b` |
| More capable | `gemma4:e4b` |
| Larger local setup | `qwen3.8:27b` |

Budget memory for Whisper and, when different from chat, the fast model used for voice intent and tool routing. Discrete GPUs use their own VRAM. The wizard recommends smaller models when detected memory is exhausted and warns when CPU fallback is needed.

</details>

## Cloud services

Claire on `mana-pc` is not fully offline. Everything that stays local is marked local below; everything else goes through these services. No key value belongs in this repository.

| Capability | Where it goes | How it is reached |
| :--- | :--- | :--- |
| Language model — chat, tool routing, planning | **OmniRouter**, provider `omniroute` | `http://127.0.0.1:20129/v1`, an OpenAI-compatible endpoint |
| Vision — screenshots and camera frames | **OmniRouter** multimodal models | the same endpoint |
| Speech synthesis | **Fish Audio**, cloned voice | called by the TTS layer |
| Speech recognition | **local** (`faster-whisper`) | nothing leaves the machine |
| Memory, diary, knowledge graph | **local** | `~/.local/share/claire/` |

- The OmniRouter key is read from the `OMNIROUTE_API_KEY` environment variable. Keep it in the environment or a local, untracked file; never commit it, paste it into `config.json`, or include it in a bug report.
- To point Claire at the router, select the OpenAI-compatible provider in the setup wizard, or set `"llm_provider": "openai_compatible"` and `"llm_base_url": "http://127.0.0.1:20129/v1"` in `~/.config/claire/config.json`.
- If you would rather keep the language model on the machine, point Claire at a local server you already run — Ollama, LM Studio or llama.cpp — and the cloud row above no longer applies.
- This rewrite is early. The OmniRouter route is the first one wired up; the cloned Fish Audio voice and multimodal vision are being brought across, and until the voice work lands the TTS layer still speaks through the inherited local engines (Piper and Chatterbox). [STATUS.md](STATUS.md) records what is live and what is not.

## What you can do

- **A third person in the room.** Bring Claire into an ongoing conversation with friends, talk through a problem aloud, or ask it to weigh in on a decision. “Claire, what do you think?” draws on the recent discussion, not just that one sentence.
- **Remember beyond one session.** Search your local diary and knowledge graph. Diary keyword search supports Unicode text, and semantic search uses local embeddings with the bundled index. Planning requests memory for personal context and distinguishes it from general facts; recall scopes diary searches by requested dates, and accuracy depends on the selected model. Claire checks graph candidates for lasting value before storing them. Browse what Claire has stored in the Memory Viewer.
- **Get things done.** Built-in tools cover web search, weather, time, screenshot OCR, file access, nutrition tracking and optional location awareness.
- **Connect your own tools.** MCP servers add browser automation, smart-home controls and other integrations. Tool routing selects a relevant subset for each request.
- **See when speech was unclear.** The face briefly shows a repeat request when speech recognition rejects a low-confidence segment. It does not speak or interrupt the current assistant state.
- **Type when you need to.** The companion chat shares your voice conversation and memory. Text replies are silent, and you can rewind a sent message to regenerate from that point.

### Bring Claire into the discussion

> **You:** We could have a picnic tomorrow.
>
> **A friend:** Depends on the weather. Should we plan something indoors instead?
>
> **You:** Claire, what do you think?

Claire can use the recent conversation to understand that you are asking about the weather for the picnic, rather than treating the last sentence as an isolated question. This is the experience it is built around. Personalised requests can also draw on saved conversation history when no external tools are needed. How reliably it understands the context depends on speech recognition and your chosen model; see the [evaluation results](EVALS.md).

## Inside Claire

The supporting desktop interfaces keep setup, activity and settings within reach. The screenshots below use current widgets with demo data, each on its own row so you can read the interface without opening an image viewer.

### A guided start

Choose the model server that fits your setup. The wizard walks through models, speech and optional capabilities without requiring you to edit a configuration file — on this machine, the OpenAI-compatible card is where OmniRouter goes.

<p align="center">
  <img src="docs/img/setup-provider.png" alt="Current setup wizard with separate cards for Ollama and an OpenAI-compatible server, the card used for OmniRouter" width="900">
</p>

### Progress you can actually follow

The activity timeline keeps ordinary events readable. Downloads have a separate card with transferred bytes, percentage, speed and remaining time when the downloader provides them. Loading and warmup are distinct stages.

<p align="center">
  <img src="docs/img/logs.png" alt="Claire Logs showing a speech model download at 48 percent, transferred bytes, speed, remaining time and a yellow optional-location warning" width="900">
</p>

### Your setup, without the JSON

Choose models, tune speech recognition, configure tools and enable Low Power Mode from **Settings** in the tray menu.

<p align="center">
  <img src="docs/img/settings-window.png" alt="Claire Settings with a category sidebar and speech recognition controls" width="900">
</p>

### A quiet companion to voice

When speaking is inconvenient, open Chat from the tray. It picks up the same conversation and memory, without reading text replies aloud.

<p align="center">
  <img src="docs/img/chat-window.png" alt="Claire companion chat in its rounded graphite phone-style window, showing illustrative messages and an amber composer" width="480">
</p>

## Known limitations

Claire is actively developed as a Linux-only fork. Model choice and hardware affect response quality and speed; [automated evaluation results](EVALS.md) show what is being measured.

- **Large web pages:** page downloads are limited to 2 MiB of decoded content. Oversized pages return a download-limit error.
- **Spoken “stop” can be mistaken for echo** while Claire is speaking ([#24](https://github.com/isair/jarvis/issues/24)). Use a standalone configured stop phrase; echoed longer instructions containing it are ignored.
- **No mobile app** is available ([#17](https://github.com/isair/jarvis/issues/17)).
- **Capture continues while Claire thinks.** Speech detection and assembly run independently of intent judging and reply generation. Recognition backlogs are bounded; Logs warns when speech arrives faster than it can be processed.
- **Continuous speech is split at your utterance limit.** The shorter limit during speech playback helps interruption requests reach recognition promptly. Both limits are adjustable in Settings.
- **Reasoning models can take longer.** Planning, recall, summaries and partial replies reserve room for reasoning and the final answer, with the configured timeout limiting the wait.
- **GPT-OSS always reasons.** Its Ollama thinking toggle selects low or high reasoning effort; turning it off selects low effort rather than disabling reasoning.
- **Long conversations feed the complete pending snapshot into the diary.** Bounded batches share the configured generation timeout; interrupted saves resume completed batches privately in memory on the next attempt.
- **Diary saves remain successful if the optional search index fails.** Saved text stays available through keyword search; semantic search can retain an older embedding.
- **Meal logging confirms saved meals even if coaching is unavailable.** Optional follow-up advice can fail without logging the meal twice.
- **Weather place fallback supports abbreviated names.** Names such as Washington D.C. retain internal punctuation when location detection is unavailable.
- **Slow speech recognition produces guidance.** After several utterances decode slower than real time, Logs recommends a smaller Whisper model or checking available acceleration. Smaller models can reduce accuracy.
- **First-run downloads can take time.** Whisper and language models can be large. Check Logs for progress before assuming startup is stuck.
- **CUDA speech failures use a CPU fallback.** If the CUDA runtime fails during decoding, Claire makes one recovery attempt on CPU. Speech recognition can be slower in this mode.
- **Whisper turbo needs a compatible backend.** The wizard hides it when the selected backend cannot load it; an existing unsupported selection uses `medium` instead.
- **Optional capabilities need their dependencies.** Location awareness needs a GeoLite2 database. Semantic memory search needs working embeddings; otherwise search falls back to keywords.

## Configuration

Most people can use **Settings** from the tray. Advanced setups can edit `~/.config/claire/config.json`.

[**Open the configuration guide →**](docs/CONFIGURATION.md)

The guide covers model servers, speech recognition, Low Power Mode, voices, location, MCP integrations and troubleshooting.

### Bring your own tools

Connect MCP servers for browser automation, Home Assistant, GitHub, databases and more. Credentials and network access depend on the tools you choose. Review a server's permissions before enabling it.

[Integration examples and server settings →](docs/CONFIGURATION.md#mcp-integrations)

## Troubleshooting

- 🔇 **Speech output unavailable:** A voice-model or dependency failure disables speech output for the current run. Check the reason in Logs, correct the voice model settings or missing dependencies, then restart Claire.

- 🔊 **Voice-model download interruptions:** Connection resets and timeouts receive bounded retries. If they persist, check connectivity and restart Claire; completed voice files are retained. Certificate errors require correcting the trust configuration.

- 📥 **Whisper download failures:** Faster-whisper downloads run in a separate process with a five-minute limit. If a download crashes or times out, restart Claire to resume it; cached models stay available offline.
- 🎤 **Whisper cache errors:** Claire attempts one clean download recovery per startup. If it fails, check connectivity and free disk space, then follow the printed cache guidance and restart. Device fallbacks retain files downloaded during recovery.

<details>
<summary><strong>No default microphone found</strong></summary>

Select an available microphone in Claire Settings, or set a default recording input in your system Settings, then restart Claire. Claire keeps the selected input throughout startup and does not choose another microphone automatically when the default is unavailable.

</details>

<details>
<summary><strong>Bluetooth microphone will not start</strong></summary>

Select the headset's microphone/input profile in Settings, not its playback device. Claire uses the selected input for listening, tries compatible mono, stereo and device-native capture formats, then converts multichannel input to mono for speech recognition. If that input is unavailable, select another microphone in Settings. If capture still fails, check that the microphone works in your system's recorder and share the error from Logs; Bluetooth driver/profile limitations can still prevent capture.

</details>

<details>
<summary><strong>Linux says Listening, but never hears speech</strong></summary>

Check Logs for missing-callback or silent-input warnings. Verify the recording source and mute state in PipeWire/PulseAudio, and select a microphone rather than an output monitor. Enable `voice_debug` in Settings for capture-level diagnostics. See the [troubleshooting guide](docs/CONFIGURATION.md#troubleshooting).

</details>

<details>
<summary><strong>Downloads look paused</strong></summary>

Open **Logs** from the tray. The progress card shows transfer details when available and elapsed waiting time when updates pause. After downloading, loading the model into memory is a separate step.

</details>

<details>
<summary><strong>Warmup passed, but a request timed out</strong></summary>

Warmup checks model loading with a small probe, not a full request. Voice intent detection has a separate timeout from chat. Check the limit shown in the log and the responsiveness of the model endpoint — for this fork, OmniRouter.

</details>

<details>
<summary><strong>The machine runs warm, or I want lower background usage</strong></summary>

Enable **Settings → Features → Low Power Mode**. It skips LLM startup warmup and shortens Ollama model residency while keeping speech recognition ready. The first model request after idle may take longer.

</details>

[More troubleshooting →](docs/CONFIGURATION.md#troubleshooting)

## For developers

<details>
<summary><strong>Run from source</strong></summary>

```bash
# from this fork's checkout
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# CLI and daemon both need src/ on the path; run_linux.sh exports it
PYTHONPATH=src python -m claire --help

# start the daemon with that path already set
bash scripts/run_linux.sh
```

Claire is run from `src/`, not installed into the virtualenv. `pyproject.toml` reads its version from `claire._version`, which has not been added to this rewrite yet, so `pip install -e .` does not complete for now; `PYTHONPATH=src` (or `run_linux.sh`, which sets it) is the supported way to run.

The upstream project is cloned for reference at `~/Documents/jarvis_real` and is never written to.

</details>

<details>
<summary><strong>Refresh the screenshots</strong></summary>

With the desktop dependencies installed in your Python environment:

```bash
PYTHONPATH=src python scripts/capture_readme_screenshots.py
```

The capture script uses the real widgets and illustrative data, isolates configuration in a temporary directory, and blocks network connections and background workers. It does not launch the daemon or use personal conversations. Keep screenshots on separate rows at readable widths.

</details>

[Evaluation results](EVALS.md) · [Report a bug (upstream)](https://github.com/isair/jarvis/issues) · [Contribute (upstream)](https://github.com/isair/jarvis/pulls)

## Privacy & storage

Speech recognition, the diary and the knowledge graph stay on this machine. The language model, the synthesised voice and vision do not: they go to the services in [Cloud services](#cloud-services), and anything you send there is processed by those services.

- **Config and data:** configuration in `~/.config/claire/config.json`, conversation memory and knowledge graph under `~/.local/share/claire`.
- **Local file access:** limited to your home directory. Directory listings identify symbolic links and reject patterns that reach external directories.
- **Sensitive information:** redacted before model context and saved diary entries. The in-memory chat still shows what you typed, and text sent to a cloud service reaches that service.
- **Secrets:** key values are read from the environment — `OMNIROUTE_API_KEY` for the router — and are never written into this repository, into `config.json` or into the diary.

<details>
<summary><strong>Reduce optional network access</strong></summary>

Besides the model and voice endpoints, disable web tools and leave MCP integrations empty. These settings turn off search and automatic location detection:

```json
{
  "web_search_enabled": false,
  "wikipedia_fallback_enabled": false,
  "brave_search_api_key": "",
  "mcps": {},
  "location_auto_detect": false,
  "location_cgnat_resolve_public_ip": false,
  "location_enabled": false
}
```

These options are not a network firewall. The language-model, vision and speech-synthesis endpoints still receive their requests; enforce network restrictions separately if required.

</details>

## Attribution

Claire is a fork, and the upstream project keeps the credit.

- **Upstream:** [isair/jarvis](https://github.com/isair/jarvis) by Baris Sencan (`isair`).
- **Fork point:** upstream commit `94c54491`. This branch starts from that state plus the mechanical `jarvis` → `claire` rename, then adds the Linux-only and cloud changes.
- [ATTRIBUTION.md](ATTRIBUTION.md) records in detail what was inherited, what was deleted and what this fork adds.

**Licence.** Claire is distributed under the same licence as upstream, kept verbatim in [LICENSE](LICENSE). In summary — the licence text governs, not this summary:

- Personal, educational and research use is permitted; the copyright notice and licence must be preserved.
- **Commercial use is not permitted** without a separate licence from the copyright holder.
- Derivative works inherit these terms, so Claire is non-commercial too.

Claire is a private personal/research build. It is not sold, not hosted and not offered as a service.

## Support

This fork is not for sale and takes no money. To support the work it is built on, support upstream: [Sponsor on GitHub](https://github.com/sponsors/isair) · [Buy a coffee](https://ko-fi.com/isair) · [Join the discussion](https://github.com/isair/jarvis/discussions).
