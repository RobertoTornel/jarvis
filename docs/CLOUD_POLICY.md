# Cloud policy

[← Back to Claire](../README.md) · [Configuration](CONFIGURATION.md)

Claire is a **cloud-permitted** application: it is allowed to reach named external
services for language, vision and speech synthesis. Everything else — speech
recognition, memory, diary, knowledge graph, tool execution — stays on the
machine. This document is the authoritative statement of what may leave the
machine, what is sent, and how keys are handled.

This policy applies to every build of Claire on Linux. Code that wants to send
data anywhere not listed here needs this document updated first.

---

## 1. What is allowed

| Capability | Vendor / service | Where it is reached | Auth |
| :--- | :--- | :--- | :--- |
| Language model — chat, tool routing, planning, digests | **OmniRouter** | `http://127.0.0.1:20129/v1` (OpenAI-compatible endpoint) | `OMNIROUTE_API_KEY` environment variable |
| Vision — screenshots and camera frames | **OmniRouter** (multimodal models) | the same `http://127.0.0.1:20129/v1` endpoint | `OMNIROUTE_API_KEY` environment variable |
| Speech synthesis | **Fish Audio**, cloned voice | Fish Audio API, called by the TTS layer | key file referenced by `fish_key_file` |

Configuring the language model and vision route:

```json
{
  "llm_provider": "openai_compatible",
  "llm_base_url": "http://127.0.0.1:20129/v1",
  "llm_api_key": ""
}
```

- `llm_provider` is `openai_compatible`; the OmniRouter route is selected through
  the provider id `omniroute`.
- `llm_api_key` stays empty in `config.json`. The key is read at runtime from the
  `OMNIROUTE_API_KEY` environment variable.
- Speech synthesis is selected with `"tts_engine": "fish"` and configured with
  `fish_voice_id`, `fish_key_file`, `fish_latency` and `fish_model`. See
  [CONFIGURATION.md](CONFIGURATION.md).

No other cloud service is permitted. In particular there is **no** cloud speech
recognition, no cloud geolocation, and no remote transcription.

## 2. What is sent to which vendor

### To OmniRouter (`http://127.0.0.1:20129/v1`)

- **Conversation text**: the current user query, the dialogue history Claire
  replays for context, tool-call and tool-result text, the composed system
  prompt with its warm-profile/digest context, and the prompts for the
  background passes (summariser, knowledge-graph extraction, best-child picker,
  planner, intent judge, tool router).
- **Images**: screenshots and camera frames, when vision is in use, as image
  content parts of the same chat request.
- Nothing else: no local file contents beyond what a tool explicitly returns into
  the conversation, and no secrets (the redaction layer scrubs credentials and
  secret-shaped strings before they are stored or sent).

### To Fish Audio

- **Reply text only**: the spoken form of Claire's reply, sent to synthesize the
  cloned voice. The text may be a shortened version of the written reply (no
  URLs, no markdown).
- The vendor receives the request authenticated with the Fish Audio key and the
  configured cloned `fish_voice_id`.
- No conversation history, no images, no microphone audio, and no local files are
  sent to Fish Audio.

### Never sent anywhere

- Microphone audio and its transcriptions (speech recognition is local,
  `faster-whisper`).
- Memory, diary and knowledge-graph databases, which live under
  `~/.local/share/claire/`.
- API keys or any other secret value.

## 3. Offline fallbacks

Every cloud capability has a local substitute, so Claire keeps working without a
network path to a vendor:

| Cloud capability | Offline fallback | How to select |
| :--- | :--- | :--- |
| OmniRouter language model | **Ollama** on the local machine | `"llm_provider": "ollama"` |
| OmniRouter vision | none — vision requests fail closed when no multimodal local model is configured | use a local multimodal chat model, or disable vision |
| Fish Audio speech synthesis | **Piper** (default offline engine) | `"tts_engine": "piper"` |
| Fish Audio speech synthesis | **Chatterbox** (offline, optional, local GPU) | `"tts_engine": "chatterbox"` |

Notes:

- Ollama is the supported offline language backend. With
  `"llm_provider": "ollama"` all chat, routing, planning and digest traffic stays
  on the machine; the OmniRouter endpoint is not contacted.
- Piper and Chatterbox run locally and send nothing to Fish Audio. Piper is the
  default engine and auto-downloads its voice on first use; Chatterbox needs a
  local Python environment and, for cloning, a local `.wav` sample.
- Embeddings for memory search can stay on Ollama (`"embedding_provider":
  "ollama"`) even when chat is routed to OmniRouter.
- Falling back must never be silently invented by code: when a cloud route is
  unavailable the failure is surfaced and the configured fallback is used, or the
  capability is reported unavailable.

## 4. Secrets rules

1. **Storage.** Keys live in the operating-system keychain where one is
   available, or in a `0600` file **outside** the repository (for example the
   path named by `fish_key_file`). A key value is never stored in a tracked file
   and never committed to git.
2. **Never in `config.json`.** Keep `llm_api_key` empty. The OmniRouter key comes
   from `OMNIROUTE_API_KEY`; the Fish Audio key comes from `fish_key_file` (or its
   environment equivalent). Do not paste a key into a config file, a command line,
   a bug report or a chat transcript.
3. **Never printed or logged.** No code path may print, log, traceback or echo a
   secret value. Debug output, error messages and exception text must be
   scrubbed of secret-shaped strings (the redaction helpers in
   `src/claire/utils/redact.py` are the shared implementation).
4. **Reads are masked.** Any UI, log line, diagnostic or report that shows a key
   shows **at most the last four characters plus a fingerprint** (a hash-derived
   identifier). The full value is never redisplayed, not even to confirm it.
5. **Environment variables are still secrets.** `OMNIROUTE_API_KEY` must not be
   dumped by a process listing, shell history, or an `env`-printing debug path;
   prefer a local untracked env file with `0600` permissions over inline export in
   a shell history.
6. **Rotation and audit.** Because a value can never be read back from Claire, a
   lost key means issuing a new one at the vendor and replacing the file or
   environment entry. Any suspected leak is treated as a compromise: rotate
   immediately and check whether the value appears in any tracked file.
